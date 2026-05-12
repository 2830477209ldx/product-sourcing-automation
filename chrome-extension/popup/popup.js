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

  function setApiUrl(url) {
    $apiUrl.value = url;
    chrome.storage.local.set({ apiUrl: url });
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
  async function checkServer(baseUrl) {
    try {
      const resp = await fetch((baseUrl || getApiUrl()) + '/api/health', {
        method: 'GET',
        signal: AbortSignal.timeout(2000),
      });
      return resp.ok;
    } catch (e) {
      return false;
    }
  }

  // ── Port discovery: scan localhost ports for the API ──
  async function discoverApiPort() {
    const ports = [8765, 8766, 8767, 8768, 8769, 8770, 8501, 8502, 8000, 5000];
    for (const port of ports) {
      try {
        const resp = await fetch(`http://localhost:${port}/api/health`, {
          method: 'GET',
          signal: AbortSignal.timeout(800),
        });
        if (resp.ok) {
          const text = await resp.text();
          try {
            const data = JSON.parse(text);
            if (data.status === 'ok') return port;
          } catch (_) {}
        }
      } catch (_) {}
    }
    return null;
  }

  // ── Try reading port from native host's port file ──
  async function getPortFromNative() {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ action: 'get_port' }, (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.ok) {
          resolve(null);
        } else {
          resolve(resp.port);
        }
      });
    });
  }

  // ── Auto-start server via native messaging ──
  async function tryStartServer() {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ action: 'start_server' }, (resp) => {
        if (chrome.runtime.lastError) {
          resolve(false);
        } else {
          resolve(resp && resp.ok);
        }
      });
    });
  }

  async function waitForServer(retries = 10, interval = 1000) {
    for (let i = 0; i < retries; i++) {
      await new Promise(r => setTimeout(r, interval));
      const port = await discoverApiPort();
      if (port) {
        setApiUrl('http://localhost:' + port);
        return true;
      }
    }
    return false;
  }

  // ── Main flow ──
  async function init() {
    await loadSettings();
    document.getElementById('debug-panel').innerHTML = '';

    // ── Step 1: Find the API server ──
    let serverOk = false;

    // Try stored URL first
    serverOk = await checkServer();

    if (!serverOk) {
      // Scan common ports to find an already-running API
      const found = await discoverApiPort();
      if (found) {
        setApiUrl('http://localhost:' + found);
        serverOk = true;
      }
    }

    if (!serverOk) {
      // Try native host to read port file (from a previous session)
      const filePort = await getPortFromNative();
      if (filePort) {
        const testUrl = 'http://localhost:' + filePort;
        if (await checkServer(testUrl)) {
          setApiUrl(testUrl);
          serverOk = true;
        }
      }
    }

    if (!serverOk) {
      // Try auto-starting the server
      const started = await tryStartServer();
      if (started) {
        show($importing);
        $importing.querySelector('.status').innerHTML = '<span class="spinner"></span> Starting backend server...';
        serverOk = await waitForServer();
      }
      if (!serverOk) {
        show($offline);
        return;
      }
    }

    // ── Step 2: Detect product page ──
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
      show($noProduct);
      return;
    }

    try {
      let ping = null;
      try {
        ping = await chrome.tabs.sendMessage(tab.id, { action: 'ping' });
      } catch (_) {
        // Content script not injected yet — inject it manually
        const isSupported = /(taobao\.com|tmall\.com|1688\.com|aliexpress\.com|xiaohongshu\.com)/i.test(tab.url || '');
        if (isSupported) {
          try {
            await chrome.scripting.executeScript({
              target: { tabId: tab.id },
              files: ['content-script.js'],
            });
            // Re-ping after injection
            ping = await chrome.tabs.sendMessage(tab.id, { action: 'ping' });
          } catch (_) {}
        }
      }

      if (ping && ping.is_product) {
        show($importing);
        var debug = document.getElementById('debug-mode').checked;
        $importing.querySelector('.status').innerHTML = '<span class="spinner"></span> Analyzing page with AI...';
        var apiUrl = getApiUrl();
        var data = await chrome.tabs.sendMessage(tab.id, { action: 'extract', apiUrl: apiUrl, debug: debug });

        if (data.error) {
          show($noProduct);
          return;
        }

        popupState.pageData = data;
        popupState.tabId = tab.id;
        $badge.innerHTML = platformBadge(data.platform);
        $preview.innerHTML = renderPreview(data);

        renderDebugPanel(data._debug);

        show($ready);
      } else {
        show($noProduct);
      }
    } catch (e) {
      show($noProduct);
    }
  }

  // ── State ──
  const popupState = {
    pageData: null,
    tabId: null,
  };

  // ── Debug panel ──
  function renderDebugPanel(debugLog) {
    var panel = document.getElementById('debug-panel');
    if (!debugLog || !debugLog.length) {
      panel.classList.remove('active');
      panel.innerHTML = '';
      return;
    }
    panel.classList.add('active');
    var html = '';
    html += '<div class="debug-toggle" style="margin-bottom:6px;"><strong>AI Agent Trace</strong> (' + debugLog.length + ' steps)</div>';
    for (var i = 0; i < debugLog.length; i++) {
      var step = debugLog[i];
      html += '<div class="debug-step">';
      html += '<div class="step-num">Step ' + step.step + '</div>';

      if (step.request) {
        html += '<div class="label">DOM sent</div>';
        html += '<pre>' + escapeHtmlTrunc('dom_size=' + step.request.dom_size + ' history=' + step.request.history_count + ' | interactive=' + (step.dom_interactive ? step.dom_interactive.length : '?' ) + ' imgs=' + (step.dom_images ? step.dom_images.length : '?') ) + '</pre>';
      }

      if (step.actions && step.actions.length) {
        html += '<div class="label">Actions</div>';
        for (var a = 0; a < step.actions.length; a++) {
          var act = step.actions[a];
          var res = step.results[a];
          html += '<pre>' + escapeHtmlTrunc(act.type + ': ' + JSON.stringify(act.reason || act.text || '').slice(0, 60)) + '<br>&rarr; ' + escapeHtmlTrunc(JSON.stringify(res).slice(0, 100)) + '</pre>';
        }
      }

      if (step.response) {
        html += '<div class="label">AI response</div>';
        if (step.response.done) {
          var keys = step.response.data ? Object.keys(step.response.data).join(', ') : 'none';
          html += '<pre>done=true | data keys: ' + escapeHtmlTrunc(keys) + '</pre>';
        } else if (step.response.actions) {
          html += '<pre>done=false | ' + step.response.actions.length + ' actions</pre>';
        } else if (step.response.error) {
          html += '<pre style="color:#c62828;">error: ' + escapeHtmlTrunc(step.response.error) + '</pre>';
        } else {
          html += '<pre>' + escapeHtmlTrunc(JSON.stringify(step.response).slice(0, 120)) + '</pre>';
        }
      }

      html += '</div>';
    }
    panel.innerHTML = html;
  }

  function escapeHtmlTrunc(s) {
    if (!s) return '';
    s = String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return s.length > 200 ? s.slice(0, 197) + '...' : s;
  }

  // ── Import button ──
  $btnImport.addEventListener('click', async () => {
    if (!popupState.pageData) return;

    show($importing);
    $btnImport.disabled = true;

    try {
      const apiUrl = getApiUrl();
      const bodyStr = JSON.stringify(popupState.pageData);
      console.log('[Importer] POST', apiUrl + '/api/import', 'body length:', bodyStr.length);

      const resp = await fetch(apiUrl + '/api/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: bodyStr,
        signal: AbortSignal.timeout(60000),
      });

      console.log('[Importer] Response status:', resp.status, resp.statusText);

      const text = await resp.text();
      console.log('[Importer] Response body (first 500):', text.slice(0, 500));

      let result;
      try {
        result = JSON.parse(text);
      } catch (jsonErr) {
        $resultStatus.className = 'status status-error';
        $resultStatus.textContent = 'Server returned: ' + resp.status + ' ' + text.slice(0, 120);
        $resultPreview.innerHTML = '';
        $btnDashboard.style.display = 'none';
        show($result);
        return;
      }

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
        const detail = (result && result.detail) ? JSON.stringify(result.detail) : '';
        $resultStatus.className = 'status status-error';
        $resultStatus.textContent = 'Error ' + resp.status + (detail ? ': ' + detail.slice(0, 100) : ': Import failed');
        $resultPreview.innerHTML = '';
        $btnDashboard.style.display = 'none';
      }
    } catch (e) {
      $resultStatus.className = 'status status-error';
      if (e.name === 'TimeoutError') {
        $resultStatus.textContent = 'Request timed out. Check if backend is running.';
      } else if (e.name === 'TypeError' && e.message.includes('Failed to fetch')) {
        $resultStatus.textContent = 'Cannot reach backend at ' + getApiUrl() + '. Is "python run.py api" running?';
      } else {
        $resultStatus.textContent = 'Error: ' + e.message;
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
