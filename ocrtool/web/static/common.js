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
  skipped: '', copied: 'good', failed: 'bad', cancelled: 'warn',
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

/* ------------------------------------------------------------------- trees

   A case folder is organised by its folders, so a flat list of a/b/c.pdf paths
   is the wrong shape to show one in — the folder names are the thing you
   navigate by. Both the folder preview and the live run list render the same
   node shape through here:

     { name, path, files, pages, folders: [...], documents: [...] }

   Folders start closed and carry a recursive count, so the tree is useful
   before you open anything. */

/* Sort the way a person numbering exhibits meant, so 2 comes before 10 rather
   than after 11. Matches natural_key() on the server; the two orders have to
   agree or the preview and the run list disagree about the same folder. */
function naturalCompare(a, b) {
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

/* Build the node shape above from flat entries carrying a `path`. */
function buildTree(entries) {
  const root = { name: '', path: '', files: 0, pages: 0, folders: new Map(), documents: [] };
  for (const entry of entries) {
    const parts = String(entry.path).split('/');
    let node = root;
    node.files += 1;
    node.pages += entry.pages || 0;
    const walked = [];
    for (const folder of parts.slice(0, -1)) {
      walked.push(folder);
      if (!node.folders.has(folder)) {
        node.folders.set(folder, {
          name: folder, path: walked.join('/'), files: 0, pages: 0, folders: new Map(), documents: [],
        });
      }
      node = node.folders.get(folder);
      node.files += 1;
      node.pages += entry.pages || 0;
    }
    node.documents.push({ ...entry, name: parts[parts.length - 1] });
  }
  return finishTree(root);
}

function finishTree(node) {
  node.folders = Array.from(node.folders.values())
    .map(finishTree)
    .sort((a, b) => naturalCompare(a.name, b.name));
  node.documents.sort((a, b) => naturalCompare(a.name, b.name));
  return node;
}

/* Which folders the reader has opened, by path. Kept outside the render so the
   tree can be redrawn on every page event — which the run page does — without
   closing what somebody is reading. */
function makeTreeState(open = new Set()) {
  return open;
}

/* `document` renders one file row; `folderNote` adds a line beside a folder's
   counts. Depth is passed so the caller can indent without measuring the DOM. */
function renderTree(node, { open, document: renderDocument, folderNote = null, onToggle = null }) {
  const rows = [];
  const walk = (current, depth) => {
    for (const folder of current.folders) {
      const isOpen = open.has(folder.path);
      const note = folderNote ? folderNote(folder) : null;
      rows.push(el('div', {
        class: `tree-folder${isOpen ? ' open' : ''}`,
        style: `padding-left:${depth * 18}px`,
        onclick: () => {
          if (isOpen) open.delete(folder.path); else open.add(folder.path);
          if (onToggle) onToggle();
        },
      },
        el('span', { class: 'twist' }, isOpen ? '▼' : '▶'),
        el('span', { class: 'tree-name' }, folder.name),
        el('span', { class: 'tree-count' },
          `${num(folder.files)} file${folder.files === 1 ? '' : 's'} · ${num(folder.pages)} pages`),
        note));
      if (isOpen) walk(folder, depth + 1);
    }
    for (const item of current.documents) {
      rows.push(el('div', {
        class: 'tree-file',
        style: `padding-left:${depth * 18 + 20}px`,
      }, renderDocument(item)));
    }
  };
  walk(node, 0);
  if (!rows.length) rows.push(el('div', { class: 'empty' }, 'nothing here'));
  return rows;
}
