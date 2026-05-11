from __future__ import annotations

import csv
from pathlib import Path

from loguru import logger

from src.models.product import Product


class CSVExporter:
    """Exports products in Shopify-compatible CSV format for manual import."""

    COLUMNS = [
        "Handle",
        "Title",
        "Body (HTML)",
        "Vendor",
        "Product Category",
        "Type",
        "Tags",
        "Published",
        "Option1 Name",
        "Option1 Value",
        "Variant SKU",
        "Variant Price",
        "Variant Compare At Price",
        "Image Src",
        "SEO Title",
        "SEO Description",
        "Status",
    ]

    def export(self, products: list[Product], output_path: str | Path) -> Path:
        """Export products to a Shopify CSV file."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.COLUMNS)
            writer.writeheader()

            for product in products:
                row = self._product_to_row(product)
                writer.writerow(row)

        logger.success(f"Exported {len(products)} products to {output_path}")
        return output_path

    def _product_to_row(self, product: Product) -> dict[str, str]:
        return {
            "Handle": (product.title_en or product.title_cn).lower()
            .replace(" ", "-")
            .replace("/", "-")[:60],
            "Title": product.title_en or product.title_cn,
            "Body (HTML)": product.optimized_description or product.description_en,
            "Vendor": product.platform.value if product.platform else "",
            "Product Category": "",
            "Type": product.tags[0] if product.tags else "",
            "Tags": ", ".join(product.tags),
            "Published": "FALSE",
            "Option1 Name": "Title",
            "Option1 Value": "Default Title",
            "Variant SKU": "",
            "Variant Price": str(product.price_usd) if product.price_usd else "",
            "Variant Compare At Price": "",
            "Image Src": product.images[0] if product.images else "",
            "SEO Title": product.title_en or "",
            "SEO Description": (product.optimized_description or product.description_en or "")[
                :320
            ],
            "Status": "draft",
        }
