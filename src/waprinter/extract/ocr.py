"""OCR for invoices that were printed as an image rather than as text.

Most ERPs print real text, which is why the ordinary path needs no OCR at all.
The exception is software that renders its output to a bitmap first — those pages
arrive with no text layer, and without this module they can only be held.

OCR runs through PyMuPDF's Tesseract integration rather than a separate
pytesseract call, for one specific reason: it returns words in *PDF coordinates*.
That means everything the extractor already does with geometry — the Bill To
block, footer and letterhead penalties, visual row grouping — keeps working
unchanged on a scanned page. Rendering to a PNG and OCRing that would put every
box in pixel space and require the whole scoring layer to learn about scaling.

OCR is best-effort. If Tesseract is missing, `available()` says so and the page
is held with an explanation instead of the service failing.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

# Where Tesseract's language files usually live, checked in order. PyMuPDF's own
# get_tessdata() is tried first and handles TESSDATA_PREFIX.
COMMON_TESSDATA = [
    r"C:\Program Files\Tesseract-OCR\tessdata",
    r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
    "/opt/homebrew/share/tessdata",
    "/usr/local/share/tessdata",
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tessdata",
]


@dataclass(frozen=True)
class OcrSettings:
    enabled: bool = True
    # 300 dpi is the usual floor for reliable digit recognition. The verify pass
    # deliberately uses a different dpi so the two reads are not the same
    # computation twice — see extract/phone.py.
    dpi: int = 300
    verify_dpi: int = 400
    language: str = "eng"
    tessdata: str | None = None


class OcrUnavailable(RuntimeError):
    """Tesseract is not installed, or its language data cannot be found."""


def _bundled_tessdata() -> Path | None:
    """Tesseract shipped alongside the frozen executable by the installer.

    Preferred over anything else on the machine: the installer also sets
    TESSDATA_PREFIX, but a service can start before that propagates, and a
    separately installed Tesseract may be a different version.
    """
    if not getattr(sys, "frozen", False):
        return None
    candidate = Path(sys.executable).parent / "tesseract" / "tessdata"
    return candidate if candidate.is_dir() else None


def find_tessdata(explicit: str | None = None) -> str | None:
    """Locate Tesseract's tessdata directory, or None if it is not installed."""
    if explicit:
        return explicit if Path(explicit).is_dir() else None

    bundled = _bundled_tessdata()
    if bundled:
        return str(bundled)

    try:
        # Honours TESSDATA_PREFIX and PyMuPDF's own discovery.
        discovered = pymupdf.get_tessdata()
    except Exception:
        discovered = False
    if discovered and Path(str(discovered)).is_dir():
        return str(discovered)

    for candidate in COMMON_TESSDATA:
        if Path(candidate).is_dir():
            return candidate
    return None


def available(settings: OcrSettings) -> bool:
    return settings.enabled and find_tessdata(settings.tessdata) is not None


def unavailable_reason(settings: OcrSettings) -> str:
    """A sentence an operator can act on, for the held-job queue."""
    if not settings.enabled:
        return "OCR is switched off in settings."
    if sys.platform == "win32":
        return (
            "OCR needs Tesseract, which is not installed. Reinstall WhatsApp "
            "Printer with the OCR component selected, or install Tesseract-OCR "
            "and restart the service."
        )
    return (
        "OCR needs Tesseract, which is not installed or whose language data "
        "could not be found."
    )


def ocr_page(
    page: pymupdf.Page,
    settings: OcrSettings,
    dpi: int | None = None,
) -> pymupdf.TextPage:
    """Run OCR over a whole page and return a TextPage in PDF coordinates."""
    tessdata = find_tessdata(settings.tessdata)
    if tessdata is None:
        raise OcrUnavailable(unavailable_reason(settings))

    try:
        return page.get_textpage_ocr(
            flags=0,
            language=settings.language,
            dpi=dpi or settings.dpi,
            full=True,  # the whole page is an image, so OCR all of it
            tessdata=tessdata,
        )
    except Exception as exc:  # pymupdf raises library-specific errors
        raise OcrUnavailable(f"OCR failed: {exc}") from exc
