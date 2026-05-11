"""Load product pages using system Edge/Chrome via Playwright, with httpx fallback."""

from __future__ import annotations

import json

from pathlib import Path
from urllib.parse import urljoin

from loguru import logger

from src.utils import SKIP_IMAGE_PATTERNS

COOKIES_FILE = Path("data/cookies.json")


class PageContent:
    def __init__(self, url: str, title: str = "", text: str = "", image_urls: list[str] | None = None) -> None:
        self.url = url
        self.title = title
        self.text = text
        self.image_urls = image_urls or []


class URLProductLoader:

    _PROFILE_DIR = Path("data/browser_profile")

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright = None
        self._browser = None

    async def _get_browser(self):
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._PROFILE_DIR),
                channel="chrome",
                headless=self.headless,
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            # Load saved cookies after context is created
            await self._load_cookies(self._browser)
        return self._browser

    async def _load_cookies(self, context) -> None:
        """Load saved cookies from cookies.json into the browser context."""
        if not COOKIES_FILE.exists():
            return
        try:
            data = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            cookies = data.get("cookies", data) if isinstance(data, dict) else data
            if isinstance(cookies, list) and cookies:
                await context.add_cookies(cookies)
                print(f"  Loaded {len(cookies)} saved cookies")
        except Exception as exc:
            logger.debug(f"Failed to load cookies: {exc}")

    async def _save_cookies(self, context) -> None:
        """Save all browser cookies (including HttpOnly) to cookies.json."""
        try:
            cookies = await context.cookies()
            if cookies:
                storage_state = {"cookies": cookies, "origins": []}
                COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
                COOKIES_FILE.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  Saved {len(cookies)} cookies for next run")
        except Exception as exc:
            logger.debug(f"Failed to save cookies: {exc}")

    async def load(self, url: str) -> PageContent:
        """Load URL via Edge browser (stealth), fallback to httpx."""
        try:
            return await self._load_with_browser(url)
        except Exception as exc:
            logger.debug(f"Browser load failed ({exc}), trying httpx...")
        try:
            return await self._load_with_httpx(url)
        except Exception as exc2:
            logger.error(f"Cannot load {url}: {exc2}")
            return PageContent(url=url, title="", text=str(exc2))

    async def _load_with_browser(self, url: str) -> PageContent:
        context = await self._get_browser()
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Auto-wait for login: if page title contains login keywords, poll until user logs in
            for attempt in range(30):
                title = await page.title()
                if title and not any(kw in title for kw in ["登录", "login", "Login"]):
                    break
                if attempt == 0:
                    logger.info("Login page detected — waiting for you to scan QR code...")
                await page.wait_for_timeout(2000)

            # Reload to get full content after login
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Save cookies after successful page load (includes HttpOnly cookies)
            await self._save_cookies(context)

            title = await page.title() or ""
            text = await page.evaluate("""() => {
                const body = document.body;
                if (!body) return '';
                const clone = body.cloneNode(true);
                clone.querySelectorAll('script, style, noscript, iframe, svg, nav, footer').forEach(el => el.remove());
                return clone.innerText || '';
            }""")
            image_urls = await page.evaluate("""() => {
                const urls = [];
                const seen = new Set();
                for (const img of document.querySelectorAll('img')) {
                    const src = img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
                    if (src && !seen.has(src)) { urls.push(src); seen.add(src); }
                }
                return urls;
            }""")
            image_urls = self._normalize_urls(image_urls, url)
            image_urls = [u for u in image_urls if not any(p in u.lower() for p in SKIP_IMAGE_PATTERNS)]

            logger.info(f"Loaded: {len(text)} chars, {len(image_urls)} images from {title[:50]}")
            return PageContent(url=url, title=title, text=text, image_urls=image_urls)
        finally:
            await page.close()

    async def _load_with_httpx(self, url: str) -> PageContent:
        import httpx
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True, verify=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "lxml")
        title = soup.title.string.strip() if soup.title else ""

        for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        image_urls = []
        seen = set()
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
            if src and src not in seen:
                seen.add(src)
                image_urls.append(src)

        image_urls = self._normalize_urls(image_urls, url)
        image_urls = [u for u in image_urls if not any(p in u.lower() for p in SKIP_IMAGE_PATTERNS)]

        logger.info(f"Loaded (httpx): {len(text)} chars, {len(image_urls)} images")
        return PageContent(url=url, title=title, text=text, image_urls=image_urls)

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    def _normalize_urls(self, urls: list[str], base_url: str) -> list[str]:
        result = []
        for url in urls:
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = urljoin(base_url, url)
            elif not url.startswith("http"):
                continue
            result.append(url)
        return result
