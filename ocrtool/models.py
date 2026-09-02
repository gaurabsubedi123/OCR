"""The shapes that move through the pipeline.

Plain dataclasses with explicit `to_dict()` methods: these objects are written
to JSON on disk and streamed to a browser, and both consumers deserve a stable,
readable shape rather than whatever a serialiser guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PageSource = Literal["text-layer", "ocr", "skipped", "failed"]
FileStatus = Literal[
    "pending", "running", "done", "skipped", "copied", "failed", "cancelled"
]


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Word":
        box = list(data.get("box") or [0, 0, 0, 0])
        while len(box) < 4:
            box.append(0)
        return cls(
            text=str(data.get("text", "")),
            x0=float(box[0]),
            y0=float(box[1]),
            x1=float(box[2]),
            y1=float(box[3]),
            confidence=float(data.get("confidence", 0.0)),
        )


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
    # Degrees clockwise this page was turned before it was read, 0 for almost
    # every page. Non-zero means the scan was sideways or upside down and was
    # stood up first — worth surfacing, because it says something about the
    # source document that the text alone does not.
    rotation_applied: int = 0
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
            "rotation_applied": self.rotation_applied,
            "preview": self.preview,
            "low_confidence_words": self.low_confidence_words,
        }
        if self.error:
            out["error"] = self.error
        if include_words and self.words:
            out["word_boxes"] = [w.to_dict() for w in self.words]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageResult":
        """Rebuild a page from what `to_dict` wrote.

        The pair has to round-trip because a page is written to disk the moment
        it is read, and a later run picks it up from there rather than reading
        the page again. `chars` and `words` are not restored: both are counts
        derived from the text, and a stored count that disagrees with the text
        it came from is a lie waiting to be believed.
        """
        size = list(data.get("size_px") or [0, 0])
        while len(size) < 2:
            size.append(0)
        confidence = data.get("confidence")
        return cls(
            page_no=int(data.get("page", 0)),
            source=data.get("source", "ocr"),
            text=str(data.get("text", "")),
            confidence=None if confidence is None else float(confidence),
            words=[Word.from_dict(w) for w in data.get("word_boxes", [])],
            needs_review=bool(data.get("needs_review", False)),
            review_reason=data.get("review_reason"),
            error=data.get("error"),
            duration_ms=int(data.get("duration_ms", 0)),
            width_px=int(size[0]),
            height_px=int(size[1]),
            skew_corrected=float(data.get("skew_corrected", 0.0)),
            rotation_applied=int(data.get("rotation_applied", 0)),
            preview=data.get("preview"),
            low_confidence_words=list(data.get("low_confidence_words", [])),
        )


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
    # Set when this document is byte-for-byte the same as another one, whose
    # pages were read instead of reading these. Holds that document's relpath.
    duplicate_of: str | None = None
    # How many of this document's pages came from a stopped run rather than
    # being read again.
    resumed_pages: int = 0

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
            "duplicate_of": self.duplicate_of,
            "resumed_pages": self.resumed_pages,
        }

    def to_dict(self, *, include_words: bool = True) -> dict[str, Any]:
        out = self.summary()
        out["pages"] = [p.to_dict(include_words=include_words) for p in self.pages]
        return out
