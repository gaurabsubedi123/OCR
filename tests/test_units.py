"""Everything that can be checked without running tesseract."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from ocrtool.config import MAX_RENDER_PIXELS, Settings
from ocrtool.discover import find_documents
from ocrtool.models import FileResult, PageResult, Word
from ocrtool.outputs import output_paths, write_json, write_pages_csv, write_text
from ocrtool.pipeline import _flag
from ocrtool.preprocess import _estimate_skew, preprocess
from ocrtool.render import _fit, render_page
from ocrtool.tesseract import parse_tsv
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
            words=[Word("page", 1, 2, 3, 4, 61.0)],
        ),
    ]
    return result


def test_outputs_are_sorted_by_type_and_keep_the_input_structure():
    paths = output_paths(Path("/out"), "Ex 13/scan.pdf")
    assert paths["pdf"] == Path("/out/pdf/Ex 13/scan.pdf")
    assert paths["txt"] == Path("/out/txt/Ex 13/scan.txt")
    assert paths["json"] == Path("/out/json/Ex 13/scan.json")


def test_outputs_can_be_kept_together_instead():
    paths = output_paths(Path("/out"), "Ex 13/scan.pdf", grouped=False)
    assert paths["pdf"] == Path("/out/Ex 13/scan.pdf")
    assert paths["txt"] == Path("/out/Ex 13/scan.txt")
    assert paths["json"] == Path("/out/Ex 13/scan.json")


def test_an_extension_is_replaced_not_appended():
    # `13.pdf` must become `13.txt`, never `13.pdf.txt`.
    assert output_paths(Path("/out"), "13.pdf")["txt"].name == "13.txt"
    assert output_paths(Path("/out"), "photo.jpeg")["pdf"].name == "photo.pdf"


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
