/* FillMyPDF — Background service worker (Manifest V3)
   Handles PDF detection on tabs and stores per-tab state.
*/

const API_KEY_STORE = 'fmp_api_key';

// Show badge on PDF pages
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete' || !tab.url) return;

  const isPdf = tab.url.toLowerCase().endsWith('.pdf') || tab.url.includes('application/pdf');
  if (isPdf) {
    chrome.action.setBadgeText({ tabId, text: 'PDF' });
    chrome.action.setBadgeBackgroundColor({ tabId, color: '#4f46e5' });
  } else {
    // Check if the page has form inputs (lightweight heuristic via title/url)
    chrome.action.setBadgeText({ tabId, text: '' });
  }
});

// Context menu: right-click "Fill this form with FillMyPDF"
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'fillmypdf-fill',
    title: 'Fill this form with FillMyPDF',
    contexts: ['page', 'editable'],
  });
  chrome.contextMenus.create({
    id: 'fillmypdf-dashboard',
    title: 'Open FillMyPDF Dashboard',
    contexts: ['page'],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'fillmypdf-fill') {
    // Open the popup (can't directly — send message to content script to show overlay)
    chrome.tabs.sendMessage(tab.id, { type: 'SHOW_FILL_OVERLAY' });
  } else if (info.menuItemId === 'fillmypdf-dashboard') {
    chrome.tabs.create({ url: 'http://localhost:8000/ui/index.html' });
  }
});

// Listen for messages from content script
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'GET_API_KEY') {
    chrome.storage.local.get([API_KEY_STORE], data => {
      sendResponse({ key: data[API_KEY_STORE] || null });
    });
    return true; // keep channel open for async response
  }
  if (msg.type === 'LOG') {
    console.log('[FillMyPDF bg]', msg.data);
  }
});
