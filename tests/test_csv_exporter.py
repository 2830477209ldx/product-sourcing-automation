import tempfile
from pathlib import Path

import pytest

from src.models.product import MarketScore, Platform, Product
from src.shopify.csv_exporter import CSVExporter


class TestCSVExporter:
    @pytest.fixture
    def sample_products(self):
        p1 = Product(
            id="p1",
            platform=Platform.TAOBAO,
            title_cn="测试产品A",
            title_en="Test Product A",
            price_usd=19.99,
            optimized_description="<h2>Amazing Product</h2><p>Buy now!</p>",
            tags=["kitchen", "gadget", "home"],
            images=["https://cdn.example.com/img1.jpg"],
            market_score=MarketScore(total=85, reasoning="good"),
        )
        p2 = Product(
            id="p2",
            platform=Platform.ALIBABA,
            title_cn="产品B",
            title_en="Product B",
            price_usd=29.99,
            description_en="Simple description",
            tags=["office", "supplies"],
            images=["https://cdn.example.com/img2.jpg"],
        )
        return [p1, p2]

    def test_export_creates_file(self, sample_products):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.csv"
            exporter = CSVExporter()
            path = exporter.export(sample_products, output)
            assert path.exists()
            assert path.suffix == ".csv"

    def test_export_has_header_and_rows(self, sample_products):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.csv"
            exporter = CSVExporter()
            exporter.export(sample_products, output)

            content = output.read_text(encoding="utf-8")
            lines = content.strip().split("\n")
            assert len(lines) == 3  # header + 2 products

            header = lines[0]
            assert "Handle" in header
            assert "Title" in header
            assert "Body (HTML)" in header
            assert "Variant Price" in header
            assert "Image Src" in header

    def test_export_empty_products(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "empty.csv"
            exporter = CSVExporter()
            exporter.export([], output)
            content = output.read_text(encoding="utf-8")
            lines = content.strip().split("\n")
            assert len(lines) == 1  # header only

    def test_export_handle_format(self, sample_products):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.csv"
            exporter = CSVExporter()
            exporter.export(sample_products, output)

            import csv
            with open(output, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert rows[0]["Handle"] == "test-product-a"
            assert rows[1]["Handle"] == "product-b"

    def test_export_prices(self, sample_products):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.csv"
            exporter = CSVExporter()
            exporter.export(sample_products, output)

            import csv
            with open(output, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert rows[0]["Variant Price"] == "19.99"
            assert rows[1]["Variant Price"] == "29.99"

    def test_export_status_draft(self, sample_products):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.csv"
            exporter = CSVExporter()
            exporter.export(sample_products, output)

            import csv
            with open(output, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert rows[0]["Status"] == "draft"
            assert rows[1]["Status"] == "draft"
            assert rows[0]["Published"] == "FALSE"

    def test_export_with_chinese_title_fallback(self):
        p = Product(id="p3", title_cn="中文产品", title_en="")
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.csv"
            exporter = CSVExporter()
            exporter.export([p], output)

            import csv
            with open(output, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert rows[0]["Title"] == "中文产品"
