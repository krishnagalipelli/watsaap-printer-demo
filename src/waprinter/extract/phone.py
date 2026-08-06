"""Find the customer's mobile number on an invoice, and say how sure we are.

This module decides who receives someone's invoice, with no human in the loop.
The bias throughout is toward *not* producing a high-confidence answer: a held
job costs the operator ten seconds, a confident wrong answer sends a customer's
billing details to a stranger and cannot be recalled.

Scoring rubric (see SCORE_* below):
  base 40   syntactically a valid Indian mobile
  +30       anchored to a customer phone label ("Mobile:", "Contact No.")
  +18       sits inside the Bill To / Consignee block
  +8        on page 1
  -30       in the page footer (usually the seller's own contact line)
  -22       in the page-1 letterhead with no customer anchor above it
  reject    anchored to a non-phone label (GSTIN, Invoice No, A/c No, ...)
  reject    matches the seller's own numbers or the blocklist

HIGH (>= 75) is the only band eligible for a silent send.
"""

from __future__ import annotations

import re

from ..models import Confidence, PhoneCandidate
from .pdf_text import Document, Page

# --- scoring weights (tunable against the real-invoice corpus) --------------

SCORE_BASE = 40
SCORE_PHONE_LABEL = 30
SCORE_CUSTOMER_BLOCK = 18
SCORE_FIRST_PAGE = 8
PENALTY_FOOTER = -30
PENALTY_LETTERHEAD = -22

THRESHOLD_HIGH = 75
THRESHOLD_MEDIUM = 55

# Fraction of page height treated as footer / letterhead.
FOOTER_BAND = 0.85
LETTERHEAD_BAND = 0.12

# How far below a "Bill To" anchor the customer block is assumed to extend,
# and how far right, in PDF points.
CUSTOMER_BLOCK_HEIGHT = 170.0
CUSTOMER_BLOCK_WIDTH = 300.0

# --- labels -----------------------------------------------------------------

# A label that means "the digits after me are a phone number".
PHONE_LABEL = re.compile(
    r"\b(?:mobile|mob|movil|phone|ph|contact|cell|whats\s*app|whatsapp|"
    r"tel|telephone|mo)\b\.?\s*(?:no\.?|nos\.?|number|#)?\s*[:\-–]?\s*$",
    re.IGNORECASE,
)

# A label that means "the digits after me are definitely NOT a phone number".
# Anything anchored to one of these is rejected outright, whatever it scores.
NOT_PHONE_LABEL = re.compile(
    r"\b(?:gstin|gst|pan|hsn|sac|ifsc|a\s*/\s*c|acc?ount|invoice|bill|"
    r"challan|e[\s\-]?way|eway|vehicle|lr|po|order|purchase|pin|pincode|"
    r"cin|tin|udyam|msme|dated?|amount|qty|quantity|rate|cheque|check|"
    r"utr|ref(?:erence)?|voucher|docu?m?e?n?t?|irn|ack|state\s*code|"
    r"licen[cs]e|dl|fssai|serial|s\.?\s*no)\b\.?\s*"
    r"(?:no\.?|nos\.?|number|code|#)?\s*[:\-–]?\s*$",
    re.IGNORECASE,
)

# Headings that introduce the customer's details.
CUSTOMER_ANCHOR = re.compile(
    r"\b(?:bill(?:ed)?\s*[-–]?\s*to|buyer|consignee|customer|"
    r"ship(?:ped)?\s*[-–]?\s*to|party\s*name|party|details\s*of\s*receiver|"
    r"receiver)\b",
    re.IGNORECASE,
)

# Separators allowed *inside* a phone number. A "/" or "." or "," between digit
# groups means we are looking at a date or an amount, not a number to dial.
# Note this matches at most ONE space, which is what stops a number in one
# column fusing with a number in the next (columns are joined by ROW_GAP).
JOINABLE_SEP = re.compile(r"^[\s\-]?$")
# Characters that, when they touch the match, indicate a date/amount/decimal.
# A frozenset, not a string: `"" in "/.,"` is True and would discard every
# number that ends at a row boundary.
STICKY_CHARS = frozenset("/.,")


def _digit_chunks(text: str) -> list[tuple[int, int, str]]:
    """Maximal runs of digits as (start, end, digits)."""
    return [(m.start(), m.end(), m.group()) for m in re.finditer(r"\d+", text)]


def normalize(digits: str, country_code: str = "91") -> str | None:
    """Reduce a digit string to E.164, or None if it is not a mobile number.

    Indian mobile numbers are exactly 10 digits beginning 6-9. Longer forms are
    accepted only when the excess is a recognised country/trunk prefix, which
    is what keeps 11-digit account numbers and 12-digit reference codes out.
    """
    d = digits
    if len(d) == 13 and d.startswith("0" + country_code):
        d = d[3:]
    elif len(d) == 12 and d.startswith(country_code):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]

    if len(d) != 10 or d[0] not in "6789":
        return None
    return f"+{country_code}{d}"


def _looks_like_landline(window: list[tuple[int, int, str]]) -> bool:
    """True for STD-code landlines such as "080-25551234" or "0422 2345678".

    These matter because stripping the trunk "0" leaves a 10-digit string that
    can begin 6-9 and so passes every mobile test. Landlines are grouped as a
    2-5 digit area code followed by a 6-8 digit local number, which a mobile
    never is (mobiles group 10, 5+5, or 4+3+3).
    """
    if len(window) < 2:
        return False
    lengths = [len(c[2]) for c in window]
    head, rest = lengths[0], sum(lengths[1:])

    # Explicit trunk prefix: "080-2555 1234"
    if window[0][2].startswith("0") and 2 <= head <= 5:
        return True
    # Area code without the trunk zero: "80 25551234", "44 2345 6789"
    if 2 <= head <= 5 and 6 <= rest <= 8:
        return True
    return False


def parse_typed_number(text: str, country_code: str = "91") -> str | None:
    """Validate a number a person typed, or None if it is not a mobile.

    Applies exactly the same rules as the page scanner, including the landline
    heuristic. That matters because the separators carry the signal: stripping
    "080-25551234" down to digits leaves "8025551234", which passes every mobile
    test on its own. Anything the extractor refuses to read off a page, an
    operator should not be able to type either.

    Returns None if the text holds no valid number, or more than one.
    """
    found: list[str] = []
    for _start, _end, digits in _find_raw_numbers(text):
        e164 = normalize(digits, country_code)
        if e164 and e164 not in found:
            found.append(e164)
    return found[0] if len(found) == 1 else None


def _find_raw_numbers(text: str) -> list[tuple[int, int, str]]:
    """Locate phone-shaped digit sequences as (start, end, digits).

    Digit runs are joined only across a single space or hyphen, so "98765 43210"
    is one candidate while "12/05/2024" and "1,23,456.78" never combine.
    """
    chunks = _digit_chunks(text)
    found: list[tuple[int, int, str]] = []

    for i in range(len(chunks)):
        for span in (1, 2, 3):
            if i + span > len(chunks):
                continue
            window = chunks[i : i + span]

            # Every gap inside the window must be a phone-style separator.
            joinable = True
            for a, b in zip(window, window[1:]):
                if not JOINABLE_SEP.match(text[a[1] : b[0]]):
                    joinable = False
                    break
            if not joinable:
                continue

            if _looks_like_landline(window):
                continue

            start, end = window[0][0], window[-1][1]

            # Reject when the sequence is glued to a date or a decimal amount.
            before = text[start - 1] if start > 0 else ""
            after = text[end] if end < len(text) else ""
            if before in STICKY_CHARS or after in STICKY_CHARS:
                continue

            digits = "".join(c[2] for c in window)
            if 10 <= len(digits) <= 13:
                found.append((start, end, digits))

    return found


def _nearest_label(prefix: str) -> tuple[str | None, bool]:
    """Classify the closest label to the left of a number.

    Returns (label_text, is_phone_label). The *nearest* label wins, so a line
    reading "Invoice No: 4471   Mobile: 9876543210" resolves correctly for both
    numbers on it.
    """
    phone_hit = None
    for m in re.finditer(PHONE_LABEL, prefix):
        phone_hit = m
    not_phone_hit = None
    for m in re.finditer(NOT_PHONE_LABEL, prefix):
        not_phone_hit = m

    if phone_hit and not_phone_hit:
        if phone_hit.start() > not_phone_hit.start():
            return phone_hit.group().strip(" :-–"), True
        return not_phone_hit.group().strip(" :-–"), False
    if phone_hit:
        return phone_hit.group().strip(" :-–"), True
    if not_phone_hit:
        return not_phone_hit.group().strip(" :-–"), False
    return None, False


def _customer_blocks(page: Page) -> list[tuple[float, float, float, float]]:
    """Rectangles covering the Bill To / Consignee areas of a page.

    Anchored on lines rather than rows so the rectangle starts at the "Bill To"
    column, not at the leftmost element of the whole row.
    """
    blocks = []
    for line in page.lines:
        m = CUSTOMER_ANCHOR.search(line.text)
        if not m:
            continue
        # Anchor horizontally on the heading itself, so a right-hand column at
        # the same height stays outside the block.
        anchor_bbox = line.bbox_of(m.start(), m.end())
        x_left = anchor_bbox[0] if anchor_bbox else line.x0
        blocks.append(
            (
                x_left - 30.0,
                line.y0 - 4.0,
                x_left + CUSTOMER_BLOCK_WIDTH,
                line.y0 + CUSTOMER_BLOCK_HEIGHT,
            )
        )
    return blocks


def _in_any_block(
    bbox: tuple[float, float, float, float],
    blocks: list[tuple[float, float, float, float]],
) -> bool:
    x0, y0, _x1, _y1 = bbox
    return any(bx0 <= x0 <= bx1 and by0 <= y0 <= by1 for bx0, by0, bx1, by1 in blocks)


def find_candidates(
    doc: Document,
    excluded: set[str] | None = None,
    country_code: str = "91",
) -> list[PhoneCandidate]:
    """All plausible customer numbers on the document, best first."""
    excluded = excluded or set()
    best: dict[str, PhoneCandidate] = {}

    for page in doc.pages:
        blocks = _customer_blocks(page)
        footer_y = page.height * FOOTER_BAND
        letterhead_y = page.height * LETTERHEAD_BAND

        # Rows, not lines: an ERP may emit "Mobile:" and the digits as separate
        # blocks at the same height, and the label has to stay attached.
        for row in page.rows:
            text = row.text
            for start, end, digits in _find_raw_numbers(text):
                e164 = normalize(digits, country_code)
                if e164 is None or e164 in excluded:
                    continue

                label, is_phone_label = _nearest_label(text[:start])
                if label and not is_phone_label:
                    continue  # anchored to GSTIN / Invoice No / A/c No / ...

                bbox = row.bbox_of(start, end)
                if bbox is None:
                    continue

                score = SCORE_BASE
                reasons = ["valid Indian mobile format"]

                if is_phone_label:
                    score += SCORE_PHONE_LABEL
                    reasons.append(f"labelled '{label}'")

                in_customer_block = _in_any_block(bbox, blocks)
                if in_customer_block:
                    score += SCORE_CUSTOMER_BLOCK
                    reasons.append("inside customer block")

                if page.number == 1:
                    score += SCORE_FIRST_PAGE
                    reasons.append("on page 1")

                if bbox[1] >= footer_y:
                    score += PENALTY_FOOTER
                    reasons.append("in page footer (likely seller contact)")

                if (
                    page.number == 1
                    and bbox[1] <= letterhead_y
                    and not in_customer_block
                ):
                    score += PENALTY_LETTERHEAD
                    reasons.append("in letterhead (likely seller contact)")

                from_ocr = page.number in doc.ocr_pages
                if from_ocr:
                    reasons.append("read by OCR from a scanned page")

                score = max(0, min(100, score))
                candidate = PhoneCandidate(
                    raw=text[start:end],
                    e164=e164,
                    score=score,
                    confidence=_confidence(score),
                    page=page.number,
                    bbox=bbox,
                    label=label if is_phone_label else None,
                    reasons=reasons,
                    from_ocr=from_ocr,
                )

                # The same number can appear more than once; keep its best sighting.
                existing = best.get(e164)
                if existing is None or candidate.score > existing.score:
                    best[e164] = candidate

    return sorted(best.values(), key=lambda c: (-c.score, c.page, c.bbox[1]))


def _confidence(score: int) -> Confidence:
    if score >= THRESHOLD_HIGH:
        return Confidence.HIGH
    if score >= THRESHOLD_MEDIUM:
        return Confidence.MEDIUM
    return Confidence.LOW


def scan_numbers(doc: Document, country_code: str = "91") -> set[str]:
    """Every normalised number anywhere in the document, ignoring scoring.

    Used to cross-check a second OCR pass — position and labels are irrelevant
    there, only whether the same digits came back.
    """
    found: set[str] = set()
    for page in doc.pages:
        for row in page.rows:
            for _start, _end, digits in _find_raw_numbers(row.text):
                e164 = normalize(digits, country_code)
                if e164:
                    found.add(e164)
    return found


def apply_ocr_verification(
    candidates: list[PhoneCandidate],
    confirmed: set[str],
) -> list[PhoneCandidate]:
    """Downgrade OCR-derived numbers that a second read did not reproduce.

    Tesseract confuses digits — 5/6, 8/3, 1/7, 0/8 — and on a ten-digit mobile a
    single slip is a different, real person. Reading the page again at a
    different resolution is a cheap independent check: a number that survives
    both reads is very unlikely to be a misread, and one that does not is
    demoted to LOW so the gate holds it.

    Mutates and returns the list, re-sorted.
    """
    for candidate in candidates:
        if not candidate.from_ocr:
            continue
        if candidate.e164 in confirmed:
            candidate.reasons.append("confirmed by a second OCR pass")
            continue
        candidate.confidence = Confidence.LOW
        candidate.score = min(candidate.score, THRESHOLD_MEDIUM - 1)
        candidate.reasons.append(
            "a second OCR pass did not read the same number — likely a misread"
        )
    candidates.sort(key=lambda c: (-c.score, c.page, c.bbox[1]))
    return candidates
