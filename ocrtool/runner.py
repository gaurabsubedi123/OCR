"""One run: discover the folder, read every page, write everything out.

The design rule here is that a long job must always be able to answer three
questions, at any second, without being asked twice:

    how much of it is done          a count, never the word "working"
    what is it doing right now      the file and page names, live
    when will it finish            an estimate from measured throughput

A progress indicator that only appears once there is output to show is
invisible for exactly the period it exists for. So the counts are published
from the moment discovery starts, and discovery exists mainly to produce a real
denominator before any page is read.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__
from .cache import PageCache, document_key
from .config import Settings
from .discover import find_documents
from .ledger import Ledger, LedgerEntry, page_recipe, recipe
from .models import FileResult, PageResult
from .outputs import (
    now_iso,
    output_basenames,
    output_paths,
    write_json,
    write_pages_csv,
    write_searchable_pdf,
    write_text,
)
from .pipeline import PageWork, process_page
from .tesseract import tesseract_version

log = logging.getLogger(__name__)

RunStatus = str  # pending | discovering | running | done | cancelled | failed


def new_run_id() -> str:
    """Sortable, readable, and unique enough for one machine."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class Totals:
    files_total: int = 0
    files_done: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    files_copied: int = 0
    pages_total: int = 0
    pages_skipped: int = 0
    # Pages that were read by a run that stopped before it could write them
    # out, and were picked back up from disk instead of being read again.
    pages_resumed: int = 0
    pages_done: int = 0
    pages_ocr: int = 0
    pages_text_layer: int = 0
    pages_flagged: int = 0
    pages_failed: int = 0
    chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Run:
    """A single execution. Safe to poll from other threads; owns its own files."""

    def __init__(self, settings: Settings, *, run_id: str | None = None):
        self.settings = settings
        self.run_id = run_id or new_run_id()
        self.status: RunStatus = "pending"
        self.error: str | None = None
        self.created_at = now_iso()
        self.finished_at: str | None = None
        self.started_monotonic: float | None = None
        self.elapsed_s: float = 0.0

        self.files: list[FileResult] = []
        self.totals = Totals()
        self.ledger: Ledger | None = None
        self.discovery_errors: list[dict[str, str]] = []

        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._subscribers: list[queue.Queue] = []
        self._in_flight: dict[int, str] = {}
        self._thread: threading.Thread | None = None
        self._events_file: Path | None = None
        # Which cache folder each document's pages live in, keyed by file
        # index. Two documents with the same bytes and the same recipe share
        # one, which is what makes a copy free.
        self._cache_keys: dict[int, str] = {}
        # A document that is read -> the identical documents whose outputs are
        # written from its pages once it is done.
        self._twins: dict[int, list[int]] = {}
        # Pages recovered from a stopped run, by file index and page number.
        self._resumed: dict[int, dict[int, PageResult]] = {}
        # Each document's content hash, so the ledger can record it.
        self._sha_by_index: dict[int, str] = {}
        # The name each document's outputs are built from, decided across the
        # whole folder once discovery is in, because a name that clashes can
        # only be spotted next to the one it clashes with. Empty until then,
        # and a missing entry falls back to the document's own stem.
        self._basenames: dict[str, str] = {}

    # ---------------------------------------------------------------- paths

    @property
    def run_dir(self) -> Path:
        return self.settings.work_path / "_runs" / self.run_id

    @property
    def previews_dir(self) -> Path:
        return self.settings.work_path / "_previews"

    def page_json_path(self, file_index: int) -> Path:
        return self.run_dir / "pages" / f"{file_index}.json"

    def wanted_paths(self, relpath: str) -> dict[str, Path]:
        """Where this document's outputs go, limited to the kinds this run writes."""
        paths = output_paths(
            self.settings.output_path,
            relpath,
            layout=self.settings.output_layout,
            basename=self._basenames.get(relpath),
        )
        return {
            kind: path
            for kind, path in paths.items()
            if getattr(self.settings, f"write_{kind}")
        }

    def _cache_for(self, file_index: int) -> PageCache:
        return PageCache(self.settings.work_path, self._cache_keys[file_index])

    # ---------------------------------------------------------------- control

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("run already started")
        self._thread = threading.Thread(target=self._run, name=f"run-{self.run_id}", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        self.publish({"type": "log", "level": "warn", "message": "Stopping — finishing the pages already in flight."})

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---------------------------------------------------------------- events

    def subscribe(self) -> queue.Queue:
        """A queue of events for one browser tab, primed with the current state
        so a tab that joins late — or reconnects — is never blank."""
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.append(q)
        q.put({"type": "snapshot", "run": self.snapshot(include_files=True)})
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("run_id", self.run_id)
        event.setdefault("at", time.time())
        with self._lock:
            subscribers = list(self._subscribers)
            events_file = self._events_file
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # A tab that stopped reading must not slow the run down.
                pass
        if events_file is not None and event.get("type") in {"file", "done", "log", "phase"}:
            try:
                with events_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass

    # ---------------------------------------------------------------- state

    def snapshot(self, *, include_files: bool = False) -> dict[str, Any]:
        with self._lock:
            elapsed = self._elapsed()
            done = self.totals.pages_done
            rate = done / elapsed if elapsed > 0 and done else 0.0
            remaining = max(0, self.totals.pages_total - done)
            eta = remaining / rate if rate > 0 else None
            snap: dict[str, Any] = {
                "run_id": self.run_id,
                "status": self.status,
                "error": self.error,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "elapsed_s": round(elapsed, 1),
                "pages_per_second": round(rate, 2),
                "eta_s": round(eta) if eta is not None else None,
                "totals": self.totals.to_dict(),
                "in_flight": sorted(self._in_flight.values()),
                "settings": self.settings.to_dict(),
                "input_dir": str(self.settings.input_path),
                "output_dir": str(self.settings.output_path),
                "work_dir": str(self.settings.work_path),
                "discovery_errors": list(self.discovery_errors),
            }
            if include_files:
                snap["files"] = [f.summary() for f in self.files]
            return snap

    def _elapsed(self) -> float:
        if self.started_monotonic is None:
            return 0.0
        if self.finished_at is not None:
            return self.elapsed_s
        return time.monotonic() - self.started_monotonic

    # ---------------------------------------------------------------- the run

    def _run(self) -> None:
        self.started_monotonic = time.monotonic()
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._events_file = self.run_dir / "events.jsonl"
            (self.run_dir / "settings.json").write_text(
                json.dumps(self.settings.to_dict(), indent=2), encoding="utf-8"
            )

            if self.settings.skip_already_done:
                self.ledger = Ledger(self.settings.work_path, self.settings.output_path)

            self._set_status("discovering")
            self.publish({"type": "phase", "phase": "discovering", "message": f"Looking through {self.settings.input_path}"})
            self._discover()

            if not self.files:
                self._finish("done", note="No supported documents found in that folder.")
                return

            self._set_status("running")
            self.publish(
                {
                    "type": "phase",
                    "phase": "running",
                    "message": (
                        f"{self.totals.files_total} documents, {self.totals.pages_total} pages. "
                        f"Writing to {self.settings.output_path}"
                    ),
                }
            )
            self._process_all()
            self._finish("cancelled" if self.cancelled else "done")
        except Exception as exc:  # noqa: BLE001 - the run reports its own death
            log.exception("run %s failed", self.run_id)
            self.error = f"{type(exc).__name__}: {exc}"
            self._finish("failed")

    def _set_status(self, status: RunStatus) -> None:
        with self._lock:
            self.status = status

    def _discover(self) -> None:
        found = find_documents(self.settings.input_path, recursive=self.settings.recursive)
        # Before the loop: _reuse_if_done asks where this document's outputs
        # go, and the answer depends on what else was found.
        self._basenames = output_basenames([item.relpath for item in found])
        with self._lock:
            for index, item in enumerate(found):
                result = FileResult(
                    relpath=item.relpath,
                    source_path=str(item.path),
                    size_bytes=item.size_bytes,
                    page_count=item.page_count,
                    modified=item.modified,
                )
                if item.error:
                    result.status = "failed"
                    result.error = item.error
                    self.discovery_errors.append({"file": item.relpath, "error": item.error})
                else:
                    self._reuse_if_done(index, result)
                self.files.append(result)

        # Copies and stopped-run pages are worked out with the lock released:
        # both touch the disk, and nothing else is running yet.
        self._plan_work(found)

        with self._lock:
            self.totals.files_total = len(self.files)
            self.totals.files_failed = sum(1 for f in self.files if f.status == "failed")
            self.totals.files_skipped = sum(1 for f in self.files if f.status == "skipped")
            self.totals.files_copied = sum(1 for f in self.files if f.status == "copied")
            # Skipped pages are not counted in the denominator: the progress bar
            # is about the work this run is doing, and counting pages nobody is
            # reading would make it crawl toward a number it never reaches. The
            # same goes for the pages of a copy, and for pages already read by a
            # run that stopped.
            self.totals.pages_skipped = sum(
                f.page_count for f in self.files if f.status == "skipped"
            )
            self.totals.pages_resumed = sum(f.resumed_pages for f in self.files)
            self.totals.pages_total = sum(
                f.page_count - f.resumed_pages
                for f in self.files
                if f.status not in {"skipped", "failed", "copied"}
            )

        if self.totals.files_skipped:
            self.publish({
                "type": "log",
                "level": "info",
                "message": (
                    f"{self.totals.files_skipped} of {self.totals.files_total} documents "
                    f"({self.totals.pages_skipped} pages) were already read into this folder "
                    "and are being left alone."
                ),
            })
        if self.totals.files_copied:
            self.publish({
                "type": "log",
                "level": "info",
                "message": (
                    f"{self.totals.files_copied} documents are byte-for-byte copies of "
                    "another one here and will be written from it rather than read again."
                ),
            })
        if self.totals.pages_resumed:
            self.publish({
                "type": "log",
                "level": "info",
                "message": (
                    f"{self.totals.pages_resumed} pages were already read by a run that "
                    "stopped, and are being picked up rather than read again."
                ),
            })
        self.publish({"type": "discovered", "files": [f.summary() for f in self.files]})
        self._publish_progress()

    def _reuse_if_done(self, index: int, result: FileResult) -> None:
        """Mark a document as already read, if this folder can prove it is."""
        if self.ledger is None:
            return
        wanted = self.wanted_paths(result.relpath)
        entry = self.ledger.already_done(
            result.relpath,
            size=result.size_bytes,
            mtime=result.modified,
            settings=self.settings.to_dict(),
            wanted=wanted,
        )
        if entry is None:
            return

        result.status = "skipped"
        result.outputs = dict(entry.outputs)
        result.skipped_from_run = entry.run_id
        if entry.pages:
            result.page_count = entry.pages

        # The viewer opens a document through the run being looked at, so the
        # earlier run's page text is copied across. Otherwise every skipped
        # document would be an empty page in the results.
        if entry.pages_json:
            source = self.settings.work_path / entry.pages_json
            destination = self.page_json_path(index)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            except OSError as exc:
                log.warning("could not carry over the previous result for %s: %s", result.relpath, exc)

    # ------------------------------------------------------- planning the work

    def _plan_work(self, found: list[Any]) -> None:
        """Work out, before a single page is read, what actually has to be read.

        Three ways a document escapes being read, and all three are decided
        here so the page count the progress bar starts from is the real one:

          it is a copy of a document already read into this folder
          it is a copy of another document in this same run
          some of its pages were read by a run that stopped before writing them

        The first two are the same question asked of different places, and both
        are answered by the content hash rather than by the name: the same
        exhibit under two numbers is the ordinary case, not the exotic one.
        """
        settings = self.settings.to_dict()
        # The pages are keyed on what makes a page, not on what makes a file:
        # changing how the text is laid out must not throw away pages already
        # read at the same resolution in the same language.
        the_recipe = page_recipe(settings)
        primary_by_key: dict[str, int] = {}

        for index, item in enumerate(found):
            result = self.files[index]
            self._sha_by_index[index] = item.sha256
            if result.status != "pending" or result.page_count <= 0:
                continue

            # One identity decides both questions, which is what keeps them
            # consistent: two documents are the same document exactly when
            # they share it, and documents that share it share their stored
            # pages. A document whose bytes could not be read still gets an
            # identity, so it can still resume — it just cannot be recognised
            # as a copy. Size and modification time stand in for the content
            # there, which is the same bar the skip rule already sets.
            identity = item.sha256 or f"path:{result.relpath}:{result.size_bytes}:{result.modified}"
            if self.settings.duplicates == "name":
                # Same contents *and* same name.
                identity = f"{identity}|{Path(result.relpath).name}"
            elif self.settings.duplicates == "off":
                identity = f"{identity}|{result.relpath}"
            key = document_key(identity, the_recipe)
            self._cache_keys[index] = key

            if self.settings.duplicates != "off":
                twin_index = primary_by_key.get(key)
                if twin_index is not None:
                    result.status = "copied"
                    result.duplicate_of = self.files[twin_index].relpath
                    result.page_count = self.files[twin_index].page_count
                    self._twins.setdefault(twin_index, []).append(index)
                    continue

                if item.sha256 and self.ledger is not None:
                    entry = self.ledger.twin_of(
                        item.sha256,
                        relpath=result.relpath,
                        settings=settings,
                        wanted=self.wanted_paths(result.relpath),
                        same_name_only=self.settings.duplicates == "name",
                    )
                    if entry is not None and self._copy_from_twin(index, result, entry):
                        continue

            primary_by_key[key] = index

            stored = self._cache_for(index).load(
                page_count=result.page_count, need_pdf=self.settings.write_pdf
            )
            if stored:
                self._resumed[index] = stored
                result.resumed_pages = len(stored)

    def _copy_from_twin(self, index: int, result: FileResult, entry: LedgerEntry) -> bool:
        """Write this document's outputs from an identical one already on disk.

        The PDF and the text are byte-for-byte what this document would have
        produced, so they are copied. The JSON is rewritten rather than copied,
        because it names the document inside itself and a file that claims to
        be a different document is worse than no file.

        Returns False if anything goes wrong, and the document is then read
        normally — this is an optimisation, never a source of truth.
        """
        out_root = self.settings.output_path
        wanted = self.wanted_paths(result.relpath)
        written: dict[str, str] = {}
        try:
            for kind, dest in wanted.items():
                source = out_root / entry.outputs[kind]
                dest.parent.mkdir(parents=True, exist_ok=True)
                if kind == "json":
                    payload = json.loads(source.read_text(encoding="utf-8"))
                    document = payload.get("document", {})
                    document["relpath"] = result.relpath
                    document["source_path"] = result.source_path
                    document["duplicate_of"] = entry.relpath
                    payload["generated_at"] = now_iso()
                    payload["settings"] = self.settings.to_dict()
                    dest.write_text(
                        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                else:
                    shutil.copyfile(source, dest)
                written[kind] = str(dest.relative_to(out_root))
        except (OSError, KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning(
                "could not write %s from the identical %s, reading it instead: %s",
                result.relpath,
                entry.relpath,
                exc,
            )
            return False

        result.status = "copied"
        result.duplicate_of = entry.relpath
        result.outputs = written
        if entry.pages:
            result.page_count = entry.pages
        if entry.pages_json:
            result.pages = self._pages_from_json(self.settings.work_path / entry.pages_json)

        self._record_done(index, result)
        return True

    @staticmethod
    def _pages_from_json(path: Path) -> list[PageResult]:
        """The page results a run stored for one document, or nothing."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("could not read stored pages from %s: %s", path, exc)
            return []
        return [PageResult.from_dict(page) for page in data.get("pages", [])]

    def _record_done(self, index: int, result: FileResult) -> None:
        """The run's own copy of the page text, and the folder's ledger entry."""
        try:
            self._write_run_copy(index, result)
        except OSError as exc:
            log.warning("could not write the run's copy of %s: %s", result.relpath, exc)

        if self.ledger is not None and result.status in {"done", "copied"}:
            self.ledger.record(
                LedgerEntry(
                    relpath=result.relpath,
                    size=result.size_bytes,
                    mtime=result.modified,
                    pages=len(result.pages) or result.page_count,
                    outputs=dict(result.outputs),
                    recipe=recipe(self.settings.to_dict()),
                    run_id=self.run_id,
                    pages_json=f"_runs/{self.run_id}/pages/{index}.json",
                    sha256=self._sha_by_index.get(index, ""),
                )
            )

    def _process_all(self) -> None:
        work_by_file: dict[int, list[PageWork]] = {}
        for index, result in enumerate(self.files):
            if result.status != "pending" or result.page_count <= 0:
                continue
            already = self._resumed.get(index, {})
            cache = self._cache_for(index)
            cache.note(
                relpath=result.relpath,
                page_count=result.page_count,
                recipe=page_recipe(self.settings.to_dict()),
            )
            work_by_file[index] = [
                self._page_work(index, result, page_no)
                for page_no in range(1, result.page_count + 1)
                if page_no not in already
            ]

        with ThreadPoolExecutor(max_workers=self.settings.workers, thread_name_prefix="page") as pool:
            futures: dict[int, list[tuple[int, Future]]] = {
                index: [(work.page_no, pool.submit(self._page_task, work)) for work in works]
                for index, works in work_by_file.items()
            }

            # Files are collected in order so results land on disk in the order
            # you would look for them, while the pool keeps every worker busy
            # across file boundaries.
            for index, file_futures in futures.items():
                result = self.files[index]
                started = any(f.done() for _, f in file_futures)
                if self.cancelled and not started and not self._resumed.get(index):
                    result.status = "cancelled"
                    self.publish({"type": "file", "summary": result.summary()})
                    continue
                self._collect_file(index, result, file_futures)

    def _page_work(self, index: int, result: FileResult, page_no: int) -> PageWork:
        preview_rel = f"_previews/{result.relpath}/p{page_no:04d}.jpg"
        return PageWork(
            file_index=index,
            relpath=result.relpath,
            source_path=Path(result.source_path),
            page_no=page_no,
            preview_dest=(self.settings.work_path / preview_rel) if self.settings.write_previews else None,
            preview_rel=preview_rel if self.settings.write_previews else None,
            # The page's PDF goes into the document's cache folder, not this
            # run's, so a later run can find it. It is written before the page
            # is stored, which makes the stored page the proof that it is whole.
            page_pdf_dest=self._cache_for(index).page_pdf(page_no)
            if self.settings.write_pdf
            else None,
        )

    def _page_task(self, work: PageWork) -> PageResult:
        if self.cancelled:
            return PageResult(page_no=work.page_no, source="skipped", review_reason="run stopped")
        label = f"{work.relpath} · page {work.page_no}"
        key = threading.get_ident()
        with self._lock:
            self._in_flight[key] = label
        try:
            result = process_page(work, self.settings)
        finally:
            with self._lock:
                self._in_flight.pop(key, None)

        # On disk immediately, and only then counted. An hour of reading must
        # never live solely in this process again.
        if result.source != "skipped":
            self._cache_for(work.file_index).store(result)

        self._count_page(work, result)
        return result

    def _count_page(self, work: PageWork, page: PageResult) -> None:
        if page.source == "skipped":
            return
        # A file is "running" from its first finished page, not from the moment
        # the coordinator gets round to collecting it — otherwise a file being
        # worked on right now shows as pending in the list.
        file_result = self.files[work.file_index]
        became_running = False
        with self._lock:
            if file_result.status == "pending":
                file_result.status = "running"
                became_running = True
        if became_running:
            self.publish({"type": "file", "summary": file_result.summary()})

        with self._lock:
            # Per document as well as in total, so that a snapshot taken in the
            # middle of a document tells the truth about it.
            file_result.pages_read += 1
            self.totals.pages_done += 1
            self.totals.chars += page.char_count
            if page.source == "ocr":
                self.totals.pages_ocr += 1
            elif page.source == "text-layer":
                self.totals.pages_text_layer += 1
            elif page.source == "failed":
                self.totals.pages_failed += 1
            if page.needs_review:
                self.totals.pages_flagged += 1
            done = self.totals.pages_done

        self.publish(
            {
                "type": "page",
                "file": work.relpath,
                "file_index": work.file_index,
                "page": page.page_no,
                "source": page.source,
                "confidence": page.confidence,
                "chars": page.char_count,
                "needs_review": page.needs_review,
                "review_reason": page.review_reason,
                "error": page.error,
                "ms": page.duration_ms,
            }
        )
        # Every page moves the counter; the summary line is throttled so a fast
        # run does not spend its time serialising its own progress.
        if done % 5 == 0 or done == self.totals.pages_total:
            self._publish_progress()

    def _collect_file(
        self, index: int, result: FileResult, futures: list[tuple[int, Future]]
    ) -> None:
        started = time.monotonic()
        # Pages carried over from a stopped run go in first, and the ones this
        # run read are added by number rather than appended, so a document that
        # was resumed comes out in page order like any other.
        pages: dict[int, PageResult] = dict(self._resumed.get(index, {}))
        for page_no, future in futures:
            try:
                page = future.result()
            except Exception as exc:  # noqa: BLE001
                page = PageResult(
                    page_no=page_no,
                    source="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    needs_review=True,
                    review_reason="page could not be read",
                )
            if page.source != "skipped":
                pages[page.page_no] = page

        result.pages = [pages[page_no] for page_no in sorted(pages)]
        result.duration_ms = int((time.monotonic() - started) * 1000)

        if not result.pages:
            result.status = "cancelled" if self.cancelled else "failed"
            result.error = result.error or "no pages were read"
        else:
            try:
                self._write_outputs(index, result)
                result.status = (
                    "cancelled"
                    if self.cancelled and len(result.pages) < result.page_count
                    else "done"
                )
            except Exception as exc:  # noqa: BLE001
                result.status = "failed"
                result.error = f"could not write output: {type(exc).__name__}: {exc}"

        with self._lock:
            if result.status == "done":
                self.totals.files_done += 1
            elif result.status == "failed":
                self.totals.files_failed += 1

        self._record_done(index, result)

        if result.status == "done":
            # Identical documents are written from these same pages and the
            # same page PDFs, which is the whole saving: one read, many copies.
            for twin_index in self._twins.get(index, []):
                self._write_twin(twin_index, result)
            # Only now is the cache spent. A document that failed or was
            # stopped keeps its pages, which is the point of them.
            self._cache_for(index).clear()
        else:
            # The document these were going to be copied from never arrived.
            # A row saying "copied" with nothing on disk is worse than a row
            # saying what actually happened.
            for twin_index in self._twins.get(index, []):
                self._abandon_twin(twin_index, result)

        # Word boxes are on disk now; holding thousands of them for the rest of
        # the run is what turns a large case file into a memory problem.
        for page in result.pages:
            page.words = []

        self.publish({"type": "file", "summary": result.summary()})
        self._publish_progress()
        self._write_manifest()

    def _abandon_twin(self, index: int, primary: FileResult) -> None:
        """Say plainly that a copy was not made, and why."""
        result = self.files[index]
        result.status = primary.status if primary.status == "cancelled" else "failed"
        result.error = f"{primary.relpath}, which this is a copy of, was not written"
        with self._lock:
            self.totals.files_copied -= 1
            if result.status == "failed":
                self.totals.files_failed += 1
        self.publish({"type": "file", "summary": result.summary()})

    def _write_twin(self, index: int, primary: FileResult) -> None:
        """Write a document that is byte-for-byte the one just read."""
        result = self.files[index]
        result.pages = primary.pages
        result.page_count = primary.page_count
        try:
            self._write_outputs(index, result)
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.error = f"could not write output: {type(exc).__name__}: {exc}"
            # It was counted as a copy when the work was planned; it is not one.
            with self._lock:
                self.totals.files_copied -= 1
                self.totals.files_failed += 1
        else:
            result.status = "copied"
            self._record_done(index, result)
        self.publish({"type": "file", "summary": result.summary()})

    def _write_outputs(self, index: int, result: FileResult) -> None:
        out_root = self.settings.output_path
        paths = output_paths(
            out_root,
            result.relpath,
            layout=self.settings.output_layout,
            basename=self._basenames.get(result.relpath),
        )
        written: dict[str, str] = {}

        if self.settings.write_txt:
            write_text(paths["txt"], result, keep_layout=self.settings.txt_keeps_layout)
            written["txt"] = str(paths["txt"].relative_to(out_root))

        if self.settings.write_json:
            write_json(
                paths["json"],
                result,
                settings=self.settings.to_dict(),
                include_words=self.settings.include_word_boxes,
            )
            written["json"] = str(paths["json"].relative_to(out_root))

        if self.settings.write_pdf:
            source_pdf = Path(result.source_path) if result.source_path.lower().endswith(".pdf") else None
            cache = self._cache_for(index)
            page_pdfs = [(page, cache.page_pdf(page.page_no)) for page in result.pages]
            page_pdfs = [(p, path if path.exists() else None) for p, path in page_pdfs]
            write_searchable_pdf(paths["pdf"], source_pdf=source_pdf, pages=page_pdfs)
            written["pdf"] = str(paths["pdf"].relative_to(out_root))

        result.outputs = written

    def _write_run_copy(self, index: int, result: FileResult) -> None:
        """The run's own copy of the page text.

        Written after the file's status is final, and written whichever
        deliverables were switched off, so the viewer never depends on the
        output settings someone chose for this particular run.
        """
        page_json = self.page_json_path(index)
        page_json.parent.mkdir(parents=True, exist_ok=True)
        page_json.write_text(
            json.dumps(result.to_dict(include_words=False), ensure_ascii=False),
            encoding="utf-8",
        )

    def _publish_progress(self) -> None:
        self.publish({"type": "progress", "run": self.snapshot()})

    def _write_manifest(self) -> None:
        manifest = self.snapshot(include_files=True)
        manifest["tool_version"] = __version__
        manifest["tesseract"] = tesseract_version()
        try:
            (self.run_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("could not write manifest: %s", exc)

    def _finish(self, status: RunStatus, *, note: str | None = None) -> None:
        if self.started_monotonic is not None:
            self.elapsed_s = time.monotonic() - self.started_monotonic
        self.finished_at = now_iso()
        self._set_status(status)
        # Nothing is deleted here. Whatever is still in the cache belongs to a
        # document that did not finish, and it is the only reason stopping a
        # long run is now cheap.
        try:
            write_pages_csv(self.run_dir / "pages.csv", self.files)
        except OSError as exc:
            log.warning("could not write pages.csv: %s", exc)
        self._write_manifest()
        if note:
            self.publish({"type": "log", "level": "info", "message": note})
        self.publish({"type": "done", "run": self.snapshot(include_files=True), "note": note})


def load_run(work_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Read a finished run's manifest back off disk.

    Runs outlive the process that made them: the manifest is the record, and
    the UI reads it the same way whether the run finished a second ago or last
    week. `work_dir` is where the bookkeeping went, which is the output folder
    unless a separate one was chosen.
    """
    path = Path(work_dir) / "_runs" / run_id / "manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(work_dir: Path) -> list[dict[str, Any]]:
    root = Path(work_dir) / "_runs"
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest = load_run(Path(work_dir), entry.name)
        if manifest is None:
            continue
        runs.append(
            {
                "run_id": manifest.get("run_id", entry.name),
                "status": manifest.get("status"),
                "created_at": manifest.get("created_at"),
                "finished_at": manifest.get("finished_at"),
                "elapsed_s": manifest.get("elapsed_s"),
                "totals": manifest.get("totals", {}),
                "input_dir": manifest.get("input_dir"),
                "output_dir": manifest.get("output_dir"),
                "work_dir": manifest.get("work_dir"),
            }
        )
    return runs


def load_document(work_dir: Path, run_id: str, file_index: int) -> dict[str, Any] | None:
    path = Path(work_dir) / "_runs" / run_id / "pages" / f"{file_index}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_blocking(settings: Settings, *, on_event: Callable[[dict[str, Any]], None] | None = None) -> Run:
    """Run to completion on this thread. Used by the CLI and the tests."""
    run = Run(settings)
    q = run.subscribe() if on_event else None
    run.start()
    if q is not None and on_event is not None:
        for event in _drain(run, q):
            on_event(event)
    run.join()
    return run


def _drain(run: Run, q: queue.Queue) -> Iterator[dict[str, Any]]:
    while True:
        try:
            event = q.get(timeout=0.25)
        except queue.Empty:
            if run.finished_at is not None:
                return
            continue
        yield event
        if event.get("type") == "done":
            return
