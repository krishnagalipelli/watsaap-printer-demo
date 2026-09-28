"""Read the fields someone taught from a sample PDF.

A taught field is a label and a kind of value: "the code after Cust ID", "the
date under Due date". This module turns a click on a sample into that rule,
and reads the rule back off every later print.

Two directions, deliberately in one place so they cannot disagree:

* **Learning** -- `rule_from_selection` looks at the words someone clicked,
  finds the label printed beside or above them, and guesses what kind of
  value it is. `infer_pattern` turns "SCF-00418" into a shape that also
  matches "SCF-12".
* **Reading** -- `read_rule` finds the label on a new page and takes the value
  the same way. The teaching screen runs it on the sample itself before
  saving, so a rule that would read something other than what was clicked is
  caught while the person who clicked is still looking.

Everything here is text-layer geometry from pdf_text. On a scanned page those
words come from OCR, so a taught value is only as good as the scan; the
recipient's number keeps its own double-read check in phone.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .pdf_text import ROW_GAP, Document, Page, TextRun
from .profile import DATE_PATTERNS, FieldRule

# Characters between a label and its value: "Cust ID : SCF-1", "Date - 1/2/26".
_SEPARATORS = re.compile(r"^[\s:.\-–#=]*")
# A row joins separate text blocks with ROW_GAP. Anything at least that wide
# is the edge of a column.
_COLUMN_GAP = re.compile(r"\s{%d,}" % len(ROW_GAP))
_MONEY = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)
_MONEY_SHAPE = re.compile(r"^(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d{1,2})?$", re.IGNORECASE)
_DATES = [re.compile(p) for p in DATE_PATTERNS]

# A label is a few words of text. More than this and the "label" is really a
# sentence the value happens to follow.
_MAX_LABEL_WORDS = 4
_MAX_LABEL_CHARS = 40

# Built-in fields a taught rule may replace, and what each is called on screen.
BUILTIN_FIELDS = {
    "customer_name": "Customer name",
    "invoice_number": "Document number",
    "invoice_date": "Date",
    "total_amount": "Amount (figures)",
    "amount_words": "Amount (in words)",
    "payment_mode": "Payment mode",
}

# The one field that is never a template value. See FieldRule.
RECIPIENT_FIELD = "customer_mobile"


def known_fields(profile=None) -> set[str]:
    """Every field name a template variable can be filled from on this install:
    the built-ins, the business name from settings, and every taught field."""
    names = set(BUILTIN_FIELDS) | {"business_name"}
    if profile is not None:
        for rules in profile.field_rules.values():
            names.update(r.name for r in rules if r.kind != "phone")
    return names


def field_name(text: str) -> str:
    """"Customer ID" -> "customer_id". What a person types, as a stable key."""
    name = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return name or "field"


def display_name(name: str) -> str:
    """"customer_id" -> "Customer id". The reverse, for the screen."""
    if name in BUILTIN_FIELDS:
        return BUILTIN_FIELDS[name]
    if name == RECIPIENT_FIELD:
        return "Customer mobile (who to send to)"
    return name.replace("_", " ").capitalize()


# -- learning ----------------------------------------------------------------


def guess_kind(value: str, country_code: str = "91") -> str:
    """What sort of value this looks like: phone, date, amount, code or text."""
    from .phone import normalize

    text = value.strip()
    digits = "".join(c for c in text if c.isdigit())
    if digits and len(digits) >= 10 and not re.search(r"[A-Za-z]", text):
        if normalize(digits, country_code):
            return "phone"
    if any(p.fullmatch(text) for p in _DATES):
        return "date"
    if _MONEY_SHAPE.match(text) and (
        re.search(r"[₹,.]|rs|inr", text, re.IGNORECASE)
    ):
        return "amount"
    if " " not in text and re.search(r"\d", text):
        return "code"
    return "text"


def infer_pattern(value: str) -> str:
    """A regex for values shaped like this one, whatever their length.

    "SCF-00418" -> literal SCF, a hyphen, variable digits. Lengths are left open, because
    member 12 and member 12345 are both members. Case is kept, because an
    identifier printed in capitals is always printed in capitals.
    """
    parts: list[str] = []
    for m in re.finditer(r"[A-Za-z]+|\d+|\s+|.", value.strip()):
        chunk = m.group()
        if chunk.isdigit():
            part = r"\d+"
        elif chunk.isalpha():
            part = re.escape(chunk)
        elif chunk.isspace():
            part = r"\s"
        else:
            part = re.escape(chunk)
        if not parts or parts[-1] != part:
            parts.append(part)
    body = "".join(parts)
    return rf"(?<![A-Za-z0-9])({body})(?![A-Za-z0-9])"


@dataclass
class Selection:
    """Words someone clicked on one row of a sample page."""

    page: Page
    row: TextRun
    first: int  # index into row.words
    last: int   # inclusive

    @property
    def span(self) -> tuple[int, int]:
        return self.row.offsets[self.first][0], self.row.offsets[self.last][1]

    @property
    def value(self) -> str:
        start, end = self.span
        return self.row.text[start:end].strip()


# A currency word between label and figure belongs to the value's formatting,
# not to the label: "Balance Due Rs. 12,450" is printed "Balance Due ₹12,450"
# by the next release of the same software.
_TRAILING_CURRENCY = re.compile(r"(?:\s+(?:rs|inr|₹))+\.?$", re.IGNORECASE)


def _clean_label(text: str) -> str:
    text = text.strip(" \t:.-–#=")
    text = _TRAILING_CURRENCY.sub("", text).strip(" \t:.-–#=")
    words = text.split()
    if len(words) > _MAX_LABEL_WORDS:
        words = words[-_MAX_LABEL_WORDS:]
    label = " ".join(words)
    if not re.search(r"[A-Za-z]{2,}", label) or len(label) > _MAX_LABEL_CHARS:
        return ""
    # A "label" that is mostly digits is another value, not a label.
    if sum(c.isdigit() for c in label) > len(label) / 3:
        return ""
    return label


def label_beside(selection: Selection) -> str:
    """The label printed to the left of the selection on the same row."""
    start, _end = selection.span
    before = selection.row.text[:start]
    for chunk in reversed(_COLUMN_GAP.split(before)):
        label = _clean_label(chunk)
        if label:
            return label
        if chunk.strip():
            return ""  # something that is not a label sits between them
    return ""


def label_above(selection: Selection) -> str:
    """A short heading directly above the selection, for stacked layouts."""
    row = selection.row
    words = row.words[selection.first : selection.last + 1]
    x0 = min(w.x0 for w in words)
    x1 = max(w.x1 for w in words)
    y0 = min(w.y0 for w in words)
    height = max(w.y1 - w.y0 for w in words) or 10.0
    above = [
        line for line in selection.page.lines
        if line.y1 <= y0 + 1
        and y0 - line.y1 <= height * 2.2
        and line.x0 <= x1 and line.x1 >= x0
    ]
    if not above:
        return ""
    nearest = max(above, key=lambda line: line.y1)
    return _clean_label(nearest.text)


def rule_from_selection(
    selection: Selection,
    name: str,
    kind: str | None = None,
    country_code: str = "91",
) -> FieldRule:
    """Turn a click on a sample into a rule that finds the same thing later."""
    value = selection.value
    kind = kind or guess_kind(value, country_code)
    label, where = label_beside(selection), "right"
    if not label:
        label = label_above(selection)
        where = "below" if label else "right"
    pattern = ""
    if kind == "code" or (not label and kind not in ("phone",)):
        pattern = infer_pattern(value)
    return FieldRule(name=name, label=label, kind=kind, pattern=pattern, where=where)


# -- reading -----------------------------------------------------------------


def _label_re(label: str) -> re.Pattern:
    words = [re.escape(w) for w in label.split()]
    return re.compile(
        r"(?<![A-Za-z0-9])" + r"\s+".join(words) + r"(?![A-Za-z0-9])", re.IGNORECASE
    )


def value_from(text: str, rule: FieldRule) -> str | None:
    """The value at the start of `text`, which is whatever follows the label."""
    rest = _SEPARATORS.sub("", text)
    column = _COLUMN_GAP.split(rest, maxsplit=1)[0].strip()
    if not column:
        return None
    if rule.kind == "text":
        return column.strip(" .,:-–") or None
    if rule.kind == "date":
        for pattern in _DATES:
            m = pattern.search(column)
            if m:
                return m.group(1)
        return None
    if rule.kind == "amount":
        m = _MONEY.search(column)
        return m.group(1) if m else None
    if rule.kind == "code":
        if rule.pattern:
            m = re.search(rule.pattern, column)
            if not m:
                return None
            return (m.group(1) if m.groups() else m.group()).strip()
        return column.split()[0]
    if rule.kind == "phone":
        m = re.search(r"\+?\d[\d \-]{8,}\d", column)
        return m.group().strip() if m else None
    return column


def _rows(doc: Document) -> list[tuple[Page, TextRun]]:
    return [(page, row) for page in doc.pages for row in page.rows]


def read_rule(doc: Document, rule: FieldRule) -> str | None:
    """The first value this rule finds in the document, in reading order."""
    if not rule.label:
        if not rule.pattern:
            return None
        pattern = re.compile(rule.pattern)
        for _page, row in _rows(doc):
            m = pattern.search(row.text)
            if m:
                return (m.group(1) if m.groups() else m.group()).strip()
        return None

    label = _label_re(rule.label)
    if rule.where == "below":
        for page in doc.pages:
            for line in page.lines:
                if not label.search(line.text):
                    continue
                height = (line.y1 - line.y0) or 10.0
                under = [
                    other for other in page.lines
                    if other.y0 >= line.y1 - 1
                    and other.y0 - line.y1 <= height * 2.2
                    and other.x0 <= line.x1 and other.x1 >= line.x0
                ]
                for other in sorted(under, key=lambda ln: ln.y0):
                    value = value_from(other.text, rule)
                    if value:
                        return value
        return None

    for _page, row in _rows(doc):
        for m in label.finditer(row.text):
            value = value_from(row.text[m.end():], rule)
            if value:
                return value
    return None


def apply_rules(doc: Document, rules: list[FieldRule]) -> dict[str, str]:
    """Every taught value found on the document, by field name.

    Phone rules are skipped: they feed the recipient scorer, not a template.
    """
    found: dict[str, str] = {}
    for rule in rules:
        if rule.kind == "phone":
            continue
        value = read_rule(doc, rule)
        if value:
            found[rule.name] = value
    return found
