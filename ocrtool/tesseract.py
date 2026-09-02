"""Tesseract wrapper.

Tesseract is run as a subprocess in TSV mode rather than through pytesseract:
one fewer dependency, and TSV is the only output carrying per-word confidence
*and* bounding boxes. Both are load-bearing here — confidence decides which
pages get flagged for review, boxes let the UI point at where a word came from.

One invocation produces both outputs. `tesseract page.png out tsv pdf` writes
out.tsv and out.pdf from a single recognition pass, so the searchable PDF costs
nothing on top of the text.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .models import Word

# A page that takes three minutes is a page tesseract is lost on. Failing it
# keeps one pathological scan from stalling a run of thousands.
TIMEOUT_SECONDS = 180

# Orientation detection is a layout pass, not a recognition pass, and it does
# not need the full 300 dpi render. Measured on a 2536x3319 page: 810ms at full
# size against 485ms at this height, with the reported confidence unchanged
# (25.1 against 25.4). Below 1600 the confidence collapses and at 900 it starts
# returning the wrong answer, so this is not a knob to turn down further.
OSD_HEIGHT_PX = 2000
# Orientation detection reads a page's layout, not its words; it has no reason
# to take as long as recognition, and a page it is lost on is one to give up on.
OSD_TIMEOUT_SECONDS = 60


class TesseractMissing(Exception):
    pass


class TesseractFailed(Exception):
    pass


@dataclass
class Osd:
    """What tesseract's orientation pass makes of a page.

    `rotate` is the correction, not the observation: turn the image this many
    degrees *clockwise* to stand it upright. Checked against pages turned by
    hand — a page laid on its side clockwise comes back as 270.

    `confidence` is on tesseract's own scale, which has no units and no ceiling.
    It is worth reading as a rough sort rather than a probability: measured
    here, a page with plenty of text scores 8-25 and is invariably right, while
    everything below about 2 is a guess. Do not act on this number alone.
    """

    rotate: int
    confidence: float
    script: str | None = None


@dataclass
class TesseractOutput:
    text: str
    words: list[Word]
    mean_confidence: float | None
    pdf_bytes: bytes | None = None


def tesseract_path() -> str | None:
    """The tesseract binary, honouring an explicit override.

    OCRTOOL_TESSERACT exists because a machine without root cannot always put
    tesseract on PATH — an unpacked build under ~/.local is a normal situation,
    not an exotic one.
    """
    override = os.environ.get("OCRTOOL_TESSERACT")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found
    return shutil.which("tesseract")


def tesseract_version() -> str | None:
    binary = tesseract_path()
    if binary is None:
        return None
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
        return (out.stdout or out.stderr).splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def installed_languages() -> list[str]:
    binary = tesseract_path()
    if binary is None:
        return []
    try:
        out = subprocess.run(
            [binary, "--list-langs"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # The first line is a header ("List of available languages (2):").
    lines = (out.stdout or "").splitlines()
    return [line.strip() for line in lines[1:] if line.strip()]


def require_tesseract() -> str:
    binary = tesseract_path()
    if binary is None:
        raise TesseractMissing(
            "tesseract was not found. Install it, or point OCRTOOL_TESSERACT at the binary. "
            "See the Installing tesseract section of README.md."
        )
    return binary


def run_osd(image: Image.Image) -> Osd | None:
    """Which way up tesseract thinks this page is, or None if it will not say.

    `--psm 0` runs orientation and script detection on its own. It needs
    `osd.traineddata` beside the language data, and it needs enough characters
    to work from: a near-blank page, a photograph or a full-page stamp makes it
    exit non-zero with "Too few characters. Skipping this page". That is a
    normal answer to a fair question, not a failure, so it comes back as None
    and the caller carries on unturned. A missing osd.traineddata lands in the
    same place, which is why nothing here has to check for it first.
    """
    binary = tesseract_path()
    if binary is None:
        return None

    small = image
    if small.height > OSD_HEIGHT_PX:
        ratio = OSD_HEIGHT_PX / small.height
        small = small.resize(
            (max(1, round(small.width * ratio)), OSD_HEIGHT_PX), Image.LANCZOS
        )

    with tempfile.TemporaryDirectory(prefix="ocrtool-osd-") as tmpdir:
        image_path = Path(tmpdir) / "page.png"
        small.save(image_path, format="PNG")
        try:
            proc = subprocess.run(
                [binary, str(image_path), "stdout", "--psm", "0"],
                capture_output=True,
                text=True,
                timeout=OSD_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    if proc.returncode != 0:
        return None
    return parse_osd(proc.stdout or "")


def parse_osd(text: str) -> Osd | None:
    """Pull the numbers out of what `--psm 0` prints.

    The output is a short block of `Key: value` lines. Anything missing or
    unparseable means no answer rather than a wrong one.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    try:
        rotate = int(fields["rotate"]) % 360
        confidence = float(fields["orientation confidence"])
    except (KeyError, ValueError):
        return None
    if rotate not in (0, 90, 180, 270):
        return None
    return Osd(rotate=rotate, confidence=confidence, script=fields.get("script") or None)


def run_tesseract(
    image: Image.Image,
    *,
    lang: str = "eng",
    psm: int = 3,
    want_pdf: bool = False,
    dpi: int | None = None,
) -> TesseractOutput:
    """Recognise one page image. Returns text, per-word boxes, and optionally a
    one-page searchable PDF of the same image."""
    binary = require_tesseract()

    with tempfile.TemporaryDirectory(prefix="ocrtool-") as tmpdir:
        tmp = Path(tmpdir)
        image_path = tmp / "page.png"
        # The dpi tag matters twice: tesseract warns and guesses without it,
        # and the searchable PDF it writes takes its page size from it, so an
        # untagged image produces a letter-size page claiming to be 15 inches.
        save_kwargs = {"dpi": (dpi, dpi)} if dpi else {}
        image.save(image_path, format="PNG", **save_kwargs)

        outputs = ["tsv"] + (["pdf"] if want_pdf else [])
        cmd = [binary, str(image_path), str(tmp / "out"), "-l", lang, "--psm", str(psm), *outputs]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise TesseractFailed(f"tesseract timed out after {TIMEOUT_SECONDS}s") from exc
        except OSError as exc:
            raise TesseractFailed(f"could not run tesseract: {exc}") from exc

        if proc.returncode != 0:
            raise TesseractFailed((proc.stderr or "tesseract failed").strip()[:500])

        tsv_path = tmp / "out.tsv"
        if not tsv_path.exists():
            raise TesseractFailed("tesseract produced no TSV output")
        parsed = parse_tsv(tsv_path.read_text(encoding="utf-8", errors="replace"))

        if want_pdf:
            pdf_path = tmp / "out.pdf"
            if pdf_path.exists():
                parsed.pdf_bytes = pdf_path.read_bytes()
            else:
                # Missing pdf.ttf in tessdata is the usual cause. The text is
                # still good, so the run continues and the manifest records it.
                raise TesseractFailed(
                    "tesseract produced no PDF output (is pdf.ttf present in tessdata?)"
                )

    return parsed


def parse_tsv(tsv: str) -> TesseractOutput:
    """Rebuild reading order from the block/paragraph/line columns.

    Tesseract emits one row per word plus structural rows. Joining words within
    a line, and lines with newlines, preserves enough layout that a date at the
    top of a form does not end up glued to a value from the footer.
    """
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)

    words: list[Word] = []
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []

    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf", "-1"))
            left = float(row["left"])
            top = float(row["top"])
            width = float(row["width"])
            height = float(row["height"])
            key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        except (TypeError, ValueError, KeyError):
            continue

        # conf == -1 marks the structural rows, which carry no word.
        if conf < 0:
            continue

        words.append(
            Word(text=text, x0=left, y0=top, x1=left + width, y1=top + height, confidence=conf)
        )
        confidences.append(conf)
        lines.setdefault(key, []).append(text)

    text = "\n".join(" ".join(tokens) for _, tokens in sorted(lines.items()))
    mean_conf = round(sum(confidences) / len(confidences), 2) if confidences else None
    return TesseractOutput(text=text, words=words, mean_confidence=mean_conf)
