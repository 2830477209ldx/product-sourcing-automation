import tempfile
from pathlib import Path

import pytest

from src.db.repository import ProductRepository
from src.llm.service import LLMService


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
async def repo(temp_db):
    """Create a ProductRepository with temp database."""
    r = ProductRepository(db_path=temp_db)
    yield r
    await r.close()


@pytest.fixture
def mock_llm():
    """Create an LLMService with dummy credentials (no real API calls)."""
    return LLMService(
        api_key="test-key",
        base_url="https://api.test.com",
    )


@pytest.fixture
def sample_product_dict():
    return {
        "id": "a1b2c3d4e5f6",
        "platform": "taobao",
        "source_url": "https://item.taobao.com/item.htm?id=123456",
        "title_cn": "测试产品",
        "price_cn": "¥99.00",
        "description_cn": "这是一个测试产品",
        "images": ["https://img.example.com/1.jpg", "https://img.example.com/2.jpg"],
        "sku_prices": [{"name": "Red", "price": "99"}, {"name": "Blue", "price": "109"}],
        "market_score": {
            "total": 72,
            "visual_appeal": 75,
            "category_demand": 80,
            "uniqueness": 65,
            "price_arbitrage": 70,
            "trend_alignment": 70,
            "reasoning": "Good product",
        },
        "title_en": "Test Product",
        "description_en": "This is a test product",
        "optimized_description": "<h2>Test</h2><p>Description</p>",
        "price_usd": 13.75,
        "tags": ["home", "kitchen", "gadget"],
        "status": "scraped",
        "created_at": "2026-05-07T00:00:00Z",
        "updated_at": "2026-05-07T00:00:00Z",
    }
