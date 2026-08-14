"""Pull the fields a message template needs off the printed page.

Everything here is best-effort and nullable. A missing customer name makes the
message read worse; a wrong phone number sends someone else their paperwork, so
that logic lives in phone.py under much stricter rules.

What counts as a label is per-client configuration — see profile.py. Label/value
pairs are read from *rows* rather than lines, because software routinely emits
"Grand Total" and "18,450.00" as separate blocks at the same height.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import ExtractedFields
from . import pdf_text
from .ocr import OcrSettings
from .phone import (
    CUSTOMER_BLOCK_WIDTH,
    apply_ocr_verification,
    default_profile,
    find_candidates,
    scan_numbers,
)
from .profile import DocumentProfile

MONEY = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)


def _document_number(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    """The document's identifying number.

    Explicit patterns are tried before the generic label list, because a client
    configures a pattern precisely when the labels get it wrong. "RCPT-99-2026"
    is the case in point: the generic reader sees "RCPT" as a label and returns
    "99-2026", losing the prefix the client cares about.
    """
    for pattern in profile.document_number_pattern_res:
        for row in doc.rows:
            m = pattern.search(row.text)
            if m:
                return m.group(1).strip(" .-/")

    for row in doc.rows:
        m = profile.document_number_re.search(row.text)
        if m:
            return m.group(1).strip(" .-/")
    return None


def _document_date(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    """A labelled date if there is one, else the most date-shaped thing found.

    Patterns are the outer loop, not rows: the most distinctive shape wins
    wherever it appears, rather than whatever turns up earliest on the page.
    A chit receipt prints "13-Aug-26" halfway down and a street address
    ("H.No. 2-7-384") near the top, and row-major order picked the address.
    """
    for pattern in profile.date_res:
        for row in doc.rows:
            m = pattern.search(row.text)
            if m:
                return m.group(1)

    # No "Date:" label anywhere on the page.
    for pattern in profile.bare_date_res:
        for row in doc.rows:
            m = pattern.search(row.text)
            if m:
                return m.group(1)
    return None


def _total_amount(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    best_rank, best_value = 0, None
    for row in doc.rows:
        for pattern, rank in profile.amount_label_res:
            if not pattern.search(row.text):
                continue
            if rank >= best_rank:
                amounts = MONEY.findall(row.text)
                if amounts:
                    # The figure furthest right on a totals row is the total.
                    best_rank, best_value = rank, amounts[-1]
            break
    return best_value


def _customer_name(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    """The name at or below a customer anchor.

    Constrained to the anchor's own column: on a two-column invoice the line
    after "Bill To:" in document order is often "Invoice Date", printed on the
    other side of the page.
    """
    for page in doc.pages:
        for line in page.lines:
            m = profile.customer_anchor_re.search(line.text)
            if not m:
                continue

            # Same line: "Bill To: ACME Traders", or
            # "Sri/Smt/M/s . ANITHA RAMESH   Mobile : 9000012345"
            tail = _trim_at_next_label(line.text[m.end() :], profile)
            if len(tail) >= 3 and not profile.not_a_name_re.match(tail):
                return tail

            # Otherwise the nearest lines below, in the same column.
            anchor_bbox = line.bbox_of(m.start(), m.end())
            x_left = anchor_bbox[0] if anchor_bbox else line.x0
            below = [
                ln
                for ln in page.lines
                if ln.y0 > line.y0
                and x_left - 20 <= ln.x0 <= x_left + CUSTOMER_BLOCK_WIDTH
            ]
            for following in sorted(below, key=lambda ln: ln.y0)[:4]:
                text = _trim_at_next_label(following.text, profile)
                if len(text) >= 3 and not profile.not_a_name_re.match(text):
                    return text
    return None


def _trim_at_next_label(text: str, profile: DocumentProfile) -> str:
    """Cut a name short at whatever label follows it on the same row.

    Chit fund receipts put the name and the mobile number on one line:
    "Sri/Smt/M/s . ANITHA RAMESH   Mobile : 9000012345". Without this the
    "name" swallows the number and the message greets the customer with their
    own phone number.
    """
    cleaned = text.strip(" .:-–\t")
    cut = len(cleaned)
    for label in (*profile.phone_labels, *profile.not_phone_labels):
        m = re.search(rf"\s\b{re.escape(label)}\b\s*[:.\-–]", cleaned, re.IGNORECASE)
        if m:
            cut = min(cut, m.start())
    # Also stop at a run of digits long enough to be a number rather than a name.
    m = re.search(r"\s\d{4,}", cleaned)
    if m:
        cut = min(cut, m.start())
    return cleaned[:cut].strip(" .:-–\t")


def extract_fields(
    pdf_path: Path,
    excluded_numbers: set[str] | None = None,
    country_code: str = "91",
    ocr: OcrSettings | None = None,
    profile: DocumentProfile | None = None,
) -> ExtractedFields:
    """Read a captured PDF into the fields the pipeline needs.

    Pages with no text layer are OCRed when `ocr` is supplied. Any number that
    comes back from OCR is cross-checked against a second read at a different
    resolution before it is allowed to look confident.
    """
    profile = profile or default_profile()
    doc = pdf_text.read(pdf_path, ocr=ocr)

    if not doc.pages or (not doc.has_text_layer and not doc.used_ocr):
        # Nothing readable. The gate holds the job and reports ocr_error.
        return ExtractedFields(
            page_count=doc.page_count,
            has_text_layer=False,
            used_ocr=False,
            ocr_error=doc.ocr_error,
        )

    candidates = find_candidates(doc, excluded_numbers, country_code, profile)

    # Only pay for the verification pass when OCR actually produced a number.
    if ocr and any(c.from_ocr for c in candidates):
        verification = pdf_text.read(pdf_path, ocr=ocr, ocr_dpi=ocr.verify_dpi)
        apply_ocr_verification(candidates, scan_numbers(verification, country_code))

    return ExtractedFields(
        candidates=candidates,
        invoice_number=_document_number(doc, profile),
        customer_name=_customer_name(doc, profile),
        invoice_date=_document_date(doc, profile),
        total_amount=_total_amount(doc, profile),
        page_count=doc.page_count,
        has_text_layer=doc.has_text_layer,
        used_ocr=doc.used_ocr,
        ocr_error=doc.ocr_error,
    )
