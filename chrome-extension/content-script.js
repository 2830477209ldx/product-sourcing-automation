/**
 * Product Sourcing Importer — Conversational DOM Drilling
 *
 * Innovation: AI explores the DOM progressively like a developer using DevTools.
 * Instead of sending the entire DOM tree at once, the script sends only top-level
 * node summaries first. AI then "drills down" into specific nodes that look
 * promising, accumulating context across rounds until it finds what it needs.
 *
 * Operations: expand_dom | click_sku | read_price | extract (done)
 * No scrolling — only SKU clicking for price collection.
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

  var SKIP_TAGS = ['script','style','noscript','iframe','svg','head','link','meta','br','hr','wbr'];

  function normalizeUrl(url) {
    if (!url) return '';
    if (url.startsWith('//')) return 'https:' + url;
    if (!url.startsWith('http')) return '';
    return url.replace(/_\d+x\d+\./g, '.').split('?')[0];
  }

  function isProductImage(url) {
    if (!url) return false;
    var skipWords = ['icon','logo','avatar','banner','qrcode','loading','pixel','track','beacon',
      'btn','button','arrow','back_top','share','collect','cart','star','crown','medal','badge',
      'gotop','upup','erweima','evaluate','rating'];
    var low = url.toLowerCase();
    for (var i = 0; i < skipWords.length; i++) {
      if (low.indexOf(skipWords[i]) !== -1) return false;
    }
    return true;
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
        var src = imgs[i].getAttribute('data-src') || imgs[i].getAttribute('data-lazyload-src') || imgs[i].src;
        var u = normalizeUrl(src);
        if (u && isProductImage(u)) samples.push(u.slice(0, 120));
      }
      if (samples.length) info.img_samples = samples;
    }

    var rect = node.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      info.rect = { w: Math.round(rect.width), h: Math.round(rect.height), t: Math.round(rect.top + window.scrollY) };
    }

    var visibleChildren = getVisibleChildren(node);
    if (visibleChildren.length > 0) {
      info.child_count = visibleChildren.length;
    }

    var hasSkuHints = node.querySelector('[data-value],[data-sku-id],[data-sku],[class*="sku"],[class*="prop"]');
    if (hasSkuHints) info.has_sku = true;

    var hasPriceHints = node.querySelector('[class*="rice"],[class*="Rice"]');
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
      parent_class: (node.className && typeof node.className === 'string') ? node.className.trim().split(/\s+/).slice(0, 3).join(' ') : '',
      total_children: visibleChildren.length,
      children: summaries
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
      top_level: topLevel
    };
  }

  // ═══════════════════════════════════════
  // 3. Action Executors
  // ═══════════════════════════════════════

  function isSkuDisabled(el) {
    var cls = (el.className || '').toLowerCase();
    if (cls.indexOf('disable') !== -1 || cls.indexOf('soldout') !== -1 || cls.indexOf('sold-out') !== -1) return true;
    if (el.getAttribute('aria-disabled') === 'true') return true;
    var style = window.getComputedStyle(el);
    if (parseFloat(style.opacity) < 0.4) return true;
    var color = style.color || '';
    if (color === 'rgb(153, 153, 153)' || color === 'rgb(204, 204, 204)' || color === '#999' || color === '#ccc') return true;
    // Check for disabled class on parent li
    var li = el.closest('li');
    if (li && (li.className || '').toLowerCase().indexOf('disable') !== -1) return true;
    return false;
  }

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
      results.push({ el: el, text: text, index: results.length, disabled: isSkuDisabled(el) });
    }
    return results;
  }

  function findSkuState() {
    var all = findSkuElements();
    var available = [];
    var disabled = [];
    for (var i = 0; i < all.length; i++) {
      if (all[i].disabled) {
        disabled.push({ index: all[i].index, text: all[i].text });
      } else {
        available.push({ index: all[i].index, text: all[i].text });
      }
    }
    return { available: available, disabled: disabled, total: all.length };
  }

  function findSkuGroups() {
    var all = findSkuElements();
    if (all.length === 0) return [];

    // Group by parent container — each container = one SKU dimension (color, size, etc.)
    var groups = {};
    for (var i = 0; i < all.length; i++) {
      var el = all[i].el;
      // Find the nearest container parent (ul, div with sku/prop/value class)
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

    var result = [];
    var groupNames = ['颜色', '尺码', '规格', '套餐', '款式', '版本', '容量', '型号'];
    var gi = 0;
    for (var key in groups) {
      if (groups.hasOwnProperty(key)) {
        // Try to match a dimension name from the container's preceding label
        var dimension = 'dim' + gi;
        var container = groups[key].length > 0 ? all.find(function(s) { return s.text === groups[key][0].text; }) : null;
        if (container && container.el) {
          var c = container.el.parentElement;
          while (c && c !== document.body) {
            var prevLabel = c.previousElementSibling || (c.parentElement ? c.parentElement.querySelector('span, label, dt') : null);
            if (prevLabel) {
              var labelText = (prevLabel.textContent || '').trim().replace(/[:：]/g, '');
              for (var gn = 0; gn < groupNames.length; gn++) {
                if (labelText.indexOf(groupNames[gn]) !== -1) {
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
    }
    return result;
  }

  function clickSkuByLabel(label) {
    var skus = findSkuElements();
    for (var i = 0; i < skus.length; i++) {
      if (skus[i].text === label || skus[i].text.indexOf(label) !== -1) {
        skus[i].el.click();
        return { clicked: true, text: skus[i].text, index: skus[i].index };
      }
    }
    return { clicked: false, reason: 'SKU label not found: ' + label };
  }

  async function clickSkuLabels(labels) {
    var results = [];
    for (var i = 0; i < labels.length; i++) {
      var r = clickSkuByLabel(labels[i]);
      results.push(r);
      if (r.clicked) {
        await new Promise(function(resolve) { setTimeout(resolve, 500); });
      }
    }
    // After clicking all labels, wait for price update
    await new Promise(function(resolve) { setTimeout(resolve, 1500); });
    var price = readCurrentPrice();
    if (!price) {
      await new Promise(function(resolve) { setTimeout(resolve, 800); });
      price = readCurrentPrice();
    }
    return { clicked: results, price: price };
  }

  function clickSkuByIndex(index) {
    var skus = findSkuElements();
    if (index >= skus.length) return { clicked: false, reason: 'index ' + index + ' out of ' + skus.length + ' SKUs' };
    var sku = skus[index];
    sku.el.click();
    return { clicked: true, text: sku.text, index: index, total: skus.length };
  }

  function readCurrentPrice() {
    // Phase 1: CSS selector patterns (broad coverage for CN e-commerce)
    var sels = [
      '.tm-promo-price .tm-price', '.tm-price', '.tb-rmb-num',
      '[class*="PriceBox"] span', '[class*="Price"] span',
      '.tb-price', 'span.price', 'strong.price', '.price-value',
      '.mod-detail-price .value', '[class*="detail-price"]',
      '[class*="priceValue"]', '[class*="price-value"]',
      '[class*="currentPrice"]', '[class*="nowPrice"]',
      '[class*="totalPrice"]', '[class*="salePrice"]',
      '[class*="promoPrice"]', '[class*="promo-price"]',
      '[class*="origPrice"]', '[class*="skuPrice"]',
      '[class*="SalePrice"]', '[class*="NowPrice"]',
    ];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) {
        var t = (el.textContent || el.innerText || '').trim();
        var m = t.match(/[¥￥]\s*(\d+\.?\d*)/);
        if (m && m[1]) return '¥' + m[1];
        m = t.match(/^(\d+\.?\d*)/);
        if (m && m[1] && parseFloat(m[1]) > 0) return '¥' + m[1];
      }
    }

    // Phase 2: Scan visible text for price patterns
    var bodyText = (document.body.innerText || '');
    var lines = bodyText.split('\n').slice(0, 60);
    for (var j = 0; j < lines.length; j++) {
      var pm = lines[j].match(/[¥￥]\s*(\d+\.?\d*)/);
      if (pm) return '¥' + pm[1];
    }

    // Phase 3: Look for price-like numbers near ¥/￥ symbols anywhere
    var allText = document.body.innerText || '';
    var m2 = allText.match(/[¥￥]\s*(\d+\.?\d{2})/);
    if (m2) return '¥' + m2[1];
    m2 = allText.match(/¥(\d+)/);
    if (m2) return '¥' + m2[1];

    // Phase 4: Try data attributes
    var dataPrice = document.querySelector('[data-price], [data-spm="price"]');
    if (dataPrice) {
      var val = dataPrice.getAttribute('data-price') || dataPrice.textContent;
      var m3 = String(val).match(/(\d+\.?\d*)/);
      if (m3) return '¥' + m3[1];
    }

    return '';
  }

  function collectAllImages(pathStr, includeAll) {
    var node = getNodeAtPath(pathStr);
    if (!node) return { error: 'node_not_found', path: pathStr };
    var imgs = node.querySelectorAll('img');
    var urls = [];
    var seen = {};
    for (var i = 0; i < imgs.length && urls.length < 30; i++) {
      var img = imgs[i];
      var src = '';
      for (var ai = 0; ai < ATTRS.length; ai++) {
        src = img.getAttribute(ATTRS[ai]);
        if (src) break;
      }
      var u = normalizeUrl(src);
      if (!u || seen[u]) continue;
      if (!includeAll && !isProductImage(u)) continue;
      var rect = img.getBoundingClientRect();
      if (rect.width < 40 || rect.height < 40) continue;
      seen[u] = true;
      urls.push({ url: u, w: Math.round(rect.width), h: Math.round(rect.height), t: Math.round(rect.top + window.scrollY) });
    }
    // Also check background images in style attributes
    if (urls.length === 0 || includeAll) {
      var allEls = node.querySelectorAll('*');
      for (var j = 0; j < allEls.length && urls.length < 30; j++) {
        var bg = allEls[j].style.backgroundImage || '';
        var bgm = bg.match(/url\(["']?([^"')]+)["']?\)/);
        if (bgm) {
          var bgu = normalizeUrl(bgm[1]);
          if (bgu && !seen[bgu]) {
            seen[bgu] = true;
            urls.push({ url: bgu, w: 0, h: 0, t: 0, from: 'bg' });
          }
        }
      }
    }
    return { path: pathStr, images: urls };
  }

  // Lazy-load image attributes to try, in priority order
  var ATTRS = ['data-src', 'data-ks-lazyload', 'data-lazy-src', 'data-original', 'data-lazyload-src', 'src'];

  function listSkus() {
    var all = findSkuElements();
    var flat = all.map(function(s) { return { index: s.index, text: s.text, disabled: s.disabled }; });
    var groups = findSkuGroups();
    var state = findSkuState();
    return {
      flat: flat,
      groups: groups,
      total: flat.length,
      available: state.available.length,
      disabled: state.disabled.length,
      is_compound: groups.length > 1
    };
  }

  // ═══════════════════════════════════════
  // 4. AI Agent Loop — Conversational DOM Drilling
  // ═══════════════════════════════════════

  async function aiExtract(apiUrl, debug) {
    var platform = detectPlatform();
    if (!platform) {
      return { error: 'unsupported_platform', url: window.location.href };
    }

    var maxRounds = 8;
    var debugLog = [];
    var history = [];
    var exploredPaths = {};
    var collectedData = {};

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
        skus_available: listSkus()
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
                await new Promise(function(r) { setTimeout(r, 1500); });
                var price = readCurrentPrice();
                if (!price) {
                  await new Promise(function(r) { setTimeout(r, 800); });
                  price = readCurrentPrice();
                }
                result.price_after_click = price;
                if (!collectedData.sku_prices) collectedData.sku_prices = [];
                collectedData.sku_prices.push({ name: result.text, price: price });
              }
              break;

            case 'click_sku_label':
              result = clickSkuByLabel(action.label || '');
              if (result.clicked) {
                await new Promise(function(r) { setTimeout(r, 1500); });
                var lblPrice = readCurrentPrice();
                if (!lblPrice) {
                  await new Promise(function(r) { setTimeout(r, 800); });
                  lblPrice = readCurrentPrice();
                }
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
              // Report SKU state after combo click
              result._sku_state = findSkuState();
              break;

            case 'check_sku_state':
              result = findSkuState();
              break;

            case 'read_price':
              result = { price: readCurrentPrice() };
              if (result.price && !collectedData.price_cn) {
                collectedData.price_cn = result.price;
              }
              break;

            case 'collect_images':
              var isDesc = (action.category === 'desc_images');
              result = collectAllImages(action.path || 'root', isDesc);
              if (!result.error) {
                var key = action.category || 'image_urls';
                if (!collectedData[key]) collectedData[key] = [];
                var newUrls = result.images.map(function(img) { return img.url; });
                collectedData[key] = collectedData[key].concat(newUrls);
              }
              break;

            case 'extract':
              if (action.data && Object.keys(action.data).length > 0) {
                for (var k in action.data) {
                  if (action.data.hasOwnProperty(k)) {
                    collectedData[k] = action.data[k];
                  }
                }
                result = { stored: Object.keys(action.data) };
              } else {
                result = { error: 'extract requires "data" object with fields to store (e.g. {"type":"extract","data":{"title_cn":"..."}}). Valid types: expand_dom, click_sku, read_price, collect_images, extract' };
              }
              break;

            default:
              result = { error: 'INVALID action type: "' + action.type + '". ONLY these 5 types are valid: expand_dom, click_sku, read_price, collect_images, extract. Use expand_dom to explore the DOM.' };
          }
        } catch (e) {
          result = { error: e.message };
        }

        stepEntry.actions.push(action);
        stepEntry.results.push(result);
        history.push({ round: round, action: action.type, path: action.path, result_summary: summarizeResult(result) });
      }

      debugLog.push(stepEntry);
    }

    return buildResult(collectedData, platform, debugLog);
  }

  function summarizeResult(result) {
    if (!result) return 'null';
    if (result.error) return 'error: ' + result.error;
    if (result.children) return result.children.length + ' children';
    if (result.available !== undefined) return 'sku_state: ' + result.available.length + ' available, ' + (result.disabled ? result.disabled.length : 0) + ' disabled';
    if (result.clicked && Array.isArray(result.clicked)) return 'combo: ' + result.clicked.map(function(r) { return r.text; }).join('+') + ' price=' + (result.price || '?');
    if (result.clicked) return 'clicked: ' + result.text + ' price=' + (result.price_after_click || result.price || '?');
    if (result.price) return 'price=' + result.price;
    if (result.images) return result.images.length + ' images';
    if (result.stored) return 'stored: ' + result.stored.join(',');
    return JSON.stringify(result).slice(0, 80);
  }

  function buildResult(data, platform, debugLog) {
    var result = {
      url: window.location.href,
      platform: platform,
      title_cn: data.title_cn || document.querySelector('h1')?.textContent?.trim()?.slice(0, 200) || document.title.replace(/[\s-]+(淘宝|天猫|1688).*$/, '').trim(),
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
  // 5. Message Listener
  // ═══════════════════════════════════════

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === 'extract') {
      var apiUrl = msg.apiUrl || 'http://localhost:8765';
      var debug = !!msg.debug;
      aiExtract(apiUrl, debug).then(function (data) {
        sendResponse(data);
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
