"""Find the documents in a folder and count their pages before any work starts.

Counting first costs a second or two and buys the thing the progress bar needs
most: a real denominator. "page 340 of 2,063" is a fact; "working..." is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .cache import file_sha256
from .config import RESERVED_DIRS, SUPPORTED_SUFFIXES

log = logging.getLogger(__name__)

# A malformed file should cost one row in the report, not the whole run.
Image.MAX_IMAGE_PIXELS = None  # large scans are normal here, not an attack


@dataclass
class Discovered:
    path: Path
    relpath: str
    size_bytes: int
    page_count: int
    error: str | None = None
    # Size and modification time together are how a later run decides whether
    # this is still the same file it read before.
    modified: float = -1.0
    # The content hash. Two documents with the same one are the same document,
    # whatever they are called and wherever they sit, so it is what identifies
    # a document's stored pages and what catches a copy of one already read.
    sha256: str = ""


def find_documents(root: Path, *, recursive: bool = True) -> list[Discovered]:
    """Every supported document under `root`, sorted, with page counts.

    Unsupported files are left out silently — a folder of case documents is
    also full of .docx notes and .DS_Store, and listing those as errors would
    train you to ignore the error column.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")

    walker = root.rglob("*") if recursive else root.glob("*")
    found: list[Discovered] = []
    for path in sorted(walker):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if any(part in RESERVED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.name.startswith("."):
            continue
        found.append(_describe(path, root))
    return found


def _describe(path: Path, root: Path) -> Discovered:
    relpath = str(path.relative_to(root)).replace("\\", "/")
    try:
        stat = path.stat()
        size, modified = stat.st_size, stat.st_mtime
    except OSError as exc:
        return Discovered(path, relpath, 0, 0, error=str(exc))

    try:
        pages = count_pages(path)
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop discovery
        log.warning("could not read %s: %s", path, exc)
        return Discovered(path, relpath, size, 0, error=f"{type(exc).__name__}: {exc}", modified=modified)

    if pages == 0:
        return Discovered(path, relpath, size, 0, error="no pages found in file", modified=modified)

    # Reading the file through once costs a fraction of a second per hundred
    # megabytes, against minutes per hundred pages of OCR. A document whose
    # hash cannot be taken simply loses the ability to resume and to be
    # recognised as a copy; it is still read normally.
    try:
        digest = file_sha256(path)
    except OSError as exc:
        log.warning("could not hash %s: %s", path, exc)
        digest = ""

    return Discovered(path, relpath, size, pages, modified=modified, sha256=digest)


def count_pages(path: Path) -> int:
    """Pages in a PDF, frames in a multi-page TIFF, 1 for a plain image."""
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        from .render import PDFIUM_LOCK

        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                return len(pdf)
            finally:
                pdf.close()

    with Image.open(path) as img:
        return int(getattr(img, "n_frames", 1))
