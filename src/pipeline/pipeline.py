"""Pipeline orchestrator — schedule stages, no business logic."""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from src.db.repository import ProductRepository
from src.llm.service import LLMService
from src.models.product import PipelineStatus, Product
from src.pipeline import StageResult
from src.pipeline.stages import (
    LoadStage,
    ExtractStage,
    AnalyzeStage,
    ProcessStage,
    PublishStage,
)


class Pipeline:
    """Orchestrates the processing pipeline. No business logic — delegates to stages."""

    MAX_CONCURRENT_IMPORTS = 3

    def __init__(
        self,
        llm: LLMService,
        repo: ProductRepository | None = None,
        threshold: int = 60,
        headless: bool = True,
    ) -> None:
        self.llm = llm
        self.repo = repo or ProductRepository()
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_IMPORTS)
        self.load_stage = LoadStage(headless=headless)
        self.extract_stage = ExtractStage(llm)
        self.analyze_stage = AnalyzeStage(llm, threshold)
        self.process_stage = ProcessStage(llm)
        self.publish_stage = PublishStage(repo=self.repo)

    async def import_from_url(self, url: str) -> StageResult[Product]:
        """Full pipeline from a single URL:
        Load → Extract → Analyze → Process → Save.
        """
        r = await self.load_stage.run(url)
        if r.failed or not r.data:
            return r

        r = await self.extract_stage.run(r.data)
        if r.failed or not r.data:
            return r

        r = await self.analyze_stage.run(r.data)
        if r.failed or not r.data:
            return r

        product = r.data
        if product.status == PipelineStatus.ANALYZED:
            r = await self.process_stage.run(product)
            if r.failed or not r.data:
                return r
            product = r.data

        product.status = PipelineStatus.REVIEW_PENDING
        await self.repo.save(product)

        logger.success(f"Pipeline complete: {product.title_en[:50]} | Score: {product.market_score.total if product.market_score else 'N/A'}")
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
        await self.load_stage.close()
        await self.repo.close()
