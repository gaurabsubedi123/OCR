"""End-to-end: a folder in, a folder of results out.

These need the tesseract binary, and they are the tests that would have caught
every defect found while building this — an empty page, a lost text layer, a
PDF that assembled but carried no text.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from conftest import colour_page, is_greyscale, needs_tesseract, text_page

from ocrtool.config import Settings
from ocrtool.models import PageResult
from ocrtool.pipeline import PageWork
from ocrtool.runner import Run, list_runs, load_document, load_run, run_blocking


def _settings(sample_folder: Path, tmp_path: Path, **overrides) -> Settings:
    return Settings(
        input_dir=str(sample_folder),
        output_dir=str(tmp_path / "output"),
        dpi=int(overrides.pop("dpi", 150)),  # fast enough for a test, still legible
        workers=int(overrides.pop("workers", 2)),
        **overrides,
    )


@needs_tesseract
def test_a_whole_folder_becomes_a_folder_of_results(sample_folder: Path, tmp_path: Path):
    run = run_blocking(_settings(sample_folder, tmp_path))
    out = tmp_path / "output"

    assert run.status == "done"
    assert run.totals.files_total == 2
    assert run.totals.pages_done == 3
    assert run.totals.pages_failed == 0

    # The output folder mirrors the input folder.
    assert (out / "scan.pdf").is_file()
    assert (out / "scan.txt").is_file()
    assert (out / "scan.json").is_file()
    assert (out / "sub" / "page.txt").is_file()

    text = (out / "scan.txt").read_text()
    assert "MARTINEZ" in text
    assert "----- page 2 (ocr) -----" in text

    # Every page kept a picture of itself.
    assert (out / "_previews" / "scan.pdf" / "p0001.jpg").is_file()

    # And the run left a report behind.
    rows = list(csv.DictReader((run.run_dir / "pages.csv").open()))
    assert len(rows) == 3
    assert {row["source"] for row in rows} == {"ocr"}


@needs_tesseract
def test_the_pdf_copy_is_actually_searchable(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path))
    pdf = pdfium.PdfDocument(str(tmp_path / "output" / "scan.pdf"))
    try:
        assert len(pdf) == 2
        page = pdf[0]
        textpage = page.get_textpage()
        found = textpage.get_text_range()
        textpage.close()
        page.close()
        assert "MARTINEZ" in found
    finally:
        pdf.close()


@needs_tesseract
def test_the_searchable_pdf_keeps_the_page_in_colour(tmp_path: Path):
    """OCR reads a cleaned-up greyscale copy, but the PDF you keep must still
    look like the document. A photograph exhibit, a highlighted passage and a
    red stamp are all coloured for a reason."""
    folder = tmp_path / "in"
    folder.mkdir()
    colour_page().save(folder / "exhibit.png")

    run_blocking(Settings(input_dir=str(folder), output_dir=str(tmp_path / "out"), dpi=150, workers=1))

    pdf = pdfium.PdfDocument(str(tmp_path / "out" / "exhibit.pdf"))
    try:
        page = pdf[0]
        textpage = page.get_textpage()
        text = textpage.get_text_range()
        textpage.close()
        rendered = page.render(scale=0.4).to_pil()
        page.close()
    finally:
        pdf.close()

    assert "EXHIBIT" in text, "the page must still be searchable"
    assert not is_greyscale(rendered), "the colour of the source page was lost"


@needs_tesseract
def test_the_cleaned_image_can_be_kept_instead(tmp_path: Path):
    folder = tmp_path / "in"
    folder.mkdir()
    colour_page().save(folder / "exhibit.png")

    run_blocking(Settings(
        input_dir=str(folder), output_dir=str(tmp_path / "out"),
        dpi=150, workers=1, pdf_keeps_source_image=False,
    ))

    pdf = pdfium.PdfDocument(str(tmp_path / "out" / "exhibit.pdf"))
    try:
        page = pdf[0]
        rendered = page.render(scale=0.4).to_pil()
        page.close()
    finally:
        pdf.close()
    assert is_greyscale(rendered)


@needs_tesseract
def test_the_invisible_text_still_lines_up_after_deskewing(tmp_path: Path):
    """The replacement picture is rotated the same way the OCR'd copy was, so
    every character box should land on ink. If the geometry were skipped, the
    text would float over a page tilted the other way."""
    import numpy as np

    folder = tmp_path / "in"
    folder.mkdir()
    colour_page(skew=1.8).save(folder / "skewed.png")

    run_blocking(Settings(input_dir=str(folder), output_dir=str(tmp_path / "out"), dpi=150, workers=1))

    pdf = pdfium.PdfDocument(str(tmp_path / "out" / "skewed.pdf"))
    try:
        page = pdf[0]
        textpage = page.get_textpage()
        rendered = page.render(scale=1.0).to_pil().convert("L")
        ink = np.asarray(rendered)
        width_pt, height_pt = page.get_size()
        scale_x, scale_y = rendered.width / width_pt, rendered.height / height_pt

        on_ink = checked = 0
        for index in range(textpage.count_chars()):
            box = textpage.get_charbox(index)
            if not box:
                continue
            left, bottom, right, top = box
            if right - left < 1 or top - bottom < 1:
                continue
            patch = ink[
                max(0, int((height_pt - top) * scale_y)) : int((height_pt - bottom) * scale_y),
                max(0, int(left * scale_x)) : int(right * scale_x),
            ]
            if patch.size == 0:
                continue
            checked += 1
            on_ink += int(patch.min() < 160)
        textpage.close()
        page.close()
    finally:
        pdf.close()

    assert checked > 20, "the page produced too few characters to judge alignment"
    assert on_ink / checked > 0.9, f"only {on_ink}/{checked} characters sit on ink"


@needs_tesseract
def test_a_pdf_that_already_has_text_is_not_ocred_again(sample_folder: Path, tmp_path: Path):
    """The searchable PDF from one run becomes a born-digital PDF for the next.

    This is the cheapest way to prove the text-layer path: the second run must
    take the text rather than re-reading a picture of it.
    """
    run_blocking(_settings(sample_folder, tmp_path))

    second_input = tmp_path / "second-input"
    second_input.mkdir()
    (second_input / "already-searchable.pdf").write_bytes((tmp_path / "output" / "scan.pdf").read_bytes())

    second = run_blocking(
        Settings(input_dir=str(second_input), output_dir=str(tmp_path / "output2"), dpi=150, workers=2)
    )
    assert second.totals.pages_text_layer >= 1
    assert "MARTINEZ" in (tmp_path / "output2" / "already-searchable.txt").read_text()


@needs_tesseract
def test_force_ocr_ignores_a_text_layer(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path))
    second_input = tmp_path / "second-input"
    second_input.mkdir()
    (second_input / "already-searchable.pdf").write_bytes((tmp_path / "output" / "scan.pdf").read_bytes())

    forced = run_blocking(
        Settings(
            input_dir=str(second_input), output_dir=str(tmp_path / "forced"),
            dpi=150, workers=2, force_ocr=True,
        )
    )
    assert forced.totals.pages_text_layer == 0
    assert forced.totals.pages_ocr >= 1


@needs_tesseract
def test_a_page_with_nothing_on_it_is_flagged_not_failed(tmp_path: Path):
    folder = tmp_path / "in"
    folder.mkdir()
    text_page([""], size=(900, 1200)).save(folder / "blank.png")

    run = run_blocking(Settings(input_dir=str(folder), output_dir=str(tmp_path / "out"), dpi=150, workers=1))
    assert run.status == "done"
    assert run.totals.pages_failed == 0
    assert run.totals.pages_flagged == 1
    assert "almost no text" in run.files[0].pages[0].review_reason


@needs_tesseract
def test_switching_the_deliverables_off_still_leaves_a_readable_run(sample_folder: Path, tmp_path: Path):
    run = run_blocking(
        _settings(sample_folder, tmp_path, write_pdf=False, write_txt=False, write_json=False)
    )
    out = tmp_path / "output"
    assert not (out / "scan.pdf").exists()
    assert not (out / "scan.txt").exists()

    # The viewer reads the run's own copy, which is written regardless.
    document = load_document(out, run.run_id, 0)
    assert document is not None and document["pages"][0]["text"]


@needs_tesseract
def test_the_manifest_can_be_read_back_after_the_run(sample_folder: Path, tmp_path: Path):
    run = run_blocking(_settings(sample_folder, tmp_path))
    out = tmp_path / "output"

    manifest = load_run(out, run.run_id)
    assert manifest is not None
    assert manifest["status"] == "done"
    assert manifest["totals"]["pages_done"] == 3
    assert len(manifest["files"]) == 2
    assert manifest["tesseract"]

    listed = list_runs(out)
    assert [entry["run_id"] for entry in listed] == [run.run_id]


@needs_tesseract
def test_events_report_progress_as_counts(sample_folder: Path, tmp_path: Path):
    events: list[dict] = []
    run_blocking(_settings(sample_folder, tmp_path), on_event=events.append)

    kinds = [event["type"] for event in events]
    assert "discovered" in kinds and "page" in kinds and "done" in kinds

    progress = [event["run"]["totals"] for event in events if event["type"] == "progress"]
    assert progress, "a run must publish progress while it works"
    # The denominator exists before the work starts, and the numerator only grows.
    assert all(totals["pages_total"] == 3 for totals in progress)
    assert progress[-1]["pages_done"] == 3
    assert [t["pages_done"] for t in progress] == sorted(t["pages_done"] for t in progress)


def test_a_cancelled_run_skips_its_remaining_pages(sample_folder: Path, tmp_path: Path):
    """Cancellation is checked before a page starts, so stopping is immediate
    for everything not already in flight."""
    run = Run(_settings(sample_folder, tmp_path))
    run.cancel()

    work = PageWork(
        file_index=0, relpath="scan.pdf", source_path=sample_folder / "scan.pdf",
        page_no=1, preview_dest=None, preview_rel=None, page_pdf_dest=None,
    )
    result: PageResult = run._page_task(work)
    assert result.source == "skipped"
    assert run.totals.pages_done == 0


def test_a_run_over_an_empty_folder_finishes_and_says_so(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run = run_blocking(Settings(input_dir=str(empty), output_dir=str(tmp_path / "out")))
    assert run.status == "done"
    assert run.totals.files_total == 0
    assert (run.run_dir / "manifest.json").is_file()
