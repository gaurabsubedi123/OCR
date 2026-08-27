"""Every knob in one place, with the reasoning for each default.

A setting that cannot be explained is a setting nobody can tune, so each of
these carries the measurement or the constraint that produced it.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Tesseract is trained at roughly 300 dpi. Below ~200 it degrades sharply;
# above 400 the render cost climbs and accuracy does not follow.
DEFAULT_DPI = 300

# Word confidence below this makes a page worth a human's eyes. Tesseract's
# scale is 0-100 and it is optimistic: a page averaging 70 is already visibly
# rough, and one averaging 85 is usually clean.
DEFAULT_MIN_CONFIDENCE = 70.0

# A hard ceiling on the long edge of a rendered page, in pixels.
#
# dpi is only meaningful relative to how big the page claims to be. A letter
# page at 300 dpi is 3300 px and ideal; a large-format scan or a PDF whose page
# box is 23 x 30 inches becomes 9000 px at the same setting, and tesseract gets
# *worse*, not better — on a test page it returned nothing at all. Capping the
# pixels keeps oversized pages at a sane effective resolution, and the dpi
# actually used is recorded per page rather than assumed.
MAX_RENDER_PIXELS = 5000

# A page that produced almost nothing is either blank, a photograph, or a
# failure — all three deserve the flag rather than a silent empty result.
MIN_CHARS_FOR_A_REAL_PAGE = 25

# A PDF page whose own text layer holds at least this many letters and digits
# is a born-digital page. Taking that text is free and exact; OCR of a render
# of it can only be worse. Scanned PDFs often carry a few stray characters
# (a stamp, a footer), which is why the bar is not simply "any text at all".
TEXT_LAYER_MIN_CHARS = 100

# Page segmentation mode 3 = fully automatic, no orientation detection. It is
# the right default for mixed documents; forms and single columns sometimes do
# better on 4 or 6, which is why this is exposed in the UI.
DEFAULT_PSM = 3

DEFAULT_LANG = "eng"

# Previews are what make the result reviewable — text on its own cannot be
# checked. 1100px wide is enough to read a letter-size page on screen and costs
# roughly 100-200 KB per page as JPEG.
PREVIEW_MAX_WIDTH = 1100
PREVIEW_QUALITY = 80

SUPPORTED_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
    ".gif",
}

# Directories the tool writes itself. Never treated as input, so pointing the
# tool at its own output folder cannot start a feedback loop.
RESERVED_DIRS = {"_runs", "_previews", "_uploads"}


def default_workers() -> int:
    """Threads for the page pool.

    The expensive stage is a tesseract subprocess, so threads are the right
    unit — the GIL is released for the whole of it. Two cores are left for the
    UI and the OS; more workers than that starves the machine you are watching
    the progress on.
    """
    cpus = os.cpu_count() or 4
    return max(1, min(8, cpus - 2))


@dataclass
class Settings:
    """One run's settings. Serialised into the run manifest so any result can
    be traced back to the exact configuration that produced it."""

    input_dir: str
    output_dir: str
    dpi: int = DEFAULT_DPI
    lang: str = DEFAULT_LANG
    psm: int = DEFAULT_PSM
    workers: int = 0  # 0 means "decide from the machine"
    force_ocr: bool = False
    deskew: bool = True
    denoise: bool = True
    write_pdf: bool = True
    write_txt: bool = True
    write_json: bool = True
    include_word_boxes: bool = True
    write_previews: bool = True
    # Put each kind of output in its own folder — output/pdf/, output/txt/,
    # output/json/ — each keeping the input's subfolder structure inside it.
    # Off puts a document's three files beside each other instead.
    outputs_grouped_by_type: bool = True
    # Put the original page picture back into the searchable PDF instead of the
    # greyscale copy OCR read. Truer to the document, and a larger file.
    pdf_keeps_source_image: bool = True
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    recursive: bool = True

    def __post_init__(self) -> None:
        if self.workers <= 0:
            self.workers = default_workers()
        self.dpi = max(72, min(600, int(self.dpi)))
        self.psm = max(0, min(13, int(self.psm)))
        self.workers = max(1, min(32, int(self.workers)))

    @property
    def input_path(self) -> Path:
        return Path(self.input_dir).expanduser().resolve()

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser().resolve()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})
