/**
 * Product Sourcing Importer — Content Script
 *
 * Injected into supported e-commerce product pages.
 * Collects raw page data when the extension popup requests it.
 * Strategy: platform-specific selectors → layout-based fallback → API interception.
 */
(function () {
  'use strict';

  // ═══════════════════════════════════════
  // 1. Platform Detection
  // ═══════════════════════════════════════

  function detectPlatform(url) {
    url = url || window.location.href;
    if (/taobao\.com|tmall\.com/i.test(url)) {
      return /tmall\.com/i.test(url) ? 'tmall' : 'taobao';
    }
    if (/1688\.com/i.test(url)) return 'alibaba';
    if (/aliexpress/i.test(url)) return 'aliexpress';
    if (/xiaohongshu\.com/i.test(url)) return 'xiaohongshu';
    return null;
  }

  // ═══════════════════════════════════════
  // 2. Image URL helpers
  // ═══════════════════════════════════════

  const SKIP_PATTERNS = [
    'icon', 'logo', 'avatar', 'banner', 'qr_code', 'qrcode',
    'loading', 'pixel', 'track', 'beacon', '1x1',
    'btn', 'button', 'arrow', 'back_top', 'backtop',
    'share', 'collect', 'cart', 'evaluate', 'rating',
    'star', 'crown', 'medal', 'badge',
    'gotop', 'gototop', 'upup', 'erweima'
  ];

  function isProductImage(url) {
    if (!url) return false;
    const low = url.toLowerCase();
    for (const p of SKIP_PATTERNS) {
      if (low.indexOf(p) !== -1) return false;
    }
    return true;
  }

  function normalizeUrl(url) {
    if (!url) return '';
    if (url.startsWith('//')) return 'https:' + url;
    if (!url.startsWith('http')) return '';
    return url.replace(/_\d+x\d+\./g, '.').split('?')[0];
  }

  function dedupeUrls(urls) {
    const seen = new Set();
    return urls.filter(u => {
      const n = normalizeUrl(u);
      if (!n || seen.has(n)) return false;
      seen.add(n);
      return true;
    }).map(normalizeUrl);
  }

  // ═══════════════════════════════════════
  // 3. Platform-specific extractors (primary)
  // ═══════════════════════════════════════

  function extractTaobaoTitle() {
    // Try known title selectors
    const sel = [
      '.tb-main-title', 'h1[data-spm="1000983"]', '.ItemTitle--mainTitle--',
      '[class*="ItemTitle"] h1', '.slogan', 'h3.tb-main-title',
      'h1[data-spm-anchor-id]',
    ];
    for (const s of sel) {
      const el = document.querySelector(s);
      if (el) {
        const t = el.textContent.trim();
        if (t.length > 2) {
          // Clean: remove "淘宝" prefix, spaces, newlines, emoji
          return t.replace(/[\s\n]+/g, ' ').replace(/^淘宝\s*/, '').slice(0, 200);
        }
      }
    }
    // Fallback: h1 with most text in main area
    const h1s = document.querySelectorAll('h1');
    let best = '';
    for (const h of h1s) {
      const t = h.textContent.trim();
      if (t.length > best.length && t.length < 300) best = t;
    }
    return best || document.title.replace(/[\s-]+淘宝.*$/, '').trim();
  }

  function extractTaobaoPrice() {
    // Tmall specific
    let el = document.querySelector('.tm-promo-price .tm-price, .tm-price');
    if (el) {
      const m = el.textContent.match(/[\d.]+/);
      if (m) return '¥' + m[0];
    }
    // Taobao specific
    for (const sel of [
      '.tb-rmb-num', '.tm-promo-price', '[class*="PriceBox"] span',
      '.tb-price span', '.tb-price em', '.tb-price',
      '[class*="tmPrice"]', '[class*="tbPrice"]',
      'span.price', 'strong.price', '.price-value',
    ]) {
      el = document.querySelector(sel);
      if (el) {
        const t = el.textContent.trim();
        const m = t.match(/[\d.]+/);
        if (m) return '¥' + m[0];
      }
    }
    return '';
  }

  function extractTaobaoImages() {
    const urls = [];
    const seen = new Set();

    // Strategy: thumbnailsWrap (stable prefix)
    const wraps = document.querySelectorAll('[class*="thumbnailsWrap"] img');
    for (const img of wraps) {
      const src = img.getAttribute('data-src') || img.src;
      const u = normalizeUrl(src);
      if (u && !seen.has(u) && isProductImage(u)) {
        seen.add(u);
        urls.push(u);
      }
    }
    if (urls.length >= 2) return urls;

    // Fallback: #J_UlThumb
    const thumb = document.querySelector('#J_UlThumb');
    if (thumb) {
      for (const img of thumb.querySelectorAll('img')) {
        const src = img.getAttribute('data-src') || img.src;
        const u = normalizeUrl(src);
        if (u && !seen.has(u) && isProductImage(u)) {
          seen.add(u);
          urls.push(u);
        }
      }
    }
    if (urls.length >= 2) return urls;

    // Fallback: all alicdn images
    for (const img of document.querySelectorAll('img')) {
      const src = img.getAttribute('data-src') || img.getAttribute('data-original') || img.src;
      const u = normalizeUrl(src);
      if (!u || seen.has(u)) continue;
      if (!isProductImage(u)) continue;
      if (!/alicdn\.com|taobaocdn\.com|gw\.alicdn\.com/i.test(u)) continue;
      const r = img.getBoundingClientRect();
      if (r.width < 60 || r.height < 60) continue;
      seen.add(u);
      urls.push(u);
    }
    return urls.slice(0, 20);
  }

  function extractTaobaoSku() {
    const items = [];
    const seen = new Set();
    // SKU containers
    const containers = document.querySelectorAll(
      '.tb-sku, .tb-prop, [class*="sku"], [class*="J_TSaleProp"], ' +
      '[class*="sku-line"], [class*="prop-list"], [class*="valueItem"]'
    );
    for (const c of containers) {
      for (const el of c.querySelectorAll('li, a, span')) {
        const title = el.getAttribute('title') || el.textContent.trim();
        if (!title || title.length > 50 || seen.has(title)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) continue;
        const img = el.querySelector('img');
        seen.add(title);
        items.push({
          name: title,
          price: '',
          images: img ? [normalizeUrl(img.getAttribute('data-src') || img.src)] : [],
        });
      }
    }
    return items;
  }

  function extract1688Title() {
    const sel = [
      '.d-title', 'h1[class*="title"]', '.mod-detail-title h1',
      '.title-text', '[class*="product-title"]',
    ];
    for (const s of sel) {
      const el = document.querySelector(s);
      if (el) {
        const t = el.textContent.replace(/[\s\n]+/g, ' ').trim();
        if (t.length > 2) return t.slice(0, 200);
      }
    }
    return document.title.replace(/[\s-]+.*$/, '').trim();
  }

  function extract1688Price() {
    for (const sel of [
      '.mod-detail-price .value', '.price-original',
      '[class*="detail-price"] span', '.mod-price .price',
      'span[class*="price"]',
    ]) {
      const el = document.querySelector(sel);
      if (el) {
        const t = el.textContent.trim();
        const m = t.match(/[\d.]+/);
        if (m) return '¥' + m[0];
      }
    }
    return '';
  }

  function extract1688Images() {
    const urls = [];
    const seen = new Set();
    const gallery = document.querySelector('.tab-demo, [data-module-name="detail"], .mod-detail-gallery');
    const imgs = gallery ? gallery.querySelectorAll('img') : [];
    for (const img of imgs) {
      const src = img.getAttribute('data-lazyload-src') || img.getAttribute('data-src') || img.src;
      const u = normalizeUrl(src);
      if (u && !seen.has(u) && isProductImage(u)) {
        seen.add(u);
        urls.push(u);
      }
    }
    if (urls.length >= 2) return urls;
    for (const img of document.querySelectorAll('img')) {
      const src = img.getAttribute('data-lazyload-src') || img.getAttribute('data-src') || img.src;
      const u = normalizeUrl(src);
      if (!u || seen.has(u) || !isProductImage(u)) continue;
      if (!/alicdn\.com|1688\.com|img\.cdn/i.test(u)) continue;
      const r = img.getBoundingClientRect();
      if (r.width < 60 || r.height < 60) continue;
      seen.add(u);
      urls.push(u);
    }
    return urls.slice(0, 20);
  }

  function extract1688Sku() {
    const items = [];
    const seen = new Set();
    const containers = document.querySelectorAll('.mod-detail-sku, [class*="sku"], [class*="prop"]');
    for (const c of containers) {
      for (const el of c.querySelectorAll('li, span, a, div[class*="item"]')) {
        const title = el.getAttribute('title') || el.textContent.trim();
        if (!title || title.length > 50 || seen.has(title)) continue;
        seen.add(title);
        items.push({ name: title, price: '', images: [] });
      }
    }
    return items;
  }

  function extractXHSData() {
    // Xiaohongshu product detail page
    const titleEl = document.querySelector('.title, [class*="title"], h1');
    const priceEl = document.querySelector('.price, [class*="price"], .red-price');
    const title = titleEl ? titleEl.textContent.trim().slice(0, 200) : '';
    const price = priceEl
      ? '¥' + (priceEl.textContent.match(/[\d.]+/) || [''])[0]
      : '';
    const imgs = [];
    const seen = new Set();
    for (const img of document.querySelectorAll('.note-scroller img, .swiper-slide img, [class*="carousel"] img')) {
      const src = img.getAttribute('data-src') || img.src;
      const u = normalizeUrl(src);
      if (u && !seen.has(u) && isProductImage(u)) {
        seen.add(u);
        imgs.push(u);
      }
    }
    return { title, price, images: imgs, skus: [] };
  }

  // ═══════════════════════════════════════
  // 4. Layout-based fallback (no CSS selectors)
  // ═══════════════════════════════════════

  function layoutCollectImages() {
    const urls = [];
    const seen = new Set();

    // Find largest image-rich container in top half of page
    const vh = window.innerHeight;
    let best = null, bestScore = 0;

    for (const el of document.querySelectorAll('div,ul,section,figure,li')) {
      const r = el.getBoundingClientRect();
      if (r.top > vh * 0.6 || r.bottom < 80) continue;
      const imgs = el.querySelectorAll('img');
      if (imgs.length < 2) continue;
      const area = r.width * r.height;
      if (area < 30000) continue;
      const score = area * (1 + imgs.length * 0.3);
      if (score > bestScore) { bestScore = score; best = el; }
    }

    const collect = (container) => {
      if (!container) return;
      for (const img of container.querySelectorAll('img')) {
        for (const attr of ['data-src', 'data-original', 'data-lazyload-src', 'src']) {
          const s = img.getAttribute(attr);
          if (!s) continue;
          const u = normalizeUrl(s);
          if (!u || seen.has(u) || !isProductImage(u)) continue;
          const r = img.getBoundingClientRect();
          if (r.width < 50 || r.height < 50) continue;
          seen.add(u);
          urls.push(u);
          break;
        }
      }
    };

    collect(best);
    return urls;
  }

  function layoutCollectDescImages() {
    // Find images between product detail section and recommendations
    const urls = [];
    const seen = new Set();
    const MARKERS_START = ['图文详情', '产品详情', '商品详情', '规格参数'];
    const MARKERS_END = ['本店推荐', '猜你喜欢', '看了又看', '店铺推荐'];

    function findY(texts) {
      const w = document.createTreeWalker(document.body, 4);
      let n, bestY = null, bestDist = Infinity;
      while (n = w.nextNode()) {
        const txt = (n.textContent || '').trim();
        if (txt.length > 20) continue;
        for (const t of texts) {
          if (txt === t || (txt.startsWith(t) && txt.length <= t.length + 2)) {
            const range = document.createRange();
            try { range.selectNodeContents(n); } catch (e) { continue; }
            const rects = range.getClientRects();
            for (const rc of rects) {
              if (rc.height > 0 && rc.width > 0 && rc.top < bestDist) {
                bestDist = rc.top;
                bestY = rc.top;
              }
            }
          }
        }
      }
      return bestY;
    }

    const startY = findY(MARKERS_START);
    if (startY === null) return [];

    const endY = findY(MARKERS_END) || 999999;

    for (const img of document.querySelectorAll('img')) {
      const r = img.getBoundingClientRect();
      if (r.top < startY || r.top > endY) continue;
      if (r.width < 60 || r.height < 60) continue;
      for (const attr of ['data-src', 'data-ks-lazyload', 'data-original', 'src']) {
        const s = img.getAttribute(attr);
        if (!s) continue;
        const u = normalizeUrl(s);
        if (u && !seen.has(u)) {
          seen.add(u);
          urls.push(u);
          break;
        }
      }
    }
    return urls;
  }

  function layoutGetAllText() {
    // Get visible text only (skip script/style/svg/nav/footer)
    let text = '';
    const SKIP = new Set(['script', 'style', 'noscript', 'iframe', 'svg', 'nav', 'footer', 'head']);
    const w = document.createTreeWalker(document.body, 4);
    let n;
    while (n = w.nextNode()) {
      const p = n.parentElement;
      if (!p || SKIP.has(p.tagName.toLowerCase())) continue;
      if (p.offsetParent === null && p.tagName !== 'BODY') continue;
      const t = n.textContent.trim();
      if (t) {
        // Avoid super-long lines (like encoded base64)
        text += (t.length > 500 ? t.slice(0, 497) + '...' : t) + '\n';
      }
      if (text.length > 15000) break;
    }
    return text;
  }

  // ═══════════════════════════════════════
  // 5. API interception — try to get clean data
  // ═══════════════════════════════════════

  let interceptedData = null;

  function setupApiInterceptor() {
    // Hook fetch to capture product detail API responses
    const origFetch = window.fetch;
    const pattern = /mtop\.(taobao|tmall|alibaba)\.detail/i;

    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const promise = origFetch.call(this, input, init);

      if (pattern.test(url)) {
        promise.then(async (resp) => {
          try {
            const clone = resp.clone();
            const data = await clone.json();
            interceptedData = { url, data, time: Date.now() };
          } catch (e) { /* ignore parse errors */ }
        }).catch(() => {});
      }

      return promise;
    };
  }

  // ═══════════════════════════════════════
  // 6. Main extraction orchestrator
  // ═══════════════════════════════════════

  function extract() {
    const url = window.location.href;
    const platform = detectPlatform(url);

    if (!platform) {
      return { error: 'unsupported_platform', url };
    }

    let result = { url, platform };

    // ── Platform-specific extraction ──
    if (platform === 'taobao' || platform === 'tmall') {
      result = {
        ...result,
        title_cn: extractTaobaoTitle(),
        price_cn: extractTaobaoPrice(),
        image_urls: extractTaobaoImages(),
        sku_prices: extractTaobaoSku(),
      };
    } else if (platform === 'alibaba') {
      result = {
        ...result,
        title_cn: extract1688Title(),
        price_cn: extract1688Price(),
        image_urls: extract1688Images(),
        sku_prices: extract1688Sku(),
      };
    } else if (platform === 'xiaohongshu') {
      const xhs = extractXHSData();
      result = {
        ...result,
        title_cn: xhs.title,
        price_cn: xhs.price,
        image_urls: xhs.images,
        sku_prices: xhs.skus,
      };
    }

    // ── Layout-based fallback for missing data ──
    if (!result.title_cn || result.title_cn.length < 2) {
      const h1 = document.querySelector('h1');
      result.title_cn = h1 ? h1.textContent.trim().slice(0, 200) : document.title;
    }

    if (!result.image_urls || result.image_urls.length < 2) {
      result.image_urls = layoutCollectImages();
    }

    // Dedupe and filter images
    result.image_urls = dedupeUrls(result.image_urls || []);

    // ── Description images (layout-based, no platform selectors) ──
    result.desc_images = layoutCollectDescImages();
    result.desc_images = dedupeUrls(result.desc_images);

    // ── Description text (clean text) ──
    result.description_cn = layoutGetAllText();

    // ── API intercepted data (bonus) ──
    if (interceptedData) {
      result._api_data = {
        url: interceptedData.url,
        has_data: !!interceptedData.data,
      };
    }

    return result;
  }

  // ═══════════════════════════════════════
  // 7. Message listener
  // ═══════════════════════════════════════

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === 'extract') {
      const data = extract();
      sendResponse(data);
      return true; // keep channel open for async
    }

    if (msg.action === 'ping') {
      const platform = detectPlatform();
      sendResponse({
        ok: true,
        platform,
        url: window.location.href,
        is_product: !!platform && window.location.pathname.length > 3,
      });
      return true;
    }
  });

  // Setup API interceptor on load
  setupApiInterceptor();

})();
