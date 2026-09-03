"""The local web interface.

Flask, server-rendered pages, and plain JavaScript — no build step, no bundler,
nothing fetched from a CDN. You clone the folder, install four packages, and it
runs. That is a deliberate constraint: this tool is useful precisely because it
does not need anything outside the machine it is on.

Progress reaches the browser over Server-Sent Events, which is a one-line
generator on this side and one line of JavaScript on the other. Every connection
is primed with a full snapshot, so a tab opened halfway through a run — or
reconnected after a laptop lid was closed — shows the true state immediately
instead of waiting for the next event.
"""

from __future__ import annotations

import json
import mimetypes
import queue
from pathlib import Path
from typing import Any, Iterator

from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

from .. import __version__
from ..config import (
    DEFAULT_DPI,
    DEFAULT_DUPLICATES,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_PSM,
    RESERVED_DIRS,
    Settings,
    default_folders,
    default_workers,
)
from ..outputs import DEFAULT_LAYOUT, output_basenames, output_paths
from ..discover import document_tree, find_documents
from ..ledger import Ledger
from ..runner import Run, load_document, load_run
from ..tesseract import installed_languages, tesseract_path, tesseract_version
from .state import Registry

bp = Blueprint("ocrtool", __name__)
registry = Registry()

# Uploads are staged here, inside the output folder, so everything a run
# touched stays in one place you can point a colleague at or delete wholesale.
UPLOAD_DIRNAME = "_uploads"


def create_app(
    *,
    default_input: str | None = None,
    default_output: str | None = None,
    default_work: str | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB of scans in one go
    folders = default_folders()
    app.config["DEFAULT_INPUT"] = default_input or folders["input"]
    app.config["DEFAULT_OUTPUT"] = default_output or folders["output"]
    app.config["DEFAULT_WORK"] = default_work or folders.get("work", "")
    app.config["JSON_SORT_KEYS"] = False
    app.register_blueprint(bp)
    return app


# ------------------------------------------------------------------ pages


@bp.get("/")
def index() -> str:
    return render_template(
        "index.html",
        version=__version__,
        tesseract=tesseract_version(),
        languages=installed_languages() or ["eng"],
        defaults={
            "input": request.args.get("input", "") or _app_config("DEFAULT_INPUT"),
            "output": request.args.get("output", "") or _app_config("DEFAULT_OUTPUT"),
            "work": request.args.get("work", "") or _app_config("DEFAULT_WORK"),
            "dpi": DEFAULT_DPI,
            "psm": DEFAULT_PSM,
            "workers": default_workers(),
            "min_confidence": DEFAULT_MIN_CONFIDENCE,
        },
        recent=registry.recent(),
        home=str(Path.home()),
    )


@bp.get("/runs/<run_id>")
def run_page(run_id: str) -> str:
    if registry.find_work_dir(run_id) is None:
        abort(404, "That run is not on this machine, or its folders have moved.")
    return render_template("run.html", run_id=run_id, version=__version__)


@bp.get("/runs/<run_id>/documents/<int:index>")
def document_page(run_id: str, index: int) -> str:
    if registry.find_work_dir(run_id) is None:
        abort(404)
    return render_template("document.html", run_id=run_id, index=index, version=__version__)


# ------------------------------------------------------------------- API


@bp.get("/api/health")
def api_health() -> Any:
    return jsonify(
        {
            "version": __version__,
            "tesseract": tesseract_version(),
            "tesseract_path": tesseract_path(),
            "languages": installed_languages(),
            "default_workers": default_workers(),
        }
    )


@bp.get("/api/browse")
def api_browse() -> Any:
    """List folders so a path can be picked by clicking rather than typed.

    Typing an absolute path is fine until it is a Windows path seen from WSL, at
    which point clicking is the only humane option.
    """
    raw = request.args.get("path", "") or str(Path.home())
    path = Path(raw).expanduser()
    if not path.is_dir():
        path = path.parent if path.parent.is_dir() else Path.home()
    path = path.resolve()

    folders = []
    documents = 0
    try:
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    folders.append({"name": child.name, "path": str(child)})
                elif child.is_file():
                    documents += 1
            except OSError:
                continue
    except PermissionError:
        return jsonify({"error": f"no permission to read {path}", "path": str(path), "folders": []}), 200

    return jsonify(
        {
            "path": str(path),
            "parent": str(path.parent) if path.parent != path else None,
            "folders": folders,
            "file_count": documents,
        }
    )


def _settings_from_payload(payload: dict, *, input_dir: str, output_dir: str) -> Settings:
    """The Settings a run would use, built from what the browser form holds.

    Shared with the folder preview so that "already read" there means the same
    thing it will mean when the run starts. The check depends on the recipe —
    resolution, language, deskew and the rest — so a preview built from
    different settings than the run would be a different question answered
    confidently.
    """
    return Settings(
        input_dir=input_dir,
        output_dir=output_dir,
        work_dir=str(payload.get("work_dir") or ""),
        dpi=int(payload.get("dpi", DEFAULT_DPI)),
        lang=str(payload.get("lang", "eng")),
        psm=int(payload.get("psm", DEFAULT_PSM)),
        workers=int(payload.get("workers", 0)),
        force_ocr=bool(payload.get("force_ocr", False)),
        deskew=bool(payload.get("deskew", True)),
        orient=bool(payload.get("orient", True)),
        denoise=bool(payload.get("denoise", True)),
        write_pdf=bool(payload.get("write_pdf", True)),
        write_txt=bool(payload.get("write_txt", True)),
        write_json=bool(payload.get("write_json", True)),
        include_word_boxes=bool(payload.get("include_word_boxes", True)),
        write_previews=bool(payload.get("write_previews", True)),
        pdf_keeps_source_image=bool(payload.get("pdf_keeps_source_image", True)),
        txt_keeps_layout=bool(payload.get("txt_keeps_layout", True)),
        duplicates=str(payload.get("duplicates", DEFAULT_DUPLICATES)),
        output_layout=str(payload.get("output_layout", DEFAULT_LAYOUT)),
        skip_already_done=bool(payload.get("skip_already_done", True)),
        min_confidence=float(payload.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
        recursive=bool(payload.get("recursive", True)),
    )


def _already_read(found: list, settings: Settings) -> dict[str, str]:
    """Which of these documents this output folder has already been through.

    Answered with the run's own ledger and the run's own rule, so adding one
    file to a folder of two thousand shows exactly the one that will be read.
    Any trouble reading the ledger means "nothing is known to be done", which
    errs toward offering to read rather than toward claiming work is finished.
    """
    if not settings.skip_already_done:
        return {}
    try:
        ledger = Ledger(settings.work_path, settings.output_path)
    except OSError:
        return {}
    if not len(ledger):
        return {}

    names = output_basenames([item.relpath for item in found])
    recipe = settings.to_dict()
    kinds = [kind for kind in ("pdf", "txt", "json") if getattr(settings, f"write_{kind}")]
    done: dict[str, str] = {}
    for item in found:
        paths = output_paths(
            settings.output_path,
            item.relpath,
            layout=settings.output_layout,
            basename=names[item.relpath],
        )
        entry = ledger.already_done(
            item.relpath,
            size=item.size_bytes,
            mtime=item.modified,
            settings=recipe,
            wanted={kind: paths[kind] for kind in kinds},
        )
        if entry is not None:
            done[item.relpath] = entry.run_id
    return done


def _mark_tree(node: dict, done: dict[str, str]) -> dict:
    """Add per-document state and per-folder new/done counts to a tree."""
    new_count = 0
    done_count = 0
    for document in node["documents"]:
        run_id = done.get(document["path"])
        document["state"] = "done" if run_id else "new"
        document["done_in"] = run_id
        if run_id:
            done_count += 1
        else:
            new_count += 1
    for child in node["folders"]:
        _mark_tree(child, done)
        new_count += child["new"]
        done_count += child["done"]
    node["new"] = new_count
    node["done"] = done_count
    return node


@bp.post("/api/preview-folder")
def api_preview_folder() -> Any:
    """What a run would actually do, before committing to it.

    Discovery is the same code the run uses and the already-read check is the
    run's own, so this is a promise the run keeps rather than an estimate that
    turns out differently. It takes the whole settings payload for that reason:
    whether a document counts as already read depends on the recipe.

    The answer is the folder tree rather than a flat list. A case folder is
    organised by its folders, and twelve rows of `a/b/c.pdf` with "and 1,932
    more" underneath says nothing about the shape of what you are about to run.
    """
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("path") or "").strip()
    path = Path(raw).expanduser()
    if not raw or not path.is_dir():
        return jsonify({"error": "not a folder", "path": raw}), 400

    settings = _settings_from_payload(
        payload,
        input_dir=str(path),
        output_dir=(payload.get("output_dir") or "").strip() or str(path),
    )
    try:
        found = find_documents(path, recursive=settings.recursive)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        done = _already_read(found, settings) if payload.get("output_dir") else {}
    except Exception:  # noqa: BLE001 - a preview must never be the thing that fails
        done = {}

    tree = _mark_tree(document_tree(found), done)
    return jsonify(
        {
            "path": str(path.resolve()),
            "name": path.name or str(path),
            "files": len(found),
            "pages": sum(f.page_count for f in found),
            "new": tree["new"],
            "done": tree["done"],
            "unreadable": [{"file": f.relpath, "error": f.error} for f in found if f.error],
            "tree": tree,
        }
    )


@bp.post("/api/upload")
def api_upload() -> Any:
    """Stage uploaded files into the output folder and return the staged path.

    Browsers only hand over copies of what you pick, so an upload always costs
    disk. Pointing the tool at a folder path instead reads the originals in
    place and copies nothing — which is why that is the first option in the UI.
    """
    output_dir = (request.form.get("output_dir") or "").strip()
    if not output_dir:
        return jsonify({"error": "output_dir is required"}), 400
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files were sent"}), 400

    from ..runner import new_run_id

    stage = Path(output_dir).expanduser().resolve() / UPLOAD_DIRNAME / new_run_id()
    stage.mkdir(parents=True, exist_ok=True)

    saved = 0
    total_bytes = 0
    for storage in files:
        relative = _safe_relpath(storage.filename or "")
        if relative is None:
            continue
        dest = stage / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        storage.save(dest)
        saved += 1
        try:
            total_bytes += dest.stat().st_size
        except OSError:
            pass

    return jsonify({"path": str(stage), "files": saved, "bytes": total_bytes})


@bp.post("/api/runs")
def api_start_run() -> Any:
    payload = request.get_json(silent=True) or {}
    input_dir = (payload.get("input_dir") or "").strip()
    output_dir = (payload.get("output_dir") or "").strip()
    if not input_dir or not Path(input_dir).expanduser().is_dir():
        return jsonify({"error": f"not a folder: {input_dir}"}), 400
    if not output_dir:
        return jsonify({"error": "an output folder is required"}), 400
    if tesseract_path() is None:
        return jsonify({"error": "tesseract was not found on this machine — see the README"}), 400

    settings = _settings_from_payload(payload, input_dir=input_dir, output_dir=output_dir)

    try:
        settings.output_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"cannot write to that output folder: {exc}"}), 400

    # Reading from inside the folder being written to would let one run's
    # output become the next run's input. Staged uploads are the exception:
    # they live in a folder this tool created for exactly that purpose, and
    # nothing is ever written back into it.
    staging = settings.output_path / UPLOAD_DIRNAME
    if _is_inside(settings.input_path, settings.output_path) and not _is_inside(
        settings.input_path, staging
    ):
        return jsonify(
            {"error": "the input folder is inside the output folder — pick separate folders"}
        ), 400

    # The work folder holds a PDF of every page and a JPEG of every page, so
    # reading from inside it is the same mistake wearing a different hat.
    if settings.work_is_separate and _is_inside(settings.input_path, settings.work_path):
        return jsonify(
            {"error": "the input folder is inside the work folder — pick separate folders"}
        ), 400

    try:
        settings.work_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"cannot write to that work folder: {exc}"}), 400

    run = Run(settings)
    registry.add(run)
    run.start()
    return jsonify({"run_id": run.run_id, "url": f"/runs/{run.run_id}"}), 201


@bp.get("/api/runs")
def api_list_runs() -> Any:
    return jsonify({"runs": registry.recent()})


@bp.get("/api/runs/<run_id>")
def api_run(run_id: str) -> Any:
    live = registry.get(run_id)
    if live is not None:
        return jsonify(live.snapshot(include_files=True))
    work_dir = registry.find_work_dir(run_id)
    manifest = load_run(work_dir, run_id) if work_dir else None
    if manifest is None:
        abort(404)
    # Not in this process's registry, so whatever the manifest says, nothing is
    # executing it — see Registry.as_read_from_disk.
    return jsonify(registry.as_read_from_disk(manifest))


@bp.post("/api/runs/<run_id>/cancel")
def api_cancel(run_id: str) -> Any:
    run = registry.get(run_id)
    if run is None:
        abort(404)
    run.cancel()
    return jsonify({"ok": True, "status": run.status})


@bp.get("/api/runs/<run_id>/events")
def api_events(run_id: str) -> Response:
    run = registry.get(run_id)
    if run is None:
        # A finished run has no event stream; the snapshot endpoint has
        # everything, and the page falls back to it.
        return Response("event: closed\ndata: {}\n\n", mimetype="text/event-stream")

    def stream() -> Iterator[str]:
        q = run.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    # A comment line keeps proxies and browsers from timing the
                    # connection out during a long quiet stretch.
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") == "done":
                    break
        finally:
            run.unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@bp.get("/api/runs/<run_id>/documents/<int:index>")
def api_document(run_id: str, index: int) -> Any:
    work_dir = registry.find_work_dir(run_id)
    if work_dir is None:
        abort(404)
    document = load_document(work_dir, run_id, index)
    if document is None:
        # The file may still be in progress: report that rather than 404, so the
        # viewer can say "still being read" instead of "not found".
        live = registry.get(run_id)
        if live is not None and 0 <= index < len(live.files):
            return jsonify({"pending": True, **live.files[index].summary()})
        abort(404)
    return jsonify(document)


@bp.get("/api/runs/<run_id>/search")
def api_search(run_id: str) -> Any:
    """Substring search across everything the run read.

    Deliberately literal and case-insensitive: an index would be faster and this
    is a folder of a few thousand pages, where reading the JSON back is already
    fast enough to feel instant.
    """
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"query": query, "hits": [], "error": "type at least two characters"})

    work_dir = registry.find_work_dir(run_id)
    if work_dir is None:
        abort(404)
    pages_dir = Path(work_dir) / "_runs" / run_id / "pages"
    if not pages_dir.is_dir():
        return jsonify({"query": query, "hits": []})

    needle = query.lower()
    hits: list[dict[str, Any]] = []
    for path in sorted(pages_dir.glob("*.json"), key=lambda p: int(p.stem)):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for page in document.get("pages", []):
            text = page.get("text") or ""
            position = text.lower().find(needle)
            if position < 0:
                continue
            hits.append(
                {
                    "file_index": int(path.stem),
                    "file": document.get("relpath"),
                    "page": page.get("page"),
                    "source": page.get("source"),
                    "snippet": _snippet(text, position, len(query)),
                }
            )
            if len(hits) >= 200:
                return jsonify({"query": query, "hits": hits, "truncated": True})
    return jsonify({"query": query, "hits": hits, "truncated": False})


# --------------------------------------------------------------- files


@bp.get("/files/<run_id>/<path:relpath>")
def files(run_id: str, relpath: str) -> Response:
    """Serve this run's results and the pictures that go with them.

    Results live in the output folder and page pictures live in the work
    folder, which are the same folder unless a separate one was chosen. The
    first path segment says which: the tool's own folders all begin with an
    underscore and are reserved, so there is nothing to guess.

    send_from_directory refuses to escape the directory it is given, so
    whichever folder is chosen is the boundary.
    """
    first = relpath.replace("\\", "/").split("/", 1)[0]
    if first in RESERVED_DIRS:
        root = registry.find_work_dir(run_id)
    else:
        root = registry.find_output_dir(run_id)
    if root is None:
        abort(404)
    download = request.args.get("download") == "1"
    mimetype, _ = mimetypes.guess_type(relpath)
    return send_from_directory(
        root,
        relpath,
        as_attachment=download,
        mimetype=mimetype or "application/octet-stream",
    )


# --------------------------------------------------------------- helpers


def _app_config(key: str) -> str:
    from flask import current_app

    return str(current_app.config.get(key, ""))


def _safe_relpath(filename: str) -> Path | None:
    """Turn a browser-supplied path into a relative path that cannot escape.

    Folder uploads send `Ex 13/scan.pdf` as the filename, and that structure is
    worth keeping — but only after every `..` and absolute root is gone.
    """
    cleaned = filename.replace("\\", "/").strip()
    if not cleaned:
        return None
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    parts = [p.replace("\x00", "") for p in parts]
    if not parts:
        return None
    return Path(*parts)


def _is_inside(inner: Path, outer: Path) -> bool:
    try:
        inner.relative_to(outer)
        return True
    except ValueError:
        return False


def _snippet(text: str, position: int, length: int, *, window: int = 90) -> dict[str, str]:
    start = max(0, position - window)
    end = min(len(text), position + length + window)
    return {
        "before": ("…" if start > 0 else "") + text[start:position].replace("\n", " "),
        "match": text[position : position + length],
        "after": text[position + length : end].replace("\n", " ") + ("…" if end < len(text) else ""),
    }
