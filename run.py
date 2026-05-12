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
from src.llm import create_llm_service


def _make_pipeline(headless: bool = False):
    from src.pipeline.pipeline import Pipeline

    llm = create_llm_service()
    return Pipeline(
        llm=llm,
        threshold=Config.instance().market_judge.get("threshold", 60),
        headless=headless,
    )


@click.group()
def cli():
    """Product Sourcing — paste URL, get Shopify-ready product data."""


@cli.command(name="add")
@click.option("--url", "-u", default=None, help="Product page URL")
@click.option("--file", "-f", default=None, help="File with one URL per line")
@click.option("--headless/--visible", default=False, help="Run browser in headless mode")
def add_product(url: str, file: str, headless: bool):
    """Import products: extract, analyze, process. Supports single URL or batch file."""
    urls: list[str] = []
    if url:
        urls.append(url)
    if file:
        with open(file, encoding="utf-8") as f:
            urls += [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not urls:
        click.echo("Provide --url or --file")
        return

    pipeline = _make_pipeline(headless=headless)

    if len(urls) == 1:
        click.echo(f"Importing: {urls[0][:80]}\n")
        result = asyncio.run(pipeline.import_from_url(urls[0]))
        if result.failed:
            click.echo(f"FAILED: {result.error}")
        else:
            click.echo(f"SUCCESS: {result.data.title_en if result.data else '?'}")
    else:
        click.echo(f"Importing {len(urls)} URLs...\n")
        results = asyncio.run(pipeline.import_batch(urls))
        ok = sum(1 for r in results if not r.failed)
        click.echo(f"Done: {ok}/{len(urls)} succeeded")

    asyncio.run(pipeline.close())


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
