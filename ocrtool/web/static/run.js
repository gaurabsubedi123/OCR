/* The live run page.
   Everything on this page is driven by events from the server, and every
   number on it is a count of something that has actually happened. */

const { run_id: runId } = JSON.parse($('#bootstrap').textContent);

const files = new Map();          // file index -> summary row
const pagesSeen = new Map();      // file index -> pages finished so far
let finished = false;
let lastSnapshot = null;

/* ------------------------------------------------------------- rendering */

function renderHead(run) {
  $('#top-status').replaceChildren(statusPill(run.status));
  const running = ['running', 'discovering', 'pending'].includes(run.status);
  $('#title').textContent = running ? 'Reading documents' : `Run ${run.status}`;
  $('#paths').replaceChildren(
    el('span', { class: 'dim' }, 'from '), el('span', { class: 'mono' }, run.input_dir || ''),
    el('br'),
    el('span', { class: 'dim' }, 'into '), el('span', { class: 'mono' }, run.output_dir || ''),
  );
  $('#stop').classList.toggle('hidden', !running);
  $('#csv-link').href = `/files/${runId}/_runs/${runId}/pages.csv?download=1`;
  $('#csv-link').classList.toggle('hidden', running);
}

function renderStats(run) {
  const t = run.totals || {};
  const cards = [
    ['pages read', num(t.pages_done), `of ${num(t.pages_total)}`, ''],
    ['documents', num(t.files_done), `of ${num(t.files_total)}`, ''],
    ['from text layers', num(t.pages_text_layer), 'no OCR needed', ''],
    ['flagged', num(t.pages_flagged), 'worth a look', 'flagged'],
    ['failed', num(t.pages_failed), 'unreadable pages', t.pages_failed ? 'failed' : ''],
    ['speed', `${(run.pages_per_second || 0).toFixed(2)}`, 'pages per second', ''],
    ['elapsed', duration(run.elapsed_s), run.eta_s ? `about ${duration(run.eta_s)} left` : '', ''],
  ];
  $('#stats').replaceChildren(...cards.map(([label, value, sub, cls]) =>
    el('div', { class: `stat ${cls}` },
      el('div', { class: 'value' }, value),
      el('div', { class: 'label' }, label),
      el('div', { class: 'sub' }, sub))));
}

function renderBar(run) {
  const t = run.totals || {};
  const done = t.pages_done || 0;
  const total = t.pages_total || 0;
  const percent = total ? Math.min(100, (done / total) * 100) : 0;
  $('#bar').classList.toggle('done', finished);
  $('#bar i').style.width = `${percent}%`;
  $('#bar-left').textContent = total
    ? `${num(done)} of ${num(total)} pages · ${percent.toFixed(percent < 10 ? 1 : 0)}%`
    : 'counting the pages in the folder…';
  $('#bar-right').textContent = finished
    ? `finished in ${duration(run.elapsed_s)}`
    : (run.eta_s ? `about ${duration(run.eta_s)} left` : '');

  const inflight = run.in_flight || [];
  $('#inflight').replaceChildren(...inflight.map((label) => el('span', {}, label)));
}

function fileRow(summary, index) {
  const seen = pagesSeen.get(index) || summary.pages_done || 0;
  const flagged = (summary.flagged_pages || []).length;
  const outputs = Object.entries(summary.outputs || {});
  return el('tr', { class: 'clickable', onclick: () => { window.location.href = `/runs/${runId}/documents/${index}`; } },
    el('td', { class: 'path' }, summary.relpath,
      summary.error ? el('div', { class: 'dim', style: 'color:var(--bad)' }, summary.error) : null),
    el('td', {}, statusPill(summary.status)),
    el('td', { class: 'num' }, `${num(seen)}/${num(summary.page_count)}`),
    el('td', { class: 'num' }, flagged ? el('span', { class: 'pill warn' }, num(flagged)) : el('span', { class: 'dim' }, '—')),
    el('td', { class: 'num' }, summary.mean_confidence === null || summary.mean_confidence === undefined
      ? el('span', { class: 'dim' }, '—')
      : `${summary.mean_confidence.toFixed(0)}`),
    el('td', {}, outputs.length
      ? outputs.map(([kind, rel]) => el('a', {
          href: `/files/${runId}/${encodeURI(rel)}${kind === 'pdf' ? '' : '?download=1'}`,
          target: '_blank', class: 'mono', style: 'margin-right:8px',
          onclick: (e) => e.stopPropagation(),
        }, kind))
      : el('span', { class: 'dim' }, '—')));
}

function renderFiles() {
  const body = $('#files');
  const rows = [];
  const sorted = Array.from(files.entries()).sort((a, b) => a[0] - b[0]);
  for (const [index, summary] of sorted) rows.push(fileRow(summary, index));
  body.replaceChildren(...rows);
  const done = sorted.filter(([, s]) => s.status === 'done').length;
  $('#files-hint').textContent = `${num(done)} of ${num(sorted.length)} written`;
}

const logLines = [];
function addLog(node) {
  logLines.unshift(node);
  if (logLines.length > 400) logLines.pop();
  $('#log').replaceChildren(...logLines);
}

function logPage(event) {
  const bits = [`${event.file} · p${event.page}`];
  let cls = 'ok';
  if (event.source === 'text-layer') { bits.push('text layer, no OCR needed'); cls = 'layer'; }
  else if (event.source === 'failed') { bits.push(event.error || 'failed'); cls = 'err'; }
  else bits.push(`${event.chars} chars · confidence ${event.confidence === null ? '—' : Math.round(event.confidence)}`);
  if (event.needs_review) { bits.push(`⚑ ${event.review_reason}`); cls = 'flag'; }
  bits.push(`${(event.ms / 1000).toFixed(1)}s`);
  addLog(el('div', { class: cls }, bits.join('  ·  ')));
}

/* ---------------------------------------------------------------- events */

function applySnapshot(run) {
  lastSnapshot = run;
  finished = !['running', 'discovering', 'pending'].includes(run.status);
  if (run.files) {
    run.files.forEach((summary, index) => {
      files.set(index, summary);
      if (summary.pages_done) pagesSeen.set(index, summary.pages_done);
    });
    renderFiles();
  }
  renderHead(run);
  renderStats(run);
  renderBar(run);
  renderNote(run);
}

function renderNote(run) {
  const note = $('#run-note');
  const t = run.totals || {};
  if (!finished) {
    note.textContent = t.pages_total
      ? 'Results appear here as each document finishes — you can open one before the run ends.'
      : 'Counting pages before starting, so the progress below is a real fraction.';
    return;
  }
  const parts = [`${num(t.pages_done)} pages from ${num(t.files_total)} documents in ${duration(run.elapsed_s)}.`];
  if (t.pages_flagged) parts.push(`${num(t.pages_flagged)} pages are flagged — open a document to see them.`);
  if (t.pages_failed) parts.push(`${num(t.pages_failed)} pages could not be read.`);
  parts.push(`Everything is in ${run.output_dir}`);
  note.textContent = parts.join(' ');
}

function handle(event) {
  if (event.type === 'snapshot') { applySnapshot(event.run); return; }
  if (event.type === 'progress') { applySnapshot({ ...lastSnapshot, ...event.run }); return; }
  if (event.type === 'discovered') {
    event.files.forEach((summary, index) => files.set(index, summary));
    renderFiles();
    return;
  }
  if (event.type === 'page') {
    pagesSeen.set(event.file_index, (pagesSeen.get(event.file_index) || 0) + 1);
    logPage(event);
    renderFiles();
    return;
  }
  if (event.type === 'file') {
    const index = Array.from(files.entries()).find(([, s]) => s.relpath === event.summary.relpath)?.[0];
    if (index !== undefined) files.set(index, event.summary);
    renderFiles();
    return;
  }
  if (event.type === 'phase' || event.type === 'log') {
    addLog(el('div', { class: event.level === 'warn' ? 'flag' : 'ok' }, event.message));
    return;
  }
  if (event.type === 'done') {
    applySnapshot(event.run);
    addLog(el('div', { class: 'layer' }, `Finished: ${event.run.status}`));
  }
}

function connect() {
  const source = new EventSource(`/api/runs/${runId}/events`);
  source.onmessage = (message) => {
    try { handle(JSON.parse(message.data)); } catch { /* ignore a malformed frame */ }
  };
  source.addEventListener('closed', () => source.close());
  source.onerror = () => {
    source.close();
    // The stream ends when the run does; fall back to the snapshot so a
    // finished run still shows its result, and retry while one is live.
    getJSON(`/api/runs/${runId}`).then((run) => {
      applySnapshot(run);
      if (!finished) setTimeout(connect, 2000);
    }).catch(() => setTimeout(connect, 4000));
  };
}

$('#stop').addEventListener('click', async () => {
  $('#stop').disabled = true;
  $('#stop').textContent = 'Stopping…';
  try { await postJSON(`/api/runs/${runId}/cancel`); } catch (err) { addLog(el('div', { class: 'err' }, String(err.message))); }
});

/* ---------------------------------------------------------------- search */

let searchTimer = null;
$('#search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 300);
});

async function runSearch() {
  const query = $('#search').value.trim();
  const host = $('#hits');
  if (query.length < 2) { host.replaceChildren(); return; }
  host.replaceChildren(el('div', { class: 'empty' }, 'searching…'));
  let data;
  try { data = await getJSON(`/api/runs/${runId}/search?q=${encodeURIComponent(query)}`); }
  catch (err) { host.replaceChildren(el('div', { class: 'notice bad' }, String(err.message))); return; }

  if (!data.hits.length) {
    host.replaceChildren(el('div', { class: 'empty' }, `No page contains “${query}” yet.`));
    return;
  }
  const nodes = data.hits.map((hit) => el('div', {
    class: 'hit',
    onclick: () => { window.location.href = `/runs/${runId}/documents/${hit.file_index}?page=${hit.page}&q=${encodeURIComponent(query)}`; },
  },
    el('div', { class: 'where' }, `${hit.file} · page ${hit.page}${hit.source === 'text-layer' ? ' · text layer' : ''}`),
    el('div', {}, hit.snippet.before, el('mark', {}, hit.snippet.match), hit.snippet.after)));
  if (data.truncated) nodes.push(el('div', { class: 'empty' }, 'showing the first 200 matches'));
  host.replaceChildren(...nodes);
}

getJSON(`/api/runs/${runId}`).then(applySnapshot).catch(() => {});
connect();
