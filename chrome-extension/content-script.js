/**
 * Product Sourcing Importer — Hybrid Extraction Engine
 *
 * Architecture v2:
 *   Phase 1: Local Fast Extract (<500ms, no AI)
 *     - Heuristic title/price/images/SKU extraction
 *     - Batch SKU price collection with MutationObserver
 *   Phase 2: AI Fallback (only if local score < 70%)
 *     - Conversational DOM drilling (original approach)
 *     - Starts with local data as baseline
 *
 * Operations: expand_dom | click_sku | read_price | collect_images | extract
 */
(function () {
  'use strict';

  // ═══════════════════════════════════════
  // 1. Platform Detection
  // ═══════════════════════════════════════

  function detectPlatform(url) {
    url = url || window.location.href;
    if (/tmall\.com/i.test(url)) return 'tmall';
    if (/taobao\.com/i.test(url)) return 'taobao';
    if (/1688\.com/i.test(url)) return 'alibaba';
    if (/aliexpress/i.test(url)) return 'aliexpress';
    if (/xiaohongshu\.com/i.test(url)) return 'xiaohongshu';
    return null;
  }

  // ═══════════════════════════════════════
  // 2. DOM Progressive Explorer
  // ═══════════════════════════════════════

  var SKIP_TAGS = ['script', 'style', 'noscript', 'iframe', 'svg', 'head', 'link', 'meta', 'br', 'hr', 'wbr'];

  function normalizeUrl(url) {
    if (!url) return '';
    if (url.startsWith('//')) return 'https:' + url;
    if (!url.startsWith('http')) return '';
    return url.replace(/_\d+x\d+\./g, '.').split('?')[0];
  }

  // [FIX] Word-boundary matching: normalize URL separators to spaces
  // so /\bicon\b/ matches "icon" in "nav-icon-32" → "nav icon 32"
  var SKIP_IMG_PATTERNS = [
    /\bicon\b/, /\blogo\b/, /\bavatar\b/, /\bbanner\b/, /\bqrcode\b/,
    /\bloading\b/, /\bpixel\b/, /\btrack(?:er)?\b/, /\bbeacon\b/,
    /\bbutton\b/, /\barrow\b/, /\bback.?top\b/, /\bshare\b/,
    /\bcollect\b/, /\bcart\b/, /\bstar\b/, /\bcrown\b/, /\bmedal\b/,
    /\bbadge\b/, /\bgotop\b/, /\bupup\b/, /\berweima\b/, /\bevaluate\b/,
    /\brating\b/,
  ];

  function isProductImage(url) {
    if (!url) return false;
    var normalized = url.toLowerCase().replace(/[-_.\/\?=&#:]/g, ' ');
    for (var i = 0; i < SKIP_IMG_PATTERNS.length; i++) {
      if (SKIP_IMG_PATTERNS[i].test(normalized)) return false;
    }
    return true;
  }

  // [NEW] Unified image source extraction — covers all common lazy-load attrs
  function getImageSrc(el) {
    return el.getAttribute('data-src')
      || el.getAttribute('data-lazyload-src')
      || el.getAttribute('data-original')
      || el.getAttribute('data-lazy-src')
      || el.src
      || '';
  }

  function getNodeAtPath(pathStr) {
    if (!pathStr || pathStr === 'root') return document.body;
    var indices = pathStr.split('.').map(Number);
    var node = document.body;
    for (var i = 0; i < indices.length; i++) {
      var visibleChildren = getVisibleChildren(node);
      if (indices[i] >= visibleChildren.length) return null;
      node = visibleChildren[indices[i]];
    }
    return node;
  }

  function getVisibleChildren(parent) {
    var children = [];
    for (var i = 0; i < parent.children.length; i++) {
      var child = parent.children[i];
      var tag = child.tagName.toLowerCase();
      if (SKIP_TAGS.indexOf(tag) !== -1) continue;
      var rect = child.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      children.push(child);
    }
    return children;
  }

  function summarizeNode(node, path) {
    var tag = node.tagName.toLowerCase();
    var info = { path: path, tag: tag };

    if (node.id) info.id = node.id;

    var cls = node.className;
    if (cls && typeof cls === 'string') {
      var parts = cls.trim().split(/\s+/).slice(0, 3);
      if (parts.length) info.cls = parts.join(' ');
    }

    var directText = '';
    for (var c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) directText += c.textContent;
    }
    directText = directText.trim().replace(/\s+/g, ' ').slice(0, 100);
    if (directText) info.text = directText;

    var imgs = node.querySelectorAll('img');
    if (imgs.length) {
      info.imgs = imgs.length;
      var samples = [];
      for (var i = 0; i < imgs.length && samples.length < 2; i++) {
        var src = getImageSrc(imgs[i]); // [FIX] use unified helper
        var u = normalizeUrl(src);
        if (u && isProductImage(u)) samples.push(u.slice(0, 120));
      }
      if (samples.length) info.img_samples = samples;
    }

    var rect = node.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      // [FIX] Math.round both components separately
      info.rect = {
        w: Math.round(rect.width),
        h: Math.round(rect.height),
        t: Math.round(rect.top) + Math.round(window.scrollY),
      };
    }

    var visibleChildren = getVisibleChildren(node);
    if (visibleChildren.length > 0) {
      info.child_count = visibleChildren.length;
    }

    var hasSkuHints = node.querySelector(
      '[data-value],[data-sku-id],[data-sku],[class*="sku"],[class*="prop"]'
    );
    if (hasSkuHints) info.has_sku = true;

    // [FIX] Match "price" as a distinct class segment, not substring like "service"/"device"
    var hasPriceHints = node.querySelector(
      '[class~="price"],[class*="-price"],[class*="Price"],[class*="price-"],[data-price]'
    );
    if (hasPriceHints) info.has_price = true;

    return info;
  }

  function expandNode(pathStr) {
    var node = getNodeAtPath(pathStr);
    if (!node) return { error: 'node_not_found', path: pathStr };

    var visibleChildren = getVisibleChildren(node);
    var summaries = [];
    for (var i = 0; i < visibleChildren.length && i < 25; i++) {
      var childPath = pathStr === 'root' ? String(i) : pathStr + '.' + i;
      summaries.push(summarizeNode(visibleChildren[i], childPath));
    }

    return {
      path: pathStr,
      parent_tag: node.tagName.toLowerCase(),
      parent_class:
        node.className && typeof node.className === 'string'
          ? node.className.trim().split(/\s+/).slice(0, 3).join(' ')
          : '',
      total_children: visibleChildren.length,
      children: summaries,
    };
  }

  function buildInitialSnapshot() {
    var topLevel = expandNode('root');
    return {
      url: window.location.href,
      title: document.title.slice(0, 200),
      platform: detectPlatform(),
      viewport: { w: window.innerWidth, h: window.innerHeight },
      page_height: Math.round(document.body.scrollHeight),
      top_level: topLevel,
    };
  }

  // ═══════════════════════════════════════
  // 3. Price Zone Detection & Smart Waiting
  // ═══════════════════════════════════════

  var _priceZoneCache = null;
  var _priceZoneCacheTime = 0;
  var PRICE_ZONE_TTL = 5000; // ms — cache price zone for 5s

  // [NEW] Score DOM elements to find the primary price display area.
  // Returns the element most likely showing the product's current price,
  // or null if nothing credible is found.
  function findPriceZone() {
    var now = Date.now();
    if (_priceZoneCache && now - _priceZoneCacheTime < PRICE_ZONE_TTL) {
      return _priceZoneCache;
    }

    var candidates = [];
    var priceSels = [
      '.tm-promo-price .tm-price', '.tm-price', '.tb-rmb-num',
      '[class*="PriceBox"] span', '[class*="Price"] span',
      '.tb-price', 'span.price', 'strong.price', '.price-value',
      '.mod-detail-price .value', '[class*="detail-price"]',
      '[class*="priceValue"]', '[class*="price-value"]',
      '[class*="currentPrice"]', '[class*="nowPrice"]',
      '[class*="salePrice"]', '[class*="promoPrice"]',
      '[data-price]', '[data-spm="price"]',
    ];

    for (var i = 0; i < priceSels.length; i++) {
      var els = document.querySelectorAll(priceSels[i]);
      for (var j = 0; j < els.length; j++) {
        var el = els[j];
        var text = (el.textContent || '').trim();
        var m = text.match(/[¥￥]?\s*(\d+\.?\d*)/);
        if (!m || parseFloat(m[1]) <= 0) continue;

        // Penalise: strikethrough / original-price elements
        if (el.closest('del, s, strike')) continue;
        if (/原价|划线价|定价|参考价|吊牌价/.test(text)) continue;
        var parentCls = (el.parentElement && el.parentElement.className || '').toLowerCase();
        if (/\boriginal\b|\bold.?price\b|\bwas\b/.test(parentCls)) continue;

        var rect = el.getBoundingClientRect();
        var score = 0;

        if (rect.top > 0 && rect.top < window.innerHeight) score += 10; // visible
        if (rect.width > 30) score += 5;
        if (/price|Price/i.test(el.className || '')) score += 8;
        if (/¥|￥/.test(text)) score += 5;

        var cs = window.getComputedStyle(el);
        var fontSize = parseFloat(cs.fontSize) || 0;
        if (fontSize > 20) score += 6;
        else if (fontSize > 16) score += 4;
        else if (fontSize > 12) score += 2;

        var fontWeight = parseInt(cs.fontWeight, 10) || 400;
        if (fontWeight >= 600) score += 3;

        if (el.id && /price/i.test(el.id)) score += 5;

        candidates.push({ el: el, score: score });
      }
    }

    _priceZoneCacheTime = now;
    if (!candidates.length) {
      _priceZoneCache = null;
      return null;
    }

    candidates.sort(function (a, b) { return b.score - a.score; });
    _priceZoneCache = candidates[0].el;
    return _priceZoneCache;
  }

  // [NEW] Wait for price to change using MutationObserver + polling fallback.
  // Returns the new price string, or whatever readCurrentPrice() gives on timeout.
  function waitForPriceChange(oldPrice, timeoutMs) {
    timeoutMs = timeoutMs || 3000;
    return new Promise(function (resolve) {
      var resolved = false;
      var observer = null;
      var pollTimer = null;

      function cleanup() {
        if (observer) observer.disconnect();
        if (pollTimer) clearInterval(pollTimer);
      }

      function check() {
        if (resolved) return;
        var p = readCurrentPrice();
        if (p && p !== oldPrice) {
          resolved = true;
          cleanup();
          resolve(p);
        }
      }

      // Primary: MutationObserver on price zone
      var zone = findPriceZone();
      if (zone) {
        observer = new MutationObserver(function () { check(); });
        observer.observe(zone, {
          childList: true, subtree: true, characterData: true,
        });
      }

      // Fallback: poll every 150ms
      pollTimer = setInterval(function () { check(); }, 150);

      // Hard timeout
      setTimeout(function () {
        if (!resolved) {
          resolved = true;
          cleanup();
          resolve(readCurrentPrice());
        }
      }, timeoutMs);
    });
  }

  // ═══════════════════════════════════════
  // 4. Price Reader (context-aware)
  // ═══════════════════════════════════════

  // [REWRITTEN] Three phases with decreasing specificity.
  // No more global body.innerText scan — that was the main source of false positives.
  function readCurrentPrice() {
    // Phase 1: Price zone (highest confidence)
    var zone = findPriceZone();
    if (zone) {
      var zoneText = (zone.textContent || '').trim();
      var zm = zoneText.match(/[¥￥]\s*(\d+\.?\d*)/);
      if (zm && zm[1] && parseFloat(zm[1]) > 0) return '¥' + zm[1];
      zm = zoneText.match(/(\d+\.?\d*)/);
      if (zm && zm[1] && parseFloat(zm[1]) > 0) return '¥' + zm[1];
    }

    // Phase 2: Known CSS selector patterns
    var sels = [
      '.tm-promo-price .tm-price', '.tm-price', '.tb-rmb-num',
      '[class*="PriceBox"] span', '[class*="Price"] span',
      '.tb-price', 'span.price', 'strong.price', '.price-value',
      '.mod-detail-price .value', '[class*="detail-price"]',
      '[class*="priceValue"]', '[class*="price-value"]',
      '[class*="currentPrice"]', '[class*="nowPrice"]',
      '[class*="totalPrice"]', '[class*="salePrice"]',
      '[class*="promoPrice"]', '[class*="promo-price"]',
      '[data-price]', '[data-spm="price"]',
    ];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (!el) continue;
      // Skip strikethrough
      if (el.closest('del, s, strike')) continue;
      var t = (el.textContent || el.innerText || '').trim();
      var sm = t.match(/[¥￥]\s*(\d+\.?\d*)/);
      if (sm && sm[1] && parseFloat(sm[1]) > 0) return '¥' + sm[1];
      sm = t.match(/^(\d+\.?\d*)/);
      if (sm && sm[1] && parseFloat(sm[1]) > 0) return '¥' + sm[1];
    }

    // Phase 3: Data attributes
    var dataEl = document.querySelector('[data-price]');
    if (dataEl) {
      var val = dataEl.getAttribute('data-price') || dataEl.textContent;
      var dm = String(val).match(/(\d+\.?\d*)/);
      if (dm && dm[1] && parseFloat(dm[1]) > 0) return '¥' + dm[1];
    }

    return '';
  }

  // ═══════════════════════════════════════
  // 5. Action Executors
  // ═══════════════════════════════════════

  function findSkuElements() {
    var skuEls = document.querySelectorAll(
      '.tb-sku li, .sku-item, [class*="sku"] li, [class*="sku-value"], ' +
      '.tb-prop li, [class*="prop"] li, [class*="valueItem"], ' +
      '[class*="variant"], [class*="option-item"], ' +
      '[data-value], [data-sku-id]'
    );
    var results = [];
    var seen = {};
    for (var i = 0; i < skuEls.length; i++) {
      var el = skuEls[i];
      var text = el.textContent.trim();
      if (!text || text.length > 50 || text.length < 1) continue;
      if (seen[text]) continue;
      var rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8) continue;
      seen[text] = true;
      results.push({ el: el, text: text, index: results.length });
    }
    return results;
  }

  function findSkuGroups() {
    var all = findSkuElements();
    if (all.length === 0) return [];

    var groups = {};
    for (var i = 0; i < all.length; i++) {
      var el = all[i].el;
      var container = el.parentElement;
      while (container && container !== document.body) {
        var tag = container.tagName.toLowerCase();
        var cls = (container.className || '').toLowerCase();
        if (tag === 'ul' || cls.indexOf('sku') !== -1 || cls.indexOf('prop') !== -1 ||
            cls.indexOf('value') !== -1 || cls.indexOf('variant') !== -1 ||
            cls.indexOf('option') !== -1 || cls.indexOf('radio') !== -1) {
          break;
        }
        container = container.parentElement;
      }
      var groupKey = container ? (container.className || container.tagName) : 'default';
      if (!groups[groupKey]) groups[groupKey] = [];
      groups[groupKey].push({ index: all[i].index, text: all[i].text });
    }

    // [FIX] Multi-language dimension name detection
    var groupNames = [
      '颜色', 'color', 'colour',
      '尺码', 'size',
      '规格', 'spec', 'specification',
      '套餐', 'combo', 'package', 'bundle',
      '款式', 'style',
      '版本', 'version',
      '容量', 'capacity',
      '型号', 'model',
      '材质', 'material',
      '图案', 'pattern',
    ];

    var result = [];
    var gi = 0;
    for (var key in groups) {
      if (!groups.hasOwnProperty(key)) continue;
      var dimension = 'dim' + gi;
      var firstItem = groups[key].length > 0
        ? all.find(function (s) { return s.text === groups[key][0].text; })
        : null;
      if (firstItem && firstItem.el) {
        var c = firstItem.el.parentElement;
        while (c && c !== document.body) {
          var prevLabel = c.previousElementSibling ||
            (c.parentElement ? c.parentElement.querySelector('span, label, dt') : null);
          if (prevLabel) {
            var labelText = (prevLabel.textContent || '').trim().replace(/[:：]/g, '').toLowerCase();
            for (var gn = 0; gn < groupNames.length; gn++) {
              if (labelText.indexOf(groupNames[gn].toLowerCase()) !== -1) {
                dimension = groupNames[gn];
                break;
              }
            }
            if (dimension !== 'dim' + gi) break;
          }
          c = c.parentElement;
        }
      }
      result.push({ dimension: dimension, group: gi, options: groups[key] });
      gi++;
    }
    return result;
  }

  // [FIX] Exact match first, then prefix/suffix, never bare substring
  function clickSkuByLabel(label) {
    if (!label) return { clicked: false, reason: 'empty label' };
    var skus = findSkuElements();

    // 1. Exact match
    for (var i = 0; i < skus.length; i++) {
      if (skus[i].text === label) {
        skus[i].el.click();
        return { clicked: true, text: skus[i].text, index: skus[i].index };
      }
    }
    // 2. Label is a prefix or suffix of SKU text (e.g. "红" matches "红色")
    for (var j = 0; j < skus.length; j++) {
      if (skus[j].text.indexOf(label) === 0 || skus[j].text.lastIndexOf(label) === skus[j].text.length - label.length) {
        skus[j].el.click();
        return { clicked: true, text: skus[j].text, index: skus[j].index };
      }
    }
    // 3. SKU text contains label (weakest match)
    for (var k = 0; k < skus.length; k++) {
      if (skus[k].text.indexOf(label) !== -1) {
        skus[k].el.click();
        return { clicked: true, text: skus[k].text, index: skus[k].index };
      }
    }
    return { clicked: false, reason: 'SKU label not found: ' + label };
  }

  // [IMPROVED] Uses MutationObserver for price detection after last click
  async function clickSkuLabels(labels) {
    var results = [];
    for (var i = 0; i < labels.length; i++) {
      var r = clickSkuByLabel(labels[i]);
      results.push(r);
      if (r.clicked && i < labels.length - 1) {
        // Short wait between dimension clicks (UI update, not price)
        await new Promise(function (resolve) { setTimeout(resolve, 300); });
      }
    }
    // After last click: MutationObserver-based price wait
    var oldPrice = readCurrentPrice();
    var price = await waitForPriceChange(oldPrice, 3000);
    return { clicked: results, price: price };
  }

  function clickSkuByIndex(index) {
    var skus = findSkuElements();
    if (index >= skus.length) return { clicked: false, reason: 'index ' + index + ' out of ' + skus.length + ' SKUs' };
    var sku = skus[index];
    sku.el.click();
    return { clicked: true, text: sku.text, index: index, total: skus.length };
  }

  function collectAllImages(pathStr) {
    var node = getNodeAtPath(pathStr);
    if (!node) return { error: 'node_not_found', path: pathStr };
    var imgs = node.querySelectorAll('img');
    var urls = [];
    var seen = {};
    for (var i = 0; i < imgs.length && urls.length < 20; i++) {
      var src = getImageSrc(imgs[i]); // [FIX] unified helper
      var u = normalizeUrl(src);
      if (!u || seen[u] || !isProductImage(u)) continue;
      var rect = imgs[i].getBoundingClientRect();
      if (rect.width < 50 || rect.height < 50) continue;
      seen[u] = true;
      urls.push({
        url: u,
        w: Math.round(rect.width),
        h: Math.round(rect.height),
        t: Math.round(rect.top) + Math.round(window.scrollY),
      });
    }
    return { path: pathStr, images: urls };
  }

  function listSkus() {
    var flat = findSkuElements().map(function (s) { return { index: s.index, text: s.text }; });
    var groups = findSkuGroups();
    return {
      flat: flat,
      groups: groups,
      total: flat.length,
      is_compound: groups.length > 1,
    };
  }

  // ═══════════════════════════════════════
  // 6. Local Fast Extractor (NEW — no AI)
  // ═══════════════════════════════════════

  // Fast heuristic extraction: title, price, images, SKUs, description.
  // Runs entirely in the content script with zero LLM calls.
  function localFastExtract() {
    var result = {
      title_cn: '',
      price_cn: '',
      image_urls: [],
      desc_images: [],
      sku_prices: [],
      description_cn: '',
    };

    // ── Title ──
    var ogTitle = document.querySelector('meta[property="og:title"]');
    if (ogTitle) {
      result.title_cn = (ogTitle.getAttribute('content') || '').trim().slice(0, 200);
    }
    if (!result.title_cn) {
      var h1El = document.querySelector('h1');
      if (h1El) result.title_cn = (h1El.textContent || '').trim().slice(0, 200);
    }
    if (!result.title_cn) {
      result.title_cn = document.title
        .replace(/[\s\-|—]+(淘宝|天猫|1688|阿里巴巴|小红书|AliExpress|天猫超市|天猫国际|全球精选).*$/, '')
        .trim().slice(0, 200);
    }

    // ── Price ──
    result.price_cn = readCurrentPrice();

    // ── Main images: largest images in upper half ──
    var allImgs = document.querySelectorAll('img');
    var imgCandidates = [];
    var seenUrls = {};
    for (var i = 0; i < allImgs.length; i++) {
      var src = getImageSrc(allImgs[i]);
      var u = normalizeUrl(src);
      if (!u || !isProductImage(u) || seenUrls[u]) continue;
      var rect = allImgs[i].getBoundingClientRect();
      if (rect.width < 50 || rect.height < 50) continue;
      seenUrls[u] = true;
      var area = rect.width * rect.height;
      var scrollY = Math.round(rect.top) + Math.round(window.scrollY);
      var inUpperHalf = scrollY < document.body.scrollHeight * 0.4;
      imgCandidates.push({ url: u, area: area, upper: inUpperHalf, top: scrollY });
    }
    imgCandidates.sort(function (a, b) {
      if (a.upper !== b.upper) return a.upper ? -1 : 1;
      return b.area - a.area;
    });
    for (var j = 0; j < imgCandidates.length && result.image_urls.length < 10; j++) {
      result.image_urls.push(imgCandidates[j].url);
    }

    // ── Description images ──
    var descMarkers = [
      '图文详情', '产品详情', '商品详情', '宝贝详情', 'detailContent',
      'descContent', 'mod-detail', 'J_Detail', 'description', 'detail',
    ];
    var descContainer = null;
    var allSections = document.querySelectorAll('div, section');
    for (var d = 0; d < allSections.length; d++) {
      var cls = (allSections[d].className || '').toLowerCase();
      var id = (allSections[d].id || '').toLowerCase();
      for (var m = 0; m < descMarkers.length; m++) {
        if (cls.indexOf(descMarkers[m].toLowerCase()) !== -1 ||
            id.indexOf(descMarkers[m].toLowerCase()) !== -1) {
          descContainer = allSections[d];
          break;
        }
      }
      if (descContainer) break;
    }

    if (descContainer) {
      var descImgs = descContainer.querySelectorAll('img');
      var descSeen = {};
      for (var di = 0; di < descImgs.length && result.desc_images.length < 20; di++) {
        var dsrc = getImageSrc(descImgs[di]);
        var du = normalizeUrl(dsrc);
        if (du && isProductImage(du) && !descSeen[du]) {
          descSeen[du] = true;
          result.desc_images.push(du);
        }
      }
      result.description_cn = (descContainer.innerText || '').trim().slice(0, 5000);
    }

    return result;
  }

  // ═══════════════════════════════════════
  // 7. Batch SKU Price Collection (NEW)
  // ═══════════════════════════════════════

  // Click ALL SKU variants and collect prices — no AI needed.
  // For compound SKUs (color × size), generates all valid combinations.
  async function batchCollectSkuPrices() {
    var skuInfo = listSkus();
    if (skuInfo.total === 0) return [];

    var prices = [];

    if (skuInfo.is_compound && skuInfo.groups.length > 1) {
      var combos = generateCombinations(skuInfo.groups);
      var limit = Math.min(combos.length, 50);
      for (var c = 0; c < limit; c++) {
        var labels = combos[c].map(function (item) { return item.text; });
        var oldPrice = readCurrentPrice();
        var clickResult = await clickSkuLabels(labels);
        prices.push({
          name: labels.join('/'),
          price: clickResult.price || '',
        });
      }
    } else {
      var skus = findSkuElements();
      var maxSkus = Math.min(skus.length, 30);
      for (var s = 0; s < maxSkus; s++) {
        var prevPrice = readCurrentPrice();
        skus[s].el.click();
        var newPrice = await waitForPriceChange(prevPrice, 3000);
        prices.push({ name: skus[s].text, price: newPrice || '' });
      }
    }

    return prices;
  }

  function generateCombinations(groups) {
    if (groups.length === 0) return [];
    if (groups.length === 1) {
      return groups[0].options.map(function (opt) { return [opt]; });
    }
    var rest = generateCombinations(groups.slice(1));
    var result = [];
    for (var i = 0; i < groups[0].options.length; i++) {
      for (var j = 0; j < rest.length; j++) {
        result.push([groups[0].options[i]].concat(rest[j]));
      }
    }
    return result;
  }

  // ═══════════════════════════════════════
  // 8. AI Agent Loop — Hybrid Approach
  // ═══════════════════════════════════════
  //
  // Phase A: Local fast extract + batch SKU collection (no AI)
  // Phase B: If completeness < 70%, fall back to conversational DOM drilling

  async function aiExtract(apiUrl, debug) {
    var platform = detectPlatform();
    if (!platform) {
      return { error: 'unsupported_platform', url: window.location.href };
    }

    // ══ Phase A: Local Fast Extract (<500ms) ══
    var localData = localFastExtract();

    // ══ Phase A2: Batch SKU Price Collection ══
    try {
      var skuPrices = await batchCollectSkuPrices();
      localData.sku_prices = skuPrices;
    } catch (e) {
      // Non-fatal: SKU collection failure shouldn't block extraction
      if (debug) console.log('[Agent] SKU batch collection failed:', e.message);
    }

    // ══ Completeness Score ══
    var score = 0;
    if (localData.title_cn) score += 25;
    if (localData.price_cn) score += 25;
    if (localData.image_urls.length >= 2) score += 25;
    if (localData.desc_images.length >= 1 || localData.description_cn) score += 15;
    if (localData.sku_prices.length >= 1) score += 10;

    // If local extraction is sufficient, skip AI loop entirely
    if (score >= 70) {
      if (debug) {
        console.log('[Agent] Local extraction sufficient (score=' + score + '), skipping AI loop');
      }
      return buildResult(localData, platform, [{
        step: 'local',
        score: score,
        note: 'No AI rounds needed',
      }]);
    }

    // ══ Phase B: AI Agent Fallback (conversational DOM drilling) ══
    if (debug) {
      console.log('[Agent] Local score=' + score + '/100, falling back to AI DOM drilling');
    }

    var maxRounds = 8;
    var debugLog = [];
    var history = [];
    var exploredPaths = {};
    var collectedData = {};

    // Seed with local data so AI only needs to fill gaps
    for (var lk in localData) {
      if (localData.hasOwnProperty(lk) && localData[lk]) {
        collectedData[lk] = localData[lk];
      }
    }

    var initialSnapshot = buildInitialSnapshot();
    exploredPaths['root'] = initialSnapshot.top_level;

    for (var round = 0; round < maxRounds; round++) {
      var stepEntry = { step: round, actions: [], results: [] };

      var requestBody = {
        platform: platform,
        round: round,
        max_rounds: maxRounds,
        initial: round === 0 ? initialSnapshot : undefined,
        explored: exploredPaths,
        collected: collectedData,
        history: history.slice(-10),
        skus_available: listSkus(),
      };

      if (debug) {
        console.log('[AI Agent] Round ' + round + ' sending ' + JSON.stringify(requestBody).length + ' bytes');
      }

      var plan;
      try {
        var resp = await fetch(apiUrl + '/api/ai-agent/step', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody),
          signal: AbortSignal.timeout(30000),
        });
        plan = await resp.json();
      } catch (e) {
        stepEntry.error = e.message;
        debugLog.push(stepEntry);
        break;
      }

      stepEntry.response = plan;

      if (plan.done) {
        debugLog.push(stepEntry);
        var data = plan.data || collectedData;
        return buildResult(data, platform, debugLog);
      }

      if (!plan.actions || !plan.actions.length) {
        debugLog.push(stepEntry);
        history.push({ round: round, note: 'no actions returned' });
        break;
      }

      for (var a = 0; a < plan.actions.length; a++) {
        var action = plan.actions[a];
        var result;

        try {
          switch (action.type) {
            case 'expand_dom':
              result = expandNode(action.path || 'root');
              if (!result.error) {
                exploredPaths[action.path || 'root'] = result;
              }
              break;

            case 'click_sku':
              result = clickSkuByIndex(action.index !== undefined ? action.index : 0);
              if (result.clicked) {
                var prevPrice = readCurrentPrice();
                var newPrice = await waitForPriceChange(prevPrice, 3000);
                result.price_after_click = newPrice;
                if (!collectedData.sku_prices) collectedData.sku_prices = [];
                collectedData.sku_prices.push({ name: result.text, price: newPrice });
              }
              break;

            case 'click_sku_label':
              result = clickSkuByLabel(action.label || '');
              if (result.clicked) {
                var prevLblPrice = readCurrentPrice();
                var lblPrice = await waitForPriceChange(prevLblPrice, 3000);
                result.price_after_click = lblPrice;
                if (!collectedData.sku_prices) collectedData.sku_prices = [];
                collectedData.sku_prices.push({ name: result.text, price: lblPrice });
              }
              break;

            case 'click_sku_combo':
              result = await clickSkuLabels(action.labels || []);
              if (result.clicked && result.clicked.length > 0) {
                var comboName = (action.labels || []).join('/');
                if (!collectedData.sku_prices) collectedData.sku_prices = [];
                collectedData.sku_prices.push({ name: comboName, price: result.price });
              }
              break;

            case 'read_price':
              result = { price: readCurrentPrice() };
              if (result.price && !collectedData.price_cn) {
                collectedData.price_cn = result.price;
              }
              break;

            case 'collect_images':
              result = collectAllImages(action.path || 'root');
              if (!result.error) {
                var imgKey = action.category || 'image_urls';
                if (!collectedData[imgKey]) collectedData[imgKey] = [];
                var newUrls = result.images.map(function (img) { return img.url; });
                collectedData[imgKey] = collectedData[imgKey].concat(newUrls);
              }
              break;

            case 'extract':
              if (action.data && Object.keys(action.data).length > 0) {
                // [FIX] Whitelist: only allow known field names
                var allowedFields = [
                  'title_cn', 'price_cn', 'description_cn',
                  'image_urls', 'desc_images', 'sku_prices',
                ];
                var stored = [];
                for (var ek in action.data) {
                  if (action.data.hasOwnProperty(ek) && allowedFields.indexOf(ek) !== -1) {
                    collectedData[ek] = action.data[ek];
                    stored.push(ek);
                  }
                }
                result = stored.length
                  ? { stored: stored }
                  : { error: 'No valid fields in extract action. Allowed: ' + allowedFields.join(', ') };
              } else {
                result = {
                  error: 'extract requires "data" object with fields to store. ' +
                    'Valid types: expand_dom, click_sku, click_sku_label, click_sku_combo, read_price, collect_images, extract',
                };
              }
              break;

            default:
              result = {
                error: 'INVALID action type: "' + action.type + '". ' +
                  'ONLY these types are valid: expand_dom, click_sku, click_sku_label, click_sku_combo, read_price, collect_images, extract',
              };
          }
        } catch (e) {
          result = { error: e.message };
        }

        stepEntry.actions.push(action);
        stepEntry.results.push(result);
        history.push({
          round: round,
          action: action.type,
          path: action.path,
          result_summary: summarizeResult(result),
        });
      }

      debugLog.push(stepEntry);
    }

    return buildResult(collectedData, platform, debugLog);
  }

  function summarizeResult(result) {
    if (!result) return 'null';
    if (result.error) return 'error: ' + result.error;
    if (result.children) return result.children.length + ' children';
    if (result.clicked && Array.isArray(result.clicked)) {
      return 'combo: ' + result.clicked.map(function (r) { return r.text; }).join('+') + ' price=' + (result.price || '?');
    }
    if (result.clicked) return 'clicked: ' + result.text + ' price=' + (result.price_after_click || result.price || '?');
    if (result.price) return 'price=' + result.price;
    if (result.images) return result.images.length + ' images';
    if (result.stored) return 'stored: ' + result.stored.join(',');
    return JSON.stringify(result).slice(0, 80);
  }

  function buildResult(data, platform, debugLog) {
    var h1El = document.querySelector('h1');
    var h1Text = h1El ? (h1El.textContent || '').trim().slice(0, 200) : '';
    var fallbackTitle = document.title
      .replace(/[\s\-|—]+(淘宝|天猫|1688|阿里巴巴|小红书|AliExpress).*$/, '')
      .trim();

    var result = {
      url: window.location.href,
      platform: platform,
      title_cn: data.title_cn || h1Text || fallbackTitle,
      price_cn: data.price_cn || readCurrentPrice() || '',
      image_urls: data.image_urls || [],
      desc_images: data.desc_images || [],
      sku_prices: data.sku_prices || [],
      description_cn: data.description_cn || '',
    };
    if (debugLog && debugLog.length) result._debug = debugLog;
    return result;
  }

  // ═══════════════════════════════════════
  // 9. Message Listener
  // ═══════════════════════════════════════

  // [NEW] URL validation: only allow localhost
  function isValidApiUrl(url) {
    if (!url || typeof url !== 'string') return false;
    try {
      var parsed = new URL(url);
      return (parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1')
        && (parsed.protocol === 'http:' || parsed.protocol === 'https:');
    } catch (e) {
      return false;
    }
  }

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (msg.action === 'extract') {
      var apiUrl = 'http://localhost:8765';
      if (msg.apiUrl && isValidApiUrl(msg.apiUrl)) {
        apiUrl = msg.apiUrl;
      }
      var debug = !!msg.debug;

      aiExtract(apiUrl, debug)
        .then(function (data) {
          sendResponse(data);
        })
        .catch(function (err) {
          sendResponse({ error: err.message || 'Unknown extraction error', url: window.location.href });
        });
      return true;
    }

    if (msg.action === 'ping') {
      var platform = detectPlatform();
      sendResponse({
        ok: true,
        platform: platform,
        url: window.location.href,
        is_product: !!platform && window.location.pathname.length > 3,
      });
      return true;
    }
  });

})();
