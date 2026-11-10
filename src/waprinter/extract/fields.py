"""Pull the fields a message template needs off the printed page.

Everything here is best-effort and nullable. A missing customer name makes the
message read worse; a wrong phone number sends someone else their paperwork, so
that logic lives in phone.py under much stricter rules.

What counts as a label is per-client configuration — see profile.py. Label/value
pairs are read from *rows* rather than lines, because software routinely emits
"Grand Total" and "18,450.00" as separate blocks at the same height.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..models import ExtractedFields
from . import pdf_text
from .ocr import OcrSettings
from .phone import (
    CUSTOMER_BLOCK_WIDTH,
    apply_ocr_verification,
    default_profile,
    find_candidates,
    phone_spans,
    scan_numbers,
)
from .profile import DocumentProfile

log = logging.getLogger(__name__)

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


# Modes that are initials, and so are not written "Upi" in a message.
_MODE_INITIALS = {"upi", "dd", "neft", "rtgs", "imps"}

# The date a payment was made online, printed beside its transaction number:
# "048669201016 dated 06/10/2026", or a cheque's on the line under its number:
# "dated 31/10/2026 of Bank". Not the receipt's date, and "dated" is a date
# label, so it has to be taken off the row before dates are read. A row that
# only starts "dated" is left alone: that is how an invoice gives its own date.
_TRANSACTION_DATED = re.compile(
    r"\b\d{6,}\s+dated\s+\S+|\bdated\s+\S+\s+of\s+bank\b", re.IGNORECASE
)


def _mode_name(word: str) -> str:
    """A payment mode as a member should read it: "Cash", but "UPI"."""
    word = word.strip()
    return word.upper() if word.lower() in _MODE_INITIALS else word.title()


def _payment_mode(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    """How the customer paid.

    A labelled value first ("Mode of Payment  Cash"), then the bare word alone
    on a row — which is all a pre-printed form leaves in the text layer — then
    "Cheque Number 351768", then "Through ONLINE-UPI", which is how an
    online payment is printed under its transaction number. Last, the bare
    word on a line of its own, whatever row it ended up in.
    """
    for row in doc.rows:
        m = profile.payment_mode_labelled_re.search(row.text)
        if m:
            return _mode_name(m.group(1))
    beside = _mode_beside_label(doc, profile)
    if beside:
        return beside
    for row in doc.rows:
        m = profile.payment_mode_bare_re.match(row.text)
        if m:
            return _mode_name(m.group(1))
    for row in doc.rows:
        m = profile.payment_mode_numbered_re.match(row.text)
        if m:
            return _mode_name(m.group(1))
    for row in doc.rows:
        m = profile.payment_mode_through_re.search(row.text)
        if m:
            # "ONLINE-UPI": the last word is the method, the first only says
            # it was not paid at the counter.
            return _mode_name(re.split(r"\s*[-/]\s*", m.group(1))[-1])
    # Rows are what a reader sees, so a value printed high in a tall box can
    # join the row above it. The PDF still declares it as a line of its own.
    for line in doc.lines:
        m = profile.payment_mode_bare_re.match(line.text)
        if m:
            return _mode_name(m.group(1))
    return None


# What a receipt calls the number of the payment itself. A chit receipt
# prints "Cheque Number 351768 / dated 31/10/2026 of Bank / SBI-PDPL" for a
# cheque and "Transaction Number / 048669201016 dated 06/10/2026 / Through
# ONLINE-UPI" for an online payment. A bare "Ref No." is left out on purpose:
# on an invoice that is the buyer's order, not how they paid.
_REFERENCE_LABEL = re.compile(
    r"\b(cheque|chq|dd|demand\s+draft|transaction|txn|utr)\s*"
    r"(?:number|no\b\.?|id\b|#)\s*[:\-–#.]?",
    re.IGNORECASE,
)
# The number itself, on the label's row or the one below it. A digit is
# required, so a word that happens to follow the label is never taken.
_REFERENCE_VALUE = re.compile(r"\s*([A-Za-z0-9]*\d[A-Za-z0-9]*)")
_PAID_ON = re.compile(
    r"\bdated\s+(\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.](?:\d{4}|\d{2}))\b",
    re.IGNORECASE,
)
# The bank is the first word after "of Bank", whether on the same row or the
# next. Capitals only, no spaces: "SBI-PDPL", "HDFC". A scan sometimes loses
# that line altogether, and then the row after it -- "*Cheques accepted ..."
# or "For SRINIDHI ..." -- must not be read as the bank.
_BANK = re.compile(r"(?i:\bof\s+bank\b)\s*[:\-]?\s*([A-Z][A-Z0-9&.\-]{1,24})(?=\s|$)")
# Instruments with a number of their own, as opposed to an online transfer.
_PAPER = {"cheque": "Cheque", "chq": "Cheque", "dd": "DD", "demand draft": "DD"}
# How many rows the payment box spans below its first line.
_PAYMENT_BOX_ROWS = 3


@dataclass
class PaymentReference:
    number: str
    paid_on: str | None = None
    bank: str | None = None
    paper: str | None = None   # "Cheque" or "DD" when the label says so


def _payment_reference(doc: pdf_text.Document) -> PaymentReference | None:
    """The cheque or transaction number the payment was made with.

    Only ever a value for the message. It never takes part in choosing the
    recipient: a twelve-digit UPI number beginning 91 reads as a mobile, and
    phone.py keeps it out of the candidates for exactly that reason.
    """
    rows = [row.text for row in doc.rows]
    for i, text in enumerate(rows):
        label = _REFERENCE_LABEL.search(text)
        if not label:
            continue
        box = "\n".join([text[label.start():], *rows[i + 1 : i + _PAYMENT_BOX_ROWS]])
        after = box[label.end() - label.start():]
        value = _REFERENCE_VALUE.match(after)
        if not value or not 4 <= len(value.group(1)) <= 30:
            continue
        paid_on = _PAID_ON.search(box)
        bank = _BANK.search(box)
        return PaymentReference(
            number=value.group(1),
            paid_on=paid_on.group(1) if paid_on else None,
            bank=bank.group(1) if bank else None,
            paper=_PAPER.get(re.sub(r"\s+", " ", label.group(1).lower())),
        )
    return None


def payment_details(method: str | None, reference: PaymentReference | None) -> str | None:
    """The payment as one line of a message.

    "Cheque No. 351768 dated 31/10/2026, SBI-PDPL", "UPI Transaction No.
    048669201016 dated 06/10/2026", or just "Cash". One line, because it
    fills the payment mode variable of a template Meta has already approved,
    and so needs no new approval to reach the member.
    """
    if reference is None:
        return method
    if reference.paper:
        text = f"{method or reference.paper} No. {reference.number}"
    elif method:
        text = f"{method} Transaction No. {reference.number}"
    else:
        text = f"Transaction No. {reference.number}"
    if reference.paid_on:
        text += f" dated {reference.paid_on}"
    if reference.bank:
        text += f", {reference.bank}"
    return text


# How much of an OCR-read number's width must be covered by shapes drawn
# inside it before it counts as printed rather than scanned. The client's
# counter draws each digit as one filled outline and covers 85% (the gaps
# between digits are the rest); a scan or photo covers nothing.
_DRAWN_COVERAGE = 0.7


def mark_drawn_numbers(pdf_path: Path, candidates) -> None:
    """Flag the OCR-read numbers whose digits the page draws as shapes.

    Some printer drivers -- Microsoft Print To PDF with the client's chit
    software -- turn every letter into a filled vector outline. The page has no
    text layer, so it is read by OCR like a scan, but it is not a scan: the
    digits are as sharp as type. Only shapes lying wholly inside the number's
    box count, and plain rectangles never do, so a table border or a shaded
    cell under a scanned number cannot pass for printed digits.
    """
    import fitz

    pending = [c for c in candidates if c.from_ocr]
    if not pending:
        return
    try:
        with fitz.open(pdf_path) as pdf:
            for candidate in pending:
                # Candidate pages count from 1, as the page reader numbers them.
                candidate.drawn = _drawn_at(pdf[candidate.page - 1], candidate.bbox)
                if candidate.drawn:
                    candidate.reasons.append("digits are drawn as shapes, not scanned")
    except Exception:
        # Telling printed from scanned only ever lets a number through; a
        # failure here leaves it held as a scan, which is the safe side.
        log.exception("could not inspect the drawing of %s", pdf_path)


def _drawn_at(page, bbox) -> bool:
    import fitz

    box = fitz.Rect(bbox)
    if box.is_empty or box.width <= 0:
        return False
    grown = box + (-2, -2, 2, 2)
    spans = []
    for drawing in page.get_drawings():
        if drawing.get("fill") is None or not grown.contains(drawing["rect"]):
            continue
        if all(item[0] in ("re", "qu") for item in drawing["items"]):
            continue  # a box, not a glyph
        spans.append((drawing["rect"].x0, drawing["rect"].x1))
    covered, reached = 0.0, box.x0
    for x0, x1 in sorted(spans):
        x0, x1 = max(x0, reached), min(x1, box.x1)
        if x1 > x0:
            covered += x1 - x0
            reached = x1
    return covered / box.width >= _DRAWN_COVERAGE


def _mode_beside_label(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    """The mode printed in the box to the right of its label.

    Form XVI prints "Cash" at the top of a box taller than its label. Rows
    are grouped by height, so a value raised far enough joins the row above
    ("Sub Total c/d   Cash   2.00 ...") and neither the labelled read nor the
    bare one sees it. Looking out from the label finds it wherever the box
    puts it. Only a line holding nothing but the mode counts, so "Cash
    Discount" on the row above is not taken for it.
    """
    for page in doc.pages:
        for label in page.lines:
            if not profile.payment_mode_label_re.search(label.text):
                continue
            mid = (label.y0 + label.y1) / 2
            reach = 2 * (label.y1 - label.y0)
            found = []
            for line in page.lines:
                if line is label or line.x0 < label.x1 - 1:
                    continue
                m = (profile.payment_mode_bare_re.match(line.text)
                     or profile.payment_mode_numbered_re.match(line.text))
                gap = abs((line.y0 + line.y1) / 2 - mid)
                if m and gap <= reach:
                    found.append((gap, line.x0, m.group(1)))
            if found:
                return _mode_name(min(found)[2])
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
    # A transaction's own date is never the document's. See _TRANSACTION_DATED.
    rows = [_TRANSACTION_DATED.sub(" ", row.text) for row in doc.rows]

    for pattern in profile.date_res:
        for text in rows:
            m = pattern.search(text)
            if m:
                return m.group(1)

    # No "Date:" label anywhere on the page.
    for pattern in profile.bare_date_res:
        for text in rows:
            m = pattern.search(text)
            if m:
                return m.group(1)
    return None


# Ten or more digits with no comma or decimal point is a phone number, a
# landline or an account number. No receipt prints a rupee amount that way.
_NOT_AN_AMOUNT = re.compile(r"^\d{10,}$")


def _figures_after(text: str, label_end: int) -> list[str]:
    """The amounts printed after a label on its row, phone numbers left out.

    OCR of a scanned chit receipt runs "an amount of Rupees", the member's
    name and "Mobile: 9876543210" together as one row. Read from the whole
    row, the figure furthest right was the mobile number, and the member was
    told they had paid ₹9876543210. Figures to the left of the label are
    border specks or another column's values, never the labelled amount.
    """
    tail = text[label_end:]
    for start, end in reversed(phone_spans(tail)):
        tail = tail[:start] + " " * (end - start) + tail[end:]
    return [a for a in MONEY.findall(tail) if not _NOT_AN_AMOUNT.match(a)]


def _total_amount(doc: pdf_text.Document, profile: DocumentProfile) -> str | None:
    best_rank, best_value = 0, None
    # Most specific label first, so a "Grand Total" row is ranked as one
    # rather than stopping at the "total" inside it.
    labels = sorted(profile.amount_label_res, key=lambda pair: pair[1], reverse=True)
    for row in doc.rows:
        for pattern, rank in labels:
            match = pattern.search(row.text)
            if not match:
                continue
            if rank >= best_rank:
                amounts = _figures_after(row.text, match.end())
                if amounts:
                    # The figure furthest right on a totals row is the total.
                    best_rank, best_value = rank, amounts[-1]
            break
    return best_value


def _settled_total(labelled: str | None, from_words: str | None) -> str | None:
    """The total, preferring the words when the two disagree.

    The amount in words names exactly one figure; a label on a scan can sit
    beside a sub-total, a charge or an OCR misread. Where they agree the
    labelled figure is kept, because it is printed the way the office writes
    money ("18,450.00").
    """
    if labelled and from_words:
        try:
            if float(labelled.replace(",", "")) != float(from_words):
                return from_words
        except ValueError:
            return from_words
    return labelled or from_words


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
    forced_kind=None,
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

    # What kind of paperwork this is decides how the rest of it is read: a
    # removal notice heads its address block "To,", not "Received from".
    kind = forced_kind or profile.kind_of(doc)
    classification_error = ("Conflicting document headings. Choose the document type before sending."
                            if len(profile.title_matches(doc)) > 1 else "")
    profile = profile.for_kind(kind)

    candidates = find_candidates(doc, excluded_numbers, country_code, profile)
    words = _amount_words(doc, profile)
    reference = _payment_reference(doc)
    # A scan puts specks in front of "Cheque Number", and the mode reader,
    # which wants the word at the start of its line, misses it. The label
    # found with the number says the same thing.
    method = _payment_mode(doc, profile) or (reference.paper if reference else None)

    # Any page OCR read gets the second pass, whether or not it held a
    # number. It used to run only for an OCR'd number, so a scan with none --
    # the normal case where the operator types the number -- had its title
    # read once and was marked verified on the strength of that one read.
    kind_verified = True
    if ocr and doc.used_ocr:
        verification = pdf_text.read(pdf_path, ocr=ocr, ocr_dpi=ocr.verify_dpi)
        apply_ocr_verification(candidates, scan_numbers(verification, country_code))
        mark_drawn_numbers(pdf_path, candidates)
        # The title gets the same treatment as the number, and for a stronger
        # reason. A misread number sends the right words to the wrong person,
        # which the recipient can see is not for them. A misread title sends
        # the wrong words to the right person -- a member who is being removed
        # thanked for a payment -- and nothing about it looks wrong until they
        # read it. The second pass runs at a higher resolution, so the common
        # failure is a title the first read missed entirely and the second one
        # found: that shows up here as a disagreement and is held.
        second = profile.kind_of(verification)
        kind_verified = (second.name if second else None) == (
            kind.name if kind else None
        )

    fields = ExtractedFields(
        candidates=candidates,
        invoice_number=_document_number(doc, profile),
        customer_name=_customer_name(doc, profile),
        invoice_date=_document_date(doc, profile),
        total_amount=_settled_total(
            _total_amount(doc, profile), amount_from_words(words or "")
        ),
        amount_words=words,
        payment_mode=payment_details(method, reference),
        payment_method=method,
        payment_reference=reference.number if reference else None,
        document_kind=kind.name if kind else None,
        document_kind_verified=kind_verified,
        classification_error=classification_error,
        page_count=doc.page_count,
        has_text_layer=doc.has_text_layer,
        used_ocr=doc.used_ocr,
        ocr_error=doc.ocr_error,
    )
    apply_taught_fields(fields, doc, profile.rules_for(kind))
    return fields


def apply_taught_fields(fields: ExtractedFields, doc, rules) -> None:
    """Put what the taught rules read onto `fields`.

    A taught value for a built-in field replaces the built-in guess: someone
    pointed at the exact spot on this layout, which beats a heuristic written
    for every layout at once. Everything else goes into `extra`, where a
    template mapping can reach it by name.
    """
    from .rules import BUILTIN_FIELDS, apply_rules

    for name, value in apply_rules(doc, rules).items():
        if name in BUILTIN_FIELDS:
            setattr(fields, name, value)
        else:
            fields.extra[name] = value
