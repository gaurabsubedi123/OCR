"""Every page written to disk the moment it is read, so stopping costs nothing.

A 4,586-page hospital record takes about two and a half hours. Stopping it an
hour in used to throw away every one of those pages: the text, the confidence
and the word boxes lived in memory until the whole document finished, and only
then were they written anywhere. An hour of reading died with the process.

So each page is stored here as soon as it comes back, and a later run picks up
from the first page that never arrived. The page's PDF is written first and its
JSON second, which makes the JSON the commit marker: if the JSON is there, the
PDF beside it is whole.

The folder is keyed by what the page actually depends on — the bytes of the
source document and the recipe that read it — and not by where the document
sits. Two consequences, both wanted:

  a document that moved or was renamed still finds its own pages
  a document that is byte-for-byte a copy of another shares its pages, and is
  never read twice

Sources are hashed rather than trusted by size and date, because "same file"
has to mean the same file for this to be safe. A page folder is deleted once
the document it belongs to has been written out, so the cache holds only work
that is genuinely unfinished.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .models import PageResult

log = logging.getLogger(__name__)

CACHE_DIRNAME = "_cache"

# 64 KB at a time: large enough that the syscall overhead disappears, small
# enough that hashing a 300 MB scan never holds it in memory.
_HASH_CHUNK = 64 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def document_key(identity: str, recipe: dict[str, Any]) -> str:
    """The cache folder name for this document read this way.

    `identity` is whatever the caller has decided makes two documents the same
    document — the content hash on its own, or the content hash with a name or
    a path folded in. It is hashed rather than truncated, because truncating it
    would silently discard everything after the first few characters and quietly
    make every document look identical.

    The recipe is folded in so that changing the resolution or the language
    cannot silently resume onto pages read at the old setting.
    """
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    recipe_json = json.dumps(recipe, sort_keys=True, default=str)
    recipe_hash = hashlib.sha256(recipe_json.encode("utf-8")).hexdigest()
    return f"{identity_hash[:16]}-{recipe_hash[:8]}"


class PageCache:
    """The unfinished pages of one document, in one folder."""

    def __init__(self, output_dir: Path, key: str):
        self.output_dir = Path(output_dir)
        self.key = key
        self.dir = self.output_dir / CACHE_DIRNAME / key

    # ------------------------------------------------------------ locations

    def page_json(self, page_no: int) -> Path:
        return self.dir / f"p{page_no:05d}.json"

    def page_pdf(self, page_no: int) -> Path:
        return self.dir / f"p{page_no:05d}.pdf"

    # ------------------------------------------------------------ writing

    def note(self, *, relpath: str, page_count: int, recipe: dict[str, Any]) -> None:
        """Leave a human-readable marker saying what this folder is.

        A folder of hex-named page files with no explanation is the kind of
        thing someone deletes to free space and then regrets.
        """
        payload = {
            "note": (
                "Pages of a document that has not finished being written out. "
                "Run ocrtool again on the same folder and it carries on from here. "
                "Safe to delete — it only costs the time to read those pages again."
            ),
            "document": relpath,
            "pages": page_count,
            "recipe": recipe,
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "about.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("could not describe the page cache for %s: %s", relpath, exc)

    def store(self, page: PageResult) -> None:
        """Write one finished page. Called from a worker thread, so it writes
        whole and then moves: a page file is either absent or complete."""
        destination = self.page_json(page.page_no)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".writing")
            temporary.write_text(
                json.dumps(page.to_dict(include_words=True), ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(destination)
        except OSError as exc:
            # Losing the ability to resume is worth a warning, not a failed run.
            log.warning("could not store page %s: %s", page.page_no, exc)

    # ------------------------------------------------------------ reading

    def load(self, *, page_count: int, need_pdf: bool) -> dict[int, PageResult]:
        """The pages already on disk for this document.

        A page counts as present only if its own PDF is there too when this run
        is writing PDFs — otherwise the document would be assembled with a hole
        in it that nothing would report.
        """
        if not self.dir.is_dir():
            return {}

        found: dict[int, PageResult] = {}
        for page_no in range(1, page_count + 1):
            path = self.page_json(page_no)
            if not path.is_file():
                continue
            if need_pdf and not self.page_pdf(page_no).is_file():
                continue
            try:
                page = PageResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("ignoring an unreadable stored page %s: %s", path.name, exc)
                continue
            # A page stored from a run that was stopped mid-page is not a result.
            if page.source == "skipped":
                continue
            found[page_no] = page
        return found

    # ------------------------------------------------------------ clean-up

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        parent = self.dir.parent
        try:
            # Leave no empty _cache behind once the last document is written.
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
