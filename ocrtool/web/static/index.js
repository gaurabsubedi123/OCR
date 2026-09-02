/* The start page: pick a folder, see what is in it, start a run. */

const boot = JSON.parse($('#bootstrap').textContent);
let stagedUpload = null;      // path the upload was written to, once uploaded
let folderCheck = null;       // last successful folder inspection

/* ---------------------------------------------------------------- tabs */

$$('.tab').forEach((tab) => tab.addEventListener('click', () => {
  $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
  $('#tab-folder').classList.toggle('hidden', tab.dataset.tab !== 'folder');
  $('#tab-upload').classList.toggle('hidden', tab.dataset.tab !== 'upload');
  refreshReadiness();
}));

/* ------------------------------------------------------------- browsing */

const browser = $('#browser');
$('#browse-toggle').addEventListener('click', () => {
  browser.classList.toggle('hidden');
  if (!browser.classList.contains('hidden')) openFolder($('#input-dir').value || boot.home);
});

async function openFolder(path) {
  browser.replaceChildren(el('div', { class: 'entry up' }, 'loading…'));
  let data;
  try {
    data = await getJSON(`/api/browse?path=${encodeURIComponent(path)}`);
  } catch (err) {
    browser.replaceChildren(el('div', { class: 'entry up' }, String(err.message)));
    return;
  }
  const rows = [
    el('div', { class: 'entry up', onclick: () => { $('#input-dir').value = data.path; checkFolder(); } },
      `✓ use this folder — ${data.path}`),
  ];
  if (data.parent) rows.push(el('div', { class: 'entry up', onclick: () => openFolder(data.parent) }, '↑ ..'));
  for (const folder of data.folders) {
    rows.push(el('div', { class: 'entry', onclick: () => openFolder(folder.path) }, `📁 ${folder.name}`));
  }
  if (!data.folders.length) rows.push(el('div', { class: 'entry up' }, `no subfolders · ${data.file_count} files here`));
  browser.replaceChildren(...rows);
}

/* -------------------------------------------------- inspecting a folder */

let checkTimer = null;
$('#input-dir').addEventListener('input', () => {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(checkFolder, 400);
});

async function checkFolder() {
  const path = $('#input-dir').value.trim();
  const report = $('#folder-report');
  folderCheck = null;
  if (!path) { report.replaceChildren(); refreshReadiness(); return; }

  report.replaceChildren(el('div', { class: 'soft', style: 'font-size:13.5px' }, 'Looking…'));
  let data;
  try {
    // POSTed with the whole form: whether a document counts as already read
    // depends on the settings the run would use, so the preview has to ask the
    // same question the run will.
    data = await postJSON('/api/preview-folder', { ...settingsPayload(), path });
  } catch (err) {
    report.replaceChildren(el('div', { class: 'notice bad' }, String(err.message)));
    refreshReadiness();
    return;
  }

  folderCheck = data;
  const nodes = [];
  if (!data.files) {
    nodes.push(el('div', { class: 'notice warn' }, 'No PDFs or images in that folder.'));
  } else {
    nodes.push(el('div', { class: 'notice good' }, headline(data)));
    nodes.push(el('div', { class: 'tree scroll' }, ...previewTree(data)));
  }
  if (data.unreadable && data.unreadable.length) {
    nodes.push(el('div', { class: 'notice warn mt' },
      `${data.unreadable.length} file(s) could not be opened and will be reported as failures: ` +
      data.unreadable.slice(0, 3).map((u) => u.file).join(', ')));
  }
  report.replaceChildren(...nodes);
  refreshReadiness();
}

function headline(data) {
  const bits = [`${num(data.files)} documents · ${num(data.pages)} pages`];
  // Only worth saying when the folder is not simply all-new, which is what it
  // is the first time and the only time the distinction means nothing.
  if (data.done) {
    bits.push(data.new
      ? `${num(data.new)} new, ${num(data.done)} already read`
      : 'all already read — nothing to do');
  }
  return bits.join(' · ');
}

const previewOpen = makeTreeState();

function previewTree(data) {
  return renderTree(data.tree, {
    open: previewOpen,
    onToggle: () => {
      $('#folder-report .tree').replaceChildren(...previewTree(folderCheck));
    },
    folderNote: (folder) => (folder.done && folder.new
      ? el('span', { class: 'tree-state new' }, `${num(folder.new)} new`)
      : null),
    document: (item) => [
      el('span', { class: 'tree-name' }, item.name),
      el('span', { class: 'tree-count' }, `${num(item.pages)} pages · ${bytes(item.bytes)}`),
      item.error
        ? el('span', { class: 'tree-state bad' }, 'cannot open')
        : (item.state === 'done'
            ? el('span', { class: 'tree-state done', title: `read by run ${item.done_in}` }, 'already read')
            : (data.done ? el('span', { class: 'tree-state new' }, 'new') : null)),
    ],
  });
}

/* ------------------------------------------------------------ uploading */

const dropzone = $('#dropzone');
['dragenter', 'dragover'].forEach((event) => dropzone.addEventListener(event, (e) => {
  e.preventDefault(); dropzone.classList.add('hover');
}));
['dragleave', 'drop'].forEach((event) => dropzone.addEventListener(event, (e) => {
  e.preventDefault(); dropzone.classList.remove('hover');
}));
dropzone.addEventListener('drop', (e) => upload(Array.from(e.dataTransfer.files)));
$('#pick-files').addEventListener('click', () => $('#file-input').click());
$('#pick-folder').addEventListener('click', () => $('#folder-input').click());
$('#file-input').addEventListener('change', (e) => upload(Array.from(e.target.files)));
$('#folder-input').addEventListener('change', (e) => upload(Array.from(e.target.files)));

async function upload(files) {
  const report = $('#upload-report');
  const output = $('#output-dir').value.trim();
  if (!output) {
    report.replaceChildren(el('div', { class: 'notice bad' }, 'Set the output folder first — uploads are staged inside it.'));
    return;
  }
  if (!files.length) return;

  const total = files.reduce((sum, f) => sum + f.size, 0);
  report.replaceChildren(el('div', { class: 'notice info' },
    `Copying ${num(files.length)} files (${bytes(total)}) into the output folder…`));

  const form = new FormData();
  for (const file of files) form.append('files', file, file.webkitRelativePath || file.name);
  form.append('output_dir', output);

  let data;
  try {
    const response = await fetch('/api/upload', { method: 'POST', body: form });
    data = await response.json();
    if (!response.ok) throw new Error(data.error || 'upload failed');
  } catch (err) {
    report.replaceChildren(el('div', { class: 'notice bad' }, String(err.message)));
    return;
  }

  stagedUpload = data.path;
  report.replaceChildren(
    el('div', { class: 'notice good' }, `${num(data.files)} files copied (${bytes(data.bytes)})`),
    el('div', { class: 'mono dim mt' }, data.path),
  );
  refreshReadiness();
}

/* ------------------------------------------------------- start the run */

/* Everything the form holds. The preview and the run both send this, so
   "already read" in the preview means what it will mean when the run starts —
   the answer depends on the recipe, and a preview built from other settings
   would be a different question answered confidently. */
function settingsPayload() {
  return {
    output_dir: $('#output-dir').value.trim(),
    work_dir: $('#work-dir').value.trim(),
    dpi: Number($('#dpi').value),
    lang: $('#lang').value,
    psm: Number($('#psm').value),
    workers: Number($('#workers').value),
    min_confidence: Number($('#min-confidence').value),
    force_ocr: $('#force-ocr').checked,
    deskew: $('#deskew').checked,
    orient: $('#orient').checked,
    denoise: $('#denoise').checked,
    recursive: $('#recursive').checked,
    write_pdf: $('#write-pdf').checked,
    write_txt: $('#write-txt').checked,
    write_json: $('#write-json').checked,
    write_previews: $('#write-previews').checked,
    pdf_keeps_source_image: $('#pdf-source-image').checked,
    txt_keeps_layout: $('#txt-layout').checked,
    output_layout: $('#output-layout').value,
    duplicates: $('#duplicates').value,
    skip_already_done: $('#skip-done').checked,
  };
}


function activeInput() {
  const onFolderTab = !$('#tab-folder').classList.contains('hidden');
  if (onFolderTab) return folderCheck && folderCheck.files ? folderCheck.path : null;
  return stagedUpload;
}

function refreshReadiness() {
  const ready = Boolean(activeInput()) && Boolean($('#output-dir').value.trim());
  $('#start').disabled = !ready;

  const estimate = $('#estimate');
  const onFolderTab = !$('#tab-folder').classList.contains('hidden');
  if (onFolderTab && folderCheck && folderCheck.files) {
    // A rough figure, said to be rough. Real throughput depends on how many
    // pages carry a text layer, and the run measures it within the first minute.
    const workers = Math.max(1, Number($('#workers').value) || 1);
    const seconds = (folderCheck.pages * 1.4) / workers;
    estimate.replaceChildren(
      el('strong', {}, `${num(folderCheck.files)} documents, ${num(folderCheck.pages)} pages`),
      document.createTextNode(` — roughly ${duration(seconds)} at ${workers} pages at once. `),
      el('span', { class: 'dim' }, 'A real estimate replaces this within the first minute of the run.'),
    );
  } else if (!onFolderTab && stagedUpload) {
    estimate.textContent = 'Uploaded files are ready to read.';
  } else {
    estimate.textContent = 'Choose a folder to see what will be read.';
  }
}

['#output-dir', '#workers'].forEach((sel) => $(sel).addEventListener('input', refreshReadiness));
$('#recursive').addEventListener('change', checkFolder);

/* Which documents count as already read depends on where the results go and on
   the recipe, so the preview is asked again when any of that changes. */
let recheckTimer = null;
const recheck = () => { clearTimeout(recheckTimer); recheckTimer = setTimeout(checkFolder, 400); };
$('#output-dir').addEventListener('input', recheck);
['#work-dir', '#dpi', '#lang', '#psm', '#force-ocr', '#deskew', '#orient', '#denoise',
 '#write-pdf', '#write-txt', '#write-json', '#output-layout', '#skip-done',
].forEach((sel) => { const node = $(sel); if (node) node.addEventListener('change', recheck); });

$('#start').addEventListener('click', async () => {
  const button = $('#start');
  const error = $('#start-error');
  error.classList.add('hidden');
  button.disabled = true;
  button.textContent = 'Starting…';
  try {
    const { url } = await postJSON('/api/runs', {
      ...settingsPayload(),
      input_dir: activeInput(),
    });
    window.location.href = url;
  } catch (err) {
    error.textContent = String(err.message);
    error.classList.remove('hidden');
    button.disabled = false;
    button.textContent = 'Start reading';
  }
});

/* ---------------------------------------------------------- past runs */

function renderRecent(runs) {
  const host = $('#recent');
  if (!runs.length) {
    host.replaceChildren(el('div', { class: 'empty' }, 'Nothing has been read on this machine yet.'));
    return;
  }
  const table = el('table');
  table.append(el('tr', {},
    el('th', {}, 'run'), el('th', {}, 'status'), el('th', {}, 'input'),
    el('th', { class: 'num' }, 'files'), el('th', { class: 'num' }, 'pages'),
    el('th', { class: 'num' }, 'flagged'), el('th', { class: 'num' }, 'time')));

  for (const run of runs) {
    const totals = run.totals || {};
    table.append(el('tr', {
      class: 'clickable',
      onclick: () => { window.location.href = `/runs/${run.run_id}`; },
    },
      el('td', {}, el('div', {}, run.run_id), el('div', { class: 'dim', style: 'font-size:12px' }, whenLocal(run.created_at))),
      el('td', {}, statusPill(run.status || 'done')),
      el('td', { class: 'path' }, run.input_dir || '—'),
      el('td', { class: 'num' }, num(totals.files_total)),
      el('td', { class: 'num' }, num(totals.pages_done)),
      el('td', { class: 'num' }, totals.pages_flagged ? el('span', { class: 'pill warn' }, num(totals.pages_flagged)) : '—'),
      el('td', { class: 'num dim' }, duration(run.elapsed_s))));
  }
  host.replaceChildren(el('div', { class: 'scroll' }, table));
}

renderRecent(boot.recent || []);
if ((boot.recent || []).some((r) => r.live)) {
  setInterval(async () => {
    try { renderRecent((await getJSON('/api/runs')).runs); } catch { /* keep the last list */ }
  }, 4000);
}

if ($('#input-dir').value.trim()) checkFolder();
refreshReadiness();
