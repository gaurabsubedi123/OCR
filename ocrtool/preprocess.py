"""Clean a page image up before OCR.

This is the cheapest accuracy win available. Faxes and photocopies arrive
skewed, speckled and low-contrast, and tesseract is measurably worse on all
three. Deskewing alone routinely moves a page from unusable to readable.

The correction is deliberately conservative: it fixes scanner skew, and it
refuses to guess at anything larger, because a page that is 90 degrees out is
a rotated page, not a skewed one, and this method cannot tell the difference.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

MAX_SKEW_DEGREES = 6.0
COARSE_STEP = 1.0
FINE_STEP = 0.2
# Below a quarter degree the rotation costs a resample and buys nothing.
MIN_CORRECTION = 0.25
# Tesseract is trained around 300 dpi; upscaling a small scan measurably helps.
MIN_HEIGHT_PX = 1000


@dataclass
class Preprocessed:
    image: Image.Image
    skew_corrected: float
    upscaled: float


def preprocess(image: Image.Image, *, deskew: bool = True, denoise: bool = True) -> Preprocessed:
    gray = ImageOps.grayscale(image)

    if denoise:
        # A 3x3 median kills scanner speckle without softening letter strokes
        # the way a blur would.
        gray = gray.filter(ImageFilter.MedianFilter(size=3))

    # Clip the light end only. Clipping both ends is the obvious thing to write
    # and it is wrong: on a page whose ink covers less than the cutoff — a title
    # page, a mostly-empty form, a photograph — the dark cut point lands inside
    # the background instead of inside the text, and autocontrast then drags the
    # off-white background toward black. Measured on a sparse page here: dark
    # pixels went from 95k to 282k and tesseract returned *nothing at all*,
    # where the untouched image read cleanly at 95% confidence. Clipping only
    # the light end still flattens a grey scanner background to white, which is
    # where the accuracy actually comes from.
    gray = ImageOps.autocontrast(gray, cutoff=(0, 1))

    angle = _estimate_skew(gray) if deskew else 0.0
    if abs(angle) >= MIN_CORRECTION:
        gray = gray.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
    else:
        angle = 0.0

    scale = 1.0
    if gray.height < MIN_HEIGHT_PX:
        scale = MIN_HEIGHT_PX / gray.height
        gray = gray.resize(
            (round(gray.width * scale), round(gray.height * scale)), Image.LANCZOS
        )

    return Preprocessed(image=gray, skew_corrected=round(angle, 2), upscaled=round(scale, 2))


def _estimate_skew(gray: Image.Image) -> float:
    """Projection-profile skew estimate.

    Rotate the page a little, sum the dark pixels in each row, and measure how
    spiky that profile is. Text lines line up with the rows only when the page
    is straight, so the spikiest angle is the correct one. Coarse pass at one
    degree, then a fine pass around the winner.
    """
    small = gray.copy()
    # The estimate does not need detail, and a small image makes the search cheap.
    if small.width > 800:
        small = small.resize((800, round(small.height * 800 / small.width)), Image.BILINEAR)

    arr = np.asarray(small, dtype=np.float32)
    ink = 255.0 - arr  # dark pixels carry the signal
    if ink.max() <= 0:
        return 0.0

    def score(angle: float) -> float:
        if angle == 0.0:
            rotated = ink
        else:
            img = Image.fromarray(ink.astype(np.uint8))
            img = img.rotate(angle, resample=Image.BILINEAR, expand=False, fillcolor=0)
            rotated = np.asarray(img, dtype=np.float32)
        profile = rotated.sum(axis=1)
        # Variance of the row sums: high when lines are horizontal, low when
        # the ink is smeared evenly across every row.
        return float(np.var(profile))

    coarse = np.arange(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES + COARSE_STEP, COARSE_STEP)
    best = max(coarse, key=score)
    fine = np.arange(best - COARSE_STEP, best + COARSE_STEP + FINE_STEP, FINE_STEP)
    best = max(fine, key=score)
    return float(best) if abs(best) <= MAX_SKEW_DEGREES else 0.0
