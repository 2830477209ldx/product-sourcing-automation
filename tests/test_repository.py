import pytest

from src.models.product import PipelineStatus, Product


class TestRepository:
    @pytest.mark.asyncio
    async def test_save_and_get(self, repo, sample_product_dict):
        p = Product(**sample_product_dict)
        saved_id = await repo.save(p)
        assert saved_id == "a1b2c3d4e5f6"

        fetched = await repo.get("a1b2c3d4e5f6")
        assert fetched is not None
        assert fetched.id == "a1b2c3d4e5f6"
        assert fetched.title_cn == "测试产品"
        assert fetched.title_en == "Test Product"
        assert fetched.price_usd == 13.75
        assert len(fetched.images) == 2
        assert len(fetched.tags) == 3
        assert fetched.market_score is not None
        assert fetched.market_score.total == 72

    @pytest.mark.asyncio
    async def test_save_twice_overwrites(self, repo, sample_product_dict):
        p = Product(**sample_product_dict)
        await repo.save(p)

        p.title_en = "Updated Title"
        p.price_usd = 19.99
        await repo.save(p)

        fetched = await repo.get("a1b2c3d4e5f6")
        assert fetched.title_en == "Updated Title"
        assert fetched.price_usd == 19.99

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo):
        fetched = await repo.get("nonexistent")
        assert fetched is None

    @pytest.mark.asyncio
    async def test_list_all_empty(self, repo):
        products = await repo.list_all()
        assert products == []

    @pytest.mark.asyncio
    async def test_list_all(self, repo, sample_product_dict):
        p1 = Product(**sample_product_dict)
        p2 = Product(**{**sample_product_dict, "id": "b2c3d4e5f6a1", "title_en": "Second"})
        await repo.save(p1)
        await repo.save(p2)

        products = await repo.list_all()
        assert len(products) == 2

    @pytest.mark.asyncio
    async def test_list_by_status(self, repo, sample_product_dict):
        p1 = Product(**sample_product_dict)  # status = scraped
        p2 = Product(**{**sample_product_dict, "id": "b2c3d4e5f6a1", "status": "approved"})
        p3 = Product(**{**sample_product_dict, "id": "c3d4e5f6a1b2", "status": "archived"})
        await repo.save(p1)
        await repo.save(p2)
        await repo.save(p3)

        scraped = await repo.list_by_status(PipelineStatus.SCRAPED)
        assert len(scraped) == 1
        assert scraped[0].id == "a1b2c3d4e5f6"

        approved = await repo.list_by_status(PipelineStatus.APPROVED)
        assert len(approved) == 1

        archived = await repo.list_by_status(PipelineStatus.ARCHIVED)
        assert len(archived) == 1

    @pytest.mark.asyncio
    async def test_count_by_status(self, repo, sample_product_dict):
        p1 = Product(**sample_product_dict)
        await repo.save(p1)

        count = await repo.count_by_status(PipelineStatus.SCRAPED)
        assert count == 1

        count = await repo.count_by_status(PipelineStatus.APPROVED)
        assert count == 0

    @pytest.mark.asyncio
    async def test_persist_json_fields(self, repo):
        p = Product(
            id="test-json",
            images=["https://a.com/1.jpg", "https://b.com/2.jpg"],
            sku_prices=[{"name": "S", "price": "10"}, {"name": "M", "price": "15"}],
            tags=["home", "garden"],
            market_score={"total": 80, "visual_appeal": 85, "category_demand": 75, "uniqueness": 80, "price_arbitrage": 70, "trend_alignment": 90, "reasoning": ""},
        )
        await repo.save(p)

        fetched = await repo.get("test-json")
        assert fetched.images == ["https://a.com/1.jpg", "https://b.com/2.jpg"]
        assert fetched.sku_prices == [{"name": "S", "price": "10"}, {"name": "M", "price": "15"}]
        assert fetched.tags == ["home", "garden"]
        assert fetched.market_score.total == 80

    @pytest.mark.asyncio
    async def test_save_minimal_product(self, repo):
        p = Product(id="minimal")
        await repo.save(p)

        fetched = await repo.get("minimal")
        assert fetched is not None
        assert fetched.id == "minimal"
        assert fetched.images == []
        assert fetched.tags == []
        assert fetched.market_score.total == 0  # empty {} deserializes to default MarketScore
