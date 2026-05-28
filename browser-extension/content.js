/* FillMyPDF — Content Script
   Injected into all pages. Listens for fill requests from the background worker.
   Shows an in-page status toast when fields are filled.
*/

(function () {
  'use strict';

  // ── Toast notification ─────────────────────────────────────────────────

  function showToast(msg, type = 'info') {
    const existing = document.getElementById('fmp-toast');
    if (existing) existing.remove();

    const colors = { info: '#4f46e5', success: '#16a34a', error: '#dc2626', warn: '#d97706' };
    const toast = document.createElement('div');
    toast.id = 'fmp-toast';
    toast.style.cssText = `
      position: fixed;
      top: 20px;
      right: 20px;
      z-index: 2147483647;
      background: ${colors[type] || colors.info};
      color: white;
      padding: 10px 16px;
      border-radius: 8px;
      font-family: system-ui, sans-serif;
      font-size: 13px;
      font-weight: 500;
      box-shadow: 0 4px 16px rgba(0,0,0,.2);
      transition: opacity .3s;
      max-width: 280px;
    `;
    toast.innerHTML = `📄 FillMyPDF: ${msg}`;
    document.body.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 350);
    }, 3500);
  }

  // ── Fill overlay trigger ───────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'FILL_RESULT') {
      const { count } = msg;
      if (count > 0) showToast(`Filled ${count} field${count !== 1 ? 's' : ''}!`, 'success');
      else showToast('No matching fields found.', 'warn');
    }

    if (msg.type === 'SHOW_FILL_OVERLAY') {
      showToast('Open the FillMyPDF popup to fill this form.', 'info');
    }

    if (msg.type === 'CLEAR_RESULT') {
      showToast('Fields cleared.', 'info');
    }
  });

  // ── PDF link interception ─────────────────────────────────────────────
  // When user clicks a .pdf link, inject a small badge near it suggesting use of extension.
  document.addEventListener('mouseover', (e) => {
    const a = e.target.closest('a[href]');
    if (!a) return;
    if (!a.href.toLowerCase().endsWith('.pdf')) return;
    if (a.dataset.fmpHinted) return;
    a.dataset.fmpHinted = '1';
    a.title = (a.title ? a.title + ' | ' : '') + '📄 Open in FillMyPDF for auto-fill';
  });

})();
