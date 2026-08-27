"""Every knob in one place, with the reasoning for each default.

A setting that cannot be explained is a setting nobody can tune, so each of
these carries the measurement or the constraint that produced it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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


def state_dir() -> Path:
    """Where this tool keeps its own small files: the list of past runs, and
    the remembered folders. OCRTOOL_STATE_DIR redirects it, which the tests use
    so a test run never appears in the list someone is working through."""
    override = os.environ.get("OCRTOOL_STATE_DIR")
    path = Path(override).expanduser() if override else Path.home() / ".ocrtool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return state_dir() / "config.json"


def default_folders() -> dict[str, str]:
    """The input and output folders offered before anyone types anything.

    Resolved in this order, most specific first:

      1. OCRTOOL_INPUT_DIR / OCRTOOL_OUTPUT_DIR in the environment
      2. input_dir / output_dir in ~/.ocrtool/config.json
      3. ~/ocr-input and ~/ocr-output

    Typing an absolute path every time is exactly the kind of friction that
    makes a tool annoying to use daily, and on WSL the path is long and easy to
    get wrong. `ocrtool folders` sets them.
    """
    folders = {
        "input": str(Path.home() / "ocr-input"),
        "output": str(Path.home() / "ocr-output"),
    }

    path = config_path()
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            for key, stored_key in (("input", "input_dir"), ("output", "output_dir")):
                if stored.get(stored_key):
                    folders[key] = str(Path(stored[stored_key]).expanduser())
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            log.warning("could not read %s: %s", path, exc)

    for key, variable in (("input", "OCRTOOL_INPUT_DIR"), ("output", "OCRTOOL_OUTPUT_DIR")):
        value = os.environ.get(variable)
        if value:
            folders[key] = str(Path(value).expanduser())

    return folders


def save_default_folders(*, input_dir: str | None = None, output_dir: str | None = None) -> dict[str, str]:
    """Remember these folders for next time, and make them if they are missing."""
    path = config_path()
    stored: dict[str, Any] = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stored = {}

    for key, value in (("input_dir", input_dir), ("output_dir", output_dir)):
        if value:
            folder = Path(value).expanduser()
            stored[key] = str(folder)
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("could not create %s: %s", folder, exc)

    path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    return default_folders()


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
    # Leave a document alone if this output folder already holds its results,
    # the source has not changed, and it was read with these same settings.
    skip_already_done: bool = True
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
