"""Pipeline stages: Load → Extract → Analyze → Process → Publish."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from loguru import logger

from src.db.repository import ProductRepository
from src.llm.service import LLMService
from src.models.product import MarketScore, PipelineStatus, Platform, Product
from src.pipeline import StageResult
from src.processing.image_styler import ImageStyler
from src.shopify.csv_exporter import CSVExporter
from src.utils import clean_price, detect_platform, make_handle_from_title, sanitize_filename


# ── Prompt templates ────────────────────────────────────────

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

DESCRIPTION_BUILD_PROMPT = """You are an expert Shopify copywriter. Generate an SEO-optimized HTML product description.

Product: {title_en}
Details: {description_en}
Target keywords: {tags}

Generate a complete Shopify product description:
1. Start with a compelling headline (h2)
2. 3-5 bullet points of key features/benefits (ul > li)
3. Persuasive closing paragraph with call-to-action (p)
4. SEO meta description (under 160 chars)

Return ONLY a JSON object:
{{"description_html": "<h2>...</h2><ul>...</ul><p>...</p>", "seo_title": "...", "seo_description": "...", "suggested_tags": ["tag1", "tag2"]}}"""

# ── Stages ──────────────────────────────────────────────────


class LoadStage:
    """Stage 1: Load a URL and extract raw page content via ProductAgent."""

    def __init__(self, headless: bool = True) -> None:
        from src.agents.product_agent import ProductAgent
        self._agent = ProductAgent(headless=headless)

    async def run(self, url: str) -> StageResult[Product]:
        try:
            data = await self._agent.extract(url)
            if data.get("_error"):
                return StageResult.fail(data.get("_error", "extraction failed"))

            platform_str = detect_platform(data.get("source_url", url))
            platform = Platform(platform_str) if platform_str else None
            product = Product(
                id=uuid.uuid4().hex[:12],
                platform=platform,
                source_url=data.get("source_url", url),
                title_cn=data.get("title_cn", ""),
                price_cn=data.get("price_cn", ""),
                description_cn=data.get("description_cn", ""),
                images=data.get("image_urls", []),
                desc_images=data.get("desc_images", []),
                sku_prices=data.get("sku_prices", []),
                status=PipelineStatus.SCRAPED,
            )
            logger.info(f"Loaded: {data.get('title_cn','')[:50]} | {len(data.get('image_urls',[]))} imgs | {len(data.get('sku_prices',[]))} skus")
            return StageResult.ok(product)
        except Exception as exc:
            return StageResult.fail(str(exc))

    async def close(self) -> None:
        await self._agent.close()


class ExtractStage:
    """Stage 2: Extract structured product data from raw page content via LLM."""

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    async def run(self, product: Product) -> StageResult[Product]:
        if not product.description_cn:
            return StageResult.fail("No page content to extract from", product)

        try:
            prompt = STRUCTURED_EXTRACT_PROMPT.format(
                page_text=product.description_cn[:10000],
                sku_prices=json.dumps(product.sku_prices[:20], ensure_ascii=False) if product.sku_prices else "none",
            )
            result = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                max_tokens=2000,
            )

            if result.get("_parse_error"):
                return StageResult.fail("LLM returned invalid JSON", product)

            product.title_en = result.get("title_en") or product.title_en or product.title_cn
            product.description_en = result.get("description_en") or product.description_en or product.description_cn[:500]
            product.price_cn = result.get("price_cn") or product.price_cn
            product.price_usd = clean_price(result.get("suggested_price_usd", 0)) or product.price_usd
            product.tags = result.get("tags") or product.tags

            logger.info(f"Extracted: {product.title_en[:50]} | ${product.price_usd} | {len(product.tags)} tags")
            return StageResult.ok(product)

        except Exception as exc:
            logger.error(f"Extract failed: {exc}")
            return StageResult.fail(str(exc), product)


class AnalyzeStage:
    """Stage 3: Analyze US market potential via LLM."""

    def __init__(self, llm: LLMService, threshold: int = 60) -> None:
        self.llm = llm
        self.threshold = threshold

    async def run(self, product: Product) -> StageResult[Product]:
        try:
            info = f"Title: {product.title_en}\nDescription: {product.description_en[:500]}\nPrice: ¥{product.price_cn}\nTags: {', '.join(product.tags)}"
            prompt = MARKET_ANALYZE_PROMPT.format(product_info=info)
            result = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                max_tokens=800,
            )

            if result.get("_parse_error"):
                logger.warning("Market analysis LLM returned invalid JSON — archiving product")
                product.status = PipelineStatus.ARCHIVED
                product.error_message = "Analyze: LLM JSON parse failed"
                return StageResult.ok(product)

            product.market_score = MarketScore(
                total=result.get("total", 0),
                visual_appeal=result.get("visual_appeal", 0),
                category_demand=result.get("category_demand", 0),
                uniqueness=result.get("uniqueness", 0),
                price_arbitrage=result.get("price_arbitrage", 0),
                trend_alignment=result.get("trend_alignment", 0),
                reasoning=result.get("reasoning", ""),
            )

            passed = product.market_score.total >= self.threshold
            product.status = PipelineStatus.ANALYZED if passed else PipelineStatus.ARCHIVED
            logger.info(f"Score: {product.market_score.total}/100 {'PASS' if passed else 'ARCHIVE'}")
            return StageResult.ok(product)

        except Exception as exc:
            logger.error(f"Analyze failed: {exc}")
            return StageResult.fail(str(exc), product)


class ProcessStage:
    """Stage 4: Generate SEO description, download and style images."""

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm
        self.styler = ImageStyler()

    async def run(self, product: Product) -> StageResult[Product]:
        if product.status != PipelineStatus.ANALYZED:
            return StageResult.ok(product)

        try:
            # Build optimized description
            prompt = DESCRIPTION_BUILD_PROMPT.format(
                title_en=product.title_en,
                description_en=product.description_en,
                tags=", ".join(product.tags),
            )
            desc = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                max_tokens=1500,
            )

            if not desc.get("_parse_error"):
                product.optimized_description = desc.get("description_html", product.description_en)
                product.tags = desc.get("suggested_tags", product.tags)

            # Download and process images (run sync styler in thread pool)
            local_images = await self._download_images(product)
            local_sku = await self._download_sku_images(product)
            all_local = local_images + local_sku
            output_dir = Path("data/processed") / product.make_handle()
            processed = []
            loop = asyncio.get_running_loop()
            for img_path in all_local:
                style_path = await loop.run_in_executor(
                    None, self.styler.adapt, img_path, output_dir, product.title_en
                )
                processed.append(style_path)

            product.images = [str(p) for p in processed] if processed else product.images
            product.status = PipelineStatus.PROCESSED

            logger.info(f"Processed: {len(processed)} images | desc: {len(product.optimized_description)} chars")
            return StageResult.ok(product)

        except Exception as exc:
            logger.error(f"Process failed: {exc}")
            return StageResult.fail(str(exc), product)

    async def _download_images(self, product: Product) -> list[Path]:
        import ipaddress
        import socket

        import httpx

        handle = product.make_handle()
        img_dir = Path("data/images") / handle
        img_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []

        for i, url in enumerate(product.images[:10]):
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
                        logger.warning(f"Blocked private/internal URL: {url}")
                        continue
                except (socket.gaierror, ValueError):
                    continue

                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, follow_redirects=True)
                    resp.raise_for_status()

                    content_type = resp.headers.get("content-type", "")
                    if not content_type.startswith("image/"):
                        logger.warning(f"Skip non-image URL (content-type={content_type}): {url}")
                        continue

                    content = await resp.aread()
                    if len(content) > 20 * 1024 * 1024:
                        logger.warning(f"Skip oversized image ({len(content)} bytes): {url}")
                        continue

                    ext = url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
                    if ext.lower() not in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
                        ext = "jpg"
                    local_path = img_dir / f"{handle}_{i+1:02d}.{ext}"
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, local_path.write_bytes, content)
                    paths.append(local_path)
            except Exception as exc:
                logger.debug(f"Skip image {i}: {exc}")

        return paths

    async def _download_sku_images(self, product: Product) -> list[Path]:
        import ipaddress
        import socket

        import httpx

        handle = product.make_handle()
        img_dir = Path("data/images") / handle
        img_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []

        for sku in product.sku_prices:
            sku_name = sanitize_filename(sku.get("name", "sku"))
            sku_images = sku.get("images", [])
            for j, url in enumerate(sku_images):
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
                        if len(sku_images) <= 1:
                            local_path = img_dir / f"{sku_name}.{ext}"
                        else:
                            local_path = img_dir / f"{sku_name}_{j+1:02d}.{ext}"
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, local_path.write_bytes, content)
                        paths.append(local_path)
                except Exception as exc:
                    logger.debug(f"Skip SKU image {sku_name}[{j}]: {exc}")

        return paths


class PublishStage:
    """Stage 5: Export to CSV / push to Shopify."""

    def __init__(self, repo: ProductRepository | None = None) -> None:
        self._repo = repo

    async def run_csv(self, products: list[Product], output_path: str = "data/exports/products.csv") -> StageResult[Path]:
        try:
            exporter = CSVExporter()
            path = exporter.export(products, output_path)
            if self._repo:
                for p in products:
                    p.status = PipelineStatus.CSV_EXPORTED
                    await self._repo.save(p)
            logger.success(f"Exported {len(products)} to {path}")
            return StageResult.ok(path)
        except Exception as exc:
            return StageResult.fail(str(exc))
