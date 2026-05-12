"""Pipeline orchestrator — schedule stages, no business logic."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from loguru import logger

from src.db.repository import ProductRepository
from src.llm.service import LLMService
from src.models.product import PipelineStatus, Platform, Product
from src.models.raw_data import ProductRawData
from src.pipeline import StageResult
from src.pipeline.stages import (
    LoadStage,
    ExtractStage,
    AnalyzeStage,
    ProcessStage,
    PublishStage,
)
from src.utils import detect_platform
from src.downloader import download_images, download_sku_images


class Pipeline:
    """Orchestrates the processing pipeline. No business logic — delegates to stages."""

    MAX_CONCURRENT_IMPORTS = 3

    def __init__(
        self,
        llm: LLMService,
        repo: ProductRepository | None = None,
        threshold: int = 60,
        headless: bool = False,
    ) -> None:
        self.llm = llm
        self.repo = repo or ProductRepository()
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_IMPORTS)
        self.load_stage = LoadStage(headless=headless)
        self.extract_stage = ExtractStage(llm)
        self.analyze_stage = AnalyzeStage(llm, threshold)
        self.process_stage = ProcessStage(llm)
        self.publish_stage = PublishStage(repo=self.repo)

    async def run_from_raw(self, raw: ProductRawData) -> StageResult[Product]:
        """Extension path: ProductRawData → Extract → Analyze → Process → Save."""
        product_id = uuid.uuid4().hex[:12]
        handle = raw.title_cn or product_id
        handle = re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-") or product_id
        handle = handle[:60]

        local_images = await download_images(handle, raw.image_urls, handle)
        local_desc = await download_images(handle, raw.desc_images, f"{handle}_desc")
        local_sku = await download_sku_images(handle, [s.model_dump() for s in raw.sku_prices])

        platform_str = detect_platform(raw.source_url)
        platform = Platform(platform_str) if platform_str else None

        product = Product(
            id=product_id,
            platform=platform,
            source_url=raw.source_url,
            title_cn=raw.title_cn,
            price_cn=raw.price_cn,
            description_cn=raw.description_cn,
            images=local_images + local_sku,
            desc_images=local_desc,
            sku_prices=[s.model_dump() for s in raw.sku_prices],
            status=PipelineStatus.SCRAPED,
        )
        await self.repo.save(product)

        return await self._run_stages(product)

    async def import_from_url(self, url: str) -> StageResult[Product]:
        """CLI path: URL → Load → Extract → Analyze → Process → Save."""
        r = await self.load_stage.run(url)
        if r.failed or not r.data:
            return r
        return await self._run_stages(r.data)

    async def _run_stages(self, product: Product) -> StageResult[Product]:
        """Extract → Analyze → Process → Save."""
        r = await self.extract_stage.run(product)
        if r.failed or not r.data:
            return StageResult.fail(r.error, product)
        product = r.data
        await self.repo.save(product)

        r = await self.analyze_stage.run(product)
        if r.failed or not r.data:
            return StageResult.fail(r.error, product)
        product = r.data
        await self.repo.save(product)

        if product.status == PipelineStatus.ANALYZED:
            r = await self.process_stage.run(product)
            if r.failed or not r.data:
                return StageResult.fail(r.error, product)
            product = r.data

        product.status = PipelineStatus.REVIEW_PENDING
        await self.repo.save(product)

        logger.success(
            f"Pipeline complete: {product.title_en[:50]} | "
            f"Score: {product.market_score.total if product.market_score else 'N/A'}"
        )
        return StageResult.ok(product)

    async def import_batch(self, urls: list[str]) -> list[StageResult[Product]]:
        """Import multiple URLs with controlled concurrency."""
        async def _import_one(url: str) -> StageResult[Product]:
            async with self._semaphore:
                return await self.import_from_url(url)
        tasks = [_import_one(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            r if isinstance(r, StageResult)
            else StageResult.fail(str(r))
            for r in results
        ]

    async def export_approved_csv(self) -> StageResult[Path]:
        """Export all approved products to CSV."""
        products = await self.repo.list_by_status(PipelineStatus.APPROVED)
        if not products:
            return StageResult.fail("No approved products")
        return await self.publish_stage.run_csv(products)

    async def close(self) -> None:
        try:
            await self.load_stage.close()
        except Exception:
            pass
        try:
            await self.repo.close()
        except Exception:
            pass
