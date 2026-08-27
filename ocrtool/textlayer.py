"""Read a PDF page's own text layer, and decide whether it is worth having.

A born-digital PDF already contains its text, exactly, for free. OCR of a
picture of that page can only be worse. On a real 2,063-page case file, 932
pages had usable text layers — nearly half the work, skipped without loss.

The judgement call is that scanned PDFs are rarely completely text-free: a
Bates stamp, a fax header, or a previous OCR pass leaves a scatter of
characters behind. So the test is not "is there text" but "is there enough
text that this page was typed rather than photographed".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import TEXT_LAYER_MIN_CHARS
from .render import PDFIUM_LOCK


@dataclass
class TextLayer:
    text: str
    char_count: int      # letters and digits only, not whitespace or punctuation
    usable: bool


def read_text_layer(source: Path, page_no: int, *, min_chars: int = TEXT_LAYER_MIN_CHARS) -> TextLayer:
    """Extract page `page_no`'s embedded text. Non-PDFs never have one."""
    if source.suffix.lower() != ".pdf":
        return TextLayer("", 0, False)

    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(source))
        try:
            if not 1 <= page_no <= len(pdf):
                return TextLayer("", 0, False)
            page = pdf[page_no - 1]
            try:
                textpage = page.get_textpage()
                try:
                    raw = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
        finally:
            pdf.close()

    text = _tidy(raw or "")
    letters = sum(1 for ch in text if ch.isalnum())
    return TextLayer(text, letters, letters >= min_chars)


def _tidy(text: str) -> str:
    """Normalise line endings and drop the runs of blank lines PDF extraction
    leaves behind, without touching the layout inside a line."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()
