"""AI-driven product extraction — AI reads page structure, outputs product data directly.

Architecture:
  1. JS_COLLECT_PAGE_DATA: collects text, images (position+context), SKU elements,
     and section markers from the live page — pure structure, no CSS selectors.
  2. AI receives this data and outputs structured product JSON directly.
  3. Layout-based JS fallbacks for image collection only (no CSS selectors).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from src.llm.service import LLMService

# ═══════════════════════════════════════════════════════════════
# 1. Page data collector — gives AI everything it needs
# ═══════════════════════════════════════════════════════════════

JS_COLLECT_PAGE_DATA = r"""() => {
    const SKIP_TAGS = new Set(['script','style','noscript','iframe','svg','path','head','meta','link','br','hr','wbr']);

    function isVisible(el) {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden';
    }

    // ── All visible text ──
    const walker = document.createTreeWalker(document.body, 4);
    let node, allText = '';
    while (node = walker.nextNode()) {
        const p = node.parentElement;
        if (!p || SKIP_TAGS.has(p.tagName.toLowerCase())) continue;
        if (!isVisible(p)) continue;
        const t = node.textContent.trim();
        if (t) allText += t + '\n';
    }

    // ── Images with position + context ──
    const images = [];
    const seenUrls = new Set();
    for (const img of document.querySelectorAll('img')) {
        if (!isVisible(img)) continue;
        const r = img.getBoundingClientRect();
        if (r.width < 50 || r.height < 50) continue;

        let src = '';
        for (const attr of ['data-src', 'src', 'data-original', 'data-ks-lazyload']) {
            src = img.getAttribute(attr);
            if (src) break;
        }
        if (!src || src.startsWith('data:')) continue;
        if (src.startsWith('//')) src = 'https:' + src;
        if (!src.startsWith('http')) continue;
        if (seenUrls.has(src)) continue;
        seenUrls.add(src);

        // Context: parent/ancestor text (helps AI distinguish product vs decoration images)
        let ctx = '';
        let el = img.parentElement;
        for (let i = 0; i < 3 && el; i++) {
            const txt = (el.textContent || '').trim().slice(0, 80);
            if (txt && !ctx.includes(txt)) ctx += (ctx ? ' | ' : '') + txt;
            el = el.parentElement;
        }

        images.push({
            u: src,
            t: Math.round(r.top),
            l: Math.round(r.left),
            w: Math.round(r.width),
            h: Math.round(r.height),
            nw: img.naturalWidth || 0,
            nh: img.naturalHeight || 0,
            x: ctx.slice(0, 180)
        });
    }

    // ── SKU-like interactive elements ──
    const skuElements = [];
    const seenLabels = new Set();
    const SKU_SEL = [
        '[data-value]', '[data-sku-id]', '[data-sku]',
        '[class*="sku"] a', '[class*="sku"] span',
        '[class*="valueItem"]', '[class*="value-item"]',
        'a[title]', 'span[title]',
        '[class*="prop"]'
    ].join(',');
    for (const el of document.querySelectorAll(SKU_SEL)) {
        if (!isVisible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) continue;
        const label = (el.getAttribute('title') || el.getAttribute('data-value') ||
                       el.textContent || '').trim();
        if (!label || label.length > 50 || seenLabels.has(label)) continue;
        seenLabels.add(label);
        // Check if element has a color swatch image child
        const swatch = el.querySelector('img');
        skuElements.push({
            l: label,
            t: Math.round(r.top),
            sw: !!swatch
        });
    }

    // ── Section markers ──
    const MARKERS = ['图文详情','产品详情','商品详情','本店推荐','猜你喜欢','看了又看','店铺推荐','规格参数','累计评价'];
    const markers = [];
    const tw = document.createTreeWalker(document.body, 4);
    let tn;
    while (tn = tw.nextNode()) {
        const t = (tn.textContent || '').trim();
        if (t.length > 20) continue;
        for (const phrase of MARKERS) {
            if (t === phrase || (t.startsWith(phrase) && t.length <= phrase.length + 2)) {
                const range = document.createRange();
                range.selectNodeContents(tn);
                const rects = range.getClientRects();
                for (const rc of rects) {
                    if (rc.height > 0 && rc.width > 0) {
                        markers.push({p: phrase, y: Math.round(rc.top)});
                        break;
                    }
                }
            }
        }
    }

    return JSON.stringify({
        url: window.location.href,
        title: (document.title || '').slice(0, 200),
        text: allText.slice(0, 12000),
        images: images.slice(0, 40),
        skus: skuElements.slice(0, 30),
        markers: markers.slice(0, 15)
    });
}"""

# ═══════════════════════════════════════════════════════════════
# 2. Layout-based image collectors (no CSS selectors, used as fallback)
# ═══════════════════════════════════════════════════════════════

JS_LAYOUT_GALLERY = r"""() => {
    const urls = [], seen = new Set();
    const collect = (el) => {
        for (const img of (el.querySelectorAll ? el.querySelectorAll('img') : [])) {
            for (const attr of ['data-src', 'src', 'data-original']) {
                let s = img.getAttribute(attr); if (!s) continue;
                if (s.startsWith('//')) s = 'https:' + s;
                if (s.startsWith('http') && !seen.has(s)) { seen.add(s); urls.push(s); break; }
            }
        }
    };

    // Strategy 1: thumbnailsWrap prefix (stable)
    const wraps = document.querySelectorAll('[class*="thumbnailsWrap"]');
    for (const w of wraps) { collect(w); }
    if (urls.length >= 2) return urls;

    // Strategy 2: Largest image-rich container in top 50% of page
    let best = null, bestScore = 0;
    const vh = window.innerHeight;
    for (const el of document.querySelectorAll('div,ul,section,figure')) {
        const r = el.getBoundingClientRect();
        if (r.top > vh * 0.5 || r.bottom < 80) continue;
        const imgs = el.querySelectorAll('img');
        if (imgs.length < 2) continue;
        const area = r.width * r.height;
        if (area < 20000) continue;
        const score = area * (1 + imgs.length * 0.5);
        if (score > bestScore) { bestScore = score; best = el; }
    }
    if (best) { collect(best); }
    if (urls.length >= 2) return urls;

    // Strategy 3: elementFromPoint at hero image positions
    const w = window.innerWidth;
    for (const [x, y] of [[w * 0.35, 280], [w * 0.5, 320], [w * 0.35, 360]]) {
        let el = document.elementFromPoint(x, y);
        for (let i = 0; i < 6 && el; i++) {
            if (el.tagName === 'IMG') {
                for (const attr of ['data-src', 'src', 'data-original']) {
                    let s = el.getAttribute(attr); if (!s) continue;
                    if (s.startsWith('//')) s = 'https:' + s;
                    if (s.startsWith('http') && !seen.has(s)) { seen.add(s); urls.push(s); break; }
                }
                break;
            }
            const imgs = el.querySelectorAll ? el.querySelectorAll('img') : [];
            if (imgs.length > 0) {
                for (const img of imgs) {
                    for (const attr of ['data-src', 'src', 'data-original']) {
                        let s = img.getAttribute(attr); if (!s) continue;
                        if (s.startsWith('//')) s = 'https:' + s;
                        if (s.startsWith('http') && !seen.has(s)) { seen.add(s); urls.push(s); break; }
                    }
                }
                if (urls.length > 0) break;
            }
            el = el.parentElement;
        }
        if (urls.length > 0) break;
    }
    return urls;
}"""

JS_LAYOUT_SKU_CLUSTER = r"""() => {
    const selectors = [
        '[data-value]', '[data-sku-id]', '[data-sku]',
        '[class*="sku"]', '[class*="valueItem"]', '[class*="value-item"]',
        '[class*="skuWrapper"] a', '[class*="skuWrapper"] span',
        'a[class*="prop"]', 'span[class*="prop"]',
    ].join(',');
    const seen = new Set(), items = [];
    for (const el of document.querySelectorAll(selectors)) {
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) continue;
        if (r.top > window.innerHeight * 0.8) continue;
        let label = el.getAttribute('title') || el.getAttribute('data-value') || el.textContent || '';
        label = label.trim();
        if (!label || label.length > 50 || seen.has(label)) continue;
        seen.add(label);
        items.push({label});
    }
    // Fallback: cluster nearby clickable small elements
    if (items.length === 0) {
        const candidates = [];
        for (const el of document.querySelectorAll('a,span,li,div')) {
            const r = el.getBoundingClientRect();
            if (r.width < 16 || r.width > 200 || r.height < 16 || r.height > 80) continue;
            if (r.top < 250 || r.top > window.innerHeight * 0.7) continue;
            const text = (el.textContent || '').trim();
            if (!text || text.length > 30) continue;
            candidates.push({label: text});
        }
        // Cluster by proximity
        const clustered = [];
        for (const c of candidates) {
            let found = false;
            for (const group of clustered) {
                const last = group[group.length - 1];
                if (Math.abs(c.r ? c.r.left - last.r.left : 0) < 60 &&
                    Math.abs(c.r ? c.r.top - last.r.top : 0) < 60) {
                    group.push(c); found = true; break;
                }
            }
            if (!found) clustered.push([c]);
        }
        const best = (clustered.sort((a, b) => b.length - a.length)[0] || []);
        for (const c of best) {
            if (!seen.has(c.label)) { seen.add(c.label); items.push({label: c.label}); }
        }
    }
    return items;
}"""

JS_EXTRACT_BETWEEN_MARKERS = r"""(start_texts, end_texts) => {
    function findY(texts) {
        const w = document.createTreeWalker(document.body, 4);
        let n, bestY = null, bestDist = Infinity;
        while (n = w.nextNode()) {
            const txt = (n.textContent || '').trim();
            if (txt.length > 20) continue;
            for (const t of texts) {
                if (txt === t || (txt.startsWith(t) && txt.length <= t.length + 2)) {
                    const range = document.createRange();
                    range.selectNodeContents(n);
                    const rects = range.getClientRects();
                    for (const r of rects) {
                        if (r.height > 0 && r.width > 0 && r.top < bestDist) {
                            bestDist = r.top; bestY = r.top;
                        }
                    }
                }
            }
        }
        return bestY;
    }
    const startY = findY(start_texts);
    if (startY === null) return [];
    const endY = findY(end_texts) || 999999;

    const urls = [], seen = new Set();
    for (const img of document.querySelectorAll('img')) {
        const r = img.getBoundingClientRect();
        if (r.top < startY || r.top > endY) continue;
        if (r.width < 60 || r.height < 60) continue;
        for (const attr of ['data-src', 'data-ks-lazyload', 'src', 'data-original']) {
            let s = img.getAttribute(attr); if (!s) continue;
            if (s.startsWith('//')) s = 'https:' + s;
            if (s.startsWith('http') && !seen.has(s)) { seen.add(s); urls.push(s); break; }
        }
    }
    for (const iframe of document.querySelectorAll('iframe')) {
        try {
            const d = iframe.contentDocument || iframe.contentWindow.document;
            if (d) for (const img of d.querySelectorAll('img')) {
                const r = img.getBoundingClientRect();
                if (r.top < startY || r.top > endY) continue;
                for (const attr of ['data-src', 'src', 'data-original']) {
                    let s = img.getAttribute(attr); if (!s) continue;
                    if (s.startsWith('//')) s = 'https:' + s;
                    if (s.startsWith('http') && !seen.has(s)) { seen.add(s); urls.push(s); break; }
                }
            }
        } catch (ex) {}
    }
    return urls;
}"""

# ═══════════════════════════════════════════════════════════════
# 2b. SKU click + post-click state collectors
# ═══════════════════════════════════════════════════════════════

JS_CLICK_SKU_BY_LABEL = r"""(label) => {
    const selectors = [
        '[data-value]', '[data-sku-id]', '[data-sku]',
        '[class*="skuItem"]', '[class*="valueItem"]',
        '[class*="skuWrapper"] a', '[class*="skuWrapper"] span',
        'a[title]', 'span[title]',
    ];
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            const txt = (el.getAttribute('title') || el.getAttribute('data-value') ||
                          el.textContent || '').trim();
            if (txt === label) { el.click(); return true; }
        }
    }
    // Fuzzy match: contains the label text
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            const txt = (el.getAttribute('title') || el.getAttribute('data-value') ||
                          el.textContent || '').trim();
            if (txt && label && (txt.includes(label) || label.includes(txt))) {
                el.click(); return true;
            }
        }
    }
    return false;
}"""

JS_COLLECT_POST_CLICK = r"""() => {
    // ── Collect price after SKU click ──
    let price = '';
    const priceSels = [
        // Tmall/Taobao specific
        '[class*="tm-promo-price"]', '[class*="tmPrice"]', '[class*="tbPrice"]',
        '.tm-price', '.tb-price', '.tm-promo-price',
        // Generic price patterns
        '[class*="Price"] [class*="price"]', '[class*="price"] [class*="value"]',
        '[class*="priceValue"]', '[class*="price-value"]',
        'span[class*="price"]', 'em[class*="price"]', 'b[class*="price"]', 'strong[class*="price"]',
        '[class*="totalPrice"]', '[class*="salePrice"]', '[class*="promoPrice"]',
        '[class*="currentPrice"]', '[class*="nowPrice"]',
        'div[class*="PriceBox"] span',
        // Data attribute based
        '[data-spm="price"]', '[data-price]',
    ];
    for (const sel of priceSels) {
        const el = document.querySelector(sel);
        if (el) {
            const t = (el.textContent || '').trim();
            // Match price patterns: ¥29.90, 29.90, 29, 券后29
            const m = t.match(/[¥￥]?\s*(\d+\.?\d*)/);
            if (m && m[1]) { price = '¥' + m[1]; break; }
        }
    }
    // Fallback: scan all visible text for price patterns near the top
    if (!price) {
        const bodyText = document.body.innerText || '';
        const lines = bodyText.split('\n').slice(0, 40);
        for (const line of lines) {
            const m = line.match(/[¥￥]\s*(\d+\.?\d*)/);
            if (m) { price = '¥' + m[1]; break; }
        }
    }

    // ── Collect main image after SKU click ──
    let image = '';

    // Strategy 1: thumbnailsWrap container (the gallery wrapper, stable prefix)
    const wraps = document.querySelectorAll('[class*="thumbnailsWrap"]');
    for (const w of wraps) {
        // Find the visible/large img inside
        const imgs = w.querySelectorAll('img');
        for (const img of imgs) {
            const r = img.getBoundingClientRect();
            if (r.width < 150 || r.height < 150) continue;
            for (const attr of ['data-src', 'src', 'data-original']) {
                let s = img.getAttribute(attr);
                if (!s || s.startsWith('data:')) continue;
                if (s.startsWith('//')) s = 'https:' + s;
                if (s.startsWith('http')) { image = s; break; }
            }
            if (image) break;
        }
        if (image) break;
    }

    // Strategy 2: elementFromPoint at the main image position (center-left, 1/3 down)
    if (!image) {
        const w = window.innerWidth;
        for (const [x, y] of [[w * 0.35, 350], [w * 0.4, 380], [w * 0.3, 300], [w * 0.45, 400]]) {
            let el = document.elementFromPoint(x, y);
            for (let i = 0; i < 8 && el; i++) {
                if (el.tagName === 'IMG') {
                    for (const attr of ['data-src', 'src', 'data-original']) {
                        let s = el.getAttribute(attr);
                        if (!s || s.startsWith('data:')) continue;
                        if (s.startsWith('//')) s = 'https:' + s;
                        if (s.startsWith('http')) { image = s; break; }
                    }
                    break;
                }
                const imgs = el.querySelectorAll ? el.querySelectorAll('img') : [];
                for (const img of imgs) {
                    const r = img.getBoundingClientRect();
                    if (r.width < 120 || r.height < 120) continue;
                    for (const attr of ['data-src', 'src', 'data-original']) {
                        let s = img.getAttribute(attr);
                        if (!s || s.startsWith('data:')) continue;
                        if (s.startsWith('//')) s = 'https:' + s;
                        if (s.startsWith('http')) { image = s; break; }
                    }
                    if (image) break;
                }
                if (image) break;
                el = el.parentElement;
            }
            if (image) break;
        }
    }

    // Strategy 3: largest image in upper half (layout-based)
    if (!image) {
        let best = null, bestSize = 0;
        for (const img of document.querySelectorAll('img')) {
            const r = img.getBoundingClientRect();
            if (r.top > 800 || r.width < 150 || r.height < 150) continue;
            const size = r.width * r.height;
            if (size > bestSize) { bestSize = size; best = img; }
        }
        if (best) {
            for (const attr of ['data-src', 'src', 'data-original']) {
                let s = best.getAttribute(attr);
                if (!s || s.startsWith('data:')) continue;
                if (s.startsWith('//')) s = 'https:' + s;
                if (s.startsWith('http')) { image = s; break; }
            }
        }
    }

    return {price, image};
}"""

# ═══════════════════════════════════════════════════════════════
# 3. AI prompts — AI reads raw page data, outputs structured product JSON
# ═══════════════════════════════════════════════════════════════

DIRECT_EXTRACT_SYSTEM = """You are a product data extraction expert. You receive raw data from a Chinese e-commerce product page (text + images with position/size/context + SKU-like elements + section markers) and output structured product data.

Key principles:
- IMAGE SELECTION: product images are LARGE (w>200 or h>200), in the UPPER HALF of the page (top < viewport_height*0.5), and their context text contains product-related words. Skip icons, logos, banner ads.
- DESCRIPTION IMAGES: images BELOW the main product area — located between "图文详情"/"产品详情" marker and "本店推荐"/"猜你喜欢" marker, OR images with top position > viewport midpoint. These are SEPARATE from main product images.
- SKU DETECTION: SKU variants are interactive elements with labels like "黑色", "XL", "套餐一". They usually appear below the price area and have data-* attributes.
  IMPORTANT: Output the SKU LABELS you see (with empty price ""). The actual prices will be collected later by clicking each SKU button — you only need to identify WHICH buttons to click.
- PRICE: look for a price value in the page text. Must contain ¥ or ￥ symbol. If the price text is unclear, ambiguous, or contains multiple different numbers — leave price_cn as "". NEVER guess or fabricate a price.

Output ONLY valid JSON — no markdown fences, no extra text."""

DIRECT_EXTRACT_PROMPT = """Extract structured product data from this Chinese e-commerce page.

URL: {url}
Page title: {title}

── Page Text (top → bottom) ──
{text}

── Images (field meanings: u=url, t=top, l=left, w=width, h=height, nw=naturalWidth, nh=naturalHeight, x=context text) ──
{images}

── SKU-like Elements (l=label, t=top position, sw=has color swatch image) ──
{skus}

── Section Markers (p=phrase, y=top position) ──
{markers}

Return this JSON structure:
{{
  "title_cn": "product title in Chinese",
  "price_cn": "price from page text (e.g. ¥29.90). Must have ¥/￥ prefix. Leave '' if unclear.",
  "image_urls": ["main product gallery images — only images in the top half (t < page_midpoint), w>150. These are the product showcase photos. Max 8."],
  "sku_prices": [
    {{"name": "variant label (price stays empty — filled by clicking)", "price": ""}}
  ],
  "desc_images": ["detail/description images — images BELOW the main gallery area: between 图文详情/产品详情 and 本店推荐/猜你喜欢 markers, or t > page_midpoint. These show product details, specs, size charts. Do NOT mix with image_urls."],
  "description_cn": "product description paragraph (not navigation, not footer, not store recommendations)"
}}

Rules:
- price_cn: ONLY extract if there is a clear single price with ¥/￥ symbol in the page text. If the text shows multiple prices, promotional prices (券后), or is ambiguous — leave price_cn as "". The script will get the real price by reading the DOM.
- image_urls: ONLY images in the product gallery area (upper half of page, large product photos). Exclude icons, logos, banners, QR codes. Do NOT include detail/description images here.
- desc_images: ONLY images from the detail section (BLOW the main gallery, between 图文详情 and 本店推荐 markers). These are separate from image_urls.
- sku_prices: List the SKU variant LABELS from the SKU elements. Leave price as "" — the script clicks each SKU to get the real price.
- If you cannot find a field, use empty string or empty array.

JSON:"""

# ═══════════════════════════════════════════════════════════════
# 4. Orchestrator
# ═══════════════════════════════════════════════════════════════


class SlimDOMExtractor:
    """AI-driven extraction: AI reads page → identifies SKU buttons → JS clicks → AI extracts.

    Pipeline:
      1. Collect page data (text, images, SKU hints, markers) via JS
      2. AI extracts static data + identifies which SKU buttons to click
      3. JS clicks each SKU, collects post-click price + image
      4. Returns structured product data with verified SKU prices/images
    """

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    async def extract(self, page, url: str) -> dict[str, Any]:
        """Collect page data → AI extracts → click SKUs → return result."""
        print("  [AI] Collecting page structure (text + images + SKU hints)...")
        try:
            raw = await page.evaluate(JS_COLLECT_PAGE_DATA)
            page_data: dict = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.warning(f"Page data collection failed: {exc}")
            return {"_error": f"page_data_failed: {exc}"}

        ni = len(page_data.get("images", []))
        ns = len(page_data.get("skus", []))
        nm = len(page_data.get("markers", []))
        nc = len(page_data.get("text", ""))
        print(f"  [AI] Collected: {nc} chars text, {ni} images, {ns} SKU hints, {nm} markers")

        print("  [AI] Analyzing page content, extracting product data...")
        result = await self._extract_via_llm(page_data, url)

        if result.get("_parse_error"):
            logger.warning(f"AI extraction failed: {result.get('_raw', '')[:200]}")
            return {"_error": "ai_extraction_failed", "raw_text": page_data.get("text", "")[:500]}

        ti = result.get("title_cn", "")
        im = len(result.get("image_urls", []))
        sk = len(result.get("sku_prices", []))
        di = len(result.get("desc_images", []))
        print(f"  [AI] Extracted: title=\"{ti[:40]}\", images={im}, skus={sk}, desc_imgs={di}")

        # ── Phase: Click SKU buttons to get real prices + images ──
        result = await self._execute_sku_clicks(page, result)

        return result

    async def _extract_via_llm(self, page_data: dict, url: str) -> dict[str, Any]:
        """Send collected page data to LLM, get structured product JSON."""
        images_json = json.dumps(page_data.get("images", [])[:35], ensure_ascii=False)
        skus_json = json.dumps(page_data.get("skus", [])[:25], ensure_ascii=False)
        markers_json = json.dumps(page_data.get("markers", [])[:12], ensure_ascii=False)

        prompt = DIRECT_EXTRACT_PROMPT.format(
            url=url,
            title=page_data.get("title", ""),
            text=page_data.get("text", "")[:10000],
            images=images_json,
            skus=skus_json,
            markers=markers_json,
        )
        messages = [
            {"role": "system", "content": DIRECT_EXTRACT_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        return await self.llm.chat_json(messages, max_tokens=2500)

    # ── SKU click phase ────────────────────────────────────────

    async def _execute_sku_clicks(self, page, result: dict) -> dict:
        """Click each SKU button identified by AI, collect post-click price + image."""
        skus: list = result.get("sku_prices", [])
        if not skus:
            print("  [SKU] No SKU variants to click")
            return result

        # Filter out entries that already have a price
        needs_click = [s for s in skus if not s.get("price")]
        if not needs_click:
            print(f"  [SKU] All {len(skus)} variants have prices, skipping clicks")
            return result

        print(f"  [SKU] Clicking {len(needs_click)} SKU buttons to get real prices/images...")
        for sku in needs_click:
            label = sku.get("name", "")
            if not label:
                continue
            try:
                clicked = await page.evaluate(JS_CLICK_SKU_BY_LABEL, label)
                if not clicked:
                    logger.debug(f"SKU click failed for '{label}' — button not found")
                    continue
                await asyncio.sleep(1.2)

                post = await page.evaluate(JS_COLLECT_POST_CLICK)
                if isinstance(post, str):
                    post = json.loads(post)
                if isinstance(post, dict):
                    if post.get("price"):
                        sku["price"] = post["price"].strip()
                        print(f"    [{label}] price={sku['price']}")
                    if post.get("image"):
                        sku["images"] = [post["image"]]
                        print(f"    [{label}] image captured")
            except Exception as exc:
                logger.debug(f"SKU click error '{label}': {exc}")

        # Reset to first SKU to restore default page state
        first = skus[0] if skus else None
        if first:
            try:
                await page.evaluate(JS_CLICK_SKU_BY_LABEL, first.get("name", ""))
                await asyncio.sleep(0.5)
            except Exception:
                pass

        # Fallback: if any SKU still has no price, use product-level price_cn
        default_price = result.get("price_cn", "")
        for sku in skus:
            if not sku.get("price") and default_price:
                sku["price"] = default_price

        return result

    async def collect_layout_images(self, page) -> dict[str, Any]:
        """Post-extraction: collect images using layout-based JS (no CSS selectors).
        Called after AI extraction to ensure all lazy-loaded images are captured.
        """
        result: dict[str, Any] = {}
        try:
            imgs = await page.evaluate(JS_LAYOUT_GALLERY)
            if isinstance(imgs, str):
                imgs = json.loads(imgs)
            if isinstance(imgs, list) and imgs:
                result["layout_main_images"] = imgs
        except Exception:
            pass

        try:
            sku = await page.evaluate(JS_LAYOUT_SKU_CLUSTER)
            if isinstance(sku, str):
                sku = json.loads(sku)
            if isinstance(sku, list) and sku:
                result["layout_sku_items"] = sku
        except Exception:
            pass

        try:
            desc = await page.evaluate(
                JS_EXTRACT_BETWEEN_MARKERS,
                ["图文详情", "产品详情", "商品详情"],
                ["本店推荐", "猜你喜欢", "看了又看", "店铺推荐"],
            )
            if isinstance(desc, str):
                desc = json.loads(desc)
            if isinstance(desc, list) and desc:
                result["layout_desc_images"] = desc
        except Exception:
            pass

        return result
