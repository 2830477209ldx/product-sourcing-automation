/**
 * Product Sourcing Importer — AI-Driven Content Script
 *
 * Instead of hardcoded CSS selectors, this agent sends DOM snapshots
 * to the backend LLM, which decides what to click/scroll/read.
 * The script is a "dumb executor" — AI tells it what to do.
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
  // 2. DOM Snapshot Builder
  // ═══════════════════════════════════════

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

  function buildDomOutline(node, maxDepth, maxChildren) {
    if (maxDepth === undefined) maxDepth = 3;
    if (maxChildren === undefined) maxChildren = 15;
    if (!node || node.nodeType !== 1) return null;

    var tag = node.tagName.toLowerCase();
    if (['script','style','noscript','iframe','svg','head','link','meta','br','hr'].indexOf(tag) !== -1) return null;

    var info = { tag: tag };
    if (node.id) info.id = node.id;

    var cls = node.className;
    if (cls && typeof cls === 'string') {
      var parts = cls.trim().split(/\s+/).slice(0, 2);
      if (parts.length) info.class = parts.join(' ');
    }

    // Text preview
    var directText = '';
    for (var c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) directText += c.textContent;
    }
    directText = directText.trim().slice(0, 80);
    if (directText) info.text = directText;

    // Image count in subtree
    var imgs = node.querySelectorAll('img');
    if (imgs.length) {
      info.img_count = imgs.length;
      var samples = [];
      for (var i = 0; i < imgs.length && samples.length < 3; i++) {
        var src = imgs[i].getAttribute('data-src') || imgs[i].getAttribute('data-lazyload-src') || imgs[i].src;
        var u = normalizeUrl(src);
        if (u) samples.push(u.slice(0, 120));
      }
      if (samples.length) info.img_samples = samples;
    }

    // Rect
    var rect = node.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      info.rect = { w: Math.round(rect.width), h: Math.round(rect.height), top: Math.round(rect.top) };
    }

    // Visible or important
    var isBig = rect.width > 200 && rect.height > 50;
    var hasContent = directText.length > 0 || imgs.length > 0;

    // Children (only if depth left and this node is meaningful)
    if (maxDepth > 1 && (isBig || hasContent || node.children.length > 0)) {
      var children = [];
      for (var j = 0; j < node.children.length && children.length < maxChildren; j++) {
        var child = buildDomOutline(node.children[j], maxDepth - 1, Math.floor(maxChildren / 2));
        if (child) children.push(child);
      }
      if (children.length) info.children = children;
    }

    return info;
  }

  function findInteractiveElements() {
    var items = [];
    // Tabs / nav items with clickable text
    var tabCandidates = document.querySelectorAll(
      '[role="tab"], .tab, .nav-item, .nav-link, [class*="tab-"], ' +
      'li > a, .menu-item, [class*="menu-item"], [class*="Tab"]'
    );
    for (var i = 0; i < tabCandidates.length; i++) {
      var el = tabCandidates[i];
      var text = el.textContent.trim();
      if (!text || text.length > 30 || text.length < 1) continue;
      // Only unique texts
      var dup = false;
      for (var j = 0; j < items.length; j++) {
        if (items[j].text === text) { dup = true; break; }
      }
      if (dup) continue;
      var rect = el.getBoundingClientRect();
      if (rect.width < 10 || rect.height < 10) continue;
      items.push({
        type: 'tab',
        text: text,
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) }
      });
    }

    // SKU / variant items
    var skuCandidates = document.querySelectorAll(
      '.tb-sku li, .sku-item, [class*="sku"] li, [class*="sku-value"], ' +
      '.tb-prop li, [class*="prop"] li, [class*="valueItem"], ' +
      '[class*="variant"], [class*="option-item"]'
    );
    for (var k = 0; k < skuCandidates.length; k++) {
      var skuEl = skuCandidates[k];
      var skuText = skuEl.textContent.trim();
      if (!skuText || skuText.length > 40 || skuText.length < 1) continue;
      var skuRect = skuEl.getBoundingClientRect();
      if (skuRect.width < 8 || skuRect.height < 8) continue;
      items.push({
        type: 'sku',
        text: skuText,
        index: items.filter(function(it) { return it.type === 'sku'; }).length,
        rect: { x: Math.round(skuRect.x), y: Math.round(skuRect.y), w: Math.round(skuRect.width), h: Math.round(skuRect.height) }
      });
    }

    return items.slice(0, 30);
  }

  function samplePageImages() {
    var urls = [];
    var seen = {};
    var allImgs = document.querySelectorAll('img');
    for (var i = 0; i < allImgs.length && urls.length < 15; i++) {
      var src = allImgs[i].getAttribute('data-src') || allImgs[i].getAttribute('data-lazyload-src') || allImgs[i].src;
      var u = normalizeUrl(src);
      if (!u || seen[u]) continue;
      if (!isProductImage(u)) continue;
      var r = allImgs[i].getBoundingClientRect();
      if (r.width < 40 || r.height < 40) continue;
      seen[u] = true;
      urls.push(u);
    }
    return urls;
  }

  function buildSnapshot(extracted) {
    var bodyOutline = buildDomOutline(document.body, 3, 20);
    return {
      url: window.location.href,
      title: document.title.slice(0, 150),
      platform: detectPlatform(),
      viewport: { w: window.innerWidth, h: window.innerHeight },
      scroll_y: Math.round(window.scrollY),
      visible_text: getVisibleTextSample(3000),
      outline: bodyOutline ? [bodyOutline] : [],
      interactive: findInteractiveElements(),
      images_sample: samplePageImages(),
      extracted: extracted || {}
    };
  }

  function getVisibleTextSample(maxLen) {
    var text = '';
    var skip = { script:1, style:1, noscript:1, iframe:1, svg:1, nav:1, footer:1, head:1 };
    var walker = document.createTreeWalker(document.body, 4);
    var node;
    while ((node = walker.nextNode()) && text.length < maxLen) {
      var p = node.parentElement;
      if (!p || skip[p.tagName.toLowerCase()]) continue;
      var t = node.textContent.trim();
      if (t && t.length < 300) {
        text += t + '\n';
      }
    }
    return text.slice(0, maxLen);
  }

  // ═══════════════════════════════════════
  // 3. Action Executor
  // ═══════════════════════════════════════

  function clickByText(text) {
    // Find clickable element by visible text
    var candidates = [];
    var all = document.querySelectorAll('a, button, span, li, div, [role="tab"], [role="button"], [class*="tab"], [class*="nav"], [class*="menu"]');
    for (var i = 0; i < all.length; i++) {
      var t = all[i].textContent.trim();
      if (t === text || t.indexOf(text) !== -1) {
        var r = all[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          candidates.push({ el: all[i], text: t, rect: r });
        }
      }
    }
    if (candidates.length === 0) return { clicked: false, reason: 'text not found: ' + text };

    // Pick the best candidate (visible, in viewport, reasonable size)
    candidates.sort(function (a, b) { return a.rect.top - b.rect.top; });
    var best = candidates[0];
    best.el.click();
    return { clicked: true, text: best.text, selector: best.el.tagName + (best.el.className ? '.' + best.el.className.split(' ')[0] : '') };
  }

  function clickSkuByIndex(index) {
    var skuEls = document.querySelectorAll(
      '.tb-sku li, .sku-item, [class*="sku"] li, [class*="sku-value"], ' +
      '.tb-prop li, [class*="prop"] li, [class*="valueItem"], ' +
      '[class*="variant"], [class*="option-item"]'
    );
    if (index >= skuEls.length) return { clicked: false, reason: 'index ' + index + ' out of ' + skuEls.length + ' SKUs' };
    var el = skuEls[index];
    el.click();
    return { clicked: true, text: el.textContent.trim().slice(0, 40), index: index };
  }

  function scrollBy(pixels) {
    window.scrollBy(0, pixels);
    return { scrolled: pixels, new_y: Math.round(window.scrollY) };
  }

  function readCurrentPrice() {
    // Try common price selectors
    var sels = [
      '.tm-promo-price .tm-price', '.tm-price', '.tb-rmb-num',
      '[class*="PriceBox"] span', '[class*="Price"] span',
      '.tb-price', 'span.price', 'strong.price', '.price-value',
      '.mod-detail-price .value', '[class*="detail-price"]',
    ];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) {
        var t = el.textContent.trim();
        var m = t.match(/[\d.]+/);
        if (m) return '¥' + m[0];
      }
    }
    return '';
  }

  // ═══════════════════════════════════════
  // 4. AI Agent Loop
  // ═══════════════════════════════════════

  async function aiExtract(apiUrl, debug) {
    var platform = detectPlatform();
    if (!platform) {
      return { error: 'unsupported_platform', url: window.location.href };
    }

    var extracted = {};
    var history = [];
    var maxRounds = 6;
    var debugLog = [];
    var dom = buildSnapshot(extracted);

    for (var round = 0; round < maxRounds; round++) {
      var stepEntry = {
        step: round,
        request: { platform: platform, round: round, dom_size: JSON.stringify(dom).length, history_count: history.length },
        response: null,
        actions: [],
        results: []
      };

      if (debug) {
        stepEntry.dom_outline = JSON.parse(JSON.stringify(dom.outline).slice(0, 3000));
        stepEntry.dom_interactive = dom.interactive.slice(0, 15);
        stepEntry.dom_images = dom.images_sample;
        console.log('[AI Agent] Step ' + round + ' request dom_size=' + stepEntry.request.dom_size + ' interactive_count=' + dom.interactive.length);
      }

      var plan;
      try {
        var resp = await fetch(apiUrl + '/api/ai-agent/step', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            platform: platform,
            round: round,
            dom: dom,
            history: history,
            debug: debug
          }),
          signal: AbortSignal.timeout(30000),
        });
        plan = await resp.json();
      } catch (e) {
        stepEntry.response = { error: e.message };
        debugLog.push(stepEntry);
        history.push({ round: round, action: { type: 'error' }, result: e.message });
        break;
      }

      stepEntry.response = plan;

      if (debug) {
        console.log('[AI Agent] Step ' + round + ' response done=' + plan.done + ' actions=' + (plan.actions ? plan.actions.length : 0));
      }

      if (plan.done) {
        if (debug) stepEntry.response = JSON.parse(JSON.stringify(plan));
        debugLog.push(stepEntry);
        var data = plan.data || extracted;
        return buildResult(data, platform, debugLog);
      }

      if (!plan.actions || !plan.actions.length) {
        debugLog.push(stepEntry);
        history.push({ round: round, action: { type: 'noop' }, result: 'no actions returned' });
        break;
      }

      for (var a = 0; a < plan.actions.length; a++) {
        var action = plan.actions[a];
        var actionResult;

        try {
          switch (action.type) {
            case 'click':
              actionResult = clickByText(action.text || '');
              break;
            case 'click_sku':
              actionResult = clickSkuByIndex(action.index !== undefined ? action.index : 0);
              break;
            case 'scroll':
              actionResult = scrollBy(action.pixels || 500);
              break;
            case 'wait':
              await new Promise(function (r) { setTimeout(r, action.ms || 1500); });
              actionResult = { waited: action.ms || 1500 };
              break;
            case 'extract':
              actionResult = { price_now: readCurrentPrice(), scroll_y: Math.round(window.scrollY) };
              if (readCurrentPrice() && !extracted.price_cn) extracted.price_cn = readCurrentPrice();
              break;
            default:
              actionResult = { error: 'unknown action type: ' + action.type };
          }
        } catch (e) {
          actionResult = { error: e.message };
        }

        stepEntry.actions.push(action);
        stepEntry.results.push(actionResult);
        history.push({ round: round, action: action, result: actionResult });

        if (action.type === 'click' || action.type === 'click_sku' || action.type === 'scroll') {
          await new Promise(function (r) { setTimeout(r, 800); });
        }
      }

      debugLog.push(stepEntry);
      dom = buildSnapshot(extracted);
    }

    return buildResult(extracted, platform, debugLog);
  }

  function buildResult(data, platform, debugLog) {
    var result = {
      url: window.location.href,
      platform: platform,
      title_cn: data.title_cn || document.querySelector('h1')?.textContent?.trim()?.slice(0, 200) || document.title.replace(/[\s-]+(淘宝|天猫|1688).*$/, '').trim(),
      price_cn: data.price_cn || '',
      image_urls: data.image_urls || [],
      desc_images: data.desc_images || [],
      sku_prices: data.sku_prices || [],
      description_cn: data.description_cn || getVisibleTextSample(15000),
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
