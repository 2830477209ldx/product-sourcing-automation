from src.models.product import MarketScore, PipelineStatus, Platform, Product


class TestPipelineStatus:
    def test_values(self):
        assert PipelineStatus.SCRAPED == "scraped"
        assert PipelineStatus.ANALYZED == "analyzed"
        assert PipelineStatus.PROCESSED == "processed"
        assert PipelineStatus.REVIEW_PENDING == "review_pending"
        assert PipelineStatus.APPROVED == "approved"
        assert PipelineStatus.REJECTED == "rejected"
        assert PipelineStatus.PUSHED_TO_SHOPIFY == "pushed_to_shopify"
        assert PipelineStatus.CSV_EXPORTED == "csv_exported"
        assert PipelineStatus.ARCHIVED == "archived"

    def test_enum_count(self):
        assert len(PipelineStatus) == 9


class TestPlatform:
    def test_values(self):
        assert Platform.XIAOHONGSHU == "xiaohongshu"
        assert Platform.TAOBAO == "taobao"
        assert Platform.ALIBABA == "alibaba"


class TestMarketScore:
    def test_defaults(self):
        score = MarketScore()
        assert score.total == 0
        assert score.visual_appeal == 0
        assert score.category_demand == 0
        assert score.uniqueness == 0
        assert score.price_arbitrage == 0
        assert score.trend_alignment == 0
        assert score.reasoning == ""

    def test_full_init(self):
        score = MarketScore(
            total=85,
            visual_appeal=90,
            category_demand=80,
            uniqueness=75,
            price_arbitrage=85,
            trend_alignment=95,
            reasoning="Strong potential",
        )
        assert score.total == 85
        assert score.visual_appeal == 90
        assert score.category_demand == 80
        assert score.uniqueness == 75
        assert score.price_arbitrage == 85
        assert score.trend_alignment == 95
        assert score.reasoning == "Strong potential"

    def test_from_dict(self):
        score = MarketScore(**{"total": 60, "visual_appeal": 55, "category_demand": 70, "uniqueness": 50, "price_arbitrage": 65, "trend_alignment": 60, "reasoning": "ok"})
        assert score.total == 60


class TestProduct:
    def test_default_product(self):
        p = Product()
        assert p.source_url == ""
        assert p.title_cn == ""
        assert p.price_cn == ""
        assert p.images == []
        assert p.sku_prices == []
        assert p.tags == []
        assert p.status == PipelineStatus.SCRAPED
        assert p.price_usd == 0.0
        assert p.created_at  # auto-generated
        assert p.updated_at  # auto-generated

    def test_full_product(self, sample_product_dict):
        p = Product(**sample_product_dict)
        assert p.id == "a1b2c3d4e5f6"
        assert p.platform == Platform.TAOBAO
        assert p.title_cn == "测试产品"
        assert p.title_en == "Test Product"
        assert p.price_usd == 13.75
        assert len(p.images) == 2
        assert len(p.tags) == 3
        assert p.market_score.total == 72

    def test_make_handle_english(self):
        p = Product(title_en="Premium Kitchen Gadget Set", title_cn="厨房用品")
        handle = p.make_handle()
        assert handle == "premium-kitchen-gadget-set"

    def test_make_handle_chinese_fallback(self):
        p = Product(title_en="", title_cn="测试产品")
        handle = p.make_handle()
        assert handle == "product"
    
    def test_make_handle_chinese_with_id(self):
        p = Product(id="a1b2c3d4e5f6", title_en="", title_cn="测试产品")
        handle = p.make_handle()
        assert handle == "product-a1b2c3d4"

    def test_make_handle_no_title(self):
        p = Product(title_en="", title_cn="")
        handle = p.make_handle()
        assert handle == "product"

    def test_make_handle_special_chars(self):
        p = Product(title_en="Hello! World & Test/Product")
        handle = p.make_handle()
        assert handle == "hello-world-test-product"

    def test_make_handle_truncation(self):
        p = Product(title_en="a" * 80)
        handle = p.make_handle()
        assert len(handle) <= 60

    def test_dict_for_db(self):
        p = Product(id="test123", title_en="Test")
        d = p.dict_for_db()
        assert d["id"] == "test123"
        assert d["title_en"] == "Test"
        assert "created_at" in d
