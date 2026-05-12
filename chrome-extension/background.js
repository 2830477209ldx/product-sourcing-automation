/**
 * Product Sourcing Importer — Background Service Worker
 *
 * Minimal: mainly a placeholder for future features.
 * Popup directly calls the backend API; content script handles extraction.
 * This worker handles:
 *   - Extension lifecycle
 *   - Storage defaults
 *   - Optional: badge updates for active product pages
 */

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(['apiUrl'], (result) => {
    if (!result.apiUrl) {
      chrome.storage.local.set({ apiUrl: 'http://localhost:8765' });
    }
  });
});

// Update extension icon badge when user navigates to a supported page
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url) {
    const isProduct = /(taobao\.com|tmall\.com|1688\.com|aliexpress\.com|xiaohongshu\.com)/i.test(tab.url);
    if (isProduct && /\/item\.|detail|product|goods|offer/i.test(tab.url)) {
      chrome.action.setBadgeText({ tabId, text: '●' });
      chrome.action.setBadgeBackgroundColor({ tabId, color: '#4caf50' });
    } else {
      chrome.action.setBadgeText({ tabId, text: '' });
    }
  }
});

// Listen for messages (e.g., from popup for cross-tab communication)
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'get_settings') {
    chrome.storage.local.get(['apiUrl'], (result) => {
      sendResponse(result);
    });
    return true;
  }

  if (msg.action === 'start_server') {
    chrome.runtime.sendNativeMessage(
      'com.product_sourcing.server_launcher',
      { action: 'start_server', port: msg.port || 0 },
      (response) => {
        if (chrome.runtime.lastError) {
          sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          sendResponse(response);
        }
      }
    );
    return true;
  }

  if (msg.action === 'get_port') {
    chrome.runtime.sendNativeMessage(
      'com.product_sourcing.server_launcher',
      { action: 'get_port' },
      (response) => {
        if (chrome.runtime.lastError) {
          sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          sendResponse(response);
        }
      }
    );
    return true;
  }
});
