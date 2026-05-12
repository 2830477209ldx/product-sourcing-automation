"""Product extraction — AI browser navigation + AI-driven data extraction.

Navigation:     browser_use Agent (handles QR login, redirects)
Extraction:     SlimDOMExtractor (AI reads page → structured data directly)
Fallback:       Layout-based JS image collectors (no CSS selectors)
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
    JS_COLLECT_POST_CLICK,
    JS_EXTRACT_BETWEEN_MARKERS,
    JS_LAYOUT_GALLERY,
    JS_LAYOUT_SKU_CLUSTER,
    SlimDOMExtractor,
)
from src.config import config
from src.llm.service import LLMService
from src.utils import SKIP_IMAGE_PATTERNS

PROFILE_DIR = Path("data/browser_profile")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_FILE = Path("data/cookies.json")


# ═══════════════════════════════════════════════════════════════
# Layout-based image collectors (no CSS selectors — used as fallback)
# ═══════════════════════════════════════════════════════════════

JS_LAYOUT_GALLERY_BROAD = JS_LAYOUT_GALLERY  # reuse from slimdom_extractor

JS_LAYOUT_SKU_CLUSTER_BROAD = JS_LAYOUT_SKU_CLUSTER  # reuse

JS_BETWEEN_MARKERS_BROAD = JS_EXTRACT_BETWEEN_MARKERS  # reuse

JS_CURRENT_PRICE = """() => {
    for (const sel of [
        '[class*="tm-promo-price"]', '[class*="tmPrice"]', '[class*="tbPrice"]',
        '.tm-price', '.tb-price',
        '[class*="Price"] [class*="price"]', '[class*="price"] [class*="value"]',
        'span[class*="price"]', 'em[class*="price"]', 'b[class*="price"]',
        '[class*="totalPrice"]', '[class*="salePrice"]', '[class*="promoPrice"]',
        '[class*="currentPrice"]', '[class*="nowPrice"]',
        'div[class*="PriceBox"] span',
    ]) {
        const el = document.querySelector(sel);
        if (el) {
            const t = el.textContent.trim();
            const m = t.match(/[¥￥]?\\s*(\\d+\\.?\\d*)/);
            if (m && m[1]) return '¥' + m[1];
        }
    }
    return '';
}"""

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
    """AI browser for navigation + AI-driven extraction (no hardcoded selectors)."""

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

        # ── Phase 1: AI Agent navigates + handles login ──
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

        # ── Verify page loaded ──
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

        # Guard: if still on login page, abort extraction
        if "login" in str(current_url).lower():
            print("\n  ⚠️  Still on login page — please scan the QR code with Taobao app.")
            print("  The browser profile is new. After this login, all future runs auto-login.\n")
            return {"_error": "login_required", "source_url": url}

        # ── Save cookies ──
        try:
            storage_state = await browser.export_storage_state()
            if storage_state and storage_state.get("cookies"):
                COOKIES_FILE.write_text(
                    json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"  Saved {len(storage_state['cookies'])} cookies for next run")
        except Exception as exc:
            logger.debug(f"Cookie save failed: {exc}")

        # ── Phase 2: AI reads page structure, outputs product data directly ──
        llm = LLMService(
            api_key=config.ai["api_key"],
            base_url=config.ai.get("base_url", ""),
            model_text=config.ai.get("model_text", "deepseek-chat"),
            temperature=0.1,
        )
        extractor = SlimDOMExtractor(llm)
        ai_result = await extractor.extract(page, url)

        if ai_result.get("_error"):
            print(f"  [AI] Extraction failed ({ai_result['_error']}), using layout fallback...")
            ai_result = await self._layout_fallback_extraction(page)

        # ── Phase 3: Collect layout images ──
        layout = await extractor.collect_layout_images(page)

        # ── Phase 4: Merge + filter results ──
        result = self._merge_results(ai_result, layout)
        self._print_summary(result)
        return result

    async def _layout_fallback_extraction(self, page) -> dict[str, Any]:
        """When AI fails, use layout-based JS (no CSS selectors) to collect raw data."""
        result: dict[str, Any] = {}
        try:
            result["title_cn"] = (await page.get_title() or "")
        except Exception:
            result["title_cn"] = ""
        try:
            result["price_cn"] = await page.evaluate(JS_CURRENT_PRICE)
            if isinstance(result["price_cn"], str):
                result["price_cn"] = result["price_cn"].strip()
        except Exception:
            result["price_cn"] = ""
        try:
            imgs = await page.evaluate(JS_LAYOUT_GALLERY)
            if isinstance(imgs, str):
                imgs = json.loads(imgs)
            result["image_urls"] = imgs if isinstance(imgs, list) else []
        except Exception:
            result["image_urls"] = []
        try:
            sku = await page.evaluate(JS_LAYOUT_SKU_CLUSTER)
            if isinstance(sku, str):
                sku = json.loads(sku)
            if isinstance(sku, list) and sku:
                sku_prices = []
                for s in sku:
                    label = s.get("label", "")
                    price = result.get("price_cn", "")
                    images: list[str] = []
                    # Click the SKU to get real price + image
                    try:
                        clicked = await page.evaluate(JS_CLICK_SKU_BY_LABEL, label)
                        if clicked:
                            await asyncio.sleep(1.2)
                            post = await page.evaluate(JS_COLLECT_POST_CLICK)
                            if isinstance(post, str):
                                post = json.loads(post)
                            if isinstance(post, dict):
                                if post.get("price"):
                                    price = post["price"].strip()
                                if post.get("image"):
                                    images = [post["image"]]
                    except Exception:
                        pass
                    sku_prices.append({"name": label, "price": price, "images": images})
                # Reset to first SKU
                if sku_prices:
                    try:
                        await page.evaluate(JS_CLICK_SKU_BY_LABEL, sku_prices[0]["name"])
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
                result["sku_prices"] = sku_prices
            else:
                result["sku_prices"] = []
        except Exception:
            result["sku_prices"] = []
        try:
            desc = await page.evaluate(
                JS_BETWEEN_MARKERS_BROAD,
                ["图文详情", "产品详情", "商品详情"],
                ["本店推荐", "猜你喜欢", "看了又看", "店铺推荐"],
            )
            if isinstance(desc, str):
                desc = json.loads(desc)
            result["desc_images"] = desc if isinstance(desc, list) else []
        except Exception:
            result["desc_images"] = []
        try:
            pt = await page.evaluate("() => document.body?.innerText || ''")
            result["description_cn"] = pt[:2000] if isinstance(pt, str) else ""
        except Exception:
            result["description_cn"] = ""
        return result

    def _merge_results(self, ai: dict, layout: dict) -> dict[str, Any]:
        """Merge AI extraction with layout-based image collection, deduplicate + filter."""
        import re

        seen: set[str] = set()

        def add_urls(target: list, sources: list) -> None:
            for src in sources:
                src = _normalize_img_url(str(src))
                if src not in seen and _is_product_image(src):
                    seen.add(src)
                    target.append(src)

        # Validate price_cn format: must be ¥/￥ followed by digits
        raw_price = ai.get("price_cn", "")
        if raw_price and not re.match(r'[¥￥]\s*\d+', str(raw_price)):
            raw_price = ""

        main_images: list[str] = []
        add_urls(main_images, ai.get("image_urls", []))
        add_urls(main_images, layout.get("layout_main_images", []))

        desc_images: list[str] = []
        add_urls(desc_images, ai.get("desc_images", []))
        add_urls(desc_images, layout.get("layout_desc_images", []))

        sku_prices = ai.get("sku_prices", [])
        if not sku_prices:
            layout_skus = layout.get("layout_sku_items", [])
            if layout_skus:
                sku_prices = [
                    {"name": s.get("label", ""), "price": raw_price}
                    for s in layout_skus
                ]
        if not sku_prices:
            if raw_price:
                sku_prices = [{"name": "Default", "price": raw_price, "images": []}]

        # Fallback: if product price is empty, take from first SKU
        if not raw_price and sku_prices:
            raw_price = sku_prices[0].get("price", "")

        all_images = main_images + [
            img for sku in sku_prices
            for img in sku.get("images", [])
            if img not in main_images
        ]

        return {
            "title_cn": ai.get("title_cn", ""),
            "price_cn": raw_price,
            "description_cn": ai.get("description_cn", ""),
            "image_urls": all_images,
            "desc_images": desc_images,
            "sku_prices": sku_prices,
            "source_url": ai.get("source_url", ""),
        }

    def _print_summary(self, result: dict) -> None:
        print("\n" + "=" * 60)
        print(f"EXTRACTION COMPLETE: {result.get('title_cn', '')[:60]}")
        print(f"  Main images:    {len(result.get('image_urls', []))}")
        print(f"  SKU variants:   {len(result.get('sku_prices', []))}")
        print(f"  Desc images:    {len(result.get('desc_images', []))}")
        print(f"  Price:          {result.get('price_cn', '')}")
        print(f"  SKU state:      {result.get('sku_state', '')}")
        print("=" * 60)

    async def close(self) -> None:
        if self._browser:
            await self._browser.stop()
            self._browser = None

    async def extract_batch(self, urls: list[str]) -> list[dict[str, Any]]:
        """Process multiple URLs in the same browser session (shared cookies, no re-login)."""
        browser = await self._get_browser()
        results: list[dict[str, Any]] = []

        for idx, url in enumerate(urls):
            print(f"\n{'─' * 40}\n  Batch [{idx + 1}/{len(urls)}]: {url[:80]}\n{'─' * 40}")
            try:
                # ── Navigate + handle login ──
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

                # Verify page loaded
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

                # ── AI extraction ──
                llm = LLMService(
                    api_key=config.ai["api_key"],
                    base_url=config.ai.get("base_url", ""),
                    model_text=config.ai.get("model_text", "deepseek-chat"),
                    temperature=0.1,
                )
                extractor = SlimDOMExtractor(llm)
                ai_result = await extractor.extract(page, url)

                if ai_result.get("_error"):
                    print(f"  [AI] Extraction failed ({ai_result['_error']}), using layout fallback...")
                    ai_result = await self._layout_fallback_extraction(page)

                # Layout image collection
                layout = await extractor.collect_layout_images(page)

                # Merge + filter
                result = self._merge_results(ai_result, layout)
                self._print_summary(result)
                results.append(result)

            except Exception as exc:
                logger.error(f"Batch extract failed for {url}: {exc}")
                results.append({"_error": str(exc), "source_url": url})

        # Save cookies after all URLs processed
        try:
            storage_state = await browser.export_storage_state()
            if storage_state and storage_state.get("cookies"):
                COOKIES_FILE.write_text(
                    json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            pass

        return results
