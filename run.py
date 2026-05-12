#!/usr/bin/env python
"""CLI entry point for product sourcing automation.

Usage:
    python run.py add --url https://item.taobao.com/item.htm?id=...
    python run.py add --file urls.txt
    python run.py review
    python run.py export
    python run.py status
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import click
from loguru import logger

from src.config import Config
from src.downloader import download_images, download_sku_images


def _make_pipeline(headless: bool = True):
    from src.llm.service import LLMService
    from src.pipeline.pipeline import Pipeline

    cfg = Config.instance()
    llm = LLMService(
        api_key=cfg.ai["api_key"],
        base_url=cfg.ai.get("base_url", ""),
        model_text=cfg.ai.get("model_text", "deepseek-chat"),
        model_vision=cfg.ai.get("model_vision", "deepseek-chat"),
        temperature=cfg.ai.get("temperature", 0.3),
        provider=cfg.ai.get("provider", "deepseek"),
    )
    return Pipeline(llm=llm, threshold=cfg.market_judge.get("threshold", 60), headless=headless)


@click.group()
def cli():
    """Product Sourcing — paste URL, get Shopify-ready product data."""


@cli.command(name="add")
@click.option("--url", "-u", required=True, help="Product page URL (Taobao, 1688, etc.)")
@click.option("--visible/--headless", default=False, help="Show browser window (needed for QR login)")
def add_product(url: str, visible: bool):
    """Import a product from URL: extract, analyze, process."""
    pipeline = _make_pipeline(headless=not visible)
    result = asyncio.run(pipeline.import_from_url(url))
    if result.failed:
        click.echo(f"FAILED: {result.error}")
    else:
        click.echo(f"SUCCESS: {result.data.title_en if result.data else '?'}")


@cli.command(name="add-i")
@click.option("--url", "-u", required=True, help="Product page URL")
def add_interactive(url: str):
    """Import product — one browser session, auto-stops when done, shows data immediately."""
    click.echo("Browser opening... if QR code appears, scan with Taobao app.\n")

    async def _run():
        import uuid
        from src.agents.product_agent import ProductAgent
        from src.db.repository import ProductRepository
        from src.models.product import Product, PipelineStatus, Platform
        from src.llm.service import LLMService
        from src.utils import detect_platform

        cfg = Config.instance()
        agent = ProductAgent(headless=False)
        repo = ProductRepository()

        # ── Step 1: Extract raw data from page (browser) ──
        data = await agent.extract(url)

        if data.get("_error") or data.get("_parse_failed"):
            click.echo(f"FAILED: {data.get('_error', 'parse failed')}")
            click.echo(f"Raw: {str(data.get('_raw_output', ''))[:500]}")
            await agent.close()
            return

        # Show extracted data IMMEDIATELY
        click.echo("\n" + "=" * 60)
        click.echo("EXTRACTED FROM PAGE:")
        click.echo(f"  Title: {data.get('title_cn', '?')[:80]}")
        click.echo(f"  Price: {data.get('price_cn', '?')}")
        click.echo(f"  Main Images: {len(data.get('image_urls', []))} URLs")
        click.echo(f"  Desc Images: {len(data.get('desc_images', []))} URLs")
        click.echo(f"  SKUs: {len(data.get('sku_prices', []))} variants")
        for sku in data.get("sku_prices", [])[:10]:
            img_tag = f" [{len(sku.get('images', []))} imgs]" if sku.get('images') else ""
            click.echo(f"    {sku.get('name','?')}: {sku.get('price','?')}{img_tag}")
        click.echo(f"  Desc: {len(data.get('description_cn',''))} chars")
        click.echo("=" * 60)

        # ── Download images locally ──
        product_id = uuid.uuid4().hex[:12]
        handle = data.get("title_cn", "") or product_id
        handle = re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-") or product_id
        handle = handle[:60]
        local_images = await download_images(handle, data.get("image_urls", []), handle)
        local_desc = await download_images(handle, data.get("desc_images", []), f"{handle}_desc")
        local_sku = await download_sku_images(handle, data.get("sku_prices", []))

        # ── Step 2: Save to DB ──
        platform_str = detect_platform(data.get("source_url", url))
        p = Product(
            id=product_id,
            platform=Platform(platform_str) if platform_str else None,
            source_url=data.get("source_url", url),
            title_cn=data.get("title_cn", ""),
            price_cn=data.get("price_cn", ""),
            description_cn=data.get("description_cn", ""),
            images=local_images + local_sku,
            desc_images=local_desc,
            sku_prices=data.get("sku_prices", []),
            status=PipelineStatus.SCRAPED,
        )
        await repo.save(p)
        click.echo(f"\nSaved to DB: {p.id}")

        # ── Step 3: LLM Processing (no browser needed) ──
        click.echo("\nAI analyzing...")
        llm = LLMService(
            api_key=cfg.ai["api_key"],
            base_url=cfg.ai.get("base_url", ""),
            model_text=cfg.ai.get("model_text", "deepseek-chat"),
            model_vision=cfg.ai.get("model_vision", "deepseek-chat"),
            temperature=cfg.ai.get("temperature", 0.3),
            provider=cfg.ai.get("provider", "deepseek"),
        )

        from src.pipeline.stages import ExtractStage, AnalyzeStage

        r = await ExtractStage(llm).run(p)
        if r.data:
            p = r.data
            r = await AnalyzeStage(llm).run(p)
            if r.data:
                p = r.data
                p.status = PipelineStatus.REVIEW_PENDING
                await repo.save(p)

                click.echo(f"\n  Score: {p.market_score.total if p.market_score else '?'}/100")
                click.echo(f"  Title EN: {p.title_en[:80]}")
                click.echo(f"  USD: ${p.price_usd}")
                click.echo(f"  Tags: {', '.join(p.tags[:8])}")

        # Browser stays open (session+cookies saved for next run)
        click.echo("\nBrowser stays open. Cookies saved. Next import won't need login.")
        click.echo("Data file: data/products.db")
        click.echo("Run 'python run.py status' to see all products.")

    asyncio.run(_run())


@cli.command(name="batch-add-i")
@click.option("--url", "-u", multiple=True, help="Product page URL (repeatable)")
@click.option("--file", "-f", default=None, help="File with one URL per line")
def batch_add_interactive(url: tuple, file: str):
    """Import multiple products in ONE browser session (cookie reuse, no re-login)."""
    urls: list[str] = list(url)
    if file:
        urls += [line.strip() for line in open(file) if line.strip() and not line.startswith("#")]
    if not urls:
        click.echo("No URLs provided. Use -u URL or -f file.txt")
        return

    click.echo(f"Batch importing {len(urls)} products in one browser session...\n")

    async def _run():
        import uuid
        from src.agents.product_agent import ProductAgent
        from src.db.repository import ProductRepository
        from src.models.product import Product, PipelineStatus, Platform
        from src.llm.service import LLMService
        from src.utils import detect_platform

        cfg = Config.instance()
        agent = ProductAgent(headless=False)
        repo = ProductRepository()

        # ── Extract all URLs in one session ──
        all_data = await agent.extract_batch(urls)

        ok_count = 0
        for i, data in enumerate(all_data):
            url = data.get("source_url", urls[i] if i < len(urls) else "")
            click.echo(f"\n{'=' * 50}\n  Processing [{i + 1}/{len(urls)}] {url[:70]}\n{'=' * 50}")

            if data.get("_error"):
                click.echo(f"  SKIP: {data['_error']}")
                continue

            # Download images
            product_id = uuid.uuid4().hex[:12]
            handle = data.get("title_cn", "") or product_id
            handle = re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-") or product_id
            handle = handle[:60]
            local_images = await download_images(handle, data.get("image_urls", []), handle)
            local_desc = await download_images(handle, data.get("desc_images", []), f"{handle}_desc")
            local_sku = await download_sku_images(handle, data.get("sku_prices", []))

            # Save to DB
            platform_str = detect_platform(url)
            p = Product(
                id=product_id,
                platform=Platform(platform_str) if platform_str else None,
                source_url=url,
                title_cn=data.get("title_cn", ""),
                price_cn=data.get("price_cn", ""),
                description_cn=data.get("description_cn", ""),
                images=local_images + local_sku,
                desc_images=local_desc,
                sku_prices=data.get("sku_prices", []),
                status=PipelineStatus.SCRAPED,
            )
            await repo.save(p)
            click.echo(f"  Saved to DB: {p.id}")

            # LLM analysis
            click.echo("  AI analyzing...")
            llm = LLMService(
                api_key=cfg.ai["api_key"],
                base_url=cfg.ai.get("base_url", ""),
                model_text=cfg.ai.get("model_text", "deepseek-chat"),
                model_vision=cfg.ai.get("model_vision", "deepseek-chat"),
                temperature=cfg.ai.get("temperature", 0.3),
                provider=cfg.ai.get("provider", "deepseek"),
            )
            from src.pipeline.stages import ExtractStage, AnalyzeStage

            r = await ExtractStage(llm).run(p)
            if r.data:
                p = r.data
                r = await AnalyzeStage(llm).run(p)
                if r.data:
                    p = r.data
                    p.status = PipelineStatus.REVIEW_PENDING
                    await repo.save(p)
                    score = p.market_score.total if p.market_score else 0
                    click.echo(f"  Score: {score}/100 | {p.title_en[:60]} | ${p.price_usd}")
            ok_count += 1

        click.echo(f"\n{'=' * 50}")
        click.echo(f"BATCH COMPLETE: {ok_count}/{len(urls)} succeeded")
        click.echo("Data file: data/products.db")
        await agent.close()
        await repo.close()

    asyncio.run(_run())


@cli.command(name="batch-add")
@click.option("--file", "-f", required=True, help="File with one URL per line")
def batch_add(file: str):
    """Import multiple products from a file of URLs."""
    urls = [line.strip() for line in open(file) if line.strip() and not line.startswith("#")]
    click.echo(f"Importing {len(urls)} URLs...")
    pipeline = _make_pipeline()
    results = asyncio.run(pipeline.import_batch(urls))
    ok = sum(1 for r in results if not r.failed)
    click.echo(f"Done: {ok}/{len(urls)} succeeded")


@cli.command()
def review():
    """Launch Streamlit review dashboard."""
    import subprocess

    dashboard = Path(__file__).parent / "src" / "webui" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(dashboard)])


@cli.command()
def export():
    """Export approved products to CSV."""
    pipeline = _make_pipeline()
    result = asyncio.run(pipeline.export_approved_csv())
    if result.failed:
        click.echo(result.error)
    else:
        click.echo(f"Exported to {result.data}")


@cli.command()
@click.option("--host", default="127.0.0.1", help="API server host")
@click.option("--port", default=0, help="API server port (0=auto)")
def api(host: str, port: int):
    """Start API server for Chrome extension integration."""
    from src.api.server import run

    run(host=host, port=port)


@cli.command()
def status():
    """Show pipeline status summary."""
    from src.db.repository import ProductRepository
    from collections import Counter

    async def _run():
        repo = ProductRepository()
        all_products = await repo.list_all()
        if not all_products:
            click.echo("No products in database.")
            return
        counts = Counter(p.status.value for p in all_products)
        click.echo(f"\nTotal: {len(all_products)}\n")
        for s, c in counts.most_common():
            click.echo(f"  {s:20s} {c:4d}")
        await repo.close()

    asyncio.run(_run())


if __name__ == "__main__":
    logger.add("pipeline.log", rotation="10 MB", level="INFO")
    cli()
