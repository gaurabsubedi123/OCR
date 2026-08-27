"""Put the page back the way it looked.

Tesseract writes its searchable PDF around the image it was handed, and the
image it is handed has been cleaned up for recognition: greyscale, despeckled,
contrast stretched. That is right for OCR and wrong for the copy you keep. A
photograph exhibit came back grey, and so would a highlighted passage, a red
stamp, or blue signature ink — the parts of a document that are coloured are
usually coloured for a reason.

So the recognised text stays where tesseract put it, and only the picture
underneath is swapped back for the original render. The invisible text is
positioned against the cleaned image, so the replacement has to match it
geometrically: same rotation from deskewing, same size. `page_image_for_pdf`
below rebuilds exactly that, in colour.
"""

from __future__ import annotations

import io
import logging

import pypdfium2 as pdfium
from PIL import Image

from .render import PDFIUM_LOCK

log = logging.getLogger(__name__)

# The page image dominates the file size. 88 keeps stamps and ink legible at
# 300 dpi without the ringing that shows up around text below about 80.
JPEG_QUALITY = 88


def replace_page_image(pdf_bytes: bytes, image: Image.Image) -> bytes:
    """Swap the picture inside a one-page PDF, keeping its invisible text.

    Returns the original bytes unchanged if anything at all is unexpected: a
    searchable PDF with the wrong-coloured picture is a cosmetic problem, and
    losing the page is not.
    """
    encoded = io.BytesIO()
    image.convert("RGB").save(encoded, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    encoded.seek(0)

    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not reopen the page PDF: %s", exc)
            return pdf_bytes

        try:
            page = document[0]
            images = [obj for obj in page.get_objects() if isinstance(obj, pdfium.PdfImage)]
            if len(images) != 1:
                # More than one image means tesseract laid the page out in a way
                # this function does not understand; leave it alone.
                return pdf_bytes

            width, height = images[0].get_px_size()
            if (width, height) != image.size:
                log.warning(
                    "page image is %sx%s but the replacement is %sx%s — keeping the original",
                    width, height, *image.size,
                )
                return pdf_bytes

            images[0].load_jpeg(encoded, pages=[page])
            page.gen_content()

            out = io.BytesIO()
            document.save(out)
            return out.getvalue()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not put the source image back: %s", exc)
            return pdf_bytes
        finally:
            document.close()
