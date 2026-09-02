"""Everything that can be checked without running tesseract."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from ocrtool.config import (
    MAX_RENDER_PIXELS,
    Settings,
    default_folders,
    save_default_folders,
)
from ocrtool.ledger import Ledger, LedgerEntry, recipe
from ocrtool.discover import document_tree, find_documents, natural_key
from ocrtool.models import FileResult, PageResult, Word
from ocrtool.cache import document_key
from ocrtool.outputs import (
    laid_out_text,
    LAYOUTS,
    output_basenames,
    output_paths,
    write_json,
    write_pages_csv,
    write_text,
)
from ocrtool.pipeline import _flag, _improves_on, _reads_badly
from ocrtool.preprocess import (
    MAX_SKEW_DEGREES,
    WIDE_SKEW_DEGREES,
    _estimate_skew,
    preprocess,
    turn,
)
from ocrtool.render import _fit, render_page
from ocrtool.tesseract import parse_osd, parse_tsv
from ocrtool.pdfpage import replace_page_image
from ocrtool.preprocess import apply_geometry
from ocrtool.textlayer import _tidy, read_text_layer

from conftest import colour_page, text_page


# --------------------------------------------------------------- settings


def test_settings_clamp_impossible_values():
    settings = Settings(input_dir=".", output_dir=".", dpi=5000, psm=99, workers=-4)
    assert settings.dpi == 600
    assert settings.psm == 13
    assert settings.workers >= 1  # 0 or negative means "decide from the machine"


def test_settings_round_trip():
    original = Settings(input_dir="/in", output_dir="/out", dpi=200, force_ocr=True)
    restored = Settings.from_dict({**original.to_dict(), "unknown_key": "ignored"})
    assert restored.dpi == 200 and restored.force_ocr is True


# ----------------------------------------------------------- default folders


def test_default_folders_fall_back_to_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OCRTOOL_INPUT_DIR", raising=False)
    monkeypatch.delenv("OCRTOOL_OUTPUT_DIR", raising=False)
    folders = default_folders()
    assert folders["input"].endswith("ocr-input")
    assert folders["output"].endswith("ocr-output")


def test_saved_folders_are_remembered_and_created(tmp_path, monkeypatch):
    monkeypatch.delenv("OCRTOOL_INPUT_DIR", raising=False)
    monkeypatch.delenv("OCRTOOL_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("OCRTOOL_WORK_DIR", raising=False)
    wanted_in, wanted_out = tmp_path / "scans", tmp_path / "results"

    folders = save_default_folders(input_dir=str(wanted_in), output_dir=str(wanted_out))

    assert folders == {"input": str(wanted_in), "output": str(wanted_out), "work": ""}
    assert wanted_in.is_dir() and wanted_out.is_dir(), "the folders should exist afterwards"
    assert default_folders()["input"] == str(wanted_in), "and survive into the next call"


def test_a_work_folder_is_remembered_and_can_be_put_back(tmp_path, monkeypatch):
    """Empty means "keep the tool's folders with the results", which is a
    decision rather than a missing value — so it has to be storable."""
    monkeypatch.delenv("OCRTOOL_WORK_DIR", raising=False)
    wanted = tmp_path / "bookkeeping"

    folders = save_default_folders(work_dir=str(wanted))
    assert folders["work"] == str(wanted)
    assert wanted.is_dir()
    assert default_folders()["work"] == str(wanted)

    assert save_default_folders(work_dir="")["work"] == ""
    assert default_folders()["work"] == ""


def test_the_environment_wins_over_the_saved_folders(tmp_path, monkeypatch):
    save_default_folders(input_dir=str(tmp_path / "saved"), output_dir=str(tmp_path / "saved-out"))
    monkeypatch.setenv("OCRTOOL_INPUT_DIR", str(tmp_path / "from-env"))
    assert default_folders()["input"] == str(tmp_path / "from-env")
    assert default_folders()["output"] == str(tmp_path / "saved-out")


# ------------------------------------------------------------------ ledger


def _entry(tmp_path: Path, **overrides) -> LedgerEntry:
    defaults = dict(
        relpath="scan.pdf", size=100, mtime=1000.0, pages=2,
        outputs={"txt": "txt/scan.txt"}, recipe=recipe({"dpi": 300, "lang": "eng"}),
        run_id="20260101-000000", pages_json=None,
    )
    defaults.update(overrides)
    return LedgerEntry(**defaults)


def _ledger_with(tmp_path: Path, entry: LedgerEntry) -> tuple[Ledger, dict]:
    ledger = Ledger(tmp_path)
    ledger.record(entry)
    written = tmp_path / "txt" / "scan.txt"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text("already read")
    return ledger, {"txt": written}


def test_the_ledger_recognises_work_it_has_already_done(tmp_path: Path):
    ledger, wanted = _ledger_with(tmp_path, _entry(tmp_path))
    found = ledger.already_done(
        "scan.pdf", size=100, mtime=1000.0, settings={"dpi": 300, "lang": "eng"}, wanted=wanted
    )
    assert found is not None and found.run_id == "20260101-000000"


def test_the_ledger_refuses_when_the_source_changed(tmp_path: Path):
    ledger, wanted = _ledger_with(tmp_path, _entry(tmp_path))
    settings = {"dpi": 300, "lang": "eng"}
    assert ledger.already_done("scan.pdf", size=101, mtime=1000.0, settings=settings, wanted=wanted) is None
    assert ledger.already_done("scan.pdf", size=100, mtime=9999.0, settings=settings, wanted=wanted) is None


def test_the_ledger_refuses_when_the_settings_changed(tmp_path: Path):
    ledger, wanted = _ledger_with(tmp_path, _entry(tmp_path))
    assert ledger.already_done(
        "scan.pdf", size=100, mtime=1000.0, settings={"dpi": 400, "lang": "eng"}, wanted=wanted
    ) is None


def test_the_ledger_refuses_when_an_output_is_missing(tmp_path: Path):
    ledger, wanted = _ledger_with(tmp_path, _entry(tmp_path))
    wanted["txt"].unlink()
    assert ledger.already_done(
        "scan.pdf", size=100, mtime=1000.0, settings={"dpi": 300, "lang": "eng"}, wanted=wanted
    ) is None
    # A kind that was never written cannot be reused either.
    ledger2, _ = _ledger_with(tmp_path, _entry(tmp_path))
    pdf = tmp_path / "pdf" / "scan.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF")
    assert ledger2.already_done(
        "scan.pdf", size=100, mtime=1000.0, settings={"dpi": 300, "lang": "eng"},
        wanted={"pdf": pdf},
    ) is None


def test_a_ledger_survives_being_written_and_read_again(tmp_path: Path):
    ledger, wanted = _ledger_with(tmp_path, _entry(tmp_path))
    assert len(Ledger(tmp_path)) == 1


# --------------------------------------------------------------- discovery


def test_discovery_finds_documents_and_ignores_the_rest(sample_folder: Path):
    found = find_documents(sample_folder)
    names = {item.relpath for item in found}
    assert names == {"scan.pdf", "sub/page.png"}
    assert {item.page_count for item in found} == {2, 1}
    assert all(item.error is None for item in found)


def test_discovery_can_stay_at_the_top_level(sample_folder: Path):
    found = find_documents(sample_folder, recursive=False)
    assert {item.relpath for item in found} == {"scan.pdf"}


def test_discovery_skips_the_folders_the_tool_writes(tmp_path: Path):
    (tmp_path / "_runs" / "20260101-000000").mkdir(parents=True)
    text_page().save(tmp_path / "_runs" / "20260101-000000" / "leftover.png")
    text_page().save(tmp_path / "real.png")
    assert {item.relpath for item in find_documents(tmp_path)} == {"real.png"}


def test_an_unreadable_file_is_reported_not_raised(tmp_path: Path):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.7 truncated and invalid")
    found = find_documents(tmp_path)
    assert len(found) == 1 and found[0].error


# ----------------------------------------------------------------- render


def test_render_caps_the_pixels_of_an_oversized_page():
    # A 23 x 30 inch page at 300 dpi would be 9000 px on the long edge.
    scale, dpi = _fit(23.0, 30.0, 300)
    assert round(30.0 * dpi) <= MAX_RENDER_PIXELS
    assert dpi < 300

    scale, dpi = _fit(8.5, 11.0, 300)
    assert dpi == 300 and scale == pytest.approx(300 / 72)


def test_rendering_an_image_returns_it_with_a_resolution(tmp_path: Path):
    text_page().save(tmp_path / "page.png")
    rendered = render_page(tmp_path / "page.png", 1, 300)
    assert rendered.image.size == (1700, 2200)
    assert rendered.dpi > 0


# ------------------------------------------------------------- preprocess


def test_preprocess_does_not_darken_a_sparse_page():
    """The autocontrast regression, kept as a test.

    Clipping both ends of the histogram destroys a page whose ink covers less
    than the cutoff: the dark cut point lands in the background and drags it
    toward black. On the page that first showed this, tesseract went from 95%
    confidence to returning nothing at all.
    """
    sparse = text_page(["EXHIBIT 3 - MEDICAL BILL"])
    before = np.asarray(ImageOps.grayscale(sparse))
    after = np.asarray(preprocess(sparse).image)

    dark_before = int((before < 128).sum())
    dark_after = int((after < 128).sum())
    assert dark_after < dark_before * 1.5, "preprocessing turned background into ink"


def test_preprocess_straightens_a_skewed_page():
    skewed = text_page(skew=2.0)
    result = preprocess(skewed)
    # The correction undoes the skew, so it points the other way.
    assert result.skew_corrected == pytest.approx(-2.0, abs=0.7)


def test_a_straight_page_is_left_alone():
    assert preprocess(text_page()).skew_corrected == 0.0


def _laid_at(degrees: float) -> Image.Image:
    """A page put down at `degrees` clockwise, the whole page kept."""
    return ImageOps.grayscale(
        text_page().rotate(-degrees, resample=Image.BICUBIC, expand=True, fillcolor="white")
    )


@pytest.mark.parametrize("degrees", [3, 6, 10, 15])
def test_skew_is_measured_across_the_whole_ordinary_range(degrees: int):
    """Up to the cap the estimate has to be right, not merely close.

    6 is here for a reason: it was the old cap, and a page laid exactly there
    came back as 0.0 — "this page is straight", the one thing known to be
    false. The fine pass searches a degree past the coarse winner, landed
    outside the cap, and the result was thrown away rather than clamped.
    """
    assert _estimate_skew(_laid_at(degrees)) == pytest.approx(degrees, abs=0.5)


@pytest.mark.parametrize("degrees", [20, 30, 45])
def test_a_page_well_past_the_cap_is_still_measured(degrees: int):
    """Saturating the first search buys a wider one, out to a quarter turn."""
    assert _estimate_skew(_laid_at(degrees)) == pytest.approx(degrees, abs=1.0)


def test_the_estimate_never_exceeds_a_quarter_turn():
    """Past 45 a quarter turn is the shorter way round and pipeline.py has it."""
    for degrees in (0, 5, 20, 44, 45):
        assert abs(_estimate_skew(_laid_at(degrees))) <= WIDE_SKEW_DEGREES


def test_the_wide_search_does_not_invent_skew_on_a_straight_page():
    """The risk of searching further is correcting a page that was already fine.

    Measured on ten real pages from a claim file, the estimate stayed at 0.0 at
    every cap tried — 6, 15, 25 and 45 alike — which is what made widening it
    safe to do.
    """
    assert _estimate_skew(_laid_at(0)) == pytest.approx(0.0, abs=0.3)
    assert preprocess(text_page()).skew_corrected == 0.0


def test_the_wide_search_starts_where_the_ordinary_one_stops():
    assert WIDE_SKEW_DEGREES > MAX_SKEW_DEGREES


def test_skew_estimate_survives_a_blank_page():
    blank = Image.new("L", (400, 400), "white")
    assert _estimate_skew(blank) == 0.0


def test_preprocess_upscales_a_small_scan():
    small = text_page(["tiny"], size=(600, 400))
    assert preprocess(small).upscaled > 1.0


def test_geometry_can_be_reapplied_to_the_untouched_page():
    """The searchable PDF gets the original picture back, so that picture has to
    end up exactly the shape of the image OCR read — or the invisible text sits
    in the wrong place."""
    original = colour_page(skew=2.0)
    cleaned = preprocess(original)
    replacement = apply_geometry(original, skew=cleaned.skew_corrected, size=cleaned.image.size)

    assert replacement.size == cleaned.image.size
    assert replacement.mode == "RGB", "the replacement must keep its colour"


def test_replacing_a_page_image_gives_up_rather_than_corrupting():
    """Anything unexpected returns the page untouched: a wrongly-coloured
    picture is cosmetic, a lost page is not."""
    assert replace_page_image(b"not a pdf at all", colour_page()) == b"not a pdf at all"


# --------------------------------------------------------------- tesseract


TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t96.5\tCase\n"
    "5\t1\t1\t1\t1\t2\t50\t20\t20\t12\t45.0\tNo.\n"
    "5\t1\t1\t1\t2\t1\t10\t60\t80\t12\t88.0\tA-00-123456-C\n"
    "4\t1\t1\t1\t2\t0\t0\t0\t0\t0\t-1\t\n"
)


def test_parse_tsv_rebuilds_lines_and_confidence():
    parsed = parse_tsv(TSV)
    assert parsed.text == "Case No.\nA-00-123456-C"
    assert [w.text for w in parsed.words] == ["Case", "No.", "A-00-123456-C"]
    assert parsed.mean_confidence == pytest.approx((96.5 + 45.0 + 88.0) / 3, abs=0.01)


def test_parse_tsv_drops_structural_rows_and_empty_text():
    parsed = parse_tsv(TSV)
    assert all(word.confidence >= 0 for word in parsed.words)
    assert len(parsed.words) == 3


def test_parse_tsv_on_empty_output():
    parsed = parse_tsv("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n")
    assert parsed.text == "" and parsed.mean_confidence is None


# --------------------------------------------------------------- text layer


def test_an_image_never_has_a_text_layer(tmp_path: Path):
    text_page().save(tmp_path / "page.png")
    layer = read_text_layer(tmp_path / "page.png", 1)
    assert layer.usable is False and layer.char_count == 0


def test_a_scanned_pdf_has_no_usable_text_layer(tmp_path: Path):
    # PIL writes the page as an image, which is exactly what a scan is.
    text_page().save(tmp_path / "scan.pdf")
    assert read_text_layer(tmp_path / "scan.pdf", 1).usable is False


def test_tidy_collapses_blank_runs_but_keeps_indentation():
    # Trailing space goes and runs of blank lines collapse to one, but leading
    # space stays: indentation is layout, and a stripped line can glue a form's
    # label to a value that belonged in another column.
    assert _tidy("A\r\n\r\n\r\n  B  \r\n") == "A\n\n  B"


# -------------------------------------------------------------- flagging


def test_a_low_confidence_page_is_flagged():
    settings = Settings(input_dir=".", output_dir=".", min_confidence=70)
    page = PageResult(page_no=1, source="ocr", text="x" * 500, confidence=52.0)
    _flag(page, settings)
    assert page.needs_review and "confidence" in page.review_reason


def test_an_empty_page_is_flagged_even_at_high_confidence():
    page = PageResult(page_no=1, source="ocr", text="hi", confidence=99.0)
    _flag(page, Settings(input_dir=".", output_dir="."))
    assert page.needs_review and "almost no text" in page.review_reason


def test_a_good_page_is_not_flagged():
    page = PageResult(page_no=1, source="ocr", text="x" * 500, confidence=91.0)
    _flag(page, Settings(input_dir=".", output_dir="."))
    assert not page.needs_review


def test_a_text_layer_page_is_judged_on_its_length_alone():
    page = PageResult(page_no=1, source="text-layer", text="x" * 500, confidence=None)
    _flag(page, Settings(input_dir=".", output_dir="."))
    assert not page.needs_review


# ---------------------------------------------------------------- outputs


def _result() -> FileResult:
    result = FileResult(relpath="Ex 13/scan.pdf", source_path="/in/Ex 13/scan.pdf", size_bytes=10, page_count=2)
    result.pages = [
        PageResult(page_no=1, source="text-layer", text="page one", duration_ms=120),
        PageResult(
            page_no=2, source="ocr", text="page two", confidence=61.0, duration_ms=900,
            needs_review=True, review_reason="mean confidence 61 below 70",
            words=[Word("page", 0, 0, 40, 18, 61.0), Word("two", 50, 0, 80, 18, 61.0)],
        ),
    ]
    return result


def test_outputs_land_in_the_folder_the_document_came_from():
    """The default: walk to where the document was and its results are there."""
    paths = output_paths(Path("/out"), "Medical/Imaging/13.pdf")
    assert paths["pdf"] == Path("/out/Medical/Imaging/pdf/13.pdf")
    assert paths["txt"] == Path("/out/Medical/Imaging/txt/13.txt")
    assert paths["json"] == Path("/out/Medical/Imaging/json/13.json")


def test_a_document_at_the_top_gets_its_folders_at_the_top():
    paths = output_paths(Path("/out"), "loose.pdf")
    assert paths["pdf"] == Path("/out/pdf/loose.pdf")
    assert paths["txt"] == Path("/out/txt/loose.txt")


def test_every_depth_gets_its_own_three_folders():
    """A folder four deep is no different from one at the top."""
    deep = output_paths(Path("/out"), "a/b/c/d/scan.pdf")
    assert deep["pdf"] == Path("/out/a/b/c/d/pdf/scan.pdf")
    assert deep["json"] == Path("/out/a/b/c/d/json/scan.json")


def test_outputs_can_be_sorted_by_type_instead():
    paths = output_paths(Path("/out"), "Ex 13/scan.pdf", layout="by-type")
    assert paths["pdf"] == Path("/out/pdf/Ex 13/scan.pdf")
    assert paths["txt"] == Path("/out/txt/Ex 13/scan.txt")
    assert paths["json"] == Path("/out/json/Ex 13/scan.json")


def test_outputs_can_be_kept_together_instead():
    paths = output_paths(Path("/out"), "Ex 13/scan.pdf", layout="together")
    assert paths["pdf"] == Path("/out/Ex 13/scan.pdf")
    assert paths["txt"] == Path("/out/Ex 13/scan.txt")
    assert paths["json"] == Path("/out/Ex 13/scan.json")


def test_an_extension_is_replaced_not_appended():
    # `13.pdf` must become `13.txt`, never `13.pdf.txt`.
    assert output_paths(Path("/out"), "13.pdf")["txt"].name == "13.txt"
    assert output_paths(Path("/out"), "photo.jpeg")["pdf"].name == "photo.pdf"


def test_a_name_with_dots_in_it_keeps_all_of_it():
    """`1. Plaintiff_Record.pdf` came out as `1.pdf` — only the extension goes.

    Numbered exhibits are named this way, so the whole folder collapsed onto
    `1.pdf`, `2.pdf`, each new document overwriting the one before it.
    """
    for layout in LAYOUTS:
        paths = output_paths(Path("/out"), "1. Plaintiff_Record.pdf", layout=layout)
        assert paths["pdf"].name == "1. Plaintiff_Record.pdf"
        assert paths["txt"].name == "1. Plaintiff_Record.txt"
        assert paths["json"].name == "1. Plaintiff_Record.json"

    assert output_paths(Path("/out"), "Dr. Smith notes.pdf")["txt"].name == (
        "Dr. Smith notes.txt"
    )
    assert output_paths(Path("/out"), "Ex 3.2 report.tif")["pdf"].name == (
        "Ex 3.2 report.pdf"
    )


def test_documents_numbered_the_same_way_do_not_overwrite_each_other():
    first = output_paths(Path("/out"), "Ex/1. Plaintiff_Record.pdf")
    second = output_paths(Path("/out"), "Ex/1. Defendant_Record.pdf")
    assert first["pdf"] != second["pdf"]


def test_a_name_with_no_rival_keeps_the_short_form():
    names = output_basenames(["Ex/13.pdf", "Ex/14.png", "other/13.tif"])
    assert names == {"Ex/13.pdf": "13", "Ex/14.png": "14", "other/13.tif": "13"}


def test_two_documents_differing_only_by_extension_both_survive():
    """`x.pdf` and `x.png` both wanted `x.txt`; the second overwrote the first."""
    names = output_basenames(["scan.pdf", "scan.png"])
    assert names == {"scan.pdf": "scan_pdf", "scan.png": "scan_png"}

    paths = {
        relpath: output_paths(Path("/out"), relpath, basename=base)
        for relpath, base in names.items()
    }
    assert paths["scan.pdf"]["txt"] == Path("/out/txt/scan_pdf.txt")
    assert paths["scan.png"]["txt"] == Path("/out/txt/scan_png.txt")
    assert paths["scan.pdf"]["pdf"] == Path("/out/pdf/scan_pdf.pdf")
    assert paths["scan.png"]["json"] == Path("/out/json/scan_png.json")
    for kind in ("pdf", "txt", "json"):
        assert paths["scan.pdf"][kind] != paths["scan.png"][kind]


def test_the_clash_is_judged_per_folder_not_across_the_tree():
    """The same name in two folders is not a clash — the folders keep them apart."""
    names = output_basenames(["a/scan.pdf", "b/scan.png"])
    assert names == {"a/scan.pdf": "scan", "b/scan.png": "scan"}


def test_names_that_differ_only_in_case_still_count_as_a_clash():
    # The results usually land on a Windows drive, where these are one name.
    names = output_basenames(["Scan.pdf", "scan.png"])
    assert names == {"Scan.pdf": "Scan_pdf", "scan.png": "scan_png"}


def test_the_folded_extension_is_lowercased():
    names = output_basenames(["scan.PDF", "scan.TIF"])
    assert names == {"scan.PDF": "scan_pdf", "scan.TIF": "scan_tif"}


def test_a_clash_left_over_after_the_rename_is_still_settled():
    """`x.pdf` and `x.png` take the name `x_pdf`, which `x_pdf.jpg` also wants."""
    names = output_basenames(["x.pdf", "x.png", "x_pdf.jpg"])
    assert names["x.pdf"] == "x_pdf"
    assert names["x.png"] == "x_png"
    assert names["x_pdf.jpg"] == "x_pdf (2)"
    assert len(set(names.values())) == 3


def test_every_document_in_a_folder_gets_its_own_name():
    relpaths = ["x.pdf", "x.png", "x.tif", "x_pdf.jpg", "y.pdf", "sub/x.pdf"]
    names = output_basenames(relpaths)
    assert len(names) == len(relpaths)
    for layout in LAYOUTS:
        written = [
            output_paths(Path("/out"), relpath, layout=layout, basename=names[relpath])[kind]
            for relpath in relpaths
            for kind in ("pdf", "txt", "json")
        ]
        assert len(set(written)) == len(written)


def test_the_layouts_still_agree_when_a_name_is_disambiguated():
    for layout in LAYOUTS:
        paths = output_paths(Path("/out"), "Ex/x.png", layout=layout, basename="x_png")
        assert paths["txt"].name == "x_png.txt"
        assert paths["pdf"].name == "x_png.pdf"
        assert paths["json"].name == "x_png.json"


def test_text_output_marks_each_page_and_its_source(tmp_path: Path):
    write_text(tmp_path / "out.txt", _result())
    written = (tmp_path / "out.txt").read_text()
    assert "----- page 1 (text-layer) -----" in written
    assert "----- page 2 (ocr) -----" in written
    assert "page two" in written


def test_json_output_carries_word_boxes_only_when_asked(tmp_path: Path):
    write_json(tmp_path / "with.json", _result(), settings={"dpi": 300}, include_words=True)
    write_json(tmp_path / "without.json", _result(), settings={"dpi": 300}, include_words=False)
    with_words = json.loads((tmp_path / "with.json").read_text())
    without = json.loads((tmp_path / "without.json").read_text())
    assert with_words["document"]["pages"][1]["word_boxes"][0]["text"] == "page"
    assert "word_boxes" not in without["document"]["pages"][1]
    assert with_words["settings"]["dpi"] == 300


def test_pages_csv_has_one_row_per_page(tmp_path: Path):
    write_pages_csv(tmp_path / "pages.csv", [_result()])
    rows = list(csv.DictReader((tmp_path / "pages.csv").open()))
    assert len(rows) == 2
    assert rows[0]["source"] == "text-layer" and rows[0]["needs_review"] == ""
    assert rows[1]["needs_review"] == "yes" and rows[1]["confidence"] == "61.0"


# ----------------------------------------------------------------- models


def test_file_summary_counts_what_the_ui_shows():
    summary = _result().summary()
    assert summary["ocr_pages"] == 1
    assert summary["text_layer_pages"] == 1
    assert summary["flagged_pages"] == [2]
    assert summary["mean_confidence"] == 61.0
    assert summary["chars"] == len("page one") + len("page two")


# --------------------------------------------------------------- laid-out text


def _row(y: float, *placed: tuple[float, str]) -> list[Word]:
    """Words on one line, each at a given x, sized like 10px characters."""
    return [Word(text, x, y, x + 10 * len(text), y + 18, 90.0) for x, text in placed]


def test_the_text_keeps_the_columns_the_page_had():
    """Tesseract returns lines flush left, which turns a two-column bill into a
    list of numbers with nothing to say which is which. The boxes know better."""
    page = PageResult(page_no=1, source="ocr", text="Consultation 240.00\nX-ray 615.50")
    page.words = _row(0, (0, "Consultation"), (400, "240.00")) + _row(
        40, (0, "X-ray"), (400, "615.50")
    )

    lines = [line for line in laid_out_text(page).splitlines() if line.strip()]
    assert lines[0].startswith("Consultation")
    assert lines[1].startswith("X-ray")
    # The amounts line up with each other, where they lined up on the page.
    assert lines[0].index("240.00") == lines[1].index("615.50")


def test_a_page_with_no_boxes_keeps_its_plain_text():
    """A page taken from a PDF's own text layer has no word boxes, and must
    still come out as itself."""
    page = PageResult(page_no=1, source="text-layer", text="already perfect text")
    assert laid_out_text(page) == "already perfect text"


def test_blank_lines_on_the_page_survive():
    page = PageResult(page_no=1, source="ocr", text="TITLE\nbody")
    page.words = _row(0, (0, "TITLE")) + _row(200, (0, "body"))
    assert "" in laid_out_text(page).splitlines()[1:-1]


def test_words_never_run_into_each_other():
    """Two words whose boxes nearly touch must still be two words."""
    page = PageResult(page_no=1, source="ocr", text="Case No")
    page.words = _row(0, (0, "Case"), (41, "No"))
    assert "CaseNo" not in laid_out_text(page)


def test_a_slightly_skewed_line_stays_one_line():
    """A scan is never perfectly straight; a line that drifts a few pixels down
    across the page is still a line."""
    page = PageResult(page_no=1, source="ocr", text="one two three")
    page.words = [
        Word("one", 0, 0, 30, 18, 90.0),
        Word("two", 100, 4, 130, 22, 90.0),
        Word("three", 200, 8, 250, 26, 90.0),
    ]
    assert len(laid_out_text(page).splitlines()) == 1


def test_the_plain_stream_of_lines_is_still_available(tmp_path: Path):
    write_text(tmp_path / "plain.txt", _result(), keep_layout=False)
    assert "page two" in (tmp_path / "plain.txt").read_text()


def test_a_document_key_uses_all_of_its_identity():
    """The key is a hash of the identity, not a slice of it. Slicing would
    throw away everything after the first few characters, and two documents
    that differ only in a name folded onto the end would collide."""
    sha = "a" * 64
    recipe = {"dpi": 300}
    assert document_key(f"{sha}|doc.pdf", recipe) != document_key(f"{sha}|copy.pdf", recipe)
    assert document_key(sha, recipe) == document_key(sha, recipe)
    assert document_key(sha, {"dpi": 300}) != document_key(sha, {"dpi": 200})


# ------------------------------------------------------- page orientation


def test_a_quarter_turn_is_exact_and_reversible():
    page = text_page()
    for degrees in (90, 180, 270):
        there = turn(page, degrees)
        back = turn(there, 360 - degrees)
        assert back.size == page.size
        assert list(back.getdata()) == list(page.getdata())


def test_turning_swaps_the_sides_for_a_quarter_but_not_for_a_half():
    page = text_page(size=(1700, 2200))
    assert turn(page, 90).size == (2200, 1700)
    assert turn(page, 270).size == (2200, 1700)
    assert turn(page, 180).size == (1700, 2200)
    assert turn(page, 0) is page
    assert turn(page, 360) is page


def test_a_page_can_only_be_turned_by_a_quarter():
    with pytest.raises(ValueError):
        turn(text_page(), 45)


def test_osd_output_is_read_as_a_clockwise_correction():
    osd = parse_osd(
        "Page number: 0\n"
        "Orientation in degrees: 90\n"
        "Rotate: 270\n"
        "Orientation confidence: 21.83\n"
        "Script: Latin\n"
        "Script confidence: 3.55\n"
    )
    assert osd is not None
    assert osd.rotate == 270 and osd.confidence == 21.83 and osd.script == "Latin"


def test_an_unreadable_orientation_report_is_no_answer_rather_than_a_wrong_one():
    assert parse_osd("") is None
    assert parse_osd("Too few characters. Skipping this page") is None
    # A rotation that is not a quarter turn is not something we can act on.
    assert parse_osd("Rotate: 45\nOrientation confidence: 9.0\n") is None
    assert parse_osd("Rotate: 90\nOrientation confidence: not-a-number\n") is None


class _Reading:
    """Stands in for what tesseract returns, for the accept/reject rules."""

    def __init__(self, text: str, confidence: float | None):
        self.text = text
        self.mean_confidence = confidence


def _settings(**kwargs):
    return Settings(input_dir=".", output_dir=".", **kwargs)


def test_a_page_is_only_turned_when_it_read_badly():
    settings = _settings(min_confidence=70)
    assert not _reads_badly(_Reading("clean page", 93.9), settings)
    assert _reads_badly(_Reading("vooododd0O0oIsA9", 38.75), settings)
    # Nothing at all is the other way a page fails.
    assert _reads_badly(_Reading("   ", None), settings)
    # A page with no confidence to judge and text on it is left alone.
    assert not _reads_badly(_Reading("from a text layer", None), settings)


def test_a_turn_is_kept_only_when_the_page_actually_reads_better():
    """OSD is a hint. The turn is judged by the recognition it produces.

    Measured on this machine, OSD reported 180 with confidence 0.3 on pages
    that were the right way up; acting on that alone would have inverted them.
    """
    original = _Reading("vooododd0O0oIsA9", 38.75)
    assert _improves_on(_Reading("100 Example Plaza", 93.9), original)
    # Noise between two recognitions of the same page is not an improvement.
    assert not _improves_on(_Reading("vooododd0O0oIsA0", 41.0), original)
    # Nor is turning a page that was already fine.
    assert not _improves_on(_Reading("anything", 60.0), _Reading("clean", 93.9))


def test_a_turn_that_finds_nothing_never_wins():
    assert not _improves_on(_Reading("", 99.0), _Reading("real text", 40.0))
    # ...but finding text where there was none does.
    assert _improves_on(_Reading("real text", 30.0), _Reading("   ", None))


def test_the_rotation_survives_the_trip_through_json():
    page = PageResult(page_no=1, source="ocr", text="x", rotation_applied=180)
    assert PageResult.from_dict(page.to_dict()).rotation_applied == 180
    # A page written before rotations were recorded reads back as unturned.
    stored = page.to_dict()
    del stored["rotation_applied"]
    assert PageResult.from_dict(stored).rotation_applied == 0


def test_turning_a_page_changes_the_recipe_so_a_folder_is_read_again():
    from ocrtool.ledger import recipe

    on = recipe(_settings(orient=True).to_dict())
    off = recipe(_settings(orient=False).to_dict())
    assert on != off


# ------------------------------------------------- order and the folder tree


def test_numbers_in_names_sort_as_numbers():
    """Exhibits are numbered, and text order lists them 1, 10, 11, 2, 20."""
    names = ["10. Ex.pdf", "2. Ex.pdf", "1. Ex.pdf", "20. Ex.pdf", "11. Ex.pdf", "3. Ex.pdf"]
    assert sorted(names, key=natural_key) == [
        "1. Ex.pdf", "2. Ex.pdf", "3. Ex.pdf", "10. Ex.pdf", "11. Ex.pdf", "20. Ex.pdf",
    ]


def test_natural_order_never_compares_a_number_against_a_word():
    """The usual way of writing this raises TypeError on `2` against `2a`."""
    assert sorted(["2a", "2", "10b", "10"], key=natural_key) == ["2", "2a", "10", "10b"]
    assert sorted(["a", "1", ""], key=natural_key) == ["", "1", "a"]


def test_case_does_not_decide_the_order():
    assert sorted(["beta.pdf", "Alpha.pdf"], key=natural_key) == ["Alpha.pdf", "beta.pdf"]


def test_documents_are_discovered_in_natural_order(tmp_path: Path):
    folder = tmp_path / "in"
    (folder / "Ex").mkdir(parents=True)
    for name in ("10. c.png", "2. b.png", "1. a.png"):
        text_page(["x"], size=(300, 200)).save(folder / "Ex" / name)
    found = find_documents(folder)
    assert [Path(f.relpath).name for f in found] == ["1. a.png", "2. b.png", "10. c.png"]


def _found(*pairs):
    from ocrtool.discover import Discovered

    return [Discovered(Path(rel), rel, 100, pages) for rel, pages in pairs]


def test_the_tree_nests_folders_and_counts_everything_beneath_them():
    tree = document_tree(_found(
        ("a.pdf", 1),
        ("Medical/1. Intake.pdf", 2),
        ("Medical/Imaging/13.pdf", 5),
    ))
    assert tree["files"] == 3 and tree["pages"] == 8
    assert [d["name"] for d in tree["documents"]] == ["a.pdf"]

    medical = tree["folders"][0]
    assert medical["name"] == "Medical"
    # Recursive: its own document plus the one in Imaging below it.
    assert medical["files"] == 2 and medical["pages"] == 7
    assert medical["folders"][0]["name"] == "Imaging"
    assert medical["folders"][0]["files"] == 1


def test_the_tree_is_in_natural_order_at_every_level():
    tree = document_tree(_found(
        ("10. b.pdf", 1), ("2. a.pdf", 1), ("Z/1.pdf", 1), ("A/1.pdf", 1),
    ))
    assert [d["name"] for d in tree["documents"]] == ["2. a.pdf", "10. b.pdf"]
    assert [f["name"] for f in tree["folders"]] == ["A", "Z"]


def test_an_empty_folder_makes_an_empty_tree():
    tree = document_tree([])
    assert tree["files"] == 0 and tree["documents"] == [] and tree["folders"] == []


def test_a_document_reports_its_progress_before_it_has_finished():
    """The count the browser shows when a tab is opened mid-document.

    `pages` is filled in one go when the whole document is collected, so until
    then it is empty. Reading progress from it alone reported zero, and leaving
    the run page and coming back restarted every document's count from nothing
    while the total at the top carried on correctly.
    """
    result = FileResult(relpath="scan.pdf", source_path="/scan.pdf", size_bytes=10, page_count=12)
    result.pages_read = 4
    assert result.summary()["pages_done"] == 4

    # Once collected, the pages themselves are the better answer.
    result.pages = [PageResult(page_no=n, source="ocr", text="x") for n in range(1, 13)]
    assert result.summary()["pages_done"] == 12
