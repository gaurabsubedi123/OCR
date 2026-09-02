"""Clean a page image up before OCR.

This is the cheapest accuracy win available. Faxes and photocopies arrive
skewed, speckled and low-contrast, and tesseract is measurably worse on all
three. Deskewing alone routinely moves a page from unusable to readable.

The correction is deliberately conservative: it fixes scanner skew, and it
refuses to guess at anything larger, because a page that is 90 degrees out is
a rotated page, not a skewed one, and the projection profile cannot tell the
difference on its own.
Quarter turns are a separate question with a separate answer — see `turn` here
and `_read_turned` in pipeline.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

# How far off square a page can be and still be called skew rather than a
# rotation. Measured on real pages: the projection profile recovers a known
# angle exactly at every cap tried, and on ten straight pages from a real claim
# file it returned 0.0 at caps of 6, 15, 25 and 45 alike — so widening it does
# not invent skew on pages that have none. 15 costs about 100ms a page over 6,
# and 45 would cost about half a second, which is not worth paying on every
# page for an angle a scanner does not produce.
MAX_SKEW_DEGREES = 15.0
# Where the search goes for a page that saturated the cap above. A page laid at
# 20 degrees used to correct by 15, read 308 characters of 2,205, and report 85%
# confidence with nothing flagged — a silent loss of seven eighths of the page,
# which is the same failure an upside-down page causes and just as quiet. Only
# a page whose first estimate lands *on* the cap pays for this, so it costs
# nothing on a folder of ordinary scans.
#
# 45 is the end of the line rather than an arbitrary stop: past it a quarter
# turn is the shorter way round, and pipeline.py tries those.
WIDE_SKEW_DEGREES = 45.0
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


# A quarter turn is exact: the pixels are moved, never resampled, so turning a
# page costs nothing in quality and turning it back returns the original.
_TURNS = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def turn(image: Image.Image, degrees: int) -> Image.Image:
    """The page turned `degrees` clockwise, for degrees a multiple of 90.

    Clockwise because that is the direction tesseract's orientation pass counts
    in, and having the two disagree is the kind of sign error that produces a
    page upside down instead of right way up.

    PIL's own ROTATE_ constants count anticlockwise, hence the mapping: a
    clockwise quarter turn is ROTATE_270. These are transposes rather than
    rotations — they move pixels between rows and columns without interpolating
    any of them, so nothing is softened.
    """
    degrees %= 360
    if degrees == 0:
        return image
    if degrees not in _TURNS:
        raise ValueError(f"a page can only be turned by a quarter: {degrees}")
    return image.transpose(_TURNS[degrees])


def apply_geometry(image: Image.Image, *, skew: float, size: tuple[int, int]) -> Image.Image:
    """Reshape an untouched page image the way `preprocess` reshaped its copy.

    Only the geometry is repeated — the rotation and the scaling — never the
    greyscale or the filtering. That is the point: the result lines up pixel for
    pixel with the image OCR read, while still looking like the original page.
    """
    result = image
    if abs(skew) >= MIN_CORRECTION:
        result = result.rotate(skew, resample=Image.BICUBIC, expand=True, fillcolor="white")
    if result.size != size:
        result = result.resize(size, Image.LANCZOS)
    return result


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

    def search(cap: float) -> float:
        coarse = np.arange(-cap, cap + COARSE_STEP, COARSE_STEP)
        best = max(coarse, key=score)
        fine = np.arange(best - COARSE_STEP, best + COARSE_STEP + FINE_STEP, FINE_STEP)
        return float(max(fine, key=score))

    best = search(MAX_SKEW_DEGREES)
    # Landing on the cap means the true angle is at it or past it, and the
    # search was cut off rather than finished. That is the one case worth
    # paying for a wider look.
    if abs(best) >= MAX_SKEW_DEGREES - FINE_STEP:
        best = search(WIDE_SKEW_DEGREES)
    # The fine pass searches a degree either side of the coarse winner, so it
    # can land just outside the cap. Clamping is the only sane answer: this
    # used to return 0.0 there, which says "the page is straight" — the one
    # thing already known to be false. A page laid at 6 degrees came back
    # uncorrected and read 559 characters of 2,205, at 85% confidence, so
    # nothing flagged it either. Silent, and a quarter of the page.
    return float(np.clip(best, -WIDE_SKEW_DEGREES, WIDE_SKEW_DEGREES))
