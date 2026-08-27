"""Write the results out: searchable PDF, plain text, JSON, and a CSV report.

Each kind of output gets its own folder, and the input's subfolder structure is
repeated inside each one. That way the searchable PDFs are a complete set on
their own — a folder you can hand to someone without explaining what the other
files are for — and the same is true of the text.

  input/Ex 13/13.pdf  ->  output/pdf/Ex 13/13.pdf     searchable copy
                          output/txt/Ex 13/13.txt     the text, page by page
                          output/json/Ex 13/13.json   text + confidence + word boxes

With `outputs_grouped_by_type` off, a document's three files sit beside each
other instead, in one tree that mirrors the input:

  input/Ex 13/13.pdf  ->  output/Ex 13/13.pdf
                          output/Ex 13/13.txt
                          output/Ex 13/13.json
"""

from __future__ import annotations

import csv
import json
import logging
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


def output_paths(output_root: Path, relpath: str, *, grouped: bool = True) -> dict[str, Path]:
    """Where this document's three outputs go.

    Extensions are replaced, not appended, so `13.pdf` becomes `13.txt` rather
    than `13.pdf.txt`.
    """
    rel = Path(relpath)
    kinds = ("pdf", "txt", "json")
    if grouped:
        return {
            kind: (output_root / kind / rel.parent / rel.stem).with_suffix(f".{kind}")
            for kind in kinds
        }
    stem_path = output_root / rel.parent / rel.stem
    return {kind: stem_path.with_suffix(f".{kind}") for kind in kinds}


def write_text(dest: Path, result: FileResult) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    for page in result.pages:
        chunks.append(PAGE_MARKER.format(n=page.page_no, source=page.source))
        chunks.append(page.text.strip())
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
