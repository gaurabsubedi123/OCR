"""End-to-end: a folder in, a folder of results out.

These need the tesseract binary, and they are the tests that would have caught
every defect found while building this — an empty page, a lost text layer, a
PDF that assembled but carried no text.
"""

from __future__ import annotations

import csv
import json
import shutil
import time
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from conftest import colour_page, is_greyscale, needs_tesseract, text_page

from ocrtool.preprocess import turn

from ocrtool.config import Settings
from ocrtool.models import PageResult
from ocrtool.pipeline import PageWork, process_page
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

    # The input folder is mirrored, and each folder holding documents gets its
    # own pdf/ txt/ json/ inside it.
    assert (out / "pdf" / "scan.pdf").is_file()
    assert (out / "txt" / "scan.txt").is_file()
    assert (out / "json" / "scan.json").is_file()
    assert (out / "sub" / "txt" / "page.txt").is_file()
    assert (out / "sub" / "pdf" / "page.pdf").is_file()

    text = (out / "txt" / "scan.txt").read_text()
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
    pdf = pdfium.PdfDocument(str(tmp_path / "output" / "pdf" / "scan.pdf"))
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

    pdf = pdfium.PdfDocument(str(tmp_path / "out" / "pdf" / "exhibit.pdf"))
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

    pdf = pdfium.PdfDocument(str(tmp_path / "out" / "pdf" / "exhibit.pdf"))
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

    pdf = pdfium.PdfDocument(str(tmp_path / "out" / "pdf" / "skewed.pdf"))
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
    (second_input / "already-searchable.pdf").write_bytes(
        (tmp_path / "output" / "pdf" / "scan.pdf").read_bytes()
    )

    second = run_blocking(
        Settings(input_dir=str(second_input), output_dir=str(tmp_path / "output2"), dpi=150, workers=2)
    )
    assert second.totals.pages_text_layer >= 1
    assert "MARTINEZ" in (tmp_path / "output2" / "txt" / "already-searchable.txt").read_text()


@needs_tesseract
def test_force_ocr_ignores_a_text_layer(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path))
    second_input = tmp_path / "second-input"
    second_input.mkdir()
    (second_input / "already-searchable.pdf").write_bytes(
        (tmp_path / "output" / "pdf" / "scan.pdf").read_bytes()
    )

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
    assert not (out / "pdf").exists()
    assert not (out / "txt").exists()

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


@needs_tesseract
def test_outputs_can_be_kept_beside_each_other(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path, output_layout="together"))
    out = tmp_path / "output"
    assert (out / "scan.pdf").is_file()
    assert (out / "scan.txt").is_file()
    assert (out / "sub" / "page.json").is_file()
    assert not (out / "pdf").exists()


@needs_tesseract
def test_a_second_run_leaves_finished_documents_alone(sample_folder: Path, tmp_path: Path):
    """Adding four documents to a folder should cost the time of four
    documents, not the time of the whole folder again."""
    first = run_blocking(_settings(sample_folder, tmp_path))
    assert first.totals.pages_done == 3

    written = tmp_path / "output" / "txt" / "scan.txt"
    stamp = written.stat().st_mtime_ns

    second = run_blocking(_settings(sample_folder, tmp_path))
    assert second.totals.files_skipped == 2
    assert second.totals.pages_skipped == 3
    assert second.totals.pages_done == 0, "nothing should have been read again"
    assert second.totals.pages_total == 0, "and nothing should be counted as work to do"
    assert written.stat().st_mtime_ns == stamp, "the output was rewritten"
    assert all(f.status == "skipped" for f in second.files)
    assert second.files[0].skipped_from_run == first.run_id


@needs_tesseract
def test_only_the_new_document_is_read(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path))
    text_page(["A LATE ARRIVAL", "Case No. A-00-123456-C"]).save(sample_folder / "extra.png")

    second = run_blocking(_settings(sample_folder, tmp_path))
    assert second.totals.files_skipped == 2
    assert second.totals.pages_done == 1
    assert (tmp_path / "output" / "txt" / "extra.txt").is_file()


@needs_tesseract
def test_an_edited_source_is_read_again(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path))
    # Same name, different content: the size and modification time both move.
    text_page(["THIS PAGE WAS RESCANNED", "Case No. A-00-123456-C", "x" * 40]).save(
        sample_folder / "sub" / "page.png"
    )

    second = run_blocking(_settings(sample_folder, tmp_path))
    assert second.totals.files_skipped == 1
    assert "RESCANNED" in (tmp_path / "output" / "sub" / "txt" / "page.txt").read_text()


@needs_tesseract
def test_changing_a_setting_reads_everything_again(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path, dpi=150))
    second = run_blocking(_settings(sample_folder, tmp_path, dpi=200))
    assert second.totals.files_skipped == 0
    assert second.totals.pages_done == 3


@needs_tesseract
def test_a_deleted_output_is_written_again(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path))
    (tmp_path / "output" / "txt" / "scan.txt").unlink()

    second = run_blocking(_settings(sample_folder, tmp_path))
    assert second.totals.files_skipped == 1
    assert (tmp_path / "output" / "txt" / "scan.txt").is_file()


@needs_tesseract
def test_reading_everything_again_can_be_asked_for(sample_folder: Path, tmp_path: Path):
    run_blocking(_settings(sample_folder, tmp_path))
    second = run_blocking(_settings(sample_folder, tmp_path, skip_already_done=False))
    assert second.totals.files_skipped == 0
    assert second.totals.pages_done == 3


@needs_tesseract
def test_a_skipped_document_still_opens_in_the_viewer(sample_folder: Path, tmp_path: Path):
    """The results page of the second run must show the earlier run's text, or
    every skipped document looks like an empty one."""
    run_blocking(_settings(sample_folder, tmp_path))
    second = run_blocking(_settings(sample_folder, tmp_path))

    document = load_document(tmp_path / "output", second.run_id, 0)
    assert document is not None
    assert "MARTINEZ" in document["pages"][0]["text"]
    assert document["outputs"], "the links to its files must survive too"


@needs_tesseract
def test_an_interrupted_run_can_be_finished_by_starting_it_again(sample_folder: Path, tmp_path: Path):
    """Stopping mid-way and starting again is the same mechanism as adding new
    files: whatever finished is on disk, and whatever did not is still work."""
    run = Run(_settings(sample_folder, tmp_path, workers=1))
    run.start()
    # Let one document finish, then stop.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and run.totals.files_done < 1:
        time.sleep(0.1)
    run.cancel()
    run.join(timeout=60)
    assert run.totals.files_done >= 1

    resumed = run_blocking(_settings(sample_folder, tmp_path))
    assert resumed.totals.files_skipped >= 1
    assert resumed.status == "done"
    assert (tmp_path / "output" / "txt" / "scan.txt").is_file()
    assert (tmp_path / "output" / "sub" / "txt" / "page.txt").is_file()


def test_a_run_over_an_empty_folder_finishes_and_says_so(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run = run_blocking(Settings(input_dir=str(empty), output_dir=str(tmp_path / "out")))
    assert run.status == "done"
    assert run.totals.files_total == 0
    assert (run.run_dir / "manifest.json").is_file()


# ------------------------------------------------------ folders inside folders


@needs_tesseract
def test_a_nested_folder_is_mirrored_all_the_way_down(nested_folder: Path, tmp_path: Path):
    """Every folder that holds documents gets its own pdf/ txt/ json/, however
    deep it is."""
    run = run_blocking(_settings(nested_folder, tmp_path))
    out = tmp_path / "output"

    assert run.status == "done"
    assert run.totals.files_total == 4

    # Top level.
    assert (out / "pdf" / "top.pdf").is_file()
    assert (out / "txt" / "top.txt").is_file()
    assert (out / "json" / "top.json").is_file()

    # One down.
    assert (out / "A" / "pdf" / "doc.pdf").is_file()
    assert (out / "A" / "txt" / "doc.txt").is_file()

    # Two down.
    assert (out / "A" / "A1" / "pdf" / "copy.pdf").is_file()
    assert (out / "A" / "A1" / "txt" / "also-top.txt").is_file()
    assert (out / "A" / "A1" / "json" / "copy.json").is_file()

    # And nothing was collected at the top that belongs further down.
    assert not (out / "pdf" / "A").exists()


# ----------------------------------------------------------------- duplicates


@needs_tesseract
def test_an_identical_document_is_read_once_and_written_twice(
    nested_folder: Path, tmp_path: Path
):
    """Four files, two documents. The copies cost the time of a file copy, not
    the time of reading them."""
    run = run_blocking(_settings(nested_folder, tmp_path, duplicates="content"))
    out = tmp_path / "output"

    assert run.totals.files_copied == 2
    # Three pages of actual reading: two in the PDF, one in the image.
    assert run.totals.pages_done == 3

    # Both copies are on disk, and they say the same thing as their originals.
    assert (out / "A" / "txt" / "doc.txt").read_text() == (
        out / "A" / "A1" / "txt" / "copy.txt"
    ).read_text()
    assert (out / "txt" / "top.txt").read_text() == (
        out / "A" / "A1" / "txt" / "also-top.txt"
    ).read_text()

    # The searchable PDF is a real PDF, not a stub.
    copy = pdfium.PdfDocument(str(out / "A" / "A1" / "pdf" / "copy.pdf"))
    try:
        assert len(copy) == 2
    finally:
        copy.close()


@needs_tesseract
def test_a_copy_says_which_document_it_came_from(nested_folder: Path, tmp_path: Path):
    """A file that was never read must not look like one that was.

    Which of an identical pair is the one actually read follows discovery
    order, so the test asks about the pair rather than about a name.
    """
    run = run_blocking(_settings(nested_folder, tmp_path, duplicates="content"))
    by_relpath = {f.relpath: f for f in run.files}

    copies = [f for f in run.files if f.status == "copied"]
    assert len(copies) == 2
    for copy in copies:
        assert copy.duplicate_of in by_relpath
        assert by_relpath[copy.duplicate_of].status == "done"

    # Exactly one of each identical pair was read.
    for pair in (("A/doc.pdf", "A/A1/copy.pdf"), ("top.png", "A/A1/also-top.png")):
        statuses = sorted(by_relpath[relpath].status for relpath in pair)
        assert statuses == ["copied", "done"]

    # And the copy's own JSON names itself, not the document it came from.
    copy = copies[0]
    written = json.loads(
        (tmp_path / "output" / copy.outputs["json"]).read_text()
    )
    assert written["document"]["relpath"] == copy.relpath
    assert written["document"]["duplicate_of"] == copy.duplicate_of


@needs_tesseract
def test_a_copy_added_later_is_written_from_the_one_already_read(
    sample_folder: Path, tmp_path: Path
):
    """The copy arrives in a second run, so its twin is in the ledger rather
    than in this run."""
    settings = dict(duplicates="content")
    run_blocking(_settings(sample_folder, tmp_path, **settings))
    shutil.copyfile(sample_folder / "scan.pdf", sample_folder / "same-scan.pdf")

    second = run_blocking(_settings(sample_folder, tmp_path, **settings))
    assert second.totals.files_copied == 1
    assert second.totals.pages_done == 0, "a copy must cost no reading at all"
    assert (tmp_path / "output" / "txt" / "same-scan.txt").read_text() == (
        tmp_path / "output" / "txt" / "scan.txt"
    ).read_text()


@needs_tesseract
def test_two_documents_that_differ_are_both_read(sample_folder: Path, tmp_path: Path):
    """The guard against the optimisation: same size, different bytes."""
    text_page(["A DIFFERENT PAGE ENTIRELY", "Case No. A-00-123456-C"]).save(
        sample_folder / "other.png"
    )
    run = run_blocking(_settings(sample_folder, tmp_path))
    assert run.totals.files_copied == 0
    assert run.totals.pages_done == 4


# -------------------------------------------------------------------- resuming


@needs_tesseract
def test_pages_survive_a_run_stopped_part_way_through_a_document(
    long_document: Path, tmp_path: Path
):
    """The failure this exists for: an hour of reading held in memory and lost
    when the run was stopped."""
    run = Run(_settings(long_document, tmp_path, workers=1))
    run.start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and run.totals.pages_done < 3:
        time.sleep(0.05)
    read_before_stopping = run.totals.pages_done
    run.cancel()
    run.join(timeout=120)

    assert read_before_stopping >= 3, "the run never got going"
    assert run.status == "cancelled"

    # Those pages are on disk, not in a process that has gone.
    cached = list((tmp_path / "output" / "_cache").rglob("p*.json"))
    assert len(cached) >= read_before_stopping

    resumed = run_blocking(_settings(long_document, tmp_path))
    assert resumed.status == "done"
    assert resumed.totals.pages_resumed >= read_before_stopping
    # Only what was still missing was read again.
    assert resumed.totals.pages_done == 8 - resumed.totals.pages_resumed

    text = (tmp_path / "output" / "txt" / "record.txt").read_text()
    assert "PAGE 1 OF THE RECORD" in text
    assert "PAGE 8 OF THE RECORD" in text
    assert text.count("----- page") == 8


@needs_tesseract
def test_a_finished_document_leaves_no_cache_behind(sample_folder: Path, tmp_path: Path):
    """The cache is unfinished work. Finished work must not sit in it."""
    run_blocking(_settings(sample_folder, tmp_path))
    assert not (tmp_path / "output" / "_cache").exists()


@needs_tesseract
def test_changing_the_resolution_does_not_resume_onto_the_old_pages(
    long_document: Path, tmp_path: Path
):
    """Pages read at one resolution are not the pages another run would read."""
    run = Run(_settings(long_document, tmp_path, workers=1, dpi=150))
    run.start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and run.totals.pages_done < 2:
        time.sleep(0.05)
    run.cancel()
    run.join(timeout=120)

    second = run_blocking(_settings(long_document, tmp_path, dpi=200))
    assert second.totals.pages_resumed == 0
    assert second.totals.pages_done == 8


@needs_tesseract
def test_a_copy_opens_in_the_viewer_with_its_page_pictures(
    nested_folder: Path, tmp_path: Path
):
    """A copied document has no previews of its own — it points at the ones
    belonging to the document it is identical to, which are the same pictures.
    Without this a copy opens as a wall of text with nothing to check it
    against."""
    run = run_blocking(_settings(nested_folder, tmp_path, duplicates="content"))
    copy_index = next(i for i, f in enumerate(run.files) if f.status == "copied")

    document = load_document(tmp_path / "output", run.run_id, copy_index)
    assert document is not None
    assert document["pages"], "a copy must have pages"
    for page in document["pages"]:
        assert page["preview"], "every page needs a picture"
        assert (tmp_path / "output" / page["preview"]).is_file()


@needs_tesseract
def test_a_copy_of_a_document_that_never_arrived_says_so(
    nested_folder: Path, tmp_path: Path, monkeypatch
):
    """A row saying "copied" with nothing on disk would be worse than a row
    saying what actually happened."""
    from ocrtool import runner as runner_module

    real = runner_module.write_searchable_pdf

    def explode(dest, **kwargs):
        if dest.stem in {"doc", "copy"}:
            raise RuntimeError("no pdf for you")
        return real(dest, **kwargs)

    monkeypatch.setattr(runner_module, "write_searchable_pdf", explode)

    run = run_blocking(_settings(nested_folder, tmp_path, duplicates="content"))
    pdf_pair = [f for f in run.files if f.relpath.endswith((".pdf",))]
    assert {f.status for f in pdf_pair} == {"failed"}
    assert all(f.error for f in pdf_pair)
    # The count must not still claim a copy that was never written.
    assert run.totals.files_copied == 1


# ------------------------------------------------- what counts as the same document


@needs_tesseract
def test_by_default_a_copy_under_a_different_name_is_read_on_its_own(
    nested_folder: Path, tmp_path: Path
):
    """The default rule needs the names to match too, so four differently
    named files are four documents even when their bytes are identical."""
    run = run_blocking(_settings(nested_folder, tmp_path))
    assert run.totals.files_copied == 0
    assert run.totals.pages_done == 6  # two 2-page PDFs, two 1-page images


@needs_tesseract
def test_the_same_name_in_two_folders_is_read_once(tmp_path: Path):
    folder = tmp_path / "in"
    (folder / "A").mkdir(parents=True)
    (folder / "B").mkdir(parents=True)
    text_page(["EXHIBIT 32 - POLICY MANUAL"]).save(folder / "A" / "manual.png")
    shutil.copyfile(folder / "A" / "manual.png", folder / "B" / "manual.png")

    run = run_blocking(_settings(folder, tmp_path))
    assert run.totals.files_copied == 1
    assert run.totals.pages_done == 1
    assert (tmp_path / "output" / "A" / "txt" / "manual.txt").read_text() == (
        tmp_path / "output" / "B" / "txt" / "manual.txt"
    ).read_text()


@needs_tesseract
def test_the_same_name_with_different_contents_is_never_copied(tmp_path: Path):
    """The rule that stops a name-based match from putting one document's text
    under another document's name. `scan.pdf` is in every folder anyone has."""
    folder = tmp_path / "in"
    (folder / "A").mkdir(parents=True)
    (folder / "B").mkdir(parents=True)
    text_page(["EXHIBIT 32 - POLICY MANUAL"]).save(folder / "A" / "scan.png")
    text_page(["EXHIBIT 41 - RADIOLOGY REPORT"]).save(folder / "B" / "scan.png")

    run = run_blocking(_settings(folder, tmp_path))
    assert run.totals.files_copied == 0
    assert run.totals.pages_done == 2
    assert "POLICY" in (tmp_path / "output" / "A" / "txt" / "scan.txt").read_text()
    assert "RADIOLOGY" in (tmp_path / "output" / "B" / "txt" / "scan.txt").read_text()


@needs_tesseract
def test_duplicate_matching_can_be_turned_off(nested_folder: Path, tmp_path: Path):
    run = run_blocking(_settings(nested_folder, tmp_path, duplicates="off"))
    assert run.totals.files_copied == 0
    assert run.totals.pages_done == 6


@needs_tesseract
def test_identical_documents_do_not_share_a_cache_when_names_must_match(
    nested_folder: Path, tmp_path: Path
):
    """Two same-content documents read separately must not write over each
    other's stored pages, or the first to finish would delete the page PDFs
    the second still needs."""
    run = run_blocking(_settings(nested_folder, tmp_path))
    assert run.status == "done"
    assert all(f.status == "done" for f in run.files), [
        (f.relpath, f.status, f.error) for f in run.files
    ]
    out = tmp_path / "output"
    for relpath in ("A/pdf/doc.pdf", "A/A1/pdf/copy.pdf"):
        pdf = pdfium.PdfDocument(str(out / relpath))
        try:
            assert len(pdf) == 2, f"{relpath} lost a page"
        finally:
            pdf.close()


@needs_tesseract
def test_the_same_name_arriving_in_a_later_run_is_copied(sample_folder: Path, tmp_path: Path):
    """The default rule, across runs: the twin is in the ledger, not this run."""
    run_blocking(_settings(sample_folder, tmp_path))
    (sample_folder / "later").mkdir()
    shutil.copyfile(sample_folder / "scan.pdf", sample_folder / "later" / "scan.pdf")

    second = run_blocking(_settings(sample_folder, tmp_path))
    assert second.totals.files_copied == 1
    assert second.totals.pages_done == 0, "a copy must cost no reading at all"
    assert (tmp_path / "output" / "later" / "txt" / "scan.txt").read_text() == (
        tmp_path / "output" / "txt" / "scan.txt"
    ).read_text()


# ---------------------------------------------------- keeping the tool's folders apart


@needs_tesseract
def test_the_output_folder_can_hold_nothing_but_results(sample_folder: Path, tmp_path: Path):
    """With a work folder chosen, the output folder is the results and nothing
    else — no _runs/, no _previews/, no _cache/."""
    work = tmp_path / "work"
    run = run_blocking(_settings(sample_folder, tmp_path, work_dir=str(work)))
    out = tmp_path / "output"

    assert run.status == "done"
    assert sorted(p.name for p in out.iterdir()) == ["json", "pdf", "sub", "txt"]
    assert not (out / "_runs").exists()
    assert not (out / "_previews").exists()

    # And the bookkeeping is all in the work folder.
    assert (work / "_runs" / run.run_id / "manifest.json").is_file()
    assert (work / "_runs" / "completed.json").is_file()
    assert (work / "_previews" / "scan.pdf" / "p0001.jpg").is_file()

    # The results themselves are still where they belong.
    assert (out / "pdf" / "scan.pdf").is_file()
    assert (out / "sub" / "txt" / "page.txt").is_file()


@needs_tesseract
def test_a_separate_work_folder_still_skips_what_it_has_read(
    sample_folder: Path, tmp_path: Path
):
    """The ledger lives in the work folder but records paths in the output
    folder. Getting that pair wrong would make it re-read everything."""
    work = tmp_path / "work"
    run_blocking(_settings(sample_folder, tmp_path, work_dir=str(work)))
    second = run_blocking(_settings(sample_folder, tmp_path, work_dir=str(work)))
    assert second.totals.files_skipped == 2
    assert second.totals.pages_done == 0


@needs_tesseract
def test_a_deleted_output_is_noticed_from_a_separate_work_folder(
    sample_folder: Path, tmp_path: Path
):
    """The check that proves the ledger is looking in the output folder for
    the outputs, not in the work folder."""
    work = tmp_path / "work"
    run_blocking(_settings(sample_folder, tmp_path, work_dir=str(work)))
    (tmp_path / "output" / "txt" / "scan.txt").unlink()

    second = run_blocking(_settings(sample_folder, tmp_path, work_dir=str(work)))
    assert second.totals.files_skipped == 1
    assert (tmp_path / "output" / "txt" / "scan.txt").is_file()


@needs_tesseract
def test_a_stopped_run_resumes_from_a_separate_work_folder(
    long_document: Path, tmp_path: Path
):
    work = tmp_path / "work"
    run = Run(_settings(long_document, tmp_path, workers=1, work_dir=str(work)))
    run.start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and run.totals.pages_done < 3:
        time.sleep(0.05)
    run.cancel()
    run.join(timeout=120)

    assert (work / "_cache").is_dir()
    assert not (tmp_path / "output" / "_cache").exists()

    resumed = run_blocking(_settings(long_document, tmp_path, work_dir=str(work)))
    assert resumed.status == "done"
    assert resumed.totals.pages_resumed >= 3
    assert (tmp_path / "output" / "txt" / "record.txt").read_text().count("----- page") == 8


def _one_page(path: Path) -> PageWork:
    return PageWork(
        file_index=0, relpath=path.name, source_path=path, page_no=1,
        preview_dest=None, preview_rel=None, page_pdf_dest=None,
    )


@needs_tesseract
@pytest.mark.parametrize("laid", [180, 270])
def test_a_page_that_is_not_upright_is_stood_up_and_read(tmp_path: Path, laid: int):
    """The failure this exists for: an inverted page reads as confident nonsense.

    Measured on a real exhibit, an upside-down page came back at 39% confidence
    reading `vooododd0O0oIsA9` — long enough and plausible enough that neither
    the character count nor a glance at the file would catch it.

    270 is here alongside 180 because a quarter turn the other way is the one
    tesseract stands up by itself; this is the direction it does not.
    """
    page = tmp_path / "sideways.png"
    turn(text_page(), laid).save(page)

    settings = Settings(input_dir=".", output_dir=".", orient=True)
    result = process_page(_one_page(page), settings)

    assert result.rotation_applied == (360 - laid) % 360
    assert "INCIDENT REPORT" in result.text
    assert result.confidence is not None and result.confidence > 70
    assert not result.needs_review


@needs_tesseract
def test_an_upright_page_is_left_alone_and_costs_nothing(tmp_path: Path):
    page = tmp_path / "upright.png"
    text_page().save(page)
    result = process_page(_one_page(page), Settings(input_dir=".", output_dir=".", orient=True))
    assert result.rotation_applied == 0
    assert "INCIDENT REPORT" in result.text


@needs_tesseract
def test_turning_can_be_switched_off(tmp_path: Path):
    """--no-orient leaves the page as it was found, however badly it reads."""
    page = tmp_path / "sideways.png"
    turn(text_page(), 180).save(page)
    result = process_page(_one_page(page), Settings(input_dir=".", output_dir=".", orient=False))
    assert result.rotation_applied == 0
    assert "INCIDENT REPORT" not in result.text


@needs_tesseract
def test_the_report_says_which_pages_had_to_be_turned(sample_folder: Path, tmp_path: Path):
    turn(text_page(["EXHIBIT 9 - UPSIDE DOWN", "Case No. A-00-123456-C"]), 180).save(
        sample_folder / "inverted.png"
    )
    run = run_blocking(_settings(sample_folder, tmp_path))
    rows = list(csv.DictReader((Path(run.run_dir) / "pages.csv").open()))
    turned = {r["file"]: r["rotated"] for r in rows if r["rotated"]}
    assert turned == {"inverted.png": "180"}
