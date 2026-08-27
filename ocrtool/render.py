"""Turn one page of a document into a picture for OCR and for the reviewer.

pypdfium2 rather than poppler or PyMuPDF: no system binary to install and a
permissive licence, which matters when the documents belong to someone else.
"""

from __future__ import annotations

import threading
from pathlib import Path

from dataclasses import dataclass

from PIL import Image, ImageOps

from .config import MAX_RENDER_PIXELS, PREVIEW_MAX_WIDTH, PREVIEW_QUALITY

# PDFium is not documented as thread-safe and the page pool renders from many
# threads at once, so its calls are serialised here. Only the PDFium calls: OCR
# is the expensive stage and runs outside this lock, so the pool still
# parallelises the part that actually costs time.
PDFIUM_LOCK = threading.Lock()


class RenderError(Exception):
    pass


@dataclass
class Rendered:
    """A page picture and the resolution it was actually produced at.

    The effective dpi is not always the requested dpi — see MAX_RENDER_PIXELS —
    and it has to travel with the image, because tesseract tags its searchable
    PDF output with it and gets the page size wrong otherwise.
    """

    image: Image.Image
    dpi: int


def render_page(source: Path, page_no: int, dpi: int) -> Rendered:
    """Render 1-based `page_no` of `source` as an RGB image."""
    if source.suffix.lower() == ".pdf":
        return _render_pdf(source, page_no, dpi)
    return _render_image(source, page_no, dpi)


def _render_pdf(source: Path, page_no: int, dpi: int) -> Rendered:
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(source))
        try:
            if not 1 <= page_no <= len(pdf):
                raise RenderError(f"page {page_no} of {len(pdf)} requested in {source.name}")
            page = pdf[page_no - 1]
            try:
                width_pt, height_pt = page.get_size()
                scale, effective_dpi = _fit(width_pt / 72.0, height_pt / 72.0, dpi)
                bitmap = page.render(scale=scale)
                # Materialise inside the lock: the bitmap points at PDFium
                # memory that is freed when the page closes.
                return Rendered(bitmap.to_pil().convert("RGB"), effective_dpi)
            finally:
                page.close()
        finally:
            pdf.close()


def _fit(width_in: float, height_in: float, dpi: int) -> tuple[float, int]:
    """Scale factor and the dpi it corresponds to, respecting the pixel cap."""
    longest_in = max(width_in, height_in, 0.01)
    capped_dpi = min(dpi, MAX_RENDER_PIXELS / longest_in)
    effective = max(72, int(capped_dpi))
    return effective / 72.0, effective


def _render_image(source: Path, page_no: int, dpi: int) -> Rendered:
    with Image.open(source) as img:
        frames = int(getattr(img, "n_frames", 1))
        if not 1 <= page_no <= frames:
            raise RenderError(f"frame {page_no} of {frames} requested in {source.name}")
        if frames > 1:
            img.seek(page_no - 1)
        # Phone photos and many scanners record orientation in EXIF rather than
        # rotating the pixels; without this a sideways page reaches OCR sideways.
        image = ImageOps.exif_transpose(img).convert("RGB")

    # An image carries no page size, so its own dpi tag is the only clue to what
    # resolution it was scanned at; 300 is the safe assumption when it is absent.
    tagged = image.info.get("dpi")
    effective = int(tagged[0]) if tagged and tagged[0] else dpi
    longest = max(image.size)
    if longest > MAX_RENDER_PIXELS:
        scale = MAX_RENDER_PIXELS / longest
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        effective = max(72, int(effective * scale))
    return Rendered(image, max(72, effective))


def write_preview(image: Image.Image, dest: Path) -> None:
    """Save a screen-sized JPEG of a page next to the run's results.

    Text alone cannot be checked. The preview is what lets you put the picture
    of the page beside the words that came off it, which is the only honest way
    to tell a good OCR result from a confident wrong one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    preview = image
    if preview.width > PREVIEW_MAX_WIDTH:
        height = round(preview.height * PREVIEW_MAX_WIDTH / preview.width)
        preview = preview.resize((PREVIEW_MAX_WIDTH, height), Image.LANCZOS)
    preview.convert("RGB").save(dest, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
