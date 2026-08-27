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
from .config import Settings
from .discover import find_documents
from .models import FileResult, PageResult
from .outputs import (
    now_iso,
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
    pages_total: int = 0
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
        self.discovery_errors: list[dict[str, str]] = []

        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._subscribers: list[queue.Queue] = []
        self._in_flight: dict[int, str] = {}
        self._thread: threading.Thread | None = None
        self._events_file: Path | None = None

    # ---------------------------------------------------------------- paths

    @property
    def run_dir(self) -> Path:
        return self.settings.output_path / "_runs" / self.run_id

    @property
    def previews_dir(self) -> Path:
        return self.settings.output_path / "_previews"

    def page_json_path(self, file_index: int) -> Path:
        return self.run_dir / "pages" / f"{file_index}.json"

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
        with self._lock:
            for item in found:
                result = FileResult(
                    relpath=item.relpath,
                    source_path=str(item.path),
                    size_bytes=item.size_bytes,
                    page_count=item.page_count,
                )
                if item.error:
                    result.status = "failed"
                    result.error = item.error
                    self.discovery_errors.append({"file": item.relpath, "error": item.error})
                self.files.append(result)
            self.totals.files_total = len(self.files)
            self.totals.pages_total = sum(f.page_count for f in self.files)
            self.totals.files_failed = sum(1 for f in self.files if f.status == "failed")
        self.publish({"type": "discovered", "files": [f.summary() for f in self.files]})
        self._publish_progress()

    def _process_all(self) -> None:
        work_by_file: dict[int, list[PageWork]] = {}
        for index, result in enumerate(self.files):
            if result.status == "failed" or result.page_count <= 0:
                continue
            work_by_file[index] = [
                self._page_work(index, result, page_no)
                for page_no in range(1, result.page_count + 1)
            ]

        with ThreadPoolExecutor(max_workers=self.settings.workers, thread_name_prefix="page") as pool:
            futures: dict[int, list[Future]] = {
                index: [pool.submit(self._page_task, work) for work in works]
                for index, works in work_by_file.items()
            }

            # Files are collected in order so results land on disk in the order
            # you would look for them, while the pool keeps every worker busy
            # across file boundaries.
            for index, file_futures in futures.items():
                result = self.files[index]
                if self.cancelled and not any(f.done() for f in file_futures):
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
            preview_dest=(self.settings.output_path / preview_rel) if self.settings.write_previews else None,
            preview_rel=preview_rel if self.settings.write_previews else None,
            page_pdf_dest=(self.run_dir / "tmp" / str(index) / f"p{page_no:05d}.pdf")
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

    def _collect_file(self, index: int, result: FileResult, futures: list[Future]) -> None:
        started = time.monotonic()
        pages: list[PageResult] = []
        for future in futures:
            try:
                page = future.result()
            except Exception as exc:  # noqa: BLE001
                page = PageResult(
                    page_no=len(pages) + 1,
                    source="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    needs_review=True,
                    review_reason="page could not be read",
                )
            if page.source != "skipped":
                pages.append(page)

        result.pages = pages
        result.duration_ms = int((time.monotonic() - started) * 1000)

        if not pages:
            result.status = "cancelled" if self.cancelled else "failed"
            result.error = result.error or "no pages were read"
        else:
            try:
                self._write_outputs(index, result)
                result.status = "cancelled" if self.cancelled and len(pages) < result.page_count else "done"
            except Exception as exc:  # noqa: BLE001
                result.status = "failed"
                result.error = f"could not write output: {type(exc).__name__}: {exc}"

        with self._lock:
            if result.status == "done":
                self.totals.files_done += 1
            elif result.status == "failed":
                self.totals.files_failed += 1

        try:
            self._write_run_copy(index, result)
        except OSError as exc:
            log.warning("could not write the run's copy of %s: %s", result.relpath, exc)

        # Word boxes are on disk now; holding thousands of them for the rest of
        # the run is what turns a large case file into a memory problem.
        for page in result.pages:
            page.words = []

        self.publish({"type": "file", "summary": result.summary()})
        self._publish_progress()
        self._write_manifest()

    def _write_outputs(self, index: int, result: FileResult) -> None:
        out_root = self.settings.output_path
        paths = output_paths(out_root, result.relpath, grouped=self.settings.outputs_grouped_by_type)
        written: dict[str, str] = {}

        if self.settings.write_txt:
            write_text(paths["txt"], result)
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
            page_pdfs = [
                (page, self.run_dir / "tmp" / str(index) / f"p{page.page_no:05d}.pdf")
                for page in result.pages
            ]
            page_pdfs = [(p, path if path.exists() else None) for p, path in page_pdfs]
            write_searchable_pdf(paths["pdf"], source_pdf=source_pdf, pages=page_pdfs)
            written["pdf"] = str(paths["pdf"].relative_to(out_root))
            shutil.rmtree(self.run_dir / "tmp" / str(index), ignore_errors=True)

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
        shutil.rmtree(self.run_dir / "tmp", ignore_errors=True)
        try:
            write_pages_csv(self.run_dir / "pages.csv", self.files)
        except OSError as exc:
            log.warning("could not write pages.csv: %s", exc)
        self._write_manifest()
        if note:
            self.publish({"type": "log", "level": "info", "message": note})
        self.publish({"type": "done", "run": self.snapshot(include_files=True), "note": note})


def load_run(output_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Read a finished run's manifest back off disk.

    Runs outlive the process that made them: the manifest is the record, and
    the UI reads it the same way whether the run finished a second ago or last
    week.
    """
    path = Path(output_dir) / "_runs" / run_id / "manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(output_dir: Path) -> list[dict[str, Any]]:
    root = Path(output_dir) / "_runs"
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest = load_run(Path(output_dir), entry.name)
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
            }
        )
    return runs


def load_document(output_dir: Path, run_id: str, file_index: int) -> dict[str, Any] | None:
    path = Path(output_dir) / "_runs" / run_id / "pages" / f"{file_index}.json"
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
