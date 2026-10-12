"""What actually goes out beside the message: the PDF, or an image of it.

Why an image at all. A PDF arrives in WhatsApp as a file — a grey tile with a
filename that the member has to tap, and on a cheap Android phone with no PDF
viewer installed that tap goes nowhere. An image is shown in the chat itself,
so the receipt is read without opening anything. That is worth having as a
choice, and it is made per document type in Setup → Messages: a counter can
send receipts as images while removal notices, which people keep and forward,
stay PDFs.

What it costs, and the app cannot paper over it. A template header carries a
document or an image, never both, and which one is fixed when Meta approves
the template. So an image send needs an approved IMAGE-header template on the
account; `MessageTemplate.carries` is what asks whether the chosen one can
take what this install wants to send, and setup says so before a print rather
than the queue saying it after. The image header also carries no filename, so
"Receipt-CR1747-26" is not shown beside an image the way it is beside a PDF —
on an image send the receipt number has to be in the message body.

How the conversion works. PyMuPDF renders the pages, which it is in the
install for already (OCR uses the same renderer), so nothing new is shipped to
a counter PC. A print with more than one page is joined into one tall JPEG
instead of sending the first page and dropping the rest: a two-page removal
notice whose second page carries the signature is not a notice without it.

Meta caps image media at 5 MB, so rather than guessing one dpi that always
fits, the page is rendered at the best of a few settings and re-encoded down
until it does. An ordinary one-page receipt goes out sharp; only a long print
loses detail, and one that cannot be made to fit at all is held with the
reason instead of failing at Meta.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf

log = logging.getLogger(__name__)

# The two shapes a document can leave in. Stored in settings per document kind.
PDF = "pdf"
IMAGE = "image"
FORMATS = (PDF, IMAGE)

MEDIA_TYPE = {PDF: "application/pdf", IMAGE: "image/jpeg"}
SUFFIX = {PDF: ".pdf", IMAGE: ".jpg"}

# Meta's ceiling for image media. A document may be 100 MB; an image may not,
# and an upload over the limit is refused with an error that does not say so.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Tried best first, and the first combination that fits the limit wins. 150 dpi
# is enough to read a receipt's small print on a phone; 72 is the floor, below
# which an amount in figures stops being legible and there is no point sending
# it at all.
RENDER_DPI = (150, 120, 96, 72)
JPEG_QUALITY = (85, 70, 55)

# A baseline JPEG cannot describe a side longer than this, so a print joined
# into a strip taller than it is refused rather than written as a broken file.
MAX_JPEG_SIDE = 65500


class ConversionError(RuntimeError):
    """This PDF cannot be turned into an image Meta would accept."""


def format_for(value: str | None) -> str:
    """Normalise a stored attachment format.

    Anything unrecognised means PDF: that is what every install did before
    this existed, and a typo in settings.json must not quietly stop a receipt
    going out.
    """
    cleaned = (value or "").strip().lower()
    if cleaned in FORMATS:
        return cleaned
    if cleaned:
        log.warning("unknown attachment format %r; sending as a PDF", value)
    return PDF


def to_jpeg(
    pdf_path: Path,
    out_path: Path,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> Path:
    """Render `pdf_path` as one JPEG at `out_path`, and return where it went.

    Every page, joined top to bottom. Raises ConversionError — never a
    PyMuPDF error — so a caller has one thing to catch and a sentence it can
    show an operator.
    """
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            if doc.page_count == 0:
                raise ConversionError(f"{pdf_path.name} has no pages to render.")
            with _single_page(doc) as source:
                data = _encode(source.load_page(0), max_bytes)
    except ConversionError:
        raise
    except Exception as exc:  # pymupdf raises library-specific errors
        raise ConversionError(f"Could not render {pdf_path.name}: {exc}") from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    log.info(
        "%s rendered as %s (%d KB)", pdf_path.name, out_path.name, len(data) // 1024
    )
    return out_path


@contextmanager
def prepared(pdf_path: Path, attachment: str, filename: str):
    """The file that goes out and its media type, for the length of the upload.

    The captured PDF unchanged for a PDF send. For an image send, a JPEG in a
    temporary directory that is removed on the way out: the image is wanted
    for the seconds the upload takes and nowhere else. What the office keeps
    is the PDF, because that is what was printed.
    """
    if format_for(attachment) != IMAGE:
        yield pdf_path, MEDIA_TYPE[PDF]
        return
    with TemporaryDirectory(prefix="waprinter-image-") as scratch:
        target = Path(scratch) / (Path(filename).stem or "document")
        yield to_jpeg(pdf_path, target.with_suffix(SUFFIX[IMAGE])), MEDIA_TYPE[IMAGE]


@contextmanager
def _single_page(doc: "pymupdf.Document"):
    """`doc` itself when it is one page, else one page with all of them on it.

    Pages are laid out top to bottom at their own size, against the left edge.
    Scaling a narrow page up to the width of the widest would show one print
    at two different scales, which on paperwork that carries figures reads as
    two different documents.
    """
    if doc.page_count == 1:
        yield doc
        return

    width = max(page.rect.width for page in doc)
    height = sum(page.rect.height for page in doc)
    joined = pymupdf.open()
    try:
        canvas = joined.new_page(width=width, height=height)
        top = 0.0
        for page in doc:
            canvas.show_pdf_page(
                pymupdf.Rect(0, top, page.rect.width, top + page.rect.height),
                doc,
                page.number,
            )
            top += page.rect.height
        yield joined
    finally:
        joined.close()


def _encode(page: "pymupdf.Page", max_bytes: int) -> bytes:
    """The best JPEG of this page that fits `max_bytes`.

    Resolution is given up before quality: a receipt rendered small and clean
    is readable, and one rendered large with JPEG artefacts over the digits is
    exactly what must not happen to an amount.
    """
    longest = max(page.rect.width, page.rect.height)
    smallest: bytes | None = None

    for dpi in RENDER_DPI:
        if longest * dpi / 72 > MAX_JPEG_SIDE:
            continue
        pixmap = page.get_pixmap(dpi=dpi)
        for quality in JPEG_QUALITY:
            data = pixmap.tobytes("jpeg", jpg_quality=quality)
            if len(data) <= max_bytes:
                return data
            if smallest is None or len(data) < len(smallest):
                smallest = data

    if smallest is None:
        raise ConversionError(
            "This print has too many pages to send as one image: joined top to "
            "bottom it is taller than a JPEG can hold. Send it as a PDF."
        )
    raise ConversionError(
        f"The image came to {_size(len(smallest))}, over WhatsApp's "
        f"{_size(max_bytes)} limit for images, even at the lowest quality this "
        f"would print at. Send this document as a PDF instead."
    )


def _size(count: int) -> str:
    """Bytes as an operator would say them."""
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MB"
    return f"{count // 1024} KB"
