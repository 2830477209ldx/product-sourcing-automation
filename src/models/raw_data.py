"""ProductRawData — unified extraction output contract.

Both entry points (CLI via SlimDOM, Extension via DOM drilling agent)
MUST produce this exact structure before feeding into the Pipeline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SkuVariant(BaseModel):
    name: str = ""
    price: str = ""
    images: list[str] = Field(default_factory=list)


class ProductRawData(BaseModel):
    source_url: str
    title_cn: str = ""
    price_cn: str = ""
    image_urls: list[str] = Field(default_factory=list)
    desc_images: list[str] = Field(default_factory=list)
    sku_prices: list[SkuVariant] = Field(default_factory=list)
    description_cn: str = ""
    sku_type: str = ""
