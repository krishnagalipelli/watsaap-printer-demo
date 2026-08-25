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

# Indian English number words, for reading an amount written out in full.
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100000, "lakhs": 100000,
           "crore": 10000000, "crores": 10000000}
_IGNORED_WORDS = {"rupees", "rupee", "only", "and", "rs", "inr", "amount", "of"}


def amount_from_words(phrase: str) -> str | None:
    """"One Hundred Only" -> "100".

    Worth the trouble because on a chit receipt the words are the *only*
    unambiguous statement of what was paid: the figures appear five times over
    as dues, sub-totals, interest and charges, and the labels that would tell
    them apart are pre-printed on the stationery, so they never reach the PDF's
    text layer.
    """
    total = 0
    current = 0
    seen = False
    for word in re.findall(r"[A-Za-z]+", phrase.lower()):
        if word in _IGNORED_WORDS:
            continue
        if word in _UNITS:
            current += _UNITS[word]
            seen = True
        elif word in _SCALES:
            scale = _SCALES[word]
            if scale >= 1000:
                total += max(current, 1) * scale
                current = 0
            else:
                current = max(current, 1) * scale
            seen = True
        else:
            return None  # an unknown word means this is not an amount
    if not seen:
        return None
    return str(total + current)


def _amount_words(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    for pattern in profile.amount_words_res:
        for row in doc.rows:
            m = pattern.search(row.text)
            if m and amount_from_words(m.group(1)) is not None:
                return m.group(1).strip()
    return None


def _payment_mode(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    """How the customer paid.

    A labelled value first ("Mode of Payment  Cash"), then the bare word alone
    on a row — which is all a pre-printed form leaves in the text layer.
    """
    for row in doc.rows:
        m = profile.payment_mode_labelled_re.search(row.text)
        if m:
            return m.group(1).strip().title()
    for row in doc.rows:
        m = profile.payment_mode_bare_re.match(row.text)
        if m:
            return m.group(1).strip().title()
    return None


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


def _anchor_tail(text: str, profile: DocumentProfile) -> str | None:
    """The text following the *last* customer anchor on a line.

    Last, not first: pre-printed stationery says "Received from Sri/Smt/M/s .
    NAME", and both of those are anchors. Taking the first would return
    "Sri/Smt/M/s . NAME" as the customer's name.
    """
    last = None
    for match in profile.customer_anchor_re.finditer(text):
        last = match
    if last is None:
        return None
    return _trim_at_next_label(text[last.end() :], profile)


# Characters that never occur inside an Indian personal name. A leading token
# containing one is OCR debris from the anchor beside it, not part of the name.
_NAME_JUNK = set(r"""/\'’‘"|<>*_=+~`^@#$%&""")


def _strip_leading_junk(text: str) -> str:
    """Drop mangled fragments in front of a name.

    On a poor scan Tesseract reads "Sri/Smt/M/s" as things like "'M’s", which
    survives the anchor trim and ends up greeting the member as
    "Dear 'M’s . ANITHA". Initials such as "R." are kept, because a period is
    not junk.
    """
    tokens = text.split()
    while tokens and any(ch in _NAME_JUNK for ch in tokens[0]):
        tokens.pop(0)
    return " ".join(tokens).strip(" .:-–\t")


def _plausible_name(text: str | None, profile: DocumentProfile) -> bool:
    if not text or len(text) < 3:
        return False
    if profile.not_a_name_re.match(text):
        return False
    # Boilerplate that follows an anchor on a pre-printed form is not a name.
    return not profile.customer_anchor_re.search(text)


def _customer_name(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    """The customer's name, from a Bill To / Sri-Smt / Received from anchor.

    Two passes, and the order is the whole point. A name printed on the *same
    row* as its anchor is far stronger evidence than one merely below it, so
    every anchor on the page gets the same-row test before any anchor is
    allowed the look-below fallback.

    Rows, not lines: at 300 dpi Tesseract split "Sri/Smt/M/s . NAME" into two
    separate lines at the same height, which left the anchor with nothing
    beside it. Grouping by vertical position puts them back together.

    Without that, a pre-printed chit receipt read by OCR fails: the form carries
    both "Received from" and "Sri/Smt/M/s . NAME". "Received from" is found
    first and has nothing after it, so the old code looked at the lines beneath
    it and returned "an amount of Rupees...", which then greeted the member by
    that phrase. The PDF's own text layer contains only the filled-in fields, so
    this only ever showed on the OCR path — the case least likely to be noticed.
    """
    for page in doc.pages:
        for row in page.rows:
            tail = _anchor_tail(row.text, profile)
            if _plausible_name(tail, profile):
                return tail

    # Nothing beside an anchor, so look underneath one — same column only, since
    # on a two-column invoice the next line in document order is often the other
    # side of the page.
    for page in doc.pages:
        for line in page.lines:
            m = profile.customer_anchor_re.search(line.text)
            if not m:
                continue
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
                if _plausible_name(text, profile):
                    return text
    return None


def _trim_at_next_label(text: str, profile: DocumentProfile) -> str:
    """Cut a name short at whatever label follows it on the same row.

    Chit fund receipts put the name and the mobile number on one line:
    "Sri/Smt/M/s . ANITHA RAMESH   Mobile : 9000012345". Without this the
    "name" swallows the number and the message greets the customer with their
    own phone number.

    A label at the very start counts too, and may carry a "No" before its
    colon. On a two-column invoice the "Bill To" row also holds
    "Invoice No: INV-2291" from the right-hand column, and without both of
    those the invoice number was returned as the customer's name.
    """
    cleaned = text.strip(" .:-–\t")
    cut = len(cleaned)
    for label in (*profile.phone_labels, *profile.not_phone_labels):
        m = re.search(
            rf"(?:^|\s)\b{re.escape(label)}\b\.?\s*"
            rf"(?:no\.?|nos\.?|number|#)?\s*[:.\-–]",
            cleaned,
            re.IGNORECASE,
        )
        if m:
            cut = min(cut, m.start())
    # Also stop at a run of digits long enough to be a number rather than a name.
    m = re.search(r"\s\d{4,}", cleaned)
    if m:
        cut = min(cut, m.start())
    return _strip_leading_junk(cleaned[:cut].strip(" .:-–\t"))


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
    words = _amount_words(doc, profile)

    # Only pay for the verification pass when OCR actually produced a number.
    if ocr and any(c.from_ocr for c in candidates):
        verification = pdf_text.read(pdf_path, ocr=ocr, ocr_dpi=ocr.verify_dpi)
        apply_ocr_verification(candidates, scan_numbers(verification, country_code))

    return ExtractedFields(
        candidates=candidates,
        invoice_number=_document_number(doc, profile),
        customer_name=_customer_name(doc, profile),
        invoice_date=_document_date(doc, profile),
        total_amount=_total_amount(doc, profile) or amount_from_words(words or ""),
        amount_words=words,
        payment_mode=_payment_mode(doc, profile),
        page_count=doc.page_count,
        has_text_layer=doc.has_text_layer,
        used_ocr=doc.used_ocr,
        ocr_error=doc.ocr_error,
    )
