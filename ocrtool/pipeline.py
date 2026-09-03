"""What happens to one page.

The whole tool is this function repeated a few thousand times, so the decisions
it makes are worth stating plainly:

  1. If the page is part of a PDF and carries its own text layer, take that
     text. It is exact and free. (Unless the run forces OCR.)
  2. Otherwise render the page, clean the image up, and read it with tesseract.
  3. Either way, save a picture of the page as it really looks, so the result
     can be checked against the source rather than trusted.
  4. Flag anything that came out thin or low-confidence. A flagged page is a
     page a person should look at; it is not a failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .config import MIN_CHARS_FOR_A_REAL_PAGE, Settings
from .models import PageResult
from .pdfpage import replace_page_image
from .preprocess import apply_geometry, preprocess, turn
from .render import render_page, write_preview
from .tesseract import TesseractFailed, run_osd, run_tesseract
from .textlayer import read_text_layer

# Text-layer pages are never OCR'd, so their picture exists only for the
# reviewer to look at. Rendering those at full OCR resolution would double the
# cost of the cheapest pages in the run for no gain.
PREVIEW_DPI = 110


@dataclass
class PageWork:
    """One unit of work in the pool: a page of a file, and where its artefacts go."""

    file_index: int
    relpath: str
    source_path: Path
    page_no: int
    preview_dest: Path | None
    preview_rel: str | None
    page_pdf_dest: Path | None


def process_page(work: PageWork, settings: Settings) -> PageResult:
    """Read one page. Never raises: a bad page becomes a failed page."""
    started = time.perf_counter()
    try:
        return _process(work, settings, started)
    except Exception as exc:  # noqa: BLE001 - one page must not end the run
        return PageResult(
            page_no=work.page_no,
            source="failed",
            needs_review=True,
            review_reason="page could not be read",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=_ms_since(started),
        )


def _process(work: PageWork, settings: Settings, started: float) -> PageResult:
    if not settings.force_ocr:
        layer = read_text_layer(work.source_path, work.page_no)
        if layer.usable:
            preview_rel = _maybe_preview(work, settings, dpi=PREVIEW_DPI)
            result = PageResult(
                page_no=work.page_no,
                source="text-layer",
                text=layer.text,
                confidence=None,  # exact text has no confidence to report
                words=layer.words,
                duration_ms=_ms_since(started),
                preview=preview_rel,
            )
            _flag(result, settings)
            return result

    rendered = render_page(work.source_path, work.page_no, settings.dpi)
    image = rendered.image
    width, height = image.size

    # The preview is written from the untouched render, not the cleaned-up
    # image: you are checking the OCR against the page as it is, and a deskewed
    # grey copy is not that page.
    preview_rel = None
    if settings.write_previews and work.preview_dest is not None:
        write_preview(image, work.preview_dest)
        preview_rel = work.preview_rel

    cleaned = preprocess(image, deskew=settings.deskew, denoise=settings.denoise)
    # Preprocessing may upscale a small page, which changes the resolution the
    # image represents; the tag has to follow it or the PDF page comes out the
    # wrong size.
    effective_dpi = round(rendered.dpi * cleaned.upscaled) if cleaned.upscaled else rendered.dpi

    want_pdf = settings.write_pdf and work.page_pdf_dest is not None
    try:
        out = run_tesseract(
            cleaned.image,
            lang=settings.lang,
            psm=settings.psm,
            want_pdf=want_pdf,
            dpi=effective_dpi,
        )
    except TesseractFailed as exc:
        # Retry once without the PDF: a tessdata install missing pdf.ttf fails
        # only the PDF half, and losing the text as well would be gratuitous.
        if not want_pdf:
            raise
        out = run_tesseract(
            cleaned.image, lang=settings.lang, psm=settings.psm, want_pdf=False, dpi=effective_dpi
        )
        result = _ocr_result(
            work, out, cleaned.skew_corrected, width, height, preview_rel, started, 0
        )
        result.error = f"searchable PDF page not written: {exc}"
        _flag(result, settings)
        return result

    # A page that came back completely empty is worth one more attempt without
    # the resolution tag. Tesseract uses dpi to judge how big a letter should
    # be, so a PDF whose declared page size disagrees with its own content — a
    # 23x30 inch page holding letter-size text — can make it reject every word
    # on the page and return nothing. Untagged, it estimates the resolution
    # itself and reads the page fine. Rare, but the failure is total and silent,
    # which is exactly the kind worth spending a second pass on.
    recovered = False
    if not out.text.strip():
        retry = run_tesseract(
            cleaned.image, lang=settings.lang, psm=settings.psm, want_pdf=want_pdf, dpi=None
        )
        if retry.text.strip():
            out = retry
            recovered = True

    # A page that still reads badly may simply be the wrong way up.
    rotation = 0
    if settings.orient and _reads_badly(out, settings):
        turned = _read_turned(
            image, out, settings, want_pdf=want_pdf, rendered_dpi=rendered.dpi
        )
        if turned is not None:
            rotation, image, cleaned, out, effective_dpi = turned
            width, height = image.size
            # The preview is there to be read alongside the text. Once the page
            # has been stood up, the page as it was read *is* the upright one,
            # and leaving a sideways picture beside upright text would make
            # checking the OCR harder than not having the picture at all.
            if preview_rel is not None and work.preview_dest is not None:
                write_preview(image, work.preview_dest)

    if want_pdf and out.pdf_bytes and work.page_pdf_dest is not None:
        page_pdf = out.pdf_bytes
        if settings.pdf_keeps_source_image:
            # Tesseract built this page around the cleaned greyscale image it
            # read. Put the real page back underneath the text it found.
            page_pdf = replace_page_image(
                page_pdf,
                apply_geometry(image, skew=cleaned.skew_corrected, size=cleaned.image.size),
            )
        work.page_pdf_dest.parent.mkdir(parents=True, exist_ok=True)
        work.page_pdf_dest.write_bytes(page_pdf)

    result = _ocr_result(
        work, out, cleaned.skew_corrected, width, height, preview_rel, started, rotation
    )
    if recovered:
        result.error = (
            "read on a second pass without the resolution tag — this page's PDF page size "
            "may differ from the rest of the document"
        )
    _flag(result, settings)
    return result



# How much better a turned page has to read before the turn is believed. A
# couple of points is noise between two recognitions of the same page; this is
# a gap you only get by actually making the text legible.
MIN_CONFIDENCE_GAIN = 5.0

# The angles a page can be wrong by, in the order they are tried. Order is
# worth having an opinion about only because of the early stop below: the
# sooner the right angle comes up, the fewer recognitions get paid for. On the
# 358-page claim file here, six of the seven recovered pages wanted 90 and one
# wanted 180, so 90 leads.
#
# Note what is *not* in this list. A page a quarter turn clockwise reads at 94%
# confidence untouched — tesseract stands that one up by itself and the page
# never gets this far. It is the other three that need help, and an inverted
# page is the worst of them: the same page upside down read at 39% and returned
# `vooododd0O0oIsA9`, which looks like text and is not.
QUARTER_TURNS = (90, 180, 270)


def _reads_badly(out, settings: Settings) -> bool:
    """Whether this page is poor enough to be worth trying another way up.

    The same bar the page would be flagged for review at, deliberately: a page
    nobody has to look at is a page not worth spending four recognitions on,
    and a page somebody does have to look at has already cost more than the
    second or two this takes.
    """
    if not out.text.strip():
        return True
    if out.mean_confidence is None:
        return False
    return out.mean_confidence < settings.min_confidence


def _improves_on(candidate, current) -> bool:
    """Whether a turned reading is better than the one already in hand.

    This is the whole safety argument for turning pages. Tesseract's
    orientation pass is a hint and nothing more — measured on this machine it
    reported 180 with confidence 0.3 and 1.1 on pages that were the right way
    up, and acting on that alone would have turned correct pages upside down.
    Judging a turn by the recognition it produces instead means a wrong guess
    costs a second and changes nothing about the output.
    """
    if not candidate.text.strip():
        return False
    if not current.text.strip():
        return True
    if candidate.mean_confidence is None or current.mean_confidence is None:
        return False
    return candidate.mean_confidence > current.mean_confidence + MIN_CONFIDENCE_GAIN


def _confidence(out) -> float:
    return out.mean_confidence if out.mean_confidence is not None else 0.0


def _read_turned(image, current, settings: Settings, *, want_pdf: bool, rendered_dpi: int):
    """Read a badly-read page at other angles and keep the best of them.

    Tesseract's orientation pass goes first because it is cheap — half a second
    against a second and a half for a recognition — and when it has an opinion
    it is almost always right. The remaining quarter turns follow, so a page it
    could not read (a form, a photograph, a full-page stamp: "Too few
    characters") is still recovered, just for more.

    Stops at the first angle that reads well enough not to need review, which
    is what keeps the usual cost at one extra recognition rather than three.

    Returns None when nothing read better, and the caller keeps what it had.
    """
    osd = run_osd(image)
    angles = list(QUARTER_TURNS)
    if osd is not None and osd.rotate:
        angles.remove(osd.rotate)
        angles.insert(0, osd.rotate)

    best = None
    for position, degrees in enumerate(angles):
        turned = turn(image, degrees)
        cleaned = preprocess(turned, deskew=settings.deskew, denoise=settings.denoise)
        dpi = round(rendered_dpi * cleaned.upscaled) if cleaned.upscaled else rendered_dpi
        try:
            out = run_tesseract(
                cleaned.image,
                lang=settings.lang,
                psm=settings.psm,
                want_pdf=want_pdf,
                dpi=dpi,
            )
        except TesseractFailed:
            continue

        # A page with words on it says *something* at every angle, even upside
        # down: the inverted exhibit measured here still returned 348 words,
        # they were simply the wrong ones. A page that yields nothing at two
        # different angles has nothing on it to find — a photograph, a dark
        # scan, a sheet of black — and the remaining angles are two more
        # recognitions spent to learn the same thing. This is what keeps a
        # folder of photographs from costing four times what it should.
        if position == 0 and not out.text.strip() and not current.text.strip():
            break

        # The margin is owed to the reading we already have, not to the other
        # candidates: it is there to stop a turn being adopted on noise. Once
        # two angles have both cleared it, the better of the two simply wins.
        # Charging the margin twice cost a real page here — 311 of the claim
        # file kept 180 at 56.0 because 90's 56.8 was not five points better
        # than the turn already in hand.
        if not _improves_on(out, current):
            continue
        if best is not None and _confidence(out) <= _confidence(best[3]):
            continue
        best = (degrees, turned, cleaned, out, dpi)
        if not _reads_badly(out, settings):
            break  # good enough to stop paying for more angles
    return best


# Below this a word is a guess. Tesseract's scale is optimistic: 60 is already
# visibly wrong more often than not.
LOW_CONFIDENCE_WORD = 60.0
# A page where everything is doubtful needs the whole page re-read, not a list
# of eighty suspect words.
MAX_LISTED_LOW_WORDS = 80


def _ocr_result(work, out, skew, width, height, preview_rel, started, rotation=0) -> PageResult:
    doubtful = [w.text for w in out.words if w.confidence < LOW_CONFIDENCE_WORD]
    return PageResult(
        page_no=work.page_no,
        source="ocr",
        text=out.text,
        confidence=out.mean_confidence,
        words=out.words,
        duration_ms=_ms_since(started),
        width_px=width,
        height_px=height,
        skew_corrected=skew,
        rotation_applied=rotation,
        preview=preview_rel,
        low_confidence_words=doubtful[:MAX_LISTED_LOW_WORDS],
    )


def _maybe_preview(work: PageWork, settings: Settings, *, dpi: int) -> str | None:
    if not settings.write_previews or work.preview_dest is None:
        return None
    write_preview(render_page(work.source_path, work.page_no, dpi).image, work.preview_dest)
    return work.preview_rel


def _flag(result: PageResult, settings: Settings) -> None:
    """Decide whether a person needs to look at this page.

    Both rules exist because of the same failure: a page that produced clean,
    plausible, wrong output looks exactly like a page that worked. Confidence
    catches the visibly rough ones; the character count catches the ones that
    silently produced nothing at all.
    """
    if result.confidence is not None and result.confidence < settings.min_confidence:
        result.needs_review = True
        result.review_reason = f"mean confidence {result.confidence:.0f} below {settings.min_confidence:.0f}"
        return
    if result.char_count < MIN_CHARS_FOR_A_REAL_PAGE:
        result.needs_review = True
        result.review_reason = (
            "almost no text found — blank page, a photograph, or a page OCR could not read"
        )


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
