"""Write the results out: searchable PDF, plain text, JSON, and a CSV report.

Three layouts, because "where did my files go" is the first question a folder
of results has to answer, and different jobs answer it differently.

`by-folder` (the default) mirrors the input folder for folder, and puts the
three kinds side by side *inside each folder that holds documents*. Walk to
where the document was and its outputs are right there:

  input/Medical/Imaging/13.pdf  ->  output/Medical/Imaging/pdf/13.pdf
                                  output/Medical/Imaging/txt/13.txt
                                  output/Medical/Imaging/json/13.json
  input/loose.pdf             ->  output/pdf/loose.pdf
                                  output/txt/loose.txt
                                  output/json/loose.json

`by-type` collects everything under one pdf/, txt/ and json/ at the top, each
repeating the input's subfolders inside it. Good when you want the searchable
PDFs as one complete set to hand to someone:

  input/Ex 13/13.pdf  ->  output/pdf/Ex 13/13.pdf
                          output/txt/Ex 13/13.txt
                          output/json/Ex 13/13.json

`together` mirrors the input and drops a document's three files beside each
other, with no folders in between:

  input/Ex 13/13.pdf  ->  output/Ex 13/13.pdf
                          output/Ex 13/13.txt
                          output/Ex 13/13.json
"""

from __future__ import annotations

import csv
import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import FileResult, PageResult

log = logging.getLogger(__name__)

PAGE_MARKER = "----- page {n} ({source}) -----"


class PdfAssembleError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


OUTPUT_KINDS = ("pdf", "txt", "json")

# The three shapes above, in the order they are offered.
LAYOUTS = ("by-folder", "by-type", "together")
DEFAULT_LAYOUT = "by-folder"


def output_basenames(relpaths: Iterable[str]) -> dict[str, str]:
    """The name each document's outputs are built from, keyed by relpath.

    Normally the document's name with its extension taken off, so `13.pdf`
    gives `13.pdf`, `13.txt` and `13.json`.

    The exception is two documents in one folder whose names differ only by
    extension — `x.pdf` and `x.png`, which is what a scan kept in two formats
    looks like. Both wanted `x.txt`, and the second to be written silently
    replaced the first: two documents in, one file out.

    Those get the source extension folded into the name with an underscore:

        x.pdf  ->  x_pdf.pdf   x_pdf.txt   x_pdf.json
        x.png  ->  x_png.pdf   x_png.txt   x_png.json

    which says where each came from without the double extension `x.pdf.pdf`
    that simply keeping the whole name would give.

    Only the clashing documents are spelled that way. One with no rival keeps
    the short name, because renaming every output to head off a rare clash
    would be the worse trade.

    Names are compared case-insensitively: the results usually land on a
    Windows drive, where `X.pdf` and `x.png` collide exactly as surely as the
    lowercase pair would. The extension is folded in lowercase for the same
    reason.

    A leftover clash — `x.pdf` and `x.png` taking the name `x_pdf`, in a folder
    that also holds `x_pdf.jpg` — is settled by ` (2)`, ` (3)`, in the order the
    documents were discovered, which is sorted. Nothing here can hand back one
    name twice.
    """
    ordered = list(dict.fromkeys(relpaths))

    by_stem: dict[tuple[str, str], list[str]] = defaultdict(list)
    for relpath in ordered:
        rel = Path(relpath)
        by_stem[(str(rel.parent).casefold(), rel.stem.casefold())].append(relpath)

    names: dict[str, str] = {}
    taken: set[tuple[str, str]] = set()
    for relpath in ordered:
        rel = Path(relpath)
        folder = str(rel.parent).casefold()
        shared = len(by_stem[(folder, rel.stem.casefold())]) > 1
        suffix = rel.suffix.lstrip(".").lower()
        # A document with no extension has nothing to fold in; it falls through
        # to the numbered form below if the short name is taken.
        base = f"{rel.stem}_{suffix}" if shared and suffix else rel.stem
        candidate, attempt = base, 2
        while (folder, candidate.casefold()) in taken:
            candidate = f"{base} ({attempt})"
            attempt += 1
        taken.add((folder, candidate.casefold()))
        names[relpath] = candidate
    return names


def output_paths(
    output_root: Path,
    relpath: str,
    *,
    layout: str = DEFAULT_LAYOUT,
    basename: str | None = None,
) -> dict[str, Path]:
    """Where this document's three outputs go under the chosen layout.

    Extensions are replaced, not appended, so `13.pdf` becomes `13.txt` rather
    than `13.pdf.txt`.

    The new extension is joined on as text rather than by `Path.with_suffix`.
    `with_suffix` replaces everything after the *last* dot in the name, and by
    that point the name is already the stem, so a document called
    `1. Plaintiff_Record.pdf` came out as `1.pdf` — the name cut at its first
    dot, and every `1. …` document in the folder overwriting the last. Dots
    inside a name are ordinary characters here.

    `basename` overrides the name the outputs are built from, and is how a run
    passes in what `output_basenames` decided for the whole folder. On its own
    this function can only see one document, and a collision is by definition
    something you need two to notice.
    """
    rel = Path(relpath)
    base = rel.stem if basename is None else basename
    names = {kind: f"{base}.{kind}" for kind in OUTPUT_KINDS}
    if layout == "by-type":
        return {
            kind: output_root / kind / rel.parent / name
            for kind, name in names.items()
        }
    if layout == "together":
        return {
            kind: output_root / rel.parent / name for kind, name in names.items()
        }
    # by-folder: the kind folder sits inside the document's own folder, so a
    # document deep in the tree keeps its results next to where it came from.
    return {
        kind: output_root / rel.parent / kind / name
        for kind, name in names.items()
    }


def laid_out_text(page: PageResult) -> str:
    """The page's text with the page's shape kept, from the word boxes.

    Tesseract hands back a stream of lines flush to the left margin, which
    throws away exactly the thing that makes a bill, a form or a deposition
    readable: what is lined up with what. A two-column invoice becomes a list
    of numbers with nothing to say which is which.

    So the words are put back where they were. Every word knows its box in
    pixels, one character's width is estimated from the words themselves, and
    each word is placed at the column its left edge lands on. The result reads
    like the page — the same thing `pdftotext -layout` does, and for the same
    reason.

    Falls back to the plain text when there are no boxes to work from, which is
    the case for a page taken from a PDF's own text layer.
    """
    if not page.words:
        return page.text.strip()

    # One character's width, taken as the median across the page rather than an
    # average: a single long box on a stamp or a logo would otherwise stretch
    # every column on the page.
    widths = [
        (word.x1 - word.x0) / len(word.text)
        for word in page.words
        if word.text and word.x1 > word.x0
    ]
    if not widths:
        return page.text.strip()
    char_width = statistics.median(widths)
    if char_width <= 0:
        return page.text.strip()

    heights = [word.y1 - word.y0 for word in page.words if word.y1 > word.y0]
    line_height = statistics.median(heights) if heights else char_width * 2

    # Words belong to the same line when their vertical centres are within half
    # a line of each other. Comparing centres rather than tops is what keeps a
    # line together when it mixes capitals, descenders and a larger heading.
    ordered = sorted(page.words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    lines: list[list[Any]] = []
    line_centres: list[float] = []
    for word in ordered:
        centre = (word.y0 + word.y1) / 2
        if lines and abs(centre - line_centres[-1]) <= line_height * 0.5:
            lines[-1].append(word)
            # Track the running centre so a line that drifts down the page —
            # a scan is never perfectly straight — stays one line.
            line_centres[-1] = (line_centres[-1] + centre) / 2
        else:
            lines.append([word])
            line_centres.append(centre)

    # How far apart this page's lines actually are, taken from the page rather
    # than guessed from the word height: a box drawn round a word stops at the
    # ink, and the space between lines is exactly what it leaves out. Below
    # three lines there is nothing to take a median of, and 1.5 line heights is
    # the ordinary spacing of a typed page.
    gaps = [b - a for a, b in zip(line_centres, line_centres[1:])]
    pitch = statistics.median(gaps) if len(gaps) >= 3 else line_height * 1.5
    if pitch <= 0:
        pitch = line_height * 1.5

    left_margin = min(word.x0 for word in page.words)
    rendered: list[str] = []
    previous_centre: float | None = None
    for words, centre in zip(lines, line_centres):
        # Blank lines are half of what makes a document scannable by eye, so a
        # gap wider than the page's own line spacing becomes one.
        if previous_centre is not None:
            blanks = round((centre - previous_centre - pitch) / pitch)
            rendered.extend([""] * max(0, min(blanks, 4)))
        previous_centre = centre

        row = ""
        for word in sorted(words, key=lambda w: w.x0):
            column = int(round((word.x0 - left_margin) / char_width))
            # Never let a wide previous word push this one off its column, and
            # never let two words run together: a column that has already been
            # reached moves along by one space instead.
            if row and column <= len(row):
                column = len(row) + 1
            row = row.ljust(column) + word.text
        rendered.append(row.rstrip())

    return "\n".join(rendered).strip()


def write_text(dest: Path, result: FileResult, *, keep_layout: bool = True) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    for page in result.pages:
        chunks.append(PAGE_MARKER.format(n=page.page_no, source=page.source))
        chunks.append(laid_out_text(page) if keep_layout else page.text.strip())
        chunks.append("")
    dest.write_text("\n".join(chunks).strip() + "\n", encoding="utf-8")


def write_json(dest: Path, result: FileResult, *, settings: dict[str, Any], include_words: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool": "ocrtool",
        "generated_at": now_iso(),
        "settings": settings,
        "document": result.to_dict(include_words=include_words),
    }
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_searchable_pdf(
    dest: Path,
    *,
    source_pdf: Path | None,
    pages: Iterable[tuple[PageResult, Path | None]],
) -> None:
    """Assemble one searchable PDF for the document.

    Two kinds of page go in, and they are handled differently on purpose:

      OCR'd page      tesseract already wrote a one-page PDF holding the page
                      image with its recognised text laid invisibly on top.
                      That file is imported as-is.

      text-layer page the original page is imported straight from the source
                      PDF. It is already searchable and already perfect, and
                      re-rendering it would only lose fidelity.

    Page PDFs are read from disk one at a time rather than held in memory: a
    500-page scan is comfortably over a gigabyte of embedded images.
    """
    import pypdfium2 as pdfium

    from .render import PDFIUM_LOCK

    dest.parent.mkdir(parents=True, exist_ok=True)
    with PDFIUM_LOCK:
        out = pdfium.PdfDocument.new()
        source_doc = None
        borrowed: list[Any] = []
        try:
            for page, page_pdf in pages:
                if page_pdf is not None and page_pdf.exists():
                    doc = pdfium.PdfDocument(str(page_pdf))
                    out.import_pages(doc, [0])
                    borrowed.append(doc)
                elif page.source == "text-layer" and source_pdf is not None:
                    if source_doc is None:
                        source_doc = pdfium.PdfDocument(str(source_pdf))
                    out.import_pages(source_doc, [page.page_no - 1])
                else:
                    # A failed page is skipped rather than faked. The manifest
                    # and the CSV both record it, so the gap is visible.
                    log.warning("no PDF page for %s page %s", dest.name, page.page_no)

            if len(out) == 0:
                raise PdfAssembleError("no pages could be written")
            out.save(str(dest))
        finally:
            for doc in borrowed:
                doc.close()
            if source_doc is not None:
                source_doc.close()
            out.close()


def write_pages_csv(dest: Path, results: Iterable[FileResult]) -> None:
    """One row per page, for opening in a spreadsheet and sorting by confidence.

    This is the fastest route to "which pages should I look at first", which is
    the question a 2,000-page run actually leaves you with.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "file",
                "page",
                "source",
                "confidence",
                "chars",
                "words",
                "needs_review",
                "review_reason",
                "seconds",
                "error",
            ]
        )
        for result in results:
            for page in result.pages:
                writer.writerow(
                    [
                        result.relpath,
                        page.page_no,
                        page.source,
                        "" if page.confidence is None else f"{page.confidence:.1f}",
                        page.char_count,
                        page.word_count,
                        "yes" if page.needs_review else "",
                        page.review_reason or "",
                        f"{page.duration_ms / 1000:.1f}",
                        page.error or "",
                    ]
                )
