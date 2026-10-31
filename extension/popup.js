/**
 * Flowboard Bridge — Popup UI
 * Polls background status every 1.5 s and renders it.
 */

let _manualDisconnect = false;

function formatTokenAge(ms) {
  if (ms === null || ms === undefined) return 'none';
  const s = Math.floor(ms / 1000);
  if (s < 60)   return `captured ${s} s ago`;
  if (s < 3600) return `captured ${Math.floor(s / 60)} m ago`;
  return `captured ${Math.floor(s / 3600)} h ago`;
}

// Plain-language fixes for the errors people actually hit.
const HINTS = [
  [/CAPTCHA/i,               'Flow could not verify the browser. Click "Open Flow tab", keep it in front, and try again.'],
  [/NO_FLOW_TAB|NO_AT_TOKEN/i, 'No signed-in Flow tab. Click "Open Flow tab" and sign in to flow.google.com.'],
  [/NO_INJECTION/i,          'The Flow tab was busy or reloading. Wait a few seconds and retry.'],
  [/QUOTA/i,                 'Your Flow credits or rate limit are used up. Wait for them to reset, then retry.'],
  [/MODEL_ACCESS_DENIED/i,   'Your Flow plan cannot use that video model. Pick a lower quality (Lite) in Studio.'],
  [/UNSAFE|FILTER/i,         'Flow blocked that prompt. Reword it and try again.'],
];
const RPC_NAMES = { ogiZ0b: 'Image generation', eb1hJf: 'Video generation', MZZa6b: 'Reference video', maseQ: 'Upload image' };
function hintFor(err) {
  for (const [re, text] of HINTS) if (re.test(err || '')) return text;
  return '';
}

function setCheck(id, ok, text) {
  const el = document.getElementById(id);
  el.className = ok ? 'ok' : 'no';
  el.textContent = (ok ? '✓ ' : '○ ') + text;
}

async function renderChecklist(status) {
  let tabOpen = false;
  try { tabOpen = (await chrome.tabs.query({ url: ['https://flow.google.com/*'] })).length > 0; } catch {}
  setCheck('ck-agent', !!status.connected && !status.manualDisconnect, 'Flowboard app running');
  setCheck('ck-tab', tabOpen, 'Flow tab open (flow.google.com)');
  setCheck('ck-proj', !!status.project, status.project ? `Project …${status.project.slice(-8)} (auto)` : 'Open a project in your Flow tab');
  setCheck('ck-key', !!status.flowKeyPresent || (tabOpen && !!status.connected), 'Signed in to Flow');
}

function renderRecent(log) {
  const box = document.getElementById('recent');
  box.textContent = '';
  const rows = (log || []).slice(0, 8);
  if (!rows.length) { box.innerHTML = '<div class="row"><span class="t">none yet</span></div>'; return; }
  for (const e of rows) {
    const row = document.createElement('div'); row.className = 'row';
    const t = document.createElement('span'); t.className = 't';
    t.textContent = RPC_NAMES[e.url] || e.label || e.url || e.method || e.id;
    t.title = e.error || '';
    const s = document.createElement('span');
    const st = e.status === 'failed' ? 'failed' : (e.status === 'done' || e.status === 'success') ? 'done' : 'run';
    s.className = st; s.textContent = st === 'done' ? '✓' : st === 'failed' ? '✗' : '…';
    row.append(t, s); box.appendChild(row);
  }
}

function render(status) {
  if (!status) return;

  _manualDisconnect = status.manualDisconnect;

  // Status dot
  const dotEl = document.getElementById('status-dot');
  if (status.manualDisconnect || !status.connected) {
    dotEl.className = 'section-value dot-offline';
    dotEl.textContent = '○ offline';
  } else if (status.state === 'running') {
    dotEl.className = 'section-value dot-running';
    dotEl.textContent = '▶ running';
  } else {
    dotEl.className = 'section-value dot-connected';
    dotEl.textContent = '● connected';
  }

  // Token
  document.getElementById('token-row').textContent =
    status.flowKeyPresent ? formatTokenAge(status.tokenAge) : 'none';

  // Stats
  const m = status.metrics || {};
  document.getElementById('stats-row').textContent =
    `${m.requestCount || 0} · ✓ ${m.successCount || 0} · ✗ ${m.failedCount || 0}`;

  renderChecklist(status);
  chrome.runtime.sendMessage({ type: 'REQUEST_LOG' }, (r) => { if (!chrome.runtime.lastError) renderRecent(r?.log); });
  const hint = document.getElementById('hint');
  const hintText = hintFor(m.lastError);
  hint.style.display = hintText ? 'block' : 'none';
  hint.textContent = hintText;

  // Error
  const errSection = document.getElementById('error-section');
  const errRow     = document.getElementById('error-row');
  if (m.lastError) {
    errSection.style.display = 'flex';
    errRow.textContent = m.lastError;
  } else {
    errSection.style.display = 'none';
  }

  // Toggle button
  const btn = document.getElementById('btn-toggle');
  if (status.manualDisconnect) {
    btn.textContent = 'Reconnect';
    btn.className   = 'reconnect';
  } else {
    btn.textContent = 'Disconnect';
    btn.className   = 'disconnect';
  }
}

function fetchStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS' }, (reply) => {
    if (chrome.runtime.lastError) return;
    render(reply);
  });
}

// Initial fetch + poll
fetchStatus();
setInterval(fetchStatus, 1500);

// Re-render on push from background
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'STATUS_PUSH') fetchStatus();
});

// Buttons
document.getElementById('btn-flow-tab').addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'OPEN_FLOW_TAB' });
});

document.getElementById('btn-studio').addEventListener('click', () => {
  chrome.tabs.create({ url: 'http://localhost:5173/?studio=1' });
});

document.getElementById('btn-refresh').addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'REFRESH_TOKEN' });
});

document.getElementById('btn-toggle').addEventListener('click', () => {
  const type = _manualDisconnect ? 'RECONNECT' : 'DISCONNECT';
  chrome.runtime.sendMessage({ type }, () => {
    if (chrome.runtime.lastError) return;
    fetchStatus();
  });
});
