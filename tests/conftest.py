"""Shared fixtures.

The test documents are generated rather than committed: a repo for OCR work
must never be the place someone's scanned records end up by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from ocrtool.tesseract import tesseract_path

needs_tesseract = pytest.mark.skipif(
    tesseract_path() is None, reason="tesseract is not installed on this machine"
)

LINES = [
    "IN THE DISTRICT COURT OF CLARK COUNTY",
    "Case No. A-00-123456-C",
    "INCIDENT REPORT",
    "Date of incident: 2/25/2021",
    "Reporting officer: J. MARTINEZ",
]


def _font(size: int = 34) -> ImageFont.ImageFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def text_page(lines: list[str] | None = None, *, size=(1700, 2200), skew: float = 0.0) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    y = 200
    for line in lines or LINES:
        draw.text((150, y), line, fill="black", font=_font())
        y += 64
    if skew:
        image = image.rotate(skew, resample=Image.BICUBIC, expand=False, fillcolor="white")
    return image


@pytest.fixture
def sample_folder(tmp_path: Path) -> Path:
    """A folder shaped like a real one: a multi-page PDF, an image in a
    subfolder, and files the tool must ignore."""
    folder = tmp_path / "input"
    (folder / "sub").mkdir(parents=True)

    first = text_page()
    second = text_page(["EXHIBIT 3 - MEDICAL BILL", "Amount billed: $4,238.75"])
    first.save(folder / "scan.pdf", save_all=True, append_images=[second], resolution=150)
    text_page().save(folder / "sub" / "page.png")

    (folder / "notes.txt").write_text("not a document this tool reads")
    (folder / ".hidden.pdf").write_bytes(b"%PDF-1.4 not really")
    return folder
