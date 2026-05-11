"""FastAPI server — receives page data from Chrome extension and runs pipeline."""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from src.config import Config
from src.db.repository import ProductRepository
from src.llm.service import LLMService
from src.models.product import MarketScore, PipelineStatus, Platform, Product
from src.utils import detect_platform

app = FastAPI(title="Product Sourcing API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PageData(BaseModel):
    url: str
    platform: str = ""
    title_cn: str = ""
    price_cn: str = ""
    image_urls: list[str] = []
    desc_images: list[str] = []
    sku_prices: list[dict] = []
    description_cn: str = ""


# ── LLM prompts (reused from pipeline stages) ──

STRUCTURED_EXTRACT_PROMPT = """You are a product data extraction expert. Given raw page text from a Chinese e-commerce product page, extract structured data in English for a US Shopify listing.

Extract these fields:
1. title_en: Clean English product title (max 70 chars, SEO-optimized)
2. description_en: HTML formatted product description with bullet points (<ul><li>...)
3. material: Product material
4. dimensions: Dimensions converted to inches
5. weight_oz: Weight in ounces
6. color_options: Available colors in English
7. size_options: Available sizes
8. features: List of 5-7 key product features/benefits in English
9. price_cn: Original price as shown on the page
10. suggested_price_usd: Suggested US retail price (CN price / 7.2, add reasonable margin)
11. category: Best Shopify product category
12. tags: 5-8 SEO tags for Shopify

Actual scraped SKU variants (for context, do NOT output sku_prices):
{sku_prices}

Page content:
{page_text}

Return ONLY a JSON object with all these fields."""

MARKET_ANALYZE_PROMPT = """You are a cross-border e-commerce analyst. Evaluate this product's US market potential.

Product:
{product_info}

Score each dimension 0-100 and compute weighted total:
1. Visual Appeal (25%): Western consumer aesthetic appeal
2. Category Demand (25%): US market demand for this category
3. Uniqueness (20%): Differentiation vs US competitors
4. Price Arbitrage (15%): Margin potential
5. Trend Alignment (15%): Current US market trends

Return ONLY a JSON object:
{{"total": N, "visual_appeal": N, "category_demand": N, "uniqueness": N, "price_arbitrage": N, "trend_alignment": N, "reasoning": "...", "target_audience": "...", "suggested_price_usd": "...", "competitive_notes": "..."}}"""


async def _download_images(folder: str, urls: list[str], name_prefix: str = "") -> list[str]:
    """Download images to data/images/{folder}/ and return local paths."""
    import ipaddress
    import socket

    import httpx

    if not urls:
        return []
    img_dir = Path("data/images") / folder
    img_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []

    for i, url in enumerate(urls):
        if not url.startswith("http"):
            continue
        try:
            parsed = httpx.URL(url)
            host = parsed.host
            if not host:
                continue
            try:
                addr = socket.getaddrinfo(host, None)[0][4][0]
                ip = ipaddress.ip_address(addr)
                if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast:
                    continue
            except (socket.gaierror, ValueError):
                continue

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    continue
                content = await resp.aread()
                if len(content) > 20 * 1024 * 1024:
                    continue

            ext = url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
            if ext.lower() not in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
                ext = "jpg"
            if name_prefix:
                local_path = img_dir / f"{name_prefix}_{i+1:02d}.{ext}"
            else:
                local_path = img_dir / f"{i:03d}.{ext}"
            local_path.write_bytes(content)
            local_paths.append(str(local_path))
        except Exception:
            pass

    logger.info(f"  Downloaded {len(local_paths)}/{len(urls)} images")
    return local_paths


async def _download_sku_images(folder: str, sku_prices: list[dict]) -> list[str]:
    """Download SKU-specific images."""
    import ipaddress
    import socket

    import httpx

    if not sku_prices:
        return []
    img_dir = Path("data/images") / folder
    img_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []

    for sku in sku_prices:
        raw_name = sku.get("name", "sku")
        sku_name = re.sub(r"[^\w\s\-.]", "", str(raw_name))
        sku_name = re.sub(r"\s+", "-", sku_name).strip("-.") or "sku"
        for j, url in enumerate(sku.get("images", [])):
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            try:
                parsed = httpx.URL(url)
                host = parsed.host
                if not host:
                    continue
                try:
                    addr = socket.getaddrinfo(host, None)[0][4][0]
                    ip = ipaddress.ip_address(addr)
                    if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast:
                        continue
                except (socket.gaierror, ValueError):
                    continue

                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, follow_redirects=True)
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if not content_type.startswith("image/"):
                        continue
                    content = await resp.aread()
                    if len(content) > 20 * 1024 * 1024:
                        continue

                ext = url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
                if ext.lower() not in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
                    ext = "jpg"
                if len(sku.get("images", [])) <= 1:
                    local_path = img_dir / f"{sku_name}.{ext}"
                else:
                    local_path = img_dir / f"{sku_name}_{j+1:02d}.{ext}"
                local_path.write_bytes(content)
                local_paths.append(str(local_path))
            except Exception:
                pass

    return local_paths


# ── Lazy-init singletons ──

_llm: LLMService | None = None
_repo: ProductRepository | None = None


def _get_llm() -> LLMService:
    global _llm
    if _llm is None:
        cfg = Config.instance()
        _llm = LLMService(
            api_key=cfg.ai["api_key"],
            base_url=cfg.ai.get("base_url", ""),
            model_text=cfg.ai.get("model_text", "deepseek-chat"),
            model_vision=cfg.ai.get("model_vision", "deepseek-chat"),
            temperature=cfg.ai.get("temperature", 0.3),
            provider=cfg.ai.get("provider", "deepseek"),
        )
    return _llm


def _get_repo() -> ProductRepository:
    global _repo
    if _repo is None:
        _repo = ProductRepository()
    return _repo


# ── API Routes ──


@app.get("/api/health")
async def health():
    return {"status": "ok", "db": str(Path("data/products.db").exists())}


@app.post("/api/import")
async def import_product(data: PageData):
    """Import a product from extension-collected page data."""
    llm = _get_llm()
    repo = _get_repo()

    logger.info(f"Import request: {data.title_cn[:50]} | {data.platform} | {len(data.image_urls)} imgs")

    # ── 1. Create Product from page data ──
    product_id = uuid.uuid4().hex[:12]

    platform_str = data.platform or detect_platform(data.url)
    platform = Platform(platform_str) if platform_str else None

    # ── 2. Download images ──
    handle = data.title_cn or product_id
    handle = re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-") or product_id
    handle = handle[:60]

    local_images = await _download_images(handle, data.image_urls, handle)
    local_desc = await _download_images(handle, data.desc_images, f"{handle}_desc")
    local_sku = await _download_sku_images(handle, data.sku_prices)

    # ── 3. Build Product ──
    product = Product(
        id=product_id,
        platform=platform,
        source_url=data.url,
        title_cn=data.title_cn,
        price_cn=data.price_cn,
        description_cn=data.description_cn,
        images=local_images + local_sku,
        desc_images=local_desc,
        sku_prices=data.sku_prices,
        status=PipelineStatus.SCRAPED,
    )

    await repo.save(product)
    logger.info(f"  Saved: {product_id} | images: {len(local_images)}+{len(local_desc)}+{len(local_sku)}")

    # ── 4. LLM Extract (English title/desc/tags) ──
    import json as _json

    try:
        prompt = STRUCTURED_EXTRACT_PROMPT.format(
            page_text=product.description_cn[:10000],
            sku_prices=_json.dumps(product.sku_prices[:20], ensure_ascii=False)
            if product.sku_prices
            else "none",
        )
        extract_result = await llm.chat_json(
            [{"role": "user", "content": prompt}],
            max_tokens=2000,
        )

        if not extract_result.get("_parse_error"):
            product.title_en = extract_result.get("title_en") or product.title_cn
            product.description_en = extract_result.get("description_en") or product.description_cn[:500]
            product.price_cn = extract_result.get("price_cn") or product.price_cn
            product.tags = extract_result.get("tags") or []
            # Parse suggested price
            price_raw = extract_result.get("suggested_price_usd", 0)
            if price_raw:
                try:
                    product.price_usd = float(str(price_raw).replace("$", "").replace(",", ""))
                except (ValueError, TypeError):
                    pass
            logger.info(f"  LLM extract: {product.title_en[:50]} | ${product.price_usd}")
    except Exception as exc:
        logger.error(f"  Extract failed: {exc}")

    # ── 5. LLM Market Analysis ──
    score = 0
    try:
        info = (
            f"Title: {product.title_en}\n"
            f"Description: {product.description_en[:500]}\n"
            f"Price: ¥{product.price_cn}\n"
            f"Tags: {', '.join(product.tags[:8])}"
        )
        analyze_result = await llm.chat_json(
            [{"role": "user", "content": MARKET_ANALYZE_PROMPT.format(product_info=info)}],
            max_tokens=800,
        )

        if not analyze_result.get("_parse_error"):
            product.market_score = MarketScore(
                total=analyze_result.get("total", 0),
                visual_appeal=analyze_result.get("visual_appeal", 0),
                category_demand=analyze_result.get("category_demand", 0),
                uniqueness=analyze_result.get("uniqueness", 0),
                price_arbitrage=analyze_result.get("price_arbitrage", 0),
                trend_alignment=analyze_result.get("trend_alignment", 0),
                reasoning=analyze_result.get("reasoning", ""),
            )
            score = product.market_score.total
            logger.info(f"  Score: {score}/100")
    except Exception as exc:
        logger.error(f"  Analyze failed: {exc}")

    # ── 6. Finalize ──
    product.status = PipelineStatus.REVIEW_PENDING
    await repo.save(product)

    return {
        "ok": True,
        "product_id": product_id,
        "message": f"Imported: {product.title_en or product.title_cn[:50]}",
        "product": {
            "title_en": product.title_en,
            "price_usd": product.price_usd,
            "score": score,
            "tags": product.tags[:8] if product.tags else [],
            "status": product.status.value,
        },
    }


@app.get("/api/product/{product_id}")
async def get_product(product_id: str):
    """Get product status and data."""
    repo = _get_repo()
    product = await repo.get(product_id)
    if not product:
        return {"ok": False, "error": "not found"}
    return {
        "ok": True,
        "product": {
            "id": product.id,
            "title_cn": product.title_cn,
            "title_en": product.title_en,
            "price_cn": product.price_cn,
            "price_usd": product.price_usd,
            "score": product.market_score.total if product.market_score else 0,
            "status": product.status.value,
            "image_count": len(product.images),
        },
    }


@app.get("/api/recent")
async def recent_imports(limit: int = 10):
    """List recently imported products."""
    repo = _get_repo()
    products = await repo.list_all()
    recent = products[:limit]
    return {
        "ok": True,
        "count": len(recent),
        "products": [
            {
                "id": p.id,
                "title_cn": p.title_cn[:80],
                "title_en": p.title_en[:80] if p.title_en else "",
                "price_cn": p.price_cn,
                "price_usd": p.price_usd,
                "score": p.market_score.total if p.market_score else 0,
                "status": p.status.value,
                "created_at": p.created_at,
            }
            for p in recent
        ],
    }


def run(host: str = "127.0.0.1", port: int = 8765):
    """Start the API server."""
    import uvicorn

    logger.info(f"Starting Product Sourcing API on {host}:{port}")
    uvicorn.run("src.api.server:app", host=host, port=port, log_level="info")
