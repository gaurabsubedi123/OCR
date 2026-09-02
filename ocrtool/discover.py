"""Find the documents in a folder and count their pages before any work starts.

Counting first costs a second or two and buys the thing the progress bar needs
most: a real denominator. "page 340 of 2,063" is a fact; "working..." is not.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from PIL import Image

from .cache import file_sha256
from .config import RESERVED_DIRS, SUPPORTED_SUFFIXES

log = logging.getLogger(__name__)

# A malformed file should cost one row in the report, not the whole run.
Image.MAX_IMAGE_PIXELS = None  # large scans are normal here, not an attack

_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """A sort key that reads runs of digits as numbers.

    Exhibits are numbered, and plain text order puts 10 before 2, so a case
    folder lists as 1, 10, 11, 2, 20, 21 — the one order nobody wants and the
    order every file listing defaults to. Splitting on digit runs and comparing
    the numbers as numbers gives 1, 2, 10, 11, 20, 21.

    Every element is the same shape — (kind, number, text) — so two keys never
    end up comparing an int against a str, which is the way this is usually
    written and the way it raises TypeError on `2` against `2a`.
    """
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part.lower())
        for part in _DIGITS.split(text)
        if part
    )


def natural_path_key(path: Path) -> tuple:
    """`natural_key` applied to each part of a path, so folders nest properly."""
    return tuple(natural_key(part) for part in path.parts)


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
    """Every supported document under `root`, in reading order, with page counts.

    The order is `natural_key`'s, not the filesystem's: exhibit 2 comes before
    exhibit 10. It is what the progress list, the report and the browser tree
    all show, so it is worth being the order a person would have chosen.

    Unsupported files are left out silently — a folder of case documents is
    also full of .docx notes and .DS_Store, and listing those as errors would
    train you to ignore the error column.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")

    walker = root.rglob("*") if recursive else root.glob("*")
    found: list[Discovered] = []
    for path in sorted(walker, key=natural_path_key):
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


def document_tree(found: Iterable[Discovered]) -> dict[str, Any]:
    """The discovered documents arranged as the folder tree they came from.

    A flat list of `Medical/Imaging/13.pdf` paths answers "what is in here?"
    badly: the shape of the folder is the thing a case file is organised by,
    and a list of 1,944 rows with the first twelve shown and "and 1,932 more"
    underneath tells you nothing about it at all.

    Every folder carries a recursive count of the documents and pages beneath
    it, so a collapsed folder is still informative — the counts are the point,
    and opening one is for when you want the names.

    Folders and documents are both in `natural_key` order, and folders come
    first, which is the order every file browser has used for thirty years.
    """
    root = _empty_node("", "")
    for item in found:
        parts = PurePosixPath(item.relpath).parts
        node = root
        node["files"] += 1
        node["pages"] += item.page_count
        walked: list[str] = []
        for folder in parts[:-1]:
            walked.append(folder)
            child = node["_folders"].get(folder)
            if child is None:
                child = _empty_node(folder, "/".join(walked))
                node["_folders"][folder] = child
            node = child
            node["files"] += 1
            node["pages"] += item.page_count
        node["documents"].append(
            {
                "name": parts[-1],
                "path": item.relpath,
                "pages": item.page_count,
                "bytes": item.size_bytes,
                "error": item.error,
            }
        )
    return _ordered(root)


def _empty_node(name: str, path: str) -> dict[str, Any]:
    return {"name": name, "path": path, "files": 0, "pages": 0, "_folders": {}, "documents": []}


def _ordered(node: dict[str, Any]) -> dict[str, Any]:
    """Turn the folder map into a sorted list, depth first."""
    folders = [_ordered(child) for child in node.pop("_folders").values()]
    node["folders"] = sorted(folders, key=lambda f: natural_key(f["name"]))
    node["documents"].sort(key=lambda d: natural_key(d["name"]))
    return node
