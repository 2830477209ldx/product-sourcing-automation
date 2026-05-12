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
from src.utils import clean_price, detect_platform
from src.downloader import download_images, download_sku_images
from src.prompts import DESCRIPTION_BUILD_PROMPT, MARKET_ANALYZE_PROMPT, STRUCTURED_EXTRACT_PROMPT


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
                sku_prices=json.dumps(
                    product.sku_prices[:20] if isinstance(product.sku_prices, list) else [],
                    ensure_ascii=False,
                ) if product.sku_prices else "none",
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
            suggested = result.get("suggested_price_usd")
            if suggested is not None and str(suggested).strip() != "":
                product.price_usd = clean_price(suggested)
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
            handle = product.make_handle()
            local_images = await download_images(handle, product.images[:10], handle)
            local_sku = await download_sku_images(handle, product.sku_prices)
            all_local = local_images + local_sku
            output_dir = Path("data/processed") / product.make_handle()
            processed = []
            loop = asyncio.get_running_loop()
            for img_path in all_local:
                style_path = await loop.run_in_executor(
                    None, self.styler.adapt, img_path, output_dir, product.title_en
                )
                processed.append(style_path)

            if processed:
                # Preserve original URLs alongside processed local paths for re-processing
                existing_urls = {str(u) for u in (product.images if isinstance(product.images, list) else []) if str(u).startswith("http")}
                processed_paths = {str(p) for p in processed}
                product.images = list(existing_urls | processed_paths)
            product.status = PipelineStatus.PROCESSED

            logger.info(f"Processed: {len(processed)} images | desc: {len(product.optimized_description)} chars")
            return StageResult.ok(product)

        except Exception as exc:
            logger.error(f"Process failed: {exc}")
            return StageResult.fail(str(exc), product)


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
