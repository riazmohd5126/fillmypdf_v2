/* =========================================================
   FillMyPDF Browser Extension — Popup Script
   ========================================================= */

const BASE_URL_KEY  = 'fmp_base_url';
const API_KEY_STORE = 'fmp_api_key';
const DEFAULT_BASE  = 'http://localhost:8000';

let selectedProfileId = null;
let profiles          = [];
let detectedFields    = 0;

// ── Boot ──────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  const stored = await get(API_KEY_STORE);
  if (stored) {
    showMain();
    await Promise.all([loadProfiles(), loadTemplates(), detectFields()]);
  } else {
    showSetup();
  }
});

// ── Storage helpers ───────────────────────────────────────────────────────

function get(key)           { return new Promise(r => chrome.storage.local.get([key], d => r(d[key] || null))); }
function set(key, val)      { return new Promise(r => chrome.storage.local.set({ [key]: val }, r)); }
function remove(key)        { return new Promise(r => chrome.storage.local.remove([key], r)); }

async function apiKey()     { return await get(API_KEY_STORE); }
async function baseUrl()    { return (await get(BASE_URL_KEY)) || DEFAULT_BASE; }

// ── API helper ────────────────────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  const key  = await apiKey();
  const base = await baseUrl();
  const headers = { 'X-API-Key': key, ...(opts.headers || {}) };
  const res = await fetch(`${base}${path}`, { ...opts, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res;
}

// ── UI helpers ────────────────────────────────────────────────────────────

function showSetup() {
  document.getElementById('setup-section').classList.remove('hidden');
  document.getElementById('main-section').classList.add('hidden');
}

function showMain() {
  document.getElementById('setup-section').classList.add('hidden');
  document.getElementById('main-section').classList.remove('hidden');
  get(API_KEY_STORE).then(k => {
    if (k) {
      const masked = k.length > 10 ? k.slice(0, 8) + '···' + k.slice(-4) : k;
      document.getElementById('key-display').textContent = `API Key: ${masked}`;
    }
  });
}

function showStatus(id, msg, type = 'info') {
  const el = document.getElementById(id);
  el.className = `status status-${type}`;
  el.textContent = msg;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 5000);
}

// ── Key management ────────────────────────────────────────────────────────

async function saveKey() {
  const key = document.getElementById('api-key-input').value.trim();
  if (!key) { showStatus('setup-status', 'Enter your API key.', 'error'); return; }
  try {
    // Validate key by fetching profiles endpoint
    const headers = { 'X-API-Key': key };
    const base = DEFAULT_BASE;
    const res = await fetch(`${base}/api/v1/profiles`, { headers });
    if (!res.ok && res.status !== 404) throw new Error(`API returned ${res.status}`);
  } catch (e) {
    showStatus('setup-status', `Could not connect: ${e.message}`, 'error');
    return;
  }
  const remember = document.getElementById('remember-key').checked;
  if (remember) await set(API_KEY_STORE, key);
  else await set(API_KEY_STORE, key); // session only not supported in MV3 local storage; store it
  showMain();
  await Promise.all([loadProfiles(), loadTemplates(), detectFields()]);
}

async function resetKey() {
  await remove(API_KEY_STORE);
  selectedProfileId = null;
  profiles = [];
  showSetup();
}

// ── Profiles ──────────────────────────────────────────────────────────────

async function loadProfiles() {
  const listEl = document.getElementById('profiles-list');
  listEl.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const res = await apiFetch('/api/v1/profiles');
    profiles = await res.json();
    if (!Array.isArray(profiles)) profiles = profiles.profiles || [];
    if (profiles.length === 0) {
      listEl.innerHTML = '<div class="empty-state">No profiles yet.<br/><a href="http://localhost:8000/ui/index.html" target="_blank" style="color:#4f46e5">Create one in the dashboard →</a></div>';
      return;
    }
    listEl.innerHTML = profiles.map(p => `
      <div class="profile-card" id="pc-${esc(p.id)}" onclick="selectProfile('${esc(p.id)}')">
        <div class="profile-initial">${(p.full_name||p.name||'?').charAt(0).toUpperCase()}</div>
        <div>
          <div class="profile-name">${esc(p.full_name || p.name || p.id)}</div>
          <div class="profile-meta">${esc(p.email || '')}</div>
        </div>
      </div>`).join('');
    // Auto-select first
    if (profiles.length > 0) selectProfile(profiles[0].id);
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state" style="color:#ef4444">${esc(e.message)}</div>`;
  }
}

function selectProfile(id) {
  selectedProfileId = id;
  document.querySelectorAll('.profile-card').forEach(el => el.classList.remove('selected'));
  const card = document.getElementById(`pc-${id}`);
  if (card) card.classList.add('selected');
}

// ── Templates ─────────────────────────────────────────────────────────────

async function loadTemplates() {
  const sel = document.getElementById('template-select');
  try {
    const res = await apiFetch('/api/v1/templates?limit=50');
    const data = await res.json();
    const items = Array.isArray(data) ? data : (data.templates || data.results || []);
    items.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = t.name || t.id;
      sel.appendChild(opt);
    });
  } catch (_) {
    // templates endpoint may not exist — silently skip
  }
}

// ── Field detection (via content script message) ──────────────────────────

async function detectFields() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]), textarea, select');
        return inputs.length;
      }
    });
    detectedFields = result?.[0]?.result || 0;
    document.getElementById('field-count-label').textContent =
      detectedFields > 0
        ? `${detectedFields} form field${detectedFields !== 1 ? 's' : ''} detected on this page`
        : 'No standard form fields detected (PDF viewer pages also supported)';
  } catch (e) {
    document.getElementById('field-count-label').textContent = 'Could not scan page fields.';
  }
}

// ── Fill current page ──────────────────────────────────────────────────────

async function doFill() {
  if (!selectedProfileId) {
    showStatus('fill-status', 'Select a profile first.', 'error');
    return;
  }
  showStatus('fill-status', 'Fetching profile data…', 'info');
  try {
    const res = await apiFetch(`/api/v1/profiles/${selectedProfileId}`);
    const profile = await res.json();
    const data = profile.data || profile;
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: fillPageWithData,
      args: [data],
    });
    const filled = result?.[0]?.result || 0;
    showStatus('fill-status', filled > 0 ? `✔ Filled ${filled} field${filled !== 1 ? 's' : ''}!` : 'No matching fields found.', filled > 0 ? 'success' : 'info');
  } catch (e) {
    showStatus('fill-status', e.message, 'error');
  }
}

/**
 * Injected into the page — fills form fields with profile values.
 * Runs in the page context, cannot access extension APIs.
 */
function fillPageWithData(data) {
  const inputs = document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]):not([type="checkbox"]):not([type="radio"]), textarea, select'
  );
  const checkboxes = document.querySelectorAll('input[type="checkbox"]');
  const radios     = document.querySelectorAll('input[type="radio"]');

  const flat = {};
  function flatten(obj, prefix = '') {
    for (const [k, v] of Object.entries(obj || {})) {
      const key = prefix ? `${prefix}_${k}` : k;
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) flatten(v, key);
      else flat[key] = String(v ?? '');
    }
  }
  flatten(data);

  // Also add flat keys at both levels
  for (const [k, v] of Object.entries(data || {})) {
    if (typeof v !== 'object') flat[k] = String(v ?? '');
  }

  function scoreMatch(fieldId, dataKey) {
    const a = fieldId.toLowerCase().replace(/[-_\s]/g, '');
    const b = dataKey.toLowerCase().replace(/[-_\s]/g, '');
    if (a === b) return 100;
    if (a.includes(b) || b.includes(a)) return 60;
    return 0;
  }

  function bestMatch(fieldEl) {
    const candidates = [fieldEl.name, fieldEl.id, fieldEl.placeholder, fieldEl.getAttribute('aria-label')]
      .filter(Boolean).map(s => s.toLowerCase());
    let best = null, bestScore = 0;
    for (const [dk, dv] of Object.entries(flat)) {
      for (const c of candidates) {
        const score = scoreMatch(c, dk);
        if (score > bestScore) { bestScore = score; best = dv; }
      }
    }
    return bestScore >= 60 ? best : null;
  }

  let filled = 0;
  inputs.forEach(el => {
    const val = bestMatch(el);
    if (val !== null && el.value !== val) {
      el.value = val;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      filled++;
    }
  });

  // Checkboxes: check if truthy value matches
  checkboxes.forEach(el => {
    const val = bestMatch(el);
    if (val !== null) {
      const shouldCheck = ['true', '1', 'yes', 'on', 'checked'].includes(val.toLowerCase());
      if (el.checked !== shouldCheck) {
        el.checked = shouldCheck;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        filled++;
      }
    }
  });

  return filled;
}

function clearFields() {
  chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]):not([type="checkbox"]):not([type="radio"]), textarea').forEach(el => {
          el.value = '';
          el.dispatchEvent(new Event('input', { bubbles: true }));
        });
        document.querySelectorAll('input[type="checkbox"]').forEach(el => { el.checked = false; });
        document.querySelectorAll('select').forEach(el => { el.selectedIndex = 0; });
      }
    });
  });
  showStatus('fill-status', 'Fields cleared.', 'info');
}

// ── Upload & fill PDF ─────────────────────────────────────────────────────

async function doUploadFill() {
  if (!selectedProfileId) { showStatus('pdf-status', 'Select a profile first.', 'error'); return; }
  const fileInput = document.getElementById('pdf-file-input');
  const file = fileInput.files[0];
  if (!file) { showStatus('pdf-status', 'Choose a PDF file.', 'error'); return; }
  showStatus('pdf-status', 'Uploading & filling…', 'info');

  try {
    // Get profile data
    const pRes = await apiFetch(`/api/v1/profiles/${selectedProfileId}`);
    const profile = await pRes.json();

    const templateId = document.getElementById('template-select').value;

    let downloadUrl;
    if (templateId) {
      // Use template batch fill
      const fd = new FormData();
      fd.append('records', JSON.stringify([profile.data || profile]));
      const r = await apiFetch(`/api/v1/templates/${templateId}/fill`, { method: 'POST', body: fd });
      const result = await r.json();
      downloadUrl = result.download_url || result.outputs?.[0]?.url;
    } else {
      // Use free-form fill
      const fd = new FormData();
      fd.append('pdf', file);
      fd.append('fill_data', JSON.stringify(profile.data || profile));
      const r = await apiFetch('/api/v1/fill', { method: 'POST', body: fd });
      const result = await r.json();
      downloadUrl = result.download_url;
    }

    if (downloadUrl) {
      const base = await baseUrl();
      const key  = await apiKey();
      const dlRes = await fetch(`${base}${downloadUrl}`, { headers: { 'X-API-Key': key } });
      const blob = await dlRes.blob();
      const url = URL.createObjectURL(blob);
      chrome.downloads.download({ url, filename: `filled_${file.name}`, saveAs: true });
      showStatus('pdf-status', '✔ Download started!', 'success');
    } else {
      showStatus('pdf-status', 'No download URL returned.', 'error');
    }
  } catch (e) {
    showStatus('pdf-status', e.message, 'error');
  }
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
