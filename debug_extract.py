"""Diagnostic: dump container data from a product page for inspection."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.agents.product_agent import ProductAgent
from src.agents.slimdom_extractor import JS_COLLECT_CONTAINERS, JS_DETECT_SKU_TYPE


async def main(url: str):
    agent = ProductAgent(headless=True)
    try:
        browser = await agent._get_browser()
        page = await browser.get_current_page()
        if asyncio.iscoroutine(page):
            page = await page

        print(f"Navigating to {url[:80]}...")
        await page.goto(url)
        await asyncio.sleep(5)

        print("Collecting containers...")
        raw = await page.evaluate(JS_COLLECT_CONTAINERS)
        data = json.loads(raw) if isinstance(raw, str) else raw
        containers = data.get("containers", [])
        vw = data.get("vw", 375)
        vh = data.get("vh", 800)
        print(f"CONTAINERS: {len(containers)}, vw={vw}, vh={vh}")
        for c in containers[:25]:
            p = c.get("p", "")
            t = c.get("t", "")
            cls = str(c.get("c", ""))[:50]
            im = c.get("im", 0)
            r = c.get("r", [0, 0, 0, 0])
            tx = str(c.get("tx", ""))[:50]
            hi = c.get("hi", False)
            ifr = c.get("ifr", "")
            extra = ""
            if ifr:
                extra = f" ifr={ifr[:60]}"
            if hi:
                extra += " [INTERACTIVE]"
            print(f"  [{p}] {t} cls={cls} im={im} r=({r[0]}x{r[1]}@{r[2]}) tx={tx}{extra}")

        print("\n--- SKU Detection ---")
        sku_raw = await page.evaluate(JS_DETECT_SKU_TYPE)
        sku = json.loads(sku_raw) if isinstance(sku_raw, str) else sku_raw
        sku_type = sku.get("type", "?")
        groups = sku.get("groups", [])
        total = sku.get("total", 0)
        print(f"type={sku_type}, groups={len(groups)}, total={total}")
        for g in groups:
            name = g.get("name", "?")
            opts = g.get("options", [])
            print(f"  {name}: {opts[:10]}")

        print("\n--- IFRAMES ---")
        iframe_count = await page.evaluate("() => document.querySelectorAll('iframe').length")
        print(f"Total iframes: {iframe_count}")

        print("\n--- Page title ---")
        print((await page.title() or "")[:100])
    finally:
        try:
            await agent.close()
        except Exception:
            pass


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://detail.tmall.com/item.htm?id=779234892168"
    asyncio.run(main(url))
