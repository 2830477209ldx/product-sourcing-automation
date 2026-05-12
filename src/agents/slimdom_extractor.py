"""AI-driven product extraction — container-level classification + targeted extraction.

Architecture:
  1. JS_COLLECT_CONTAINERS: walks DOM, collects container-level structural data
  2. AI container classification: identifies gallery, description, SKU area
  3. AI targeted extraction: extracts data only from classified containers
  4. JS-based SKU processing: detects simple vs compound, clicks, diffs images
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from src.llm.service import LLMService

# ═══════════════════════════════════════════════════════════════
# 1. Container-level DOM collector
# ═══════════════════════════════════════════════════════════════

JS_COLLECT_CONTAINERS = r"""() => {
    const SKIP_TAGS = new Set(['script','style','noscript','svg','path',
        'head','meta','link','br','hr','wbr','input','textarea','select','option']);

    function isSignificant(el) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        if (r.width < 20 || r.height < 20 || r.bottom < 0) return false;
        return true;
    }

    function isImgVisible(img) {
        const style = window.getComputedStyle(img);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        const r = img.getBoundingClientRect();
        return r.width >= 40 && r.height >= 40;
    }

    function collect(root, path, depth, maxDepth) {
        if (depth > maxDepth) return [];
        const results = [];
        let idx = 0;
        for (const child of root.children) {
            const tag = child.tagName.toUpperCase();
            if (SKIP_TAGS.has(tag.toLowerCase())) {
                if (tag === 'IFRAME') {
                    const rect = child.getBoundingClientRect();
                    const entry = {
                        p: path ? path + '.' + idx : String(idx),
                        t: 'IFRAME',
                        c: (typeof child.className === 'string') ? child.className.slice(0, 80) : null,
                        id: child.id || null,
                        r: [Math.round(rect.w), Math.round(rect.h), Math.round(rect.t), Math.round(rect.l)],
                        im: 0, is: [], tx: null, hi: false, ifr: child.src ? child.src.slice(0, 100) : null
                    };
                    let inner = 0;
                    try {
                        const doc = child.contentDocument || child.contentWindow.document;
                        if (doc && doc.body) {
                            const innerImgs = doc.querySelectorAll('img');
                            for (const img of innerImgs) {
                                if (img.getBoundingClientRect().width >= 40) inner++;
                            }
                            entry.im = inner;
                            const innerText = (doc.body.textContent || '').trim().slice(0, 120);
                            if (innerText) entry.tx = innerText;
                        }
                    } catch (_) {}
                    if (inner > 0 || entry.ifr || (rect.w > 100 && rect.h > 100)) {
                        results.push(entry);
                    }
                }
                idx++; continue;
            }
            if (!isSignificant(child)) { idx++; continue; }

            const cls = (typeof child.className === 'string') ? child.className.slice(0, 80) : '';
            const id = child.id || '';
            const rect = child.getBoundingClientRect();

            const allImgs = child.querySelectorAll('img');
            let imgCount = 0;
            const imgSample = [];
            for (const img of allImgs) {
                if (!isImgVisible(img)) continue;
                imgCount++;
                if (imgSample.length < 4) {
                    let src = '';
                    for (const attr of ['data-src','src','data-original','data-ks-lazyload']) {
                        src = img.getAttribute(attr); if (src) break;
                    }
                    const ir = img.getBoundingClientRect();
                    imgSample.push({
                        w: Math.round(ir.width), h: Math.round(ir.height),
                        src_hint: (src || '').slice(0, 60)
                    });
                }
            }

            const text = (child.textContent || '').trim().slice(0, 120);
            const area = Math.max(rect.width * rect.height, 1);

            const clickables = child.querySelectorAll('a,button,[data-value],[data-sku-id],[onclick],[class*="prop"]');
            let hasInteractive = false;
            for (const c of clickables) {
                const cr = c.getBoundingClientRect();
                if (cr.width >= 8 && cr.height >= 8 && window.getComputedStyle(c).visibility !== 'hidden') {
                    hasInteractive = true; break;
                }
            }

            const nodePath = path ? path + '.' + idx : String(idx);
            const entry = {
                p: nodePath,
                t: tag,
                c: cls || null,
                id: id || null,
                r: [Math.round(rect.w), Math.round(rect.h), Math.round(rect.t), Math.round(rect.l)],
                im: imgCount,
                is: imgSample,
                tx: text || null,
                hi: hasInteractive,
            };

            if (entry.im > 0 || entry.tx || entry.hi) {
                results.push(entry);
                if (child.children.length > 0 && depth < maxDepth) {
                    const subs = collect(child, nodePath, depth + 1, maxDepth);
                    results.push(...subs);
                }
            }
            idx++;
        }
        return results;
    }

    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const all = collect(document.body, '', 0, 5);
    return JSON.stringify({
        url: window.location.href,
        title: (document.title || '').slice(0, 200),
        vw: vw, vh: vh,
        containers: all.slice(0, 60)
    });
}"""


# ═══════════════════════════════════════════════════════════════
# 2. SKU-related JS (unchanged from original for click mechanics)
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

JS_DETECT_SKU_TYPE = r"""() => {
    const groups = [];
    const seen = new Set();

    // Strategy 1: Tmall/1688 structure: dl.J_Prop > dt (label) + dd > ul > li[data-value]
    const propLists = document.querySelectorAll('dl.J_Prop, dl[class*="prop"], dl[class*="sku"]');
    for (const dl of propLists) {
        const dt = dl.querySelector('dt');
        const dimName = dt ? (dt.textContent || '').trim().replace(/[:：\s]/g, '') : '';
        if (!dimName || dimName.length > 20) continue;
        const options = [];
        const items = dl.querySelectorAll('li[data-value], li[title], a[title], span[title]');
        for (const item of items) {
            const label = (item.getAttribute('title') || item.getAttribute('data-value') || item.textContent || '').trim();
            if (!label || label.length > 50 || seen.has(label)) continue;
            const r = item.getBoundingClientRect();
            if (r.width < 6 || r.height < 6) continue;
            seen.add(label);
            options.push(label);
        }
        if (options.length > 0) {
            groups.push({ name: dimName || '规格', options });
        }
    }

    // Strategy 2: Generic selector-based SKU detection
    const SKU_SEL = [
        '[data-value]', '[data-sku-id]', '[data-sku]',
        '[class*="skuItem"]', '[class*="valueItem"]',
        'a[title]', 'span[title]', 'li[title]',
    ].join(',');
    
    const handled = new Set();
    for (const el of document.querySelectorAll(SKU_SEL)) {
        const label = (el.getAttribute('title') || el.getAttribute('data-value') || el.textContent || '').trim();
        if (!label || label.length > 50 || seen.has(label)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 6 || r.height < 6) continue;
        seen.add(label);
        handled.add(el);
    }

    if (handled.size > 0 && groups.length === 0) {
        // Group elements by parent container proximity
        const parentGroups = {};
        for (const el of handled) {
            let p = el.parentElement;
            let key = 'default';
            for (let i = 0; i < 3 && p; i++) {
                const tag = p.tagName.toLowerCase();
                const cls = (p.className || '').toLowerCase();
                if (tag === 'ul' || cls.includes('sku') || cls.includes('prop')) {
                    key = cls.slice(0, 40) || tag;
                    break;
                }
                p = p.parentElement;
            }
            if (!parentGroups[key]) parentGroups[key] = [];
            parentGroups[key].push(el);
        }
        for (const [key, els] of Object.entries(parentGroups)) {
            const options = [...new Set(els.map(e => {
                const l = (e.getAttribute('title') || e.getAttribute('data-value') || e.textContent || '').trim();
                return l.length <= 50 ? l : '';
            }).filter(Boolean))];
            if (options.length > 0) {
                groups.push({ name: key === 'default' ? '规格' : key, options });
            }
        }
    }

    const type = groups.length >= 2 ? 'compound' : (groups.length === 1 ? 'simple' : 'simple');
    const total = groups.reduce((sum, g) => sum + g.options.length, 0);
    return { type, groups, total };
}"""

JS_SNAPSHOT_VISIBLE_IMAGES = r"""() => {
    const urls = new Set();
    for (const img of document.querySelectorAll('img')) {
        const r = img.getBoundingClientRect();
        if (r.width < 100 || r.height < 100) continue;
        for (const attr of ['data-src','src','data-original','data-ks-lazyload']) {
            let s = img.getAttribute(attr);
            if (!s || s.startsWith('data:')) continue;
            if (s.startsWith('//')) s = 'https:' + s;
            if (s.startsWith('http')) { urls.add(s); break; }
        }
    }
    return [...urls];
}"""


# ═══════════════════════════════════════════════════════════════
# 3. AI prompts — container classification + targeted extraction
# ═══════════════════════════════════════════════════════════════

CONTAINER_CLASSIFY_SYSTEM = """You are a DOM structure analyst for Chinese e-commerce product pages (Taobao, Tmall, 1688).

You receive a list of page containers with structural metadata. Your task: classify which container holds the MAIN PRODUCT GALLERY, which holds the DESCRIPTION/DETAIL section, and where the SKU selection area is.

Container fields:
- p: DOM path (e.g. "0.1.2")
- t: HTML tag (DIV, SECTION, UL, IFRAME, etc.)
- c: CSS class string
- id: element ID
- r: [width, height, topY, leftX] in pixels
- im: image count inside this container
- is: sample image dimensions [{w, h, src_hint}, ...]
- tx: first visible text (can be section header like "图文详情")
- hi: has interactive (clickable) elements
- ifr: iframe src URL (only present on IFRAME tags)

Classification rules:

1. GALLERY container (product main images):
   - Located in the upper portion of page (topY < vh * 0.7)
   - Width typically 300-500px on mobile layout (NOT full page width)
   - Contains 3-10 images that are roughly uniform size
   - Image URLs often contain "alicdn.com", "imgcdn", or "taobaocdn"
   - Often has class containing "gallery", "pic", "thumb", "swipe"

2. DESCRIPTION container (图文详情, product details):
   - Located BELOW the gallery/price area (usually topY > vh * 0.5)
   - Width is FULL page width (matches vw or nearly so)
   - IMPORTANT: Check for IFRAME tags (t=IFRAME) with im>0 — these often contain description images
   - Contains many images (5+) that span full container width
   - Often has section header text like "图文详情", "产品详情", "商品详情"
   - Class often contains "detail", "desc", "content", "description"
   - An IFRAME with many images and large dimensions IS a description container

3. SKU area container:
   - Located near the price/title area (upper-mid page)
   - Contains many small interactive elements (short text labels)
   - hi=true (has clickable children)
   - Class often contains "sku", "prop", "value", "option"
   - Elements have labels like "红色", "XL", "套餐一"

Output ONLY valid JSON (no markdown fences):"""

CONTAINER_CLASSIFY_PROMPT = """Page: {url}
Title: {title}
Viewport: {vw}x{vh}

Containers (fields: p=path, t=tag, c=class, r=[w,h,topY,leftX], im=imgCount, is=imgSample, tx=text, hi=hasInteractive):
{containers}

Classify the containers. Output JSON:
{{"gallery_path": "0.1", "description_path": "3.0", "sku_area_path": "1.2", "description_in_iframe": false}}

- gallery_path: path of the main product image gallery container. If unsure, pick the container with the most images in the upper half.
- description_path: path of the product detail/description container. If unsure, pick the full-width container below the gallery with many images.
- sku_area_path: path of the SKU selection container. If unsure, pick the interactive container near the page upper area.
- description_in_iframe: true if description content is inside an iframe (common on 1688/AliExpress).
- Leave any path as "" if you cannot identify it.

JSON:"""

TARGETED_EXTRACT_SYSTEM = """You extract structured product data from Chinese e-commerce pages. You already know which container holds what data. Extract precisely.

Container fields: p=path, t=tag, c=class, r=[w,h,topY,leftX], im=imgCount, is=imgSample, tx=text, hi=hasInteractive

Rules:
- image_urls: ONLY from the gallery container. These are product showcase photos. Max 8.
- desc_images: ONLY from the description container. These are detail/spec images. Do NOT mix with image_urls.
- price_cn: must have ¥/￥ prefix. If unclear from containers, leave as "".
- sku_prices: list SKU variant LABELS with empty price "". The price will be filled by clicking.
- sku_type: "simple" if all SKU options are from one dimension group, "compound" if there are 2+ groups (e.g. color + size).
- description_cn: text from the description container (first 2000 chars), NOT navigation/footer/recommendation text.

Output ONLY valid JSON:"""

TARGETED_EXTRACT_PROMPT = """URL: {url}
Title: {title}
Viewport: {vw}x{vh}

Classification:
- Gallery container: {gallery_path}
- Description container: {description_path}
- SKU area container: {sku_area_path}
- Description in iframe: {in_iframe}

ALL Containers for context:
{containers}

Return JSON:
{{
  "title_cn": "product title",
  "price_cn": "¥xx.xx or empty",
  "image_urls": ["urls from gallery container only"],
  "desc_images": ["urls from description container only"],
  "sku_prices": [{{"name": "variant label", "price": ""}}],
  "sku_type": "simple or compound",
  "description_cn": "product description text"
}}

Rules:
- image_urls: ONLY images inside the gallery container (path={gallery_path}). Each image: look at its is[].src_hint field.
- desc_images: ONLY images inside the description container (path={description_path}). Each image: look at its is[].src_hint field.
- NEVER mix gallery and description images. They are completely separate.
- For image URLs, use the src_hint values from the container's img samples (is field). If src_hint is truncated, output it as-is — the downloader will handle it.
- sku_type: count how many dimension groups. 1 group = "simple", 2+ groups = "compound".

JSON:"""


# ═══════════════════════════════════════════════════════════════
# 4. Orchestrator
# ═══════════════════════════════════════════════════════════════

class SlimDOMExtractor:
    """Container-classification extraction pipeline:
      1. Collect container structure via JS
      2. AI classifies containers (gallery vs description vs SKU)
      3. AI extracts from classified containers only
      4. JS-based SKU clicking with before/after image diff
    """

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    async def extract(self, page, url: str) -> dict[str, Any]:
        print("  [AI] Collecting container structure...")
        try:
            raw = await page.evaluate(JS_COLLECT_CONTAINERS)
            page_data: dict = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.warning(f"Container collection failed: {exc}")
            return {"_error": f"container_collect_failed: {exc}"}

        containers = page_data.get("containers", [])
        nc = len(containers)
        print(f"  [AI] Collected {nc} containers")

        if nc == 0:
            return {"_error": "no_containers_found"}

        print("  [AI] Classifying containers (gallery vs description vs SKU)...")
        classification = await self._classify_containers(page_data, url)

        if classification.get("_parse_error"):
            logger.warning("Container classification failed, using position heuristics")
            classification = self._fallback_classify(containers, page_data.get("vh", 800))

        gp = classification.get("gallery_path", "")
        dp = classification.get("description_path", "")
        sp = classification.get("sku_area_path", "")
        print(f"  [AI] gallery={gp}, desc={dp}, sku_area={sp}")

        print("  [AI] Targeted extraction from classified containers...")
        result = await self._targeted_extract(page_data, classification, url)

        if result.get("_parse_error"):
            logger.warning(f"Targeted extraction failed: {result.get('_raw', '')[:200]}")
            return {"_error": "extraction_failed", "classification": classification}

        ti = result.get("title_cn", "")
        im = len(result.get("image_urls", []))
        sk = len(result.get("sku_prices", []))
        di = len(result.get("desc_images", []))
        st = result.get("sku_type", "")
        print(f"  [AI] title=\"{ti[:40]}\", imgs={im}, desc_imgs={di}, skus={sk}, type={st}")

        result = await self._execute_sku_clicks(page, result)

        return result

    # ── Phase 1: Container classification ────────────────────

    async def _classify_containers(self, page_data: dict, url: str) -> dict[str, Any]:
        containers_json = json.dumps(page_data.get("containers", [])[:50], ensure_ascii=False)
        prompt = CONTAINER_CLASSIFY_PROMPT.format(
            url=url,
            title=page_data.get("title", ""),
            vw=page_data.get("vw", 375),
            vh=page_data.get("vh", 800),
            containers=containers_json,
        )
        messages = [
            {"role": "system", "content": CONTAINER_CLASSIFY_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        return await self.llm.chat_json(messages, max_tokens=500)

    def _fallback_classify(self, containers: list, vh: int) -> dict[str, Any]:
        best_gallery = ""
        best_gallery_score = 0
        best_desc = ""
        best_desc_score = 0
        best_sku = ""
        best_sku_score = 0

        for c in containers:
            p = c.get("p", "")
            r = c.get("r", [0, 0, 0, 0])
            w, h, top = r[0], r[1], r[2]
            im = c.get("im", 0)
            hi = c.get("hi", False)
            tx = c.get("tx", "") or ""
            cls = c.get("c", "") or ""
            tag = c.get("t", "")

            if w < 30 or h < 30:
                continue

            # Gallery: upper portion, moderate width, many uniform images
            if top < vh * 0.65 and im >= 2 and w < 600:
                score = im * 10 + (1 if "gallery" in cls.lower() or "pic" in cls.lower() or "thumb" in cls.lower() else 0)
                if top < vh * 0.3:
                    score += 20
                if 250 <= w <= 500:
                    score += 15
                if score > best_gallery_score:
                    best_gallery_score = score
                    best_gallery = p

            # Description: below gallery, full width, many images OR large iframe
            if top > vh * 0.4 and ((im >= 3 and w > 500) or (tag == "IFRAME" and w > 400)):
                score = im * 8 + (1 if any(k in cls.lower() for k in ["detail", "desc", "content"]) else 0)
                if tag == "IFRAME":
                    score += 40
                if any(k in tx for k in ["图文详情", "产品详情", "商品详情"]):
                    score += 30
                if w > 700:
                    score += 10
                if score > best_desc_score:
                    best_desc_score = score
                    best_desc = p

            # SKU area: interactive, moderate size, near upper area
            if hi and top < vh * 0.8 and im < 3:
                score = 5 + (1 if any(k in cls.lower() for k in ["sku", "prop", "value", "option"]) else 0)
                if top < vh * 0.5:
                    score += 10
                if score > best_sku_score:
                    best_sku_score = score
                    best_sku = p

        return {
            "gallery_path": best_gallery,
            "description_path": best_desc,
            "sku_area_path": best_sku,
            "description_in_iframe": True,
        }

    # ── Phase 2: Targeted extraction ─────────────────────────

    async def _targeted_extract(self, page_data: dict, classification: dict, url: str) -> dict[str, Any]:
        containers_json = json.dumps(page_data.get("containers", [])[:50], ensure_ascii=False)
        prompt = TARGETED_EXTRACT_PROMPT.format(
            url=url,
            title=page_data.get("title", ""),
            vw=page_data.get("vw", 375),
            vh=page_data.get("vh", 800),
            gallery_path=classification.get("gallery_path", "(unknown)"),
            description_path=classification.get("description_path", "(unknown)"),
            sku_area_path=classification.get("sku_area_path", "(unknown)"),
            in_iframe=classification.get("description_in_iframe", False),
            containers=containers_json,
        )
        messages = [
            {"role": "system", "content": TARGETED_EXTRACT_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        return await self.llm.chat_json(messages, max_tokens=2500)

    # ── Phase 3: SKU clicking ────────────────────────────────

    async def _execute_sku_clicks(self, page, result: dict) -> dict:
        skus: list = result.get("sku_prices", [])
        if not skus:
            print("  [SKU] No variants to click")
            return result

        needs_click = [s for s in skus if not s.get("price")]
        if not needs_click:
            print(f"  [SKU] All {len(skus)} variants have prices")
            return result

        sku_type = result.get("sku_type", "simple")
        print(f"  [SKU] Type={sku_type}, clicking {len(needs_click)} variants...")

        if sku_type == "compound":
            await self._click_compound_skus(page, skus)
        else:
            await self._click_simple_skus(page, skus)

        # Reset to first SKU
        first = skus[0] if skus else None
        if first:
            try:
                await page.evaluate(JS_CLICK_SKU_BY_LABEL, first.get("name", ""))
                await asyncio.sleep(0.5)
            except Exception:
                pass

        # Fallback: use product-level price for SKUs that still lack price
        default_price = result.get("price_cn", "")
        for sku in skus:
            if not sku.get("price") and default_price:
                sku["price"] = default_price

        return result

    async def _click_simple_skus(self, page, skus: list) -> None:
        for sku in skus:
            label = sku.get("name", "")
            if not label or sku.get("price"):
                continue
            try:
                before = set(await page.evaluate(JS_SNAPSHOT_VISIBLE_IMAGES))
            except Exception:
                before = set()
            try:
                clicked = await page.evaluate(JS_CLICK_SKU_BY_LABEL, label)
                if not clicked:
                    continue
                await asyncio.sleep(1.2)
                after = set(await page.evaluate(JS_SNAPSHOT_VISIBLE_IMAGES))
                diff = list(after - before)
                if diff:
                    sku["images"] = diff[:3]
                    print(f"    [{label}] {len(diff)} new image(s)")
            except Exception as exc:
                logger.debug(f"SKU click '{label}': {exc}")

    async def _click_compound_skus(self, page, skus: list) -> None:
        try:
            sku_info = await page.evaluate(JS_DETECT_SKU_TYPE)
            if isinstance(sku_info, str):
                sku_info = json.loads(sku_info)
        except Exception:
            sku_info = {"type": "simple", "groups": [], "total": len(skus)}

        groups = sku_info.get("groups", [])
        if len(groups) < 2:
            await self._click_simple_skus(page, skus)
            return

        dim1 = groups[0]
        dim2 = groups[1]
        print(f"  [SKU-compound] {dim1.get('name')} x {dim2.get('name')}")

        for opt1 in dim1.get("options", []):
            try:
                clicked = await page.evaluate(JS_CLICK_SKU_BY_LABEL, opt1)
                if not clicked:
                    continue
                await asyncio.sleep(0.6)
            except Exception:
                continue

            # Re-detect available dim2 options after clicking dim1
            try:
                state = await page.evaluate(JS_DETECT_SKU_TYPE)
                if isinstance(state, str):
                    state = json.loads(state)
                available_dim2 = []
                for g in state.get("groups", []):
                    if g.get("name") != dim1.get("name"):
                        available_dim2 = g.get("options", [])
                        break
                if not available_dim2:
                    available_dim2 = dim2.get("options", [])
            except Exception:
                available_dim2 = dim2.get("options", [])

            for opt2 in available_dim2:
                combo_name = f"{opt1}-{opt2}"
                try:
                    before = set(await page.evaluate(JS_SNAPSHOT_VISIBLE_IMAGES))
                except Exception:
                    before = set()
                try:
                    clicked = await page.evaluate(JS_CLICK_SKU_BY_LABEL, opt2)
                    if not clicked:
                        continue
                    await asyncio.sleep(1.0)
                    after = set(await page.evaluate(JS_SNAPSHOT_VISIBLE_IMAGES))
                    diff = list(after - before)
                    skus.append({
                        "name": combo_name,
                        "price": "",
                        "images": diff[:3] if diff else [],
                    })
                    if diff:
                        print(f"    [{combo_name}] {len(diff)} new image(s)")
                except Exception as exc:
                    logger.debug(f"Compound SKU '{combo_name}': {exc}")

    # ── Layout image collection (legacy, kept for backward compat) ──

    async def collect_layout_images(self, page) -> dict[str, Any]:
        return {}
