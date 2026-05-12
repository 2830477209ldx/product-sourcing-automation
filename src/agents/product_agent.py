"""Product extraction — AI browser navigation + container-classified extraction.

Navigation:     browser_use Agent (handles QR login, redirects)
Extraction:     SlimDOMExtractor (container classification + targeted extraction)
Fallback:       Container-based position heuristics (no CSS selectors)
Cookie:         browser_use CDP-based export_storage_state() → cookies.json
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from browser_use import Agent, Browser, BrowserProfile
from browser_use.llm import ChatDeepSeek
from loguru import logger

from src.agents.slimdom_extractor import (
    JS_CLICK_SKU_BY_LABEL,
    JS_COLLECT_CONTAINERS,
    JS_DETECT_SKU_TYPE,
    JS_SNAPSHOT_VISIBLE_IMAGES,
    SlimDOMExtractor,
)
from src.config import config
from src.llm import create_llm_service
from src.utils import SKIP_IMAGE_PATTERNS

PROFILE_DIR = Path("data/browser_profile")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_FILE = Path("data/cookies.json")

ALICDN_IMG_RE = re.compile(r'img\.alicdn\.com|img\.taobaocdn\.com|gw\.alicdn\.com')


def _is_product_image(url: str) -> bool:
    if any(p in url.lower() for p in SKIP_IMAGE_PATTERNS):
        return False
    if ALICDN_IMG_RE.search(url):
        return True
    if url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return True
    return False


def _normalize_img_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    url = re.sub(r'_\d+x\d+\.', '.', url, count=1)
    return url


class ProductAgent:
    """AI browser for navigation + container-classified extraction."""

    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._browser: Browser | None = None

    async def _get_browser(self) -> Browser:
        if self._browser is None:
            for attempt in range(3):
                try:
                    profile = BrowserProfile(
                        headless=self.headless,
                        keep_alive=True,
                        user_data_dir=str(PROFILE_DIR),
                    )
                    self._browser = Browser(browser_profile=profile)
                    await self._browser.start()
                    print(f"  Browser started (profile: {PROFILE_DIR})")
                    break
                except Exception as exc:
                    if attempt < 2:
                        print(f"  Browser launch attempt {attempt + 1} failed ({exc}), retrying...")
                        await asyncio.sleep(3)
                    else:
                        raise
        return self._browser

    async def extract(self, url: str) -> dict[str, Any]:
        browser = await self._get_browser()

        agent = Agent(
            task=(
                f"Navigate to {url}. "
                "If a login/QR page appears, wait for the user to scan the QR code. "
                "Once the product detail page is fully loaded, call done(). "
                "Do NOT create new tabs. Do NOT navigate away from the product page."
            ),
            llm=ChatDeepSeek(
                api_key=config.ai["api_key"],
                base_url=config.ai.get("base_url") or None,
                temperature=0.0,
            ),
            browser=browser,
            use_vision=False,
        )
        await agent.run(max_steps=8)

        page = browser.get_current_page()
        if asyncio.iscoroutine(page):
            page = await page
        if page is None:
            logger.error("No page available after agent run")
            return {"_error": "no page"}

        body_len = 0
        current_url = ""
        for attempt in range(20):
            try:
                body_len = await page.evaluate("() => (document.body?.innerText || '').length")
                if isinstance(body_len, str):
                    body_len = int(body_len) if body_len.isdigit() else 0
                current_url = await page.evaluate("() => window.location.href")
            except Exception:
                body_len = 0
                current_url = ""
            if body_len > 500 and "login" not in str(current_url).lower():
                break
            if attempt == 0:
                print(f"  Verifying page load... ({body_len} chars, {str(current_url)[:60]})")
            await asyncio.sleep(2)

        if body_len < 200:
            logger.warning(f"Page appears empty ({body_len} chars), proceeding anyway")

        if "login" in str(current_url).lower():
            print("\n  Still on login page — scan the QR code with Taobao app.")
            print("  The browser profile is new. After this login, all future runs auto-login.\n")
            return {"_error": "login_required", "source_url": url}

        try:
            storage_state = await browser.export_storage_state()
            if storage_state and storage_state.get("cookies"):
                COOKIES_FILE.write_text(
                    json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"  Saved {len(storage_state['cookies'])} cookies for next run")
        except Exception as exc:
            logger.debug(f"Cookie save failed: {exc}")

        llm = create_llm_service(temperature=0.1)
        extractor = SlimDOMExtractor(llm)
        ai_result = await extractor.extract(page, url)

        if ai_result.get("_error"):
            print(f"  [AI] Extraction failed ({ai_result['_error']}), using fallback...")
            ai_result = await self._fallback_extraction(page, url)

        result = self._normalize(ai_result)
        self._print_summary(result)
        return result

    async def _fallback_extraction(self, page, url: str) -> dict[str, Any]:
        """Container-based fallback: no CSS selectors, no AI — pure structural heuristics."""
        result: dict[str, Any] = {"source_url": url}
        try:
            raw = await page.evaluate(JS_COLLECT_CONTAINERS)
            page_data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return {"_error": "fallback_collect_failed", "source_url": url}

        containers = page_data.get("containers", [])
        vh = page_data.get("vh", 800)
        vw = page_data.get("vw", 375)
        result["title_cn"] = page_data.get("title", "")

        gallery_imgs = []
        desc_imgs = []
        gallery_paths = []
        desc_paths = []

        for c in containers:
            p = c.get("p", "")
            r = c.get("r", [0, 0, 0, 0])
            w, h, top = r[0], r[1], r[2]
            im = c.get("im", 0)
            cls = (c.get("c") or "").lower()
            tx = (c.get("tx") or "")

            if w < 30 or h < 30 or im == 0:
                continue

            is_detail = any(k in cls for k in ["detail", "desc", "content", "description"])
            is_detail = is_detail or any(k in tx for k in ["图文详情", "产品详情", "商品详情"])

            if is_detail or (top > vh * 0.5 and w > vw * 0.8):
                desc_paths.append(p)
            elif top < vh * 0.65 and w < vw * 0.85:
                gallery_paths.append(p)

        for c in containers:
            for sample in c.get("is", []):
                src = sample.get("src_hint", "")
                if not src or not src.startswith("http"):
                    continue
                src = _normalize_img_url(src)
                if not _is_product_image(src):
                    continue
                if c.get("p") in gallery_paths and src not in gallery_imgs:
                    gallery_imgs.append(src)
                if c.get("p") in desc_paths and src not in desc_imgs:
                    desc_imgs.append(src)

        result["image_urls"] = gallery_imgs[:8]
        result["desc_images"] = desc_imgs[:20]

        try:
            body_text = await page.evaluate("() => (document.body?.innerText || '').slice(0, 2000)")
            result["description_cn"] = body_text
        except Exception:
            result["description_cn"] = ""

        result["sku_prices"] = []
        try:
            sku_info = await page.evaluate(JS_DETECT_SKU_TYPE)
            if isinstance(sku_info, str):
                sku_info = json.loads(sku_info)
            for group in sku_info.get("groups", []):
                for opt in group.get("options", []):
                    sku = {"name": opt, "price": "", "images": []}
                    try:
                        clicked = await page.evaluate(JS_CLICK_SKU_BY_LABEL, opt)
                        if clicked:
                            before = set(await page.evaluate(JS_SNAPSHOT_VISIBLE_IMAGES))
                            await asyncio.sleep(1.2)
                            after = set(await page.evaluate(JS_SNAPSHOT_VISIBLE_IMAGES))
                            diff = list(after - before)
                            if diff:
                                sku["images"] = diff[:3]
                    except Exception:
                        pass
                    result["sku_prices"].append(sku)
            if result["sku_prices"]:
                try:
                    await page.evaluate(JS_CLICK_SKU_BY_LABEL, result["sku_prices"][0]["name"])
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
        except Exception:
            pass

        return result

    def _normalize(self, raw: dict) -> dict[str, Any]:
        seen: set[str] = set()
        result: dict[str, Any] = {}

        def dedupe(urls: list) -> list:
            out = []
            for u in urls:
                u = _normalize_img_url(str(u))
                if u not in seen and _is_product_image(u):
                    seen.add(u)
                    out.append(u)
            return out

        result["title_cn"] = raw.get("title_cn", "")
        result["description_cn"] = raw.get("description_cn", "")
        result["source_url"] = raw.get("source_url", "")

        raw_price = raw.get("price_cn", "")
        if raw_price and not re.match(r'[¥￥]\s*\d+', str(raw_price)):
            raw_price = ""
        result["price_cn"] = raw_price

        result["image_urls"] = dedupe(raw.get("image_urls", []))
        result["desc_images"] = dedupe(raw.get("desc_images", []))
        result["sku_prices"] = raw.get("sku_prices", [])

        return result

    def _print_summary(self, result: dict) -> None:
        print("\n" + "=" * 60)
        print(f"EXTRACTION COMPLETE: {result.get('title_cn', '')[:60]}")
        print(f"  Main images:    {len(result.get('image_urls', []))}")
        print(f"  SKU variants:   {len(result.get('sku_prices', []))}")
        print(f"  Desc images:    {len(result.get('desc_images', []))}")
        print(f"  Price:          {result.get('price_cn', '')}")
        print("=" * 60)

    async def close(self) -> None:
        if self._browser:
            await self._browser.stop()
            self._browser = None

    async def extract_batch(self, urls: list[str]) -> list[dict[str, Any]]:
        browser = await self._get_browser()
        results: list[dict[str, Any]] = []

        for idx, url in enumerate(urls):
            print(f"\n{'─' * 40}\n  Batch [{idx + 1}/{len(urls)}]: {url[:80]}\n{'─' * 40}")
            try:
                agent = Agent(
                    task=(
                        f"Navigate to {url}. "
                        "If a login/QR page appears, wait for the user to scan the QR code. "
                        "Once the product detail page is fully loaded, call done(). "
                        "Do NOT create new tabs. Do NOT navigate away from the product page."
                    ),
                    llm=ChatDeepSeek(
                        api_key=config.ai["api_key"],
                        base_url=config.ai.get("base_url") or None,
                        temperature=0.0,
                    ),
                    browser=browser,
                    use_vision=False,
                )
                await agent.run(max_steps=8)

                page = browser.get_current_page()
                if asyncio.iscoroutine(page):
                    page = await page
                if page is None:
                    results.append({"_error": "no page", "source_url": url})
                    continue

                body_len = 0
                for attempt in range(15):
                    try:
                        body_len = await page.evaluate("() => (document.body?.innerText || '').length")
                        if isinstance(body_len, str):
                            body_len = int(body_len) if body_len.isdigit() else 0
                    except Exception:
                        body_len = 0
                    if body_len > 500:
                        break
                    await asyncio.sleep(2)

                if body_len < 200:
                    logger.warning(f"Page empty ({body_len} chars) for {url}")

                llm = create_llm_service(temperature=0.1)
                extractor = SlimDOMExtractor(llm)
                ai_result = await extractor.extract(page, url)

                if ai_result.get("_error"):
                    print(f"  [AI] Extraction failed ({ai_result['_error']}), using fallback...")
                    ai_result = await self._fallback_extraction(page, url)

                result = self._normalize(ai_result)
                self._print_summary(result)
                results.append(result)

            except Exception as exc:
                logger.error(f"Batch extract failed for {url}: {exc}")
                results.append({"_error": str(exc), "source_url": url})

        try:
            storage_state = await browser.export_storage_state()
            if storage_state and storage_state.get("cookies"):
                COOKIES_FILE.write_text(
                    json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            pass

        return results
