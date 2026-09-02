"""What this output folder has already read, so a second run does not repeat it.

Reading two thousand pages costs a quarter of an hour. Adding four documents to
the folder afterwards should cost the time of four documents, not another
quarter of an hour, and a run interrupted halfway should be resumable by simply
starting it again.

The ledger is one small JSON file in the output folder, and it is deliberately
conservative: a document is skipped only when the source file is unchanged
(same size, same modification time), every output this run would write is
already on disk, and it was read with the same settings. Anything else — an
edited source, a deleted output, a different resolution or language — is read
again. Being wrong in that direction costs time; being wrong in the other
direction silently leaves someone with a stale document.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

LEDGER_NAME = "completed.json"

# The settings that change what a *page* actually is — its text, and the
# one-page PDF stored beside it. These key the stored pages: change any of
# them and pages read under the old setting are not this run's pages.
# Deliberately excluded: min_confidence, which only decides which pages get
# flagged, and workers, which changes nothing about the outcome.
PAGE_RECIPE_KEYS = (
    "dpi",
    "lang",
    "psm",
    "force_ocr",
    "deskew",
    "denoise",
    "orient",
    "pdf_keeps_source_image",
)

# Everything above, plus the settings that change the *files* written from
# those pages. A document is read again when any of these differ, because the
# file on disk would not be the file this run would write. Kept separate from
# the page keys so that changing how the text is laid out rewrites the outputs
# without throwing away pages that are perfectly good.
RECIPE_KEYS = PAGE_RECIPE_KEYS + ("txt_keeps_layout",)


def recipe(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings.get(key) for key in RECIPE_KEYS}


def page_recipe(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings.get(key) for key in PAGE_RECIPE_KEYS}


@dataclass
class LedgerEntry:
    relpath: str
    size: int
    mtime: float
    pages: int
    outputs: dict[str, str]
    recipe: dict[str, Any]
    run_id: str
    pages_json: str | None = None
    # The source document's content hash. Empty on entries written before the
    # ledger recorded it, and on ones backfilled from an old manifest.
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "mtime": self.mtime,
            "pages": self.pages,
            "outputs": self.outputs,
            "recipe": self.recipe,
            "run_id": self.run_id,
            "pages_json": self.pages_json,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, relpath: str, data: dict[str, Any]) -> "LedgerEntry":
        return cls(
            relpath=relpath,
            size=int(data.get("size", -1)),
            mtime=float(data.get("mtime", -1)),
            pages=int(data.get("pages", 0)),
            outputs=dict(data.get("outputs", {})),
            recipe=dict(data.get("recipe", {})),
            run_id=str(data.get("run_id", "")),
            pages_json=data.get("pages_json"),
            sha256=str(data.get("sha256", "")),
        )


class Ledger:
    """The record of what has been read into one output folder."""

    def __init__(self, work_dir: Path, output_dir: Path | None = None):
        # The ledger lives with the rest of the bookkeeping, but the output
        # paths it records are relative to the folder the results went into.
        # The two are the same folder unless a work folder was chosen.
        self.work_dir = Path(work_dir)
        self.output_dir = Path(output_dir) if output_dir is not None else self.work_dir
        self.path = self.work_dir / "_runs" / LEDGER_NAME
        self._entries: dict[str, LedgerEntry] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._entries = {
                    relpath: LedgerEntry.from_dict(relpath, data)
                    for relpath, data in raw.get("documents", {}).items()
                }
                return
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("could not read the ledger, starting a fresh one: %s", exc)
                self._entries = {}
        self._backfill()

    def _backfill(self) -> None:
        """Build the ledger from earlier runs' manifests the first time.

        Someone who has already read a large folder should not have to read it
        all again just because this feature arrived afterwards. Manifests do not
        record modification times, so those entries carry `mtime = -1` and are
        matched on size alone — a slightly weaker check, applied only to runs
        that predate the ledger.
        """
        runs_dir = self.work_dir / "_runs"
        if not runs_dir.is_dir():
            return

        for entry in sorted(runs_dir.iterdir()):
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("status") not in {"done", "cancelled"}:
                continue

            settings = manifest.get("settings", {})
            run_id = manifest.get("run_id", entry.name)
            for index, file_summary in enumerate(manifest.get("files", [])):
                if file_summary.get("status") != "done":
                    continue
                relpath = file_summary.get("relpath")
                if not relpath:
                    continue
                self._entries[relpath] = LedgerEntry(
                    relpath=relpath,
                    size=int(file_summary.get("size_bytes", -1)),
                    mtime=-1.0,
                    pages=int(file_summary.get("page_count", 0)),
                    outputs=dict(file_summary.get("outputs", {})),
                    recipe=recipe(settings),
                    run_id=run_id,
                    pages_json=f"_runs/{run_id}/pages/{index}.json",
                )

        if self._entries:
            log.info("ledger built from %d earlier results", len(self._entries))
            self.save()

    # ------------------------------------------------------------ queries

    def already_done(
        self,
        relpath: str,
        *,
        size: int,
        mtime: float,
        settings: dict[str, Any],
        wanted: dict[str, Path],
    ) -> LedgerEntry | None:
        """The stored result for this document, if it can still be trusted."""
        entry = self._entries.get(relpath)
        if entry is None:
            return None
        if entry.size != size:
            return None
        # A backfilled entry has no modification time to compare.
        if entry.mtime >= 0 and abs(entry.mtime - mtime) > 1.0:
            return None
        if entry.recipe != recipe(settings):
            return None
        for kind, path in wanted.items():
            if kind not in entry.outputs or not path.is_file():
                return None
        if entry.pages_json and not (self.work_dir / entry.pages_json).is_file():
            return None
        return entry

    def twin_of(
        self,
        sha256: str,
        *,
        relpath: str,
        settings: dict[str, Any],
        wanted: Iterable[str],
        same_name_only: bool = False,
    ) -> LedgerEntry | None:
        """A document already read into this folder with these exact bytes.

        The same exhibit routinely appears twice in a case folder under two
        numbers, and reading it twice produces two identical files at the cost
        of two lots of time. Matching on content rather than on name is what
        makes that free: same bytes and same recipe means the same result, so
        the finished one can simply be copied.

        Only offered when the twin still has every output this run wants, and
        those files are still on disk.
        """
        if not sha256:
            return None
        wanted = set(wanted)
        name = Path(relpath).name
        for entry in self._entries.values():
            if entry.relpath == relpath or entry.sha256 != sha256:
                continue
            if same_name_only and Path(entry.relpath).name != name:
                continue
            if entry.recipe != recipe(settings):
                continue
            if not wanted.issubset(entry.outputs):
                continue
            if all((self.output_dir / entry.outputs[kind]).is_file() for kind in wanted):
                return entry
        return None

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------ writing

    def record(self, entry: LedgerEntry) -> None:
        with self._lock:
            self._entries[entry.relpath] = entry
        self.save()

    def save(self) -> None:
        payload = {
            "note": "What has been read into this folder. Delete this file to read everything again.",
            "documents": {relpath: entry.to_dict() for relpath, entry in sorted(self._entries.items())},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Written whole and then moved, so an interrupted write cannot leave
            # a half-file that would be discarded on the next run.
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            log.warning("could not write the ledger: %s", exc)
