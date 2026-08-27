"""The shapes that move through the pipeline.

Plain dataclasses with explicit `to_dict()` methods: these objects are written
to JSON on disk and streamed to a browser, and both consumers deserve a stable,
readable shape rather than whatever a serialiser guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PageSource = Literal["text-layer", "ocr", "skipped", "failed"]
FileStatus = Literal["pending", "running", "done", "skipped", "failed", "cancelled"]


@dataclass
class Word:
    """One OCR'd word and where it sits on the page image, in pixels."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "box": [round(self.x0, 1), round(self.y0, 1), round(self.x1, 1), round(self.y1, 1)],
            "confidence": round(self.confidence, 1),
        }


@dataclass
class PageResult:
    """What we ended up with for one page, and how we got it."""

    page_no: int
    source: PageSource
    text: str = ""
    confidence: float | None = None
    words: list[Word] = field(default_factory=list)
    needs_review: bool = False
    review_reason: str | None = None
    error: str | None = None
    duration_ms: int = 0
    width_px: int = 0
    height_px: int = 0
    skew_corrected: float = 0.0
    preview: str | None = None
    # The words tesseract was least sure of, kept as plain strings so they
    # survive after the word boxes are dropped from memory. The viewer marks
    # them in the text: "which words should I not trust" is the question a page
    # of OCR actually raises, and a page-level average cannot answer it.
    low_confidence_words: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self, *, include_words: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "page": self.page_no,
            "source": self.source,
            "text": self.text,
            "chars": self.char_count,
            "words": self.word_count,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
            "duration_ms": self.duration_ms,
            "size_px": [self.width_px, self.height_px],
            "skew_corrected": self.skew_corrected,
            "preview": self.preview,
            "low_confidence_words": self.low_confidence_words,
        }
        if self.error:
            out["error"] = self.error
        if include_words and self.words:
            out["word_boxes"] = [w.to_dict() for w in self.words]
        return out


@dataclass
class FileResult:
    """One input document: its pages, its outputs, and what went wrong if anything."""

    relpath: str
    source_path: str
    size_bytes: int
    page_count: int = 0
    status: FileStatus = "pending"
    pages: list[PageResult] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    outputs: dict[str, str] = field(default_factory=dict)
    modified: float = -1.0
    # Set when this document was read by an earlier run into the same folder
    # and did not need reading again.
    skipped_from_run: str | None = None

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.source == "ocr")

    @property
    def text_layer_pages(self) -> int:
        return sum(1 for p in self.pages if p.source == "text-layer")

    @property
    def flagged_pages(self) -> list[int]:
        return [p.page_no for p in self.pages if p.needs_review]

    @property
    def mean_confidence(self) -> float | None:
        scored = [p.confidence for p in self.pages if p.confidence is not None]
        return round(sum(scored) / len(scored), 1) if scored else None

    def summary(self) -> dict[str, Any]:
        """The row the file list shows. Deliberately free of page text."""
        return {
            "relpath": self.relpath,
            "source_path": self.source_path,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "page_count": self.page_count,
            "pages_done": len(self.pages),
            "ocr_pages": self.ocr_pages,
            "text_layer_pages": self.text_layer_pages,
            "flagged_pages": self.flagged_pages,
            "mean_confidence": self.mean_confidence,
            "chars": sum(p.char_count for p in self.pages),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "outputs": self.outputs,
            "skipped_from_run": self.skipped_from_run,
        }

    def to_dict(self, *, include_words: bool = True) -> dict[str, Any]:
        out = self.summary()
        out["pages"] = [p.to_dict(include_words=include_words) for p in self.pages]
        return out
