"""Command line: `ocrtool run`, `ocrtool ui`, `ocrtool doctor`.

argparse rather than a CLI framework — one fewer dependency, and this is three
commands, not thirty.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_DPI,
    DEFAULT_DUPLICATES,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_PSM,
    DUPLICATE_RULES,
    Settings,
    config_path,
    default_folders,
    default_workers,
    save_default_folders,
)
from .outputs import DEFAULT_LAYOUT, LAYOUTS
from .runner import Run, run_blocking
from .tesseract import installed_languages, tesseract_path, tesseract_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocrtool",
        description="OCR every document in a folder, on this machine, with nothing sent anywhere.",
    )
    parser.add_argument("--version", action="version", version=f"ocrtool {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    folders = default_folders()

    run_cmd = sub.add_parser("run", help="OCR a folder from the command line")
    run_cmd.add_argument(
        "input", nargs="?", default=None, help=f"folder of documents to read (default {folders['input']})"
    )
    run_cmd.add_argument(
        "-o", "--output", default=None, help=f"folder to write results into (default {folders['output']})"
    )
    run_cmd.add_argument(
        "--work-dir",
        default=None,
        help=(
            "where the tool's own folders go — _runs/, _previews/ and _cache/. "
            "Defaults to the output folder; point it elsewhere to leave the "
            "output folder holding nothing but your results"
        ),
    )
    run_cmd.add_argument("--dpi", type=int, default=DEFAULT_DPI, help=f"render resolution (default {DEFAULT_DPI})")
    run_cmd.add_argument("--lang", default="eng", help="tesseract language code (default eng)")
    run_cmd.add_argument("--psm", type=int, default=DEFAULT_PSM, help=f"page segmentation mode (default {DEFAULT_PSM})")
    run_cmd.add_argument("--workers", type=int, default=0, help=f"parallel pages (default {default_workers()} here)")
    run_cmd.add_argument("--force-ocr", action="store_true", help="OCR every page, even one with a text layer")
    run_cmd.add_argument("--no-deskew", action="store_true", help="do not straighten skewed scans")
    run_cmd.add_argument(
        "--no-orient",
        action="store_true",
        help="do not try turning a page that reads badly — leave sideways and "
        "upside-down scans as they are",
    )
    run_cmd.add_argument("--no-denoise", action="store_true", help="do not despeckle scans")
    run_cmd.add_argument("--no-pdf", action="store_true", help="skip the searchable PDF copies")
    run_cmd.add_argument("--no-txt", action="store_true", help="skip the .txt files")
    run_cmd.add_argument("--no-json", action="store_true", help="skip the .json files")
    run_cmd.add_argument("--no-previews", action="store_true", help="skip page images (the UI needs these)")
    run_cmd.add_argument(
        "--redo",
        action="store_true",
        help="read every document again, even ones this output folder already holds results for",
    )
    run_cmd.add_argument(
        "--outputs",
        choices=LAYOUTS,
        default=DEFAULT_LAYOUT,
        help=(
            "where results go: by-folder mirrors the input and puts pdf/ txt/ json/ "
            "inside each folder that holds documents (default); by-type collects one "
            "pdf/, txt/ and json/ at the top; together puts a document's three files "
            "side by side"
        ),
    )
    run_cmd.add_argument(
        "--outputs-together",
        action="store_true",
        help="the same as --outputs together",
    )
    run_cmd.add_argument(
        "--duplicates",
        choices=DUPLICATE_RULES,
        default=DEFAULT_DUPLICATES,
        help=(
            "when two documents count as one, so one is read and the other is "
            "written from it: name = same name and same contents (default); "
            "content = same contents whatever they are called; off = never"
        ),
    )
    run_cmd.add_argument(
        "--txt-plain",
        action="store_true",
        help="write the .txt as a plain stream of lines instead of keeping the page's layout",
    )
    run_cmd.add_argument(
        "--pdf-cleaned-image",
        action="store_true",
        help="put the greyscale image OCR read into the PDF instead of the original page (smaller files)",
    )
    run_cmd.add_argument("--no-recursive", action="store_true", help="only the top level of the folder")
    run_cmd.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help=f"flag pages below this OCR confidence (default {DEFAULT_MIN_CONFIDENCE:.0f})",
    )
    run_cmd.add_argument("--quiet", action="store_true", help="only print the summary")

    ui_cmd = sub.add_parser("ui", help="start the local web interface")
    ui_cmd.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 — this machine only")
    ui_cmd.add_argument("--port", type=int, default=5000)
    ui_cmd.add_argument("--output", default=None, help=f"output folder to offer in the form (default {folders['output']})")
    ui_cmd.add_argument("--input", default=None, help=f"input folder to offer in the form (default {folders['input']})")
    ui_cmd.add_argument(
        "--work-dir",
        default=None,
        help="folder to offer for the tool's own _runs/, _previews/ and _cache/",
    )
    ui_cmd.add_argument("--debug", action="store_true")

    sub.add_parser("doctor", help="check that everything this tool needs is present")

    folders_cmd = sub.add_parser("folders", help="show or set the default input and output folders")
    folders_cmd.add_argument("--input", default=None, help="folder to read documents from by default")
    folders_cmd.add_argument("--output", default=None, help="folder to write results into by default")
    folders_cmd.add_argument(
        "--work",
        default=None,
        help=(
            "folder for the tool's own _runs/, _previews/ and _cache/ "
            "(pass an empty string to put them back with the results)"
        ),
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "ui":
        return _cmd_ui(args)
    if args.command == "folders":
        return _cmd_folders(args)
    return _cmd_doctor()


def _is_inside(inner: Path, outer: Path) -> bool:
    try:
        inner.relative_to(outer)
        return True
    except ValueError:
        return False


def _cmd_run(args: argparse.Namespace) -> int:
    folders = default_folders()
    settings = Settings(
        input_dir=args.input or folders["input"],
        output_dir=args.output or folders["output"],
        work_dir=args.work_dir if args.work_dir is not None else folders.get("work", ""),
        dpi=args.dpi,
        lang=args.lang,
        psm=args.psm,
        workers=args.workers,
        force_ocr=args.force_ocr,
        deskew=not args.no_deskew,
        orient=not args.no_orient,
        denoise=not args.no_denoise,
        write_pdf=not args.no_pdf,
        write_txt=not args.no_txt,
        write_json=not args.no_json,
        write_previews=not args.no_previews,
        pdf_keeps_source_image=not args.pdf_cleaned_image,
        txt_keeps_layout=not args.txt_plain,
        duplicates=args.duplicates,
        output_layout="together" if args.outputs_together else args.outputs,
        skip_already_done=not args.redo,
        min_confidence=args.min_confidence,
        recursive=not args.no_recursive,
    )

    if not settings.input_path.is_dir():
        print(f"not a folder: {settings.input_path}", file=sys.stderr)
        return 2
    if tesseract_path() is None:
        print("tesseract was not found — run `ocrtool doctor`.", file=sys.stderr)
        return 2

    if settings.work_is_separate and _is_inside(settings.input_path, settings.work_path):
        print(
            f"the input folder is inside the work folder: {settings.work_path}",
            file=sys.stderr,
        )
        return 2
    try:
        settings.work_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"cannot write to that work folder: {exc}", file=sys.stderr)
        return 2

    print(f"Reading  {settings.input_path}")
    print(f"Writing  {settings.output_path}")
    if settings.work_is_separate:
        print(f"Working  {settings.work_path}")
    print(f"Workers  {settings.workers}   dpi {settings.dpi}   lang {settings.lang}\n")

    state = {"line": "", "printed_at": 0.0}

    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind == "phase" and not args.quiet:
            _clear(state)
            print(event.get("message", ""))
        elif kind == "file" and not args.quiet:
            summary = event["summary"]
            if summary["status"] in {"done", "failed", "cancelled"}:
                _clear(state)
                flagged = len(summary["flagged_pages"])
                note = f", {flagged} flagged" if flagged else ""
                err = f"  {summary['error']}" if summary.get("error") else ""
                print(
                    f"  {summary['status']:9} {summary['relpath']}  "
                    f"({summary['pages_done']} pages{note}){err}"
                )
        elif kind == "progress" and not args.quiet:
            run = event["run"]
            totals = run["totals"]
            eta = run.get("eta_s")
            eta_text = f"  eta {_duration(eta)}" if eta else ""
            line = (
                f"  {totals['pages_done']}/{totals['pages_total']} pages  "
                f"{totals['files_done']}/{totals['files_total']} files  "
                f"{run['pages_per_second']:.2f} pages/s{eta_text}"
            )
            _status(state, line)
        elif kind == "done":
            _clear(state)

    run = run_blocking(settings, on_event=on_event)
    return _report(run)


def _report(run: Run) -> int:
    totals = run.totals
    print()
    print(f"Status    {run.status}")
    print(f"Files     {totals.files_done} written, {totals.files_failed} failed of {totals.files_total}")
    print(
        f"Pages     {totals.pages_done} read "
        f"({totals.pages_text_layer} from text layers, {totals.pages_ocr} OCR'd, {totals.pages_failed} failed)"
    )
    if totals.files_skipped:
        print(
            f"Skipped   {totals.files_skipped} documents ({totals.pages_skipped} pages) "
            "already read into this folder — use --redo to read them again"
        )
    print(f"Flagged   {totals.pages_flagged} pages need a look")
    print(f"Time      {_duration(run.elapsed_s)}")
    print(f"Output    {run.settings.output_path}")
    print(f"Report    {run.run_dir / 'pages.csv'}")
    if run.error:
        print(f"Error     {run.error}", file=sys.stderr)
    return 0 if run.status == "done" else 1


def _cmd_ui(args: argparse.Namespace) -> int:
    from .web.app import create_app

    folders = default_folders()
    app = create_app(
        default_input=args.input or folders["input"],
        default_output=args.output or folders["output"],
        default_work=args.work_dir if args.work_dir is not None else folders.get("work", ""),
    )
    url = f"http://{args.host}:{args.port}"
    print(f"ocrtool {__version__}")
    print(f"tesseract: {tesseract_version() or 'NOT FOUND — run `ocrtool doctor`'}")
    print(f"reading from: {args.input or folders['input']}")
    print(f"writing to:   {args.output or folders['output']}")
    work = args.work_dir if args.work_dir is not None else folders.get("work", "")
    if work:
        print(f"working in:   {work}")
    print(f"\n  Open {url}\n")
    print("Ctrl-C to stop.")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


def _cmd_folders(args: argparse.Namespace) -> int:
    if args.input or args.output or args.work is not None:
        folders = save_default_folders(
            input_dir=args.input, output_dir=args.output, work_dir=args.work
        )
        print(f"saved in {config_path()}\n")
    else:
        folders = default_folders()

    for label, key in (("input ", "input"), ("output", "output")):
        path = Path(folders[key]).expanduser()
        state = "" if path.is_dir() else "   (does not exist yet)"
        print(f"{label}  {path}{state}")

    if folders.get("work"):
        path = Path(folders["work"]).expanduser()
        state = "" if path.is_dir() else "   (does not exist yet)"
        print(f"work    {path}{state}")
    else:
        print("work    (kept with the results)")
    return 0


def _cmd_doctor() -> int:
    ok = True
    print(f"ocrtool     {__version__}")
    print(f"python      {sys.version.split()[0]}")

    binary = tesseract_path()
    if binary:
        print(f"tesseract   {tesseract_version()}\n            {binary}")
        langs = installed_languages()
        print(f"languages   {', '.join(langs) if langs else 'none found'}")
        if "eng" not in langs:
            print("            ! English data is missing — see README.md")
            ok = False
    else:
        print("tesseract   NOT FOUND")
        print("            Install it, or set OCRTOOL_TESSERACT to the binary path.")
        ok = False

    for module in ("pypdfium2", "PIL", "numpy", "flask"):
        try:
            __import__(module)
            print(f"{module:11} ok")
        except ImportError:
            print(f"{module:11} MISSING — run: uv pip install -e .")
            ok = False

    folders = default_folders()
    print(f"input       {folders['input']}")
    print(f"output      {folders['output']}")
    print(f"workers     {default_workers()} by default on this machine")
    print("\nready" if ok else "\nnot ready — fix the lines marked above")
    return 0 if ok else 1


def _status(state: dict, line: str) -> None:
    """Rewrite one progress line in place — but only on a terminal.

    Piped into a file or a log, carriage returns produce an unreadable smear of
    half-overwritten lines, so there the progress is printed at intervals as
    ordinary lines instead.
    """
    if sys.stdout.isatty():
        sys.stdout.write("\r" + line.ljust(len(state["line"])))
        sys.stdout.flush()
        state["line"] = line
        return

    now = time.monotonic()
    if now - state.get("printed_at", 0.0) >= 5.0:
        print(line.strip())
        state["printed_at"] = now


def _clear(state: dict) -> None:
    if state["line"] and sys.stdout.isatty():
        sys.stdout.write("\r" + " " * len(state["line"]) + "\r")
        sys.stdout.flush()
        state["line"] = ""


def _duration(seconds: float | None) -> str:
    if not seconds:
        return "0s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


if __name__ == "__main__":
    raise SystemExit(main())
