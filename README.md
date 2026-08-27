# ocrtool

Point it at a folder. It reads every PDF and image inside — page by page, on
your own machine, with nothing sent anywhere — and writes back a searchable
copy of each document, its text, and a per-page report saying how well each
page was read.

It does OCR and nothing else. It does not extract fields, summarise, classify,
or call a model over your documents.

```
  input/                          output/
    Ex 13/                          Ex 13/
      exhibit-01.pdf   ──────▶        exhibit-01.pdf     same pages, now searchable
      exhibit-02.pdf                  exhibit-01.txt     the text, page by page
    Ex 25 photos/                     exhibit-01.json    text + confidence + word positions
      IMG_4001.jpg                  Ex 25 photos/
                                      IMG_4001.txt
                                    _previews/           a picture of every page
                                    _runs/               what happened, and pages.csv
```

---

## Contents

- [What you get](#what-you-get)
- [Installing](#installing)
- [Installing tesseract](#installing-tesseract)
- [Using it in the browser](#using-it-in-the-browser)
- [Using it from the command line](#using-it-from-the-command-line)
- [Every option](#every-option)
- [How it decides things](#how-it-decides-things)
- [How fast it is](#how-fast-it-is)
- [Troubleshooting](#troubleshooting)
- [What it does not do](#what-it-does-not-do)
- [Development](#development)

---

## What you get

For every document, in a folder tree that mirrors your input exactly:

| File | What it is |
| --- | --- |
| `name.pdf` | The same pages, now with selectable, searchable text behind the page image. Pages that already had a text layer are copied through untouched. |
| `name.txt` | Plain text, with a `----- page 3 (ocr) -----` marker before each page saying where that page's text came from. |
| `name.json` | Per page: the text, tesseract's confidence, whether it was flagged, and every word with its position on the page image. |

And for the run as a whole:

| Path | What it is |
| --- | --- |
| `_previews/<document>/p0001.jpg` | A screen-sized picture of every page. This is what lets you check a result against the page it came from. |
| `_runs/<run-id>/pages.csv` | One row per page: source, confidence, characters, seconds, and why it was flagged. Sort by confidence to find what to check first. |
| `_runs/<run-id>/manifest.json` | The full record of the run: settings, timings, per-file results. |
| `_runs/<run-id>/events.jsonl` | What happened, in order, as it happened. |

Nothing in the output folder is required by anything else — the PDFs and text
files stand alone, and you can delete `_runs/` and `_previews/` once you have
what you need. (The browser viewer reads `_runs/`, so keep it while you are
still reviewing.)

---

## Installing

You need **Python 3.10 or newer** and the **tesseract** binary. Everything else
installs into a local virtual environment.

### With uv (recommended — fastest)

```bash
cd /path/to/ocr
uv venv                          # creates .venv
uv pip install -e ".[dev]"       # the tool plus its test dependencies
```

### With plain pip

```bash
cd /path/to/ocr
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Four dependencies get installed, all permissive-licensed: `pypdfium2` (renders
and assembles PDFs), `pillow` (images), `numpy` (deskew), and `Flask` (the local
web interface).

### Check the install

```bash
.venv/bin/ocrtool doctor
```

```
ocrtool     1.0.0
python      3.10.12
tesseract   tesseract 4.1.1
            /home/you/.local/opt/tesseract/usr/bin/tesseract
languages   eng, osd
pypdfium2   ok
PIL         ok
numpy       ok
flask       ok
workers     8 by default on this machine

ready
```

Anything that says `MISSING` or `NOT FOUND` is a thing to fix before running.

---

## Installing tesseract

This is the one piece that is not a Python package.

**Ubuntu / Debian / WSL**

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-eng
```

**macOS**

```bash
brew install tesseract
```

**Windows** — install [UB Mannheim's build](https://github.com/UB-Mannheim/tesseract/wiki),
then either tick "add to PATH" during setup or set `OCRTOOL_TESSERACT` to
`C:\Program Files\Tesseract-OCR\tesseract.exe`.

**Without root access.** Unpack the `.deb` into your home directory and point
the tool at it:

```bash
mkdir -p ~/.local/opt/tesseract && cd ~/.local/opt/tesseract
apt-get download tesseract-ocr tesseract-ocr-eng libtesseract4 libleptonica-dev
for f in *.deb; do dpkg-deb -x "$f" .; done
export OCRTOOL_TESSERACT=~/.local/opt/tesseract/usr/bin/tesseract
export TESSDATA_PREFIX=~/.local/opt/tesseract/usr/share/tesseract-ocr/4.00/tessdata
```

Put those two `export` lines in your `~/.bashrc` so they survive a new terminal.

**More languages.** `ocrtool doctor` lists what is installed. To add one,
install the matching package (`tesseract-ocr-spa`, `tesseract-ocr-fra`, …) or
drop the `.traineddata` file into your `tessdata` folder; it then appears in the
language menu in the browser and works as `--lang spa` on the command line.
Multiple languages at once use `+`, as in `--lang eng+spa`.

---

## Using it in the browser

```bash
.venv/bin/ocrtool ui
```

```
ocrtool 1.0.0
tesseract: tesseract 4.1.1

  Open http://127.0.0.1:5000

Ctrl-C to stop.
```

It listens on `127.0.0.1` — this machine only, not your network.

**1 — Where the documents are.** Either *Point at a folder*, which reads the
files where they already are and copies nothing, or *Upload files*, which
accepts a drag-and-drop, a multi-file pick, or a whole folder. As soon as a
folder path is entered the page tells you what it found: `12 documents · 2,063
pages`, the first dozen files, and any file that could not be opened. That count
comes from the same code the run uses, so it is a promise rather than a guess.

**2 — Where the results go.** Any folder; it is created if it does not exist.
The page states exactly what will be written into it.

**3 — How to read them.** The defaults suit scanned paper. *More options* opens
resolution, language, segmentation mode, which outputs to write, and whether to
straighten and despeckle.

Then **Start reading**, and the run page shows, live:

- **pages read** out of the total, as a real fraction with a progress bar
- **which pages are being read right now**, by name
- **speed** in pages per second, measured, and an estimate of the time left
- **every page as it finishes**, with its confidence and its flags
- **each document** as it is written, with links to its PDF, text, and JSON

You can open a finished document while the rest of the run continues, stop the
run at any point — anything already read is still written — and search the text
of everything read so far.

**Looking at a document.** Click any file. Each page shows the picture of the
page beside the text that came off it, the words tesseract was least sure of
marked in the text, and a flag on any page worth a second look. Arrow keys move
through pages. This pairing is the point: a confidently wrong reading and a
correct one are indistinguishable until you can see the page.

---

## Using it from the command line

Same engine, no browser:

```bash
.venv/bin/ocrtool run ~/Documents/case-file -o ~/Documents/case-file-ocr
```

```
Reading  /home/you/Documents/case-file
Writing  /home/you/Documents/case-file-ocr
Workers  8   dpi 300   lang eng

12 documents, 52 pages. Writing to /home/you/Documents/case-file-ocr
  done      Ex 13/exhibit-01.pdf  (6 pages)
  done      Ex 13/exhibit-02.pdf  (6 pages)
  ...
  done      Ex 25 photos/IMG_4001.jpg  (1 pages, 1 flagged)

Status    done
Files     12 written, 0 failed of 12
Pages     52 read (0 from text layers, 52 OCR'd, 0 failed)
Flagged   4 pages need a look
Time      11s
Output    /home/you/Documents/case-file-ocr
Report    /home/you/Documents/case-file-ocr/_runs/20260826-171850/pages.csv
```

Useful variations:

```bash
# only the top level of the folder, no subfolders
ocrtool run ./scans -o ./out --no-recursive

# text only, as fast as possible
ocrtool run ./scans -o ./out --no-pdf --no-json --no-previews

# a bad batch of faxes: lower the flag threshold so more pages get looked at
ocrtool run ./faxes -o ./out --min-confidence 80

# a page of a form that OCR keeps mangling
ocrtool run ./forms -o ./out --psm 6

# ignore text layers and OCR everything (rarely what you want)
ocrtool run ./mixed -o ./out --force-ocr
```

A run started on the command line is listed in the browser too — the output
folder is the record, and the UI reads it back.

---

## Every option

| Browser | Command line | Default | What it does |
| --- | --- | --- | --- |
| Resolution | `--dpi` | 300 | How finely pages are rendered before OCR. Below 200 accuracy drops sharply; above 400 costs time for nothing. Capped so an oversized page cannot become a 9,000-pixel render. |
| Language | `--lang` | `eng` | Any language installed in tesseract. Combine with `+`. |
| Pages at once | `--workers` | cores − 2, max 8 | How many pages are read in parallel. |
| Flag pages below | `--min-confidence` | 70 | Pages under this average word confidence are flagged for review. |
| OCR every page | `--force-ocr` | off | Ignore text layers PDFs already carry. |
| Straighten skewed scans | `--no-deskew` to disable | on | Corrects scanner skew up to 6°. |
| Remove scanner speckle | `--no-denoise` to disable | on | A 3×3 median filter. |
| Include subfolders | `--no-recursive` to disable | on | |
| Write searchable PDFs | `--no-pdf` to disable | on | |
| Write .txt files | `--no-txt` to disable | on | |
| Write .json files | `--no-json` to disable | on | |
| Save page pictures | `--no-previews` to disable | on | The browser viewer needs these. |
| Page segmentation mode | `--psm` | 3 | 3 automatic, 4 single column, 6 one block, 11/12 sparse text. |

`OCRTOOL_TESSERACT` overrides where the tesseract binary is found.

---

## How it decides things

**A PDF page that already has text is not OCR'd.** Born-digital PDFs carry their
text exactly; OCR of a picture of that page can only be worse. A page qualifies
when its text layer holds at least 100 letters and digits — not merely *some*
text, because scanned PDFs often carry a stray Bates stamp or fax header. On a
real 2,063-page case file this took nearly half the pages off the OCR queue with
no loss of quality.

**Pages are cleaned before OCR.** Grayscale, a median filter for speckle, a
light-end contrast stretch, and a projection-profile deskew that tries angles
between ±6° and keeps the one where text lines sit squarest. Anything past 6° is
a rotated page rather than a skewed one, and this method cannot tell those apart,
so it declines to guess.

**The contrast stretch clips only the light end**, and that detail matters. The
obvious version clips both ends, and on a page whose ink covers less than the
cutoff — a title page, a mostly-empty form, a photograph — the dark cut point
lands inside the background and drags it toward black. Measured on such a page
here: dark pixels went from 95k to 282k and tesseract returned *nothing at all*,
where the untouched image read cleanly at 95% confidence. There is a test for it.

**A page that comes back completely empty is tried once more** without its
resolution tag. Tesseract judges letter size from dpi, so a PDF whose declared
page size disagrees with its own content can make it reject every word. Rare,
but the failure is total and silent.

**The searchable PDF is assembled from two kinds of page.** OCR'd pages come
from tesseract, which writes the page image with its recognised text laid
invisibly on top. Text-layer pages are imported from the original PDF unchanged,
since they are already searchable and re-rendering would only lose fidelity.

**Flagging is deliberately blunt.** A page is flagged when its average word
confidence is below the threshold, or when it produced almost no text at all.
The second rule exists because a page that silently yields nothing looks exactly
like a page that worked. Flags are not failures: a photograph is correctly
flagged and correctly kept.

**Counts, not spinners.** Pages are counted before any work starts, so progress
is a real fraction from the first second, and the estimate of time remaining
comes from throughput actually measured during the run.

---

## How fast it is

Measured on this machine (8 workers, 300 dpi, letter-size pages):

- **~4.5 pages per second** on clean typed pages — a 52-page folder in 11
  seconds.
- **Pages with a usable text layer are effectively free**: they are read, not
  recognised.
- **Real scans are slower than clean test pages.** On a real 2,063-page legal
  case file, tesseract on photocopied and faxed medical records ran closer to
  1–2 seconds per page per worker. Budget roughly **10–20 minutes per 1,000
  pages** on 8 workers, and expect a bad batch of faxes to be worse.

The first minute of any run replaces every estimate with a measured one.

---

## Troubleshooting

**`tesseract was not found`** — install it (above), or set `OCRTOOL_TESSERACT`
to the binary. `ocrtool doctor` shows what is being found.

**`tesseract produced no PDF output (is pdf.ttf present in tessdata?)`** — the
searchable-PDF writer needs `pdf.ttf` in your `tessdata` folder. The text is
still read and written; only that page's PDF layer is missing. Installing the
distribution's `tesseract-ocr` package normally supplies it.

**A page came out empty.** Open it in the viewer: if the picture is blank, the
source is blank. If there is visible text, try `--psm 6` (one uniform block) or
`--psm 11` (sparse text), and try `--dpi 400` for small print. Handwriting will
not work — tesseract does not read it.

**Everything is flagged.** Your scans are probably low-contrast or skewed past
6°. Check a preview image. Rotating the source pages correctly and rescanning at
300 dpi beats any setting here.

**It is slower than expected.** Lower `--dpi` to 200, or raise `--workers` if
the machine has cores to spare. Rendering is serialised (PDFium is not
thread-safe) while OCR is not, so past about 8 workers there is little gain.

**The browser page says the run is not on this machine.** Runs are remembered in
`~/.ocrtool/recent.json` by output folder. If the output folder moved or was
deleted, the run is gone with it — the output folder is the record.

**Nothing happens when I press Start.** Check the terminal running `ocrtool ui`:
errors that cannot be shown in the browser are printed there.

---

## What it does not do

Stated plainly, because each of these is a thing OCR tools are assumed to do:

- **It does not read handwriting.** Tesseract is a printed-text engine.
  Handwritten pages come back near-empty and flagged, which is the honest
  result.
- **It does not fix a misread digit.** Confidence measures how sure tesseract
  is, not whether it is right. A stable wrong answer looks exactly like a stable
  right one, which is why every page keeps a picture of itself.
- **It does not do layout OCR.** Tables and multi-column pages come back in a
  reasonable reading order, not a structured one.
- **It does not extract fields, summarise, or classify.** No model is called on
  your documents at any point.
- **It does not touch your originals.** Input files are only ever read.
- **It does not go online.** No network call is made by anything in this
  repository.

---

## Development

```bash
.venv/bin/python -m pytest -q          # 51 tests, about 15 seconds
```

Tests that need the tesseract binary skip themselves when it is absent. No test
document is committed to the repository — they are generated at test time, so
nobody's scanned records can end up in git by accident.

```
ocrtool/
  cli.py          run / ui / doctor
  config.py       every setting, with the reasoning for each default
  discover.py     find documents and count pages before any work starts
  render.py       page -> image (pypdfium2), and the pixel cap
  textlayer.py    read a PDF's own text, and decide if it is worth having
  preprocess.py   deskew, despeckle, contrast
  tesseract.py    the subprocess wrapper: TSV + searchable PDF in one pass
  pipeline.py     what happens to one page
  runner.py       one run: discovery, the page pool, progress, outputs
  outputs.py      .pdf / .txt / .json / pages.csv writers
  web/
    app.py        Flask routes and the event stream
    state.py      which runs this machine knows about
    templates/    three pages: start, run, document
    static/       one stylesheet, three scripts, no build step
tests/
```

The layering holds in one direction: `pipeline` knows about one page, `runner`
knows about one run, and `web` knows about neither — it starts runs and reads
what they wrote. The CLI and the browser drive exactly the same code.

MIT licensed.
