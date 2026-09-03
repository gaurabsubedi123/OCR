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

from dataclasses import dataclass, field
from pathlib import Path

from .config import TEXT_LAYER_MIN_CHARS
from .models import Word
from .render import PDFIUM_LOCK


@dataclass
class TextLayer:
    text: str
    char_count: int      # letters and digits only, not whitespace or punctuation
    usable: bool
    # Where each word sits on the page, so that a page taken from a text layer
    # can be laid out the same way an OCR'd one is. Without these the .txt of a
    # born-digital PDF came out as one flat run of lines while the scanned page
    # beside it kept its columns — the same document, two different shapes,
    # depending on something the reader cannot see.
    words: list[Word] = field(default_factory=list)


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
                height = page.get_size()[1]
                textpage = page.get_textpage()
                try:
                    raw = textpage.get_text_range()
                    words = _words_from(textpage, raw, height)
                finally:
                    textpage.close()
            finally:
                page.close()
        finally:
            pdf.close()

    text = _tidy(raw or "")
    letters = sum(1 for ch in text if ch.isalnum())
    return TextLayer(text, letters, letters >= min_chars, words)


def _words_from(textpage, raw: str, page_height: float) -> list[Word]:
    """Words and their boxes, built from the page's own character positions.

    PDFium gives a box per character; a word is the run of characters between
    whitespace, and its box is the union of theirs. That is enough for
    `laid_out_text` to rebuild the page, and it is exact rather than estimated
    — these positions are what the PDF itself says, not something measured off
    a picture.

    Two conversions matter. PDF coordinates start at the bottom-left and count
    upwards, while everything downstream expects a page read from the top down,
    so the vertical axis is flipped. And the units are points rather than
    pixels, which nothing downstream minds: the layout is rebuilt from ratios
    between the boxes, never from their absolute size.

    Any disagreement between the character count and the text means the indices
    cannot be trusted to line up, and no boxes are better than wrong ones.
    """
    try:
        count = textpage.count_chars()
    except Exception:  # noqa: BLE001 - a page with no text page is not an error
        return []
    if not raw or count != len(raw):
        return []

    words: list[Word] = []
    letters: list[str] = []
    box: list[float] | None = None

    def flush() -> None:
        nonlocal letters, box
        if letters and box is not None:
            words.append(
                Word(
                    text="".join(letters),
                    x0=box[0],
                    y0=page_height - box[3],
                    x1=box[2],
                    y1=page_height - box[1],
                    # The PDF says so. There is nothing here to be unsure of,
                    # and the viewer marks doubtful words by this number.
                    confidence=100.0,
                )
            )
        letters, box = [], None

    for index, char in enumerate(raw):
        if char.isspace():
            flush()
            continue
        try:
            left, bottom, right, top = textpage.get_charbox(index)
        except Exception:  # noqa: BLE001 - one unreadable box, not one lost page
            continue
        letters.append(char)
        if box is None:
            box = [left, bottom, right, top]
        else:
            box = [min(box[0], left), min(box[1], bottom), max(box[2], right), max(box[3], top)]
    flush()
    return words


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
