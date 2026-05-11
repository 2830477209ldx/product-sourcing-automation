"""SQLite product repository — async, concurrent-safe, indexed."""

import asyncio
import json
from pathlib import Path

import aiosqlite

from src.models.product import PipelineStatus, Product

DB_PATH = Path("data/products.db")


class ProductRepository:
    """Async SQLite-backed product storage."""

    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    self._conn = await aiosqlite.connect(str(self.db_path))
                    self._conn.row_factory = aiosqlite.Row
                    await self._conn.execute("PRAGMA journal_mode=WAL")
                    await self._migrate()
        return self._conn

    async def _migrate(self) -> None:
        conn = await self._get_conn()
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                platform TEXT,
                source_url TEXT,
                title_cn TEXT,
                price_cn TEXT,
                description_cn TEXT,
                images TEXT,
                desc_images TEXT,
                sku_prices TEXT,
                market_score TEXT,
                title_en TEXT,
                description_en TEXT,
                optimized_description TEXT,
                price_usd REAL,
                tags TEXT,
                status TEXT DEFAULT 'scraped',
                shopify_product_id TEXT,
                error_message TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # Add desc_images column if missing (migration for existing DBs)
        try:
            await conn.execute("ALTER TABLE products ADD COLUMN desc_images TEXT DEFAULT '[]'")
        except Exception:
            pass
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON products(status)")
        await conn.commit()

    async def save(self, product: Product) -> str:
        conn = await self._get_conn()
        d = product.model_dump(mode="json")
        # Serialize complex fields
        d["images"] = json.dumps(d.get("images", []))
        d["desc_images"] = json.dumps(d.get("desc_images", []))
        d["sku_prices"] = json.dumps(d.get("sku_prices", []))
        d["tags"] = json.dumps(d.get("tags", []))
        d["market_score"] = json.dumps(d["market_score"]) if d.get("market_score") else "{}"
        await conn.execute(
            """INSERT OR REPLACE INTO products
               (id,platform,source_url,title_cn,price_cn,description_cn,images,desc_images,sku_prices,
                market_score,title_en,description_en,optimized_description,price_usd,tags,
                status,shopify_product_id,error_message,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d["id"], d.get("platform"), d.get("source_url"),
                d.get("title_cn"), d.get("price_cn"), d.get("description_cn"),
                d["images"], d["desc_images"], d["sku_prices"], d["market_score"],
                d.get("title_en"), d.get("description_en"), d.get("optimized_description"),
                d.get("price_usd", 0), d["tags"],
                d.get("status", "scraped"), d.get("shopify_product_id"),
                d.get("error_message"), d.get("created_at"), d.get("updated_at"),
            ),
        )
        await conn.commit()
        return product.id or ""

    async def get(self, product_id: str) -> Product | None:
        conn = await self._get_conn()
        row = await conn.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        row = await row.fetchone()
        return self._row_to_product(row) if row else None

    async def list_by_status(self, status: PipelineStatus) -> list[Product]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM products WHERE status = ? ORDER BY updated_at DESC",
            (status.value,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_product(r) for r in rows]

    async def list_all(self) -> list[Product]:
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT * FROM products ORDER BY updated_at DESC")
        rows = await cursor.fetchall()
        return [self._row_to_product(r) for r in rows]

    async def count_by_status(self, status: PipelineStatus) -> int:
        conn = await self._get_conn()
        row = await conn.execute("SELECT COUNT(*) FROM products WHERE status = ?", (status.value,))
        row = await row.fetchone()
        return row[0] if row else 0

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _row_to_product(row: aiosqlite.Row) -> Product:
        d = dict(row)
        for field in ("images", "desc_images", "sku_prices", "tags"):
            d[field] = json.loads(d.get(field, "[]"))
        score = d.get("market_score", "{}")
        d["market_score"] = json.loads(score) if isinstance(score, str) else score
        d["status"] = d.get("status", "scraped")
        # Clean price_usd: normalize prefixed strings like "$18.99"
        price = d.get("price_usd")
        if isinstance(price, str):
            d["price_usd"] = price.strip().replace("$", "").replace(",", "")
        return Product(**d)
