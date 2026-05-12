"""FastAPI server — receives page data from Chrome extension and runs pipeline."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from src.config import Config
from src.db.repository import ProductRepository
from src.llm.service import LLMService
from src.models.product import MarketScore, PipelineStatus, Platform, Product
from src.utils import detect_platform, clean_price
from src.downloader import download_images, download_sku_images
from src.prompts import MARKET_ANALYZE_PROMPT, STRUCTURED_EXTRACT_PROMPT
from src.api.ai_agent import agent_step

app = FastAPI(title="Product Sourcing API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r".*",
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f">>> {request.method} {request.url.path} from {request.client.host if request.client else '?'}")
    response = await call_next(request)
    logger.info(f"<<< {request.method} {request.url.path} → {response.status_code}")
    return response


class PageData(BaseModel):
    url: str
    platform: str = ""
    title_cn: str = ""
    price_cn: str = ""
    image_urls: list[str] = []
    desc_images: list[str] = []
    sku_prices: list[dict] = []
    description_cn: str = ""


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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": str(exc)[:500]},
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "db": str(Path("data/products.db").exists())}


class AgentStepRequest(BaseModel):
    platform: str = ""
    round: int = 0
    dom: dict = {}
    history: list[dict] = []
    debug: bool = False


@app.post("/api/ai-agent/step")
async def ai_agent_step(req: AgentStepRequest):
    llm = _get_llm()
    result = await agent_step(
        llm=llm,
        platform=req.platform or "unknown",
        round_num=req.round,
        dom_state=req.dom,
        history=req.history,
        debug=req.debug,
    )
    return result


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

    local_images = await download_images(handle, data.image_urls, handle)
    local_desc = await download_images(handle, data.desc_images, f"{handle}_desc")
    local_sku = await download_sku_images(handle, data.sku_prices)

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
            sku_prices=_json.dumps(
                product.sku_prices[:20] if isinstance(product.sku_prices, list) else [],
                ensure_ascii=False,
            ) if product.sku_prices else "none",
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
            price_raw = extract_result.get("suggested_price_usd")
            if price_raw is not None:
                try:
                    product.price_usd = clean_price(price_raw)
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
    recent = await repo.list_recent(limit)
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


def _find_available_port(host: str, start: int = 8765, tries: int = 20) -> int:
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start


def _write_port_file(port: int):
    port_file = Path("data") / "api_port.txt"
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port))


@app.on_event("startup")
async def _on_startup():
    pass


def run(host: str = "127.0.0.1", port: int = 0):
    """Start the API server. port=0 means auto-find available port."""
    import uvicorn
    import socket

    if port == 0:
        port = _find_available_port(host)

    _write_port_file(port)
    logger.info(f"Starting Product Sourcing API on {host}:{port} (written to data/api_port.txt)")
    uvicorn.run("src.api.server:app", host=host, port=port, log_level="info")
