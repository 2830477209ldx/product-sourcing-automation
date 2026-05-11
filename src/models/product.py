"""Product domain model."""

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.utils import clean_price


class PipelineStatus(str, Enum):
    SCRAPED = "scraped"
    ANALYZED = "analyzed"
    PROCESSED = "processed"
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUSHED_TO_SHOPIFY = "pushed_to_shopify"
    CSV_EXPORTED = "csv_exported"
    ARCHIVED = "archived"


class Platform(str, Enum):
    XIAOHONGSHU = "xiaohongshu"
    TAOBAO = "taobao"
    ALIBABA = "alibaba"


class MarketScore(BaseModel):
    total: float = 0.0
    visual_appeal: float = 0.0
    category_demand: float = 0.0
    uniqueness: float = 0.0
    price_arbitrage: float = 0.0
    trend_alignment: float = 0.0
    reasoning: str = ""


class Product(BaseModel):
    id: str | None = None
    platform: Platform | None = None
    source_url: str = ""

    title_cn: str = ""
    price_cn: str = ""
    description_cn: str = ""
    images: list[str] = Field(default_factory=list)
    desc_images: list[str] = Field(default_factory=list)
    sku_prices: Any = Field(default_factory=list)  # list[dict], intentionally untyped — raw JSON from scraper

    market_score: MarketScore | None = None

    title_en: str = ""
    description_en: str = ""
    optimized_description: str = ""
    price_usd: float = 0.0
    tags: list[str] = Field(default_factory=list)

    status: PipelineStatus = PipelineStatus.SCRAPED
    shopify_product_id: str | None = None
    error_message: str | None = None

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("price_usd", mode="before")
    @classmethod
    def _clean_price(cls, v: Any) -> float:
        return clean_price(v)

    def make_handle(self) -> str:
        base = self.title_en or self.title_cn or "product"
        handle = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
        if not handle:
            handle = f"product-{self.id[:8]}" if self.id else "product"
        return handle[:60]

    def dict_for_db(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
