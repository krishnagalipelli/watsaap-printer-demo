"""Pull the fields a message template needs off the printed page.

Everything here is best-effort and nullable. A missing customer name degrades
the message text; a missing or wrong phone number is the only thing that can
cause harm, so that logic lives in `phone.py` under much stricter rules.

Label/value pairs are read from *rows* rather than lines, because ERPs
routinely emit "Grand Total" and "18,450.00" as separate blocks at the same
height. See pdf_text.Page.rows.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import ExtractedFields
from . import pdf_text
from .ocr import OcrSettings
from .phone import (
    CUSTOMER_BLOCK_WIDTH,
    CUSTOMER_ANCHOR,
    apply_ocr_verification,
    find_candidates,
    scan_numbers,
)

# A colon or dash is required between label and value, which is what keeps
# "Invoice Date: 12/05/2026" from being read as an invoice number.
INVOICE_NO = re.compile(
    r"\b(?:tax\s+)?(?:invoice|bill|inv|voucher|document|doc)\s*(?!\s*date\b)"
    r"(?:no\.?|number|#)?\s*[:\-–]\s*"
    r"([A-Za-z0-9][A-Za-z0-9/\-]{1,24})",
    re.IGNORECASE,
)

# "Invoice Date: 12/05/2026", "Dated 12-05-2026"
INVOICE_DATE = re.compile(
    r"\b(?:invoice\s*date|bill\s*date|dated|date)\s*[:\-–]?\s*"
    r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# Prefer the most final-sounding total on the page.
TOTAL_LABELS = [
    (re.compile(r"\bgrand\s*total\b", re.IGNORECASE), 3),
    (re.compile(r"\b(?:amount|net)\s*payable\b", re.IGNORECASE), 3),
    (re.compile(r"\bnet\s*(?:amount|total)\b", re.IGNORECASE), 2),
    (re.compile(r"\btotal\s*amount\b", re.IGNORECASE), 2),
    (re.compile(r"\btotal\b", re.IGNORECASE), 1),
]

MONEY = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)

# Lines that follow "Bill To" but are not the customer's name.
NOT_A_NAME = re.compile(
    r"^\s*(?:gstin|gst|pan|state|address|phone|mobile|mob|contact|email|"
    r"place\s*of\s*supply|code|ph\b)",
    re.IGNORECASE,
)


def _invoice_number(doc: pdf_text.Document) -> str | None:
    for row in doc.rows:
        m = INVOICE_NO.search(row.text)
        if m:
            return m.group(1).strip(" .-/")
    return None


def _invoice_date(doc: pdf_text.Document) -> str | None:
    for row in doc.rows:
        m = INVOICE_DATE.search(row.text)
        if m:
            return m.group(1)
    return None


def _total_amount(doc: pdf_text.Document) -> str | None:
    best_rank, best_value = 0, None
    for row in doc.rows:
        for pattern, rank in TOTAL_LABELS:
            if not pattern.search(row.text):
                continue
            if rank >= best_rank:
                amounts = MONEY.findall(row.text)
                if amounts:
                    # The figure furthest right on a totals row is the total.
                    best_rank, best_value = rank, amounts[-1]
            break
    return best_value


def _customer_name(doc: pdf_text.Document) -> str | None:
    """The first plausible name line at or below a Bill To / Consignee anchor.

    Constrained to the anchor's own column: on a two-column invoice the line
    immediately after "Bill To:" in document order is often "Invoice Date",
    printed on the other side of the page.
    """
    for page in doc.pages:
        for line in page.lines:
            m = CUSTOMER_ANCHOR.search(line.text)
            if not m:
                continue

            anchor_bbox = line.bbox_of(m.start(), m.end())
            x_left = anchor_bbox[0] if anchor_bbox else line.x0

            # Same line: "Bill To: ACME Traders"
            tail = line.text[m.end() :].strip(" :-–\t")
            if len(tail) >= 3 and not NOT_A_NAME.match(tail):
                return tail

            # Otherwise the nearest lines below, in the same column.
            below = [
                ln
                for ln in page.lines
                if ln.y0 > line.y0
                and x_left - 20 <= ln.x0 <= x_left + CUSTOMER_BLOCK_WIDTH
            ]
            for following in sorted(below, key=lambda ln: ln.y0)[:4]:
                text = following.text.strip(" :-–\t")
                if len(text) >= 3 and not NOT_A_NAME.match(text):
                    return text
    return None


def extract_fields(
    pdf_path: Path,
    excluded_numbers: set[str] | None = None,
    country_code: str = "91",
    ocr: OcrSettings | None = None,
) -> ExtractedFields:
    """Read a captured PDF into the fields the pipeline needs.

    Pages with no text layer are OCRed when `ocr` is supplied. Any number that
    comes back from OCR is cross-checked against a second read at a different
    resolution before it is allowed to look confident.
    """
    doc = pdf_text.read(pdf_path, ocr=ocr)

    if not doc.pages or (not doc.has_text_layer and not doc.used_ocr):
        # Nothing readable. The gate holds the job and reports ocr_error.
        return ExtractedFields(
            page_count=doc.page_count,
            has_text_layer=False,
            used_ocr=False,
            ocr_error=doc.ocr_error,
        )

    candidates = find_candidates(doc, excluded_numbers, country_code)

    # Only pay for the verification pass when OCR actually produced a number.
    if ocr and any(c.from_ocr for c in candidates):
        verification = pdf_text.read(pdf_path, ocr=ocr, ocr_dpi=ocr.verify_dpi)
        apply_ocr_verification(
            candidates, scan_numbers(verification, country_code)
        )

    return ExtractedFields(
        candidates=candidates,
        invoice_number=_invoice_number(doc),
        customer_name=_customer_name(doc),
        invoice_date=_invoice_date(doc),
        total_amount=_total_amount(doc),
        page_count=doc.page_count,
        has_text_layer=doc.has_text_layer,
        used_ocr=doc.used_ocr,
        ocr_error=doc.ocr_error,
    )
