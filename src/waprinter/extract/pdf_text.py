"""Turn a PDF into words, lines, and visual rows with geometry.

PDFs produced by the "Microsoft Print To PDF" driver carry a real text layer,
so this is a text extraction problem, not an OCR problem. `has_text_layer`
exists so the pipeline can detect the exception — an ERP that prints a raster
image — and route it to the OCR fallback instead of silently finding nothing.

Two views of the page matter downstream:

* **Lines** are what the PDF itself declares (a block/line pair). Reliable, but
  a two-column invoice puts "Bill To" and "Invoice No:" in *different* lines
  that happen to sit at the same height.
* **Rows** regroup lines by vertical position, which is what a human sees. This
  is what makes "Grand Total" find the figure printed to its right, and what
  keeps a "Mobile:" label attached to a number the ERP emitted as a separate
  block.

Row text joins its segments with ROW_GAP (three spaces) rather than one. That
gap is deliberate: the phone scanner only bridges digit groups across a single
space or hyphen, so a PIN code at the end of one column can never fuse with a
phone number at the start of the next.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

from .ocr import OcrSettings, OcrUnavailable, ocr_page, unavailable_reason

log = logging.getLogger(__name__)

ROW_GAP = "   "  # see module docstring — must not match phone.JOINABLE_SEP


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int
    block: int
    line: int
    from_ocr: bool = False

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class TextRun:
    """A sequence of words with their character offsets into `text`.

    The offsets let a regex match on `text` be mapped back to page geometry.
    """

    words: list[Word]
    page: int
    text: str = ""
    offsets: list[tuple[int, int]] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)

    def words_covering(self, start: int, end: int) -> list[Word]:
        """Words overlapping the character span [start, end)."""
        return [
            w
            for w, (ws, we) in zip(self.words, self.offsets)
            if ws < end and we > start
        ]

    def bbox_of(self, start: int, end: int) -> tuple[float, float, float, float] | None:
        covered = self.words_covering(start, end)
        if not covered:
            return None
        return (
            min(w.x0 for w in covered),
            min(w.y0 for w in covered),
            max(w.x1 for w in covered),
            max(w.y1 for w in covered),
        )


def _compose(words: list[Word], page: int, separators: list[str]) -> TextRun:
    """Build a TextRun, recording where each word lands in the joined text.

    `separators[i]` is the string placed between word i and word i+1.
    """
    parts: list[str] = []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for i, w in enumerate(words):
        offsets.append((cursor, cursor + len(w.text)))
        parts.append(w.text)
        cursor += len(w.text)
        if i < len(words) - 1:
            sep = separators[i]
            parts.append(sep)
            cursor += len(sep)
    return TextRun(words=words, page=page, text="".join(parts), offsets=offsets)


def _line_run(words: list[Word], page: int) -> TextRun:
    words = sorted(words, key=lambda w: w.x0)
    return _compose(words, page, [" "] * max(0, len(words) - 1))


def _row_run(lines: list[TextRun], page: int) -> TextRun:
    """Merge lines sharing a visual row, gapped so digits cannot fuse."""
    lines = sorted(lines, key=lambda ln: ln.x0)
    words: list[Word] = []
    separators: list[str] = []
    for i, line in enumerate(lines):
        for j, w in enumerate(line.words):
            words.append(w)
            if j < len(line.words) - 1:
                separators.append(" ")
        if i < len(lines) - 1:
            separators.append(ROW_GAP)
    return _compose(words, page, separators)


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list[TextRun]

    @property
    def words(self) -> list[Word]:
        return [w for ln in self.lines for w in ln.words]

    @property
    def rows(self) -> list[TextRun]:
        """Lines regrouped by vertical position, top to bottom."""
        if not self.lines:
            return []
        ordered = sorted(self.lines, key=lambda ln: (ln.y0, ln.x0))
        groups: list[list[TextRun]] = [[ordered[0]]]

        for line in ordered[1:]:
            current = groups[-1]
            ref = current[0]
            # Same row when the vertical midpoints are close relative to the
            # taller of the two lines. Tolerant of superscripts and mixed sizes.
            tolerance = max(2.5, 0.45 * max(ref.y1 - ref.y0, line.y1 - line.y0))
            ref_mid = (ref.y0 + ref.y1) / 2
            line_mid = (line.y0 + line.y1) / 2
            if abs(line_mid - ref_mid) <= tolerance:
                current.append(line)
            else:
                groups.append([line])

        return [_row_run(g, self.number) for g in groups]


@dataclass
class Document:
    pages: list[Page]
    # True when the PDF carried real text. False means every page was an image;
    # `ocr_pages` then says whether OCR managed to read them anyway.
    has_text_layer: bool
    ocr_pages: set[int] = field(default_factory=set)
    # Why OCR did not run, when it was needed but unavailable.
    ocr_error: str | None = None

    @property
    def used_ocr(self) -> bool:
        return bool(self.ocr_pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def lines(self) -> list[TextRun]:
        return [ln for p in self.pages for ln in p.lines]

    @property
    def rows(self) -> list[TextRun]:
        return [r for p in self.pages for r in p.rows]

    def page(self, number: int) -> Page | None:
        for p in self.pages:
            if p.number == number:
                return p
        return None


# A page with fewer than this many extractable words is treated as an image
# scan rather than a text document, and is a candidate for OCR.
MIN_WORDS_FOR_TEXT_LAYER = 5


def _words_from(
    raw: list, page_number: int, from_ocr: bool
) -> dict[tuple[int, int], list[Word]]:
    grouped: dict[tuple[int, int], list[Word]] = {}
    for x0, y0, x1, y1, text, block, line, _word_no in raw:
        if not text.strip():
            continue
        w = Word(
            text=text,
            x0=x0, y0=y0, x1=x1, y1=y1,
            page=page_number, block=int(block), line=int(line),
            from_ocr=from_ocr,
        )
        grouped.setdefault((w.block, w.line), []).append(w)
    return grouped


def read(
    pdf_path: Path,
    ocr: OcrSettings | None = None,
    ocr_dpi: int | None = None,
) -> Document:
    """Extract every word with its bounding box, grouped into lines.

    Pages with no usable text layer are passed through OCR when `ocr` is given.
    The decision is per page, so a multi-page invoice with one scanned page
    still reads the rest natively.
    """
    pages: list[Page] = []
    native_words = 0
    ocr_pages: set[int] = set()
    ocr_error: str | None = None

    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            raw = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
            usable = [w for w in raw if w[4].strip()]
            native_words += len(usable)
            from_ocr = False

            if len(usable) < MIN_WORDS_FOR_TEXT_LAYER and ocr and ocr.enabled:
                try:
                    textpage = ocr_page(page, ocr, dpi=ocr_dpi)
                    raw = page.get_text("words", textpage=textpage)
                    from_ocr = True
                    ocr_pages.add(index)
                    log.info("page %s of %s read by OCR", index, pdf_path.name)
                except OcrUnavailable as exc:
                    # Recorded once, not raised: the job is held with this as
                    # the reason rather than the service failing.
                    ocr_error = ocr_error or str(exc)
                    log.warning("OCR unavailable for %s: %s", pdf_path.name, exc)
                except Exception as exc:
                    ocr_error = ocr_error or f"OCR failed: {exc}"
                    log.exception("OCR failed for page %s of %s", index, pdf_path)

            grouped = _words_from(raw, index, from_ocr)
            lines = [_line_run(ws, index) for _key, ws in sorted(grouped.items())]
            lines.sort(key=lambda ln: (ln.y0, ln.x0))

            pages.append(
                Page(
                    number=index,
                    width=page.rect.width,
                    height=page.rect.height,
                    lines=lines,
                )
            )

    if ocr_error is None and not ocr_pages and native_words < MIN_WORDS_FOR_TEXT_LAYER:
        # Nothing to read and OCR was never offered.
        ocr_error = unavailable_reason(ocr) if ocr else "OCR is not configured."

    return Document(
        pages=pages,
        has_text_layer=native_words >= MIN_WORDS_FOR_TEXT_LAYER,
        ocr_pages=ocr_pages,
        ocr_error=ocr_error,
    )
