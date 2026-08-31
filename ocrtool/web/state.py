"""What the web process remembers between requests, and between restarts.

Two layers, on purpose:

  in memory   the runs this process is executing right now, with their live
              progress and their event subscribers
  on disk     a short list of runs this machine has done, so closing the
              browser — or the server — does not lose the results

The results themselves are never in either layer. They live in the output
folder, which is the whole point: the output folder is the product, and the UI
is a way of looking at it, not a place things are kept.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..config import state_dir
from ..runner import Run, list_runs, load_run

RECENT_LIMIT = 40


class Registry:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.RLock()

    @property
    def _recent_path(self) -> Path:
        # Resolved per call rather than at construction, so redirecting the
        # state folder takes effect even though the registry is created when
        # the module is first imported.
        return state_dir() / "recent.json"

    # ------------------------------------------------------------ live runs

    def add(self, run: Run) -> None:
        with self._lock:
            self._runs[run.run_id] = run
        self.remember(run)

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def active(self) -> list[Run]:
        with self._lock:
            return [r for r in self._runs.values() if r.finished_at is None]

    # ------------------------------------------------------- remembered runs

    def _read_recent(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._recent_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def remember(self, run: Run) -> None:
        entry = {
            "run_id": run.run_id,
            "input_dir": str(run.settings.input_path),
            "output_dir": str(run.settings.output_path),
            "work_dir": str(run.settings.work_path),
            "created_at": run.created_at,
        }
        with self._lock:
            recent = [r for r in self._read_recent() if r.get("run_id") != run.run_id]
            recent.insert(0, entry)
            try:
                self._recent_path.write_text(
                    json.dumps(recent[:RECENT_LIMIT], indent=2), encoding="utf-8"
                )
            except OSError:
                pass

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent runs, live ones first, each with whatever the manifest knows.

        A run whose output folder has since been deleted is dropped rather than
        listed as broken — the folder being gone is a decision someone made.
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        for run in sorted(self.active(), key=lambda r: r.created_at, reverse=True):
            out.append({**run.snapshot(), "live": True})
            seen.add(run.run_id)

        for entry in self._read_recent():
            run_id = entry.get("run_id", "")
            if run_id in seen:
                continue
            work_dir = Path(entry.get("work_dir") or entry.get("output_dir", ""))
            manifest = load_run(work_dir, run_id) if work_dir.is_dir() else None
            if manifest is None:
                continue
            manifest["live"] = False
            out.append(manifest)
            seen.add(run_id)
            if len(out) >= limit:
                break
        return out[:limit]

    def find_output_dir(self, run_id: str) -> Path | None:
        """Which output folder a run wrote its results into — the live run
        knows, and a finished one is looked up in the recents file."""
        return self._find_dir(run_id, "output_dir")

    def find_work_dir(self, run_id: str) -> Path | None:
        """Which folder holds a run's `_runs/` and `_previews/`.

        The same as the output folder unless a separate work folder was chosen,
        and runs recorded before that was possible have only the one — so the
        output folder is the fallback rather than a failure.
        """
        return self._find_dir(run_id, "work_dir") or self._find_dir(run_id, "output_dir")

    def _find_dir(self, run_id: str, key: str) -> Path | None:
        live = self.get(run_id)
        if live is not None:
            return live.settings.work_path if key == "work_dir" else live.settings.output_path
        for entry in self._read_recent():
            if entry.get("run_id") == run_id:
                path = Path(entry.get(key) or "")
                if str(path) and path.is_dir():
                    return path
        return None

    def runs_in(self, work_dir: Path) -> list[dict[str, Any]]:
        return list_runs(work_dir)
