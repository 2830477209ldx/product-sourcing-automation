/**
 * Product Sourcing Importer — Popup Script
 */
(function () {
  'use strict';

  // ── DOM elements ──
  const $noProduct = document.getElementById('state-no-product');
  const $ready = document.getElementById('state-ready');
  const $importing = document.getElementById('state-importing');
  const $result = document.getElementById('state-result');
  const $offline = document.getElementById('state-offline');
  const $badge = document.getElementById('badge');
  const $preview = document.getElementById('preview');
  const $btnImport = document.getElementById('btn-import');
  const $btnReset = document.getElementById('btn-reset');
  const $btnDashboard = document.getElementById('btn-dashboard');
  const $btnRetry = document.getElementById('btn-retry');
  const $resultStatus = document.getElementById('result-status');
  const $resultPreview = document.getElementById('result-preview');
  const $apiUrl = document.getElementById('api-url');
  const $btnSaveApi = document.getElementById('btn-save-api');

  const states = [$noProduct, $ready, $importing, $result, $offline];

  function show(state) {
    states.forEach(s => s.classList.add('hidden'));
    state.classList.remove('hidden');
  }

  // ── Settings ──
  function getApiUrl() {
    return $apiUrl.value.trim().replace(/\/+$/, '') || 'http://localhost:8765';
  }

  async function loadSettings() {
    const stored = await chrome.storage.local.get(['apiUrl', 'lastImports']);
    if (stored.apiUrl) $apiUrl.value = stored.apiUrl;
  }

  async function saveApiUrl() {
    await chrome.storage.local.set({ apiUrl: getApiUrl() });
  }

  $btnSaveApi.addEventListener('click', saveApiUrl);

  // ── Platform badge ──
  function platformBadge(platform) {
    const map = {
      taobao: 'badge-taobao',
      tmall: 'badge-tmall',
      alibaba: 'badge-alibaba',
      xiaohongshu: 'badge-xiaohongshu',
    };
    const names = {
      taobao: 'Taobao',
      tmall: 'Tmall',
      alibaba: '1688',
      xiaohongshu: 'Xiaohongshu',
    };
    const cls = map[platform] || 'badge-unknown';
    const name = names[platform] || 'Unknown';
    return `<span class="platform-badge ${cls}">${name}</span>`;
  }

  // ── Product preview ──
  function renderPreview(data) {
    const imgs = data.image_urls || [];
    const skus = data.sku_prices || [];
    const descImgs = data.desc_images || [];
    let html = '';
    if (data.title_cn) {
      html += `<div class="title">${escapeHtml(data.title_cn.slice(0, 100))}</div>`;
    }
    if (data.price_cn) {
      html += `<div class="price">${escapeHtml(data.price_cn)}</div>`;
    }
    const parts = [];
    if (imgs.length) parts.push(`${imgs.length} images`);
    if (skus.length) parts.push(`${skus.length} SKUs`);
    if (descImgs.length) parts.push(`${descImgs.length} desc imgs`);
    if (parts.length) {
      html += `<div class="meta">${parts.join(' · ')}</div>`;
    }
    return html;
  }

  // ── Score bar color ──
  function scoreClass(score) {
    if (score >= 70) return 'score-high';
    if (score >= 40) return 'score-mid';
    return 'score-low';
  }

  function renderResult(preview, result) {
    let html = '';
    if (preview.title_cn) {
      html += `<div class="title">${escapeHtml(preview.title_cn.slice(0, 80))}</div>`;
    }
    if (result.title_en) {
      html += `<div>EN: ${escapeHtml(result.title_en.slice(0, 80))}</div>`;
    }
    if (result.price_usd !== undefined && result.price_usd !== null) {
      html += `<div class="price">$${result.price_usd}</div>`;
    }
    if (result.score !== undefined) {
      html += `
        <div class="meta">Score: ${result.score}/100</div>
        <div class="score-bar"><div class="score-fill ${scoreClass(result.score)}" style="width:${result.score}%"></div></div>
      `;
    }
    if (result.tags && result.tags.length) {
      html += `<div class="meta">${result.tags.slice(0, 5).join(', ')}</div>`;
    }
    return html;
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  // ── Check server health ──
  async function checkServer() {
    try {
      const resp = await fetch(getApiUrl() + '/api/health', {
        method: 'GET',
        signal: AbortSignal.timeout(3000),
      });
      return resp.ok;
    } catch (e) {
      return false;
    }
  }

  // ── Main flow ──
  async function init() {
    await loadSettings();

    // Check server first
    const serverOk = await checkServer();
    if (!serverOk) {
      show($offline);
      return;
    }

    // Ping active tab to check if it's a product page
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
      show($noProduct);
      return;
    }

    try {
      const ping = await chrome.tabs.sendMessage(tab.id, { action: 'ping' });
      if (ping && ping.is_product) {
        // Run extraction to show preview
        const data = await chrome.tabs.sendMessage(tab.id, { action: 'extract' });

        if (data.error) {
          show($noProduct);
          return;
        }

        // Store for later
        popupState.pageData = data;
        popupState.tabId = tab.id;

        // Render preview
        $badge.innerHTML = platformBadge(data.platform);
        $preview.innerHTML = renderPreview(data);
        show($ready);
      } else {
        show($noProduct);
      }
    } catch (e) {
      // Content script not injected — page doesn't match our patterns
      show($noProduct);
    }
  }

  // ── State ──
  const popupState = {
    pageData: null,
    tabId: null,
  };

  // ── Import button ──
  $btnImport.addEventListener('click', async () => {
    if (!popupState.pageData) return;

    show($importing);
    $btnImport.disabled = true;

    try {
      const resp = await fetch(getApiUrl() + '/api/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(popupState.pageData),
        signal: AbortSignal.timeout(60000),
      });

      const result = await resp.json();

      if (resp.ok && result.ok) {
        $resultStatus.className = 'status status-success';
        $resultStatus.textContent = result.message || 'Imported successfully!';
        $resultPreview.innerHTML = renderResult(popupState.pageData, result.product || {});

        // Store last import
        const stored = await chrome.storage.local.get('lastImports');
        const imports = stored.lastImports || [];
        imports.unshift({
          url: popupState.pageData.url,
          title: popupState.pageData.title_cn,
          time: new Date().toISOString(),
          id: result.product_id,
        });
        await chrome.storage.local.set({ lastImports: imports.slice(0, 20) });

        // Show dashboard button
        $btnDashboard.style.display = 'block';
      } else {
        $resultStatus.className = 'status status-error';
        $resultStatus.textContent = result.error || 'Import failed';
        $resultPreview.innerHTML = '';
        $btnDashboard.style.display = 'none';
      }
    } catch (e) {
      $resultStatus.className = 'status status-error';
      if (e.name === 'TimeoutError') {
        $resultStatus.textContent = 'Request timed out. Check if backend is running.';
      } else {
        $resultStatus.textContent = 'Cannot reach backend: ' + e.message;
      }
      $resultPreview.innerHTML = '';
      $btnDashboard.style.display = 'none';
    }

    show($result);
  });

  // ── Reset button ──
  $btnReset.addEventListener('click', () => {
    $btnDashboard.style.display = 'none';
    init();
  });

  // ── Retry button ──
  $btnRetry.addEventListener('click', init);

  // ── Dashboard button ──
  $btnDashboard.addEventListener('click', () => {
    chrome.tabs.create({ url: 'http://localhost:8501' });
  });

  // ── Start ──
  init();

})();
