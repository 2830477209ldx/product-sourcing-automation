"""Experimental test script — search Taobao and extract product details."""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.agents.product_agent import ProductAgent
from src.models.product import Product, PipelineStatus, Platform
from src.db.repository import ProductRepository


async def main():
    agent = ProductAgent(headless=False)
    repo = ProductRepository()

    url = "https://item.taobao.com/item.htm?id=..."

    print("Opening browser... scan QR code if prompted\n")
    data = await agent.extract(url)

    if data.get("_error"):
        print(f"Extraction failed: {data['_error']}")
        await agent.close()
        return

    p = Product(
        id=uuid.uuid4().hex[:12],
        platform=Platform.TAOBAO,
        source_url=data.get("source_url", url),
        title_cn=data.get("title_cn", ""),
        price_cn=data.get("price_cn", ""),
        description_cn=data.get("description_cn", ""),
        images=data.get("image_urls", []),
        sku_prices=data.get("sku_prices", []),
        status=PipelineStatus.SCRAPED,
    )
    await repo.save(p)
    print(f"Saved product: {p.id}")

    await agent.close()
    await repo.close()

    print("\n=== Product ===\n")
    d = p.model_dump()
    for key in ["title_cn", "price_cn", "source_url", "images", "sku_prices"]:
        val = d.get(key)
        if isinstance(val, list):
            print(f"  {key}: {len(val)} items")
            if key == "sku_prices":
                for sku in val[:5]:
                    print(f"         {str(sku.get('name','?'))[:30]}: {str(sku.get('price','?'))}")
        else:
            print(f"  {key}: {str(val)[:80]}")
    print()


asyncio.run(main())
