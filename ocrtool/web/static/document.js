/* One document, page by page: the picture of the page beside the text that
   came off it. That pairing is the whole point of the page — a confident wrong
   reading and a correct one look identical until you can see the source. */

const boot = JSON.parse($('#bootstrap').textContent);
const params = new URLSearchParams(window.location.search);

let document_ = null;
let current = Number(params.get('page') || 1);
let needle = params.get('q') || '';

async function load() {
  let data;
  try {
    data = await getJSON(`/api/runs/${boot.run_id}/documents/${boot.index}`);
  } catch (err) {
    $('#doc-title').textContent = 'Not found';
    $('#doc-summary').textContent = String(err.message);
    return;
  }

  if (data.pending) {
    $('#doc-title').textContent = data.relpath;
    $('#doc-summary').textContent = 'Still being read — this page will refresh when the document is finished.';
    setTimeout(load, 3000);
    return;
  }

  document_ = data;
  renderHeader();
  renderPageList();
  showPage(current);
  if (needle) { $('#find').value = needle; updateFindSummary(); }
}

function renderHeader() {
  const doc = document_;
  $('#doc-title').textContent = doc.relpath;
  const flagged = (doc.flagged_pages || []).length;
  const parts = [
    `${num(doc.page_count)} pages`,
    `${num(doc.text_layer_pages)} from the text layer`,
    `${num(doc.ocr_pages)} OCR'd`,
  ];
  if (doc.mean_confidence !== null && doc.mean_confidence !== undefined) {
    parts.push(`average confidence ${doc.mean_confidence.toFixed(0)}`);
  }
  if (flagged) parts.push(`${num(flagged)} flagged`);
  $('#doc-summary').textContent = parts.join(' · ');
  $('#doc-meta').replaceChildren(statusPill(doc.status));

  const links = Object.entries(doc.outputs || {}).map(([kind, rel]) =>
    el('a', {
      class: 'button small', style: 'margin-left:6px',
      href: `/files/${boot.run_id}/${encodeURI(rel)}${kind === 'pdf' ? '' : '?download=1'}`,
      target: '_blank',
    }, kind === 'pdf' ? 'searchable PDF' : `.${kind}`));
  $('#downloads').replaceChildren(...links);
}

function renderPageList() {
  const rows = document_.pages.map((page) => el('div', {
    class: `p ${page.page === current ? 'active' : ''}`,
    'data-page': page.page,
    onclick: () => showPage(page.page),
  },
    page.preview
      ? el('img', { src: `/files/${boot.run_id}/${encodeURI(page.preview)}`, loading: 'lazy', alt: '' })
      : el('div', { class: 'dim', style: 'width:34px;height:44px' }),
    el('div', {},
      el('div', {}, `Page ${page.page}`),
      el('div', { class: 'dim', style: 'font-size:11.5px' },
        page.source === 'text-layer' ? 'text layer'
          : page.source === 'failed' ? 'failed'
          : `conf ${page.confidence === null ? '—' : Math.round(page.confidence)}`)),
    page.needs_review ? el('div', { class: 'flagdot', title: page.review_reason }) : null));
  $('#pagelist').replaceChildren(...rows);
}

function showPage(number) {
  const page = document_.pages.find((p) => p.page === number) || document_.pages[0];
  if (!page) return;
  current = page.page;

  $$('.pagelist .p').forEach((row) => row.classList.toggle('active', Number(row.dataset.page) === current));
  const active = $(`.pagelist .p[data-page="${current}"]`);
  if (active) active.scrollIntoView({ block: 'nearest' });

  const meta = [
    el('span', { class: `pill ${page.source === 'text-layer' ? 'good' : page.source === 'failed' ? 'bad' : ''}` },
      page.source === 'text-layer' ? 'read from the PDF text layer' : page.source === 'failed' ? 'could not be read' : 'read by OCR'),
    el('span', { class: 'dim', style: 'margin-left:10px' },
      `${num(page.chars)} characters · ${num(page.words)} words · ${(page.duration_ms / 1000).toFixed(1)}s`),
  ];
  if (page.confidence !== null && page.confidence !== undefined) {
    meta.push(el('span', { class: 'dim', style: 'margin-left:10px' }, `confidence ${page.confidence.toFixed(1)}`));
  }
  if (page.skew_corrected) {
    meta.push(el('span', { class: 'dim', style: 'margin-left:10px' }, `straightened ${page.skew_corrected}°`));
  }
  const notes = [];
  if (page.needs_review) notes.push(el('div', { class: 'notice warn mt' }, `⚑ ${page.review_reason}`));
  if (page.error) notes.push(el('div', { class: 'notice bad mt' }, page.error));
  if ((page.low_confidence_words || []).length) {
    notes.push(el('div', { class: 'soft mt', style: 'font-size:13px' },
      `${page.low_confidence_words.length} word(s) OCR was unsure of are marked in the text.`));
  }
  $('#page-meta').replaceChildren(el('div', {}, ...meta), ...notes);

  $('#page-image').replaceChildren(page.preview
    ? el('img', { src: `/files/${boot.run_id}/${encodeURI(page.preview)}`, alt: `page ${page.page}` })
    : el('div', { class: 'empty' }, 'No page picture was saved for this run.'));

  const text = $('#page-text');
  text.replaceChildren(page.text
    ? renderText(page.text, $('#find').value.trim(), page.low_confidence_words)
    : el('span', { class: 'dim' }, 'No text came off this page.'));
  text.scrollTop = 0;
}

/* ------------------------------------------------------------- find */

let findTimer = null;
$('#find').addEventListener('input', () => {
  clearTimeout(findTimer);
  findTimer = setTimeout(() => { updateFindSummary(); showPage(current); }, 250);
});

function updateFindSummary() {
  const query = $('#find').value.trim().toLowerCase();
  const summary = $('#find-summary');
  if (query.length < 2 || !document_) { summary.textContent = ''; renderPageList(); return; }

  const hits = document_.pages
    .map((page) => ({ page: page.page, count: (page.text || '').toLowerCase().split(query).length - 1 }))
    .filter((hit) => hit.count > 0);

  if (!hits.length) { summary.textContent = `“${query}” is not on any page of this document.`; return; }
  const total = hits.reduce((sum, hit) => sum + hit.count, 0);
  summary.replaceChildren(
    document.createTextNode(`${total} match(es) on ${hits.length} page(s): `),
    ...hits.map((hit) => el('a', {
      href: '#', style: 'margin-right:8px',
      onclick: (e) => { e.preventDefault(); showPage(hit.page); },
    }, `p${hit.page} (${hit.count})`)));
  if (hits.length && !hits.some((hit) => hit.page === current)) showPage(hits[0].page);
}

/* Arrow keys move through pages, which is how anyone reads a scan. */
window.addEventListener('keydown', (event) => {
  if (event.target.tagName === 'INPUT') return;
  if (event.key === 'ArrowRight' || event.key === 'j') showPage(Math.min(current + 1, document_.pages.length));
  if (event.key === 'ArrowLeft' || event.key === 'k') showPage(Math.max(current - 1, 1));
});

load();
