/* Helpers shared by all three pages. No framework, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

const num = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString());

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function bytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function whenLocal(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

async function getJSON(url) {
  const response = await fetch(url);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

const STATUS_CLASS = {
  done: 'good', running: 'live', discovering: 'live', pending: '',
  failed: 'bad', cancelled: 'warn',
};

function statusPill(status, extra = '') {
  return el('span', { class: `pill ${STATUS_CLASS[status] || ''}` }, status + (extra ? ` ${extra}` : ''));
}

/* Highlight every occurrence of `needle`, and mark the words OCR was least
   sure of. Both are done on text nodes only, so nothing can inject markup. */
function renderText(text, needle, doubtful) {
  const container = document.createDocumentFragment();
  const doubtfulSet = new Set((doubtful || []).filter((w) => w && w.length > 2));
  const pattern = needle && needle.length > 1
    ? new RegExp(`(${needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'ig')
    : null;

  for (const line of String(text || '').split('\n')) {
    const parts = pattern ? line.split(pattern) : [line];
    parts.forEach((part, index) => {
      if (pattern && index % 2 === 1) {
        container.append(el('mark', { class: 'hit' }, part));
        return;
      }
      if (doubtfulSet.size === 0) { container.append(document.createTextNode(part)); return; }
      for (const token of part.split(/(\s+)/)) {
        const bare = token.replace(/[^\w$.,/-]/g, '');
        if (bare && doubtfulSet.has(bare)) container.append(el('mark', { title: 'OCR was unsure of this word' }, token));
        else container.append(document.createTextNode(token));
      }
    });
    container.append(document.createTextNode('\n'));
  }
  return container;
}
