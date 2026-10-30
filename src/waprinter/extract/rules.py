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
from dataclasses import dataclass, field

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

# Always words, whatever the clicked value looks like. "UPI/5512" guessed as a
# code would tie the rule to that shape, and "Cash" on the next print would
# read nothing.
ALWAYS_TEXT = {"customer_name", "payment_mode"}

# How far a value running over several lines may go when nothing ends it.
MAX_VALUE_LINES = 6


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
    # Further lines of the same value, top to bottom: (row, first, last).
    more: list[tuple[TextRun, int, int]] = field(default_factory=list)

    @property
    def span(self) -> tuple[int, int]:
        return self.row.offsets[self.first][0], self.row.offsets[self.last][1]

    @property
    def value(self) -> str:
        start, end = self.span
        parts = [self.row.text[start:end].strip()]
        parts += [_slice(row, first, last) for row, first, last in self.more]
        return " ".join(parts)

    @property
    def parts(self) -> list[tuple[TextRun, int, int]]:
        """Every line of the selection, first to last."""
        return [(self.row, self.first, self.last), *self.more]


def _slice(row: TextRun, first: int, last: int) -> str:
    return " ".join(row.text[row.offsets[first][0]:row.offsets[last][1]].split())


def _columns(row: TextRun) -> list[tuple[int, int]]:
    """A row's columns, as (first word, last word) pairs, left to right."""
    columns: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(row.words)):
        if row.offsets[index][0] - row.offsets[index - 1][1] >= len(ROW_GAP):
            columns.append((start, index - 1))
            start = index
    if row.words:
        columns.append((start, len(row.words) - 1))
    return columns


def _column_under(row: TextRun, left: float, right: float) -> tuple[int, int] | None:
    """The words of `row` printed under the horizontal range left..right.

    More than one column when a following line is itself a label and a value:
    "Transaction ID" and ": T998" are one line of the payment details.
    """
    under = [
        (first, last) for first, last in _columns(row)
        if row.words[last].x1 >= left and row.words[first].x0 < right
    ]
    return (under[0][0], under[-1][1]) if under else None


def selection_between(
    page: Page, rows: list[TextRun], start: tuple[int, int], end: tuple[int, int]
) -> Selection:
    """Everything from one clicked word to another, in reading order.

    `start` and `end` are (row, word) indexes into `rows`. On one row that is
    the words between them. Across rows it is the value's own column on each
    row, so a total printed to the right of the value does not come along.
    """
    start, end = sorted((start, end))
    first_row = rows[start[0]]
    if start[0] == end[0]:
        return Selection(page, first_row, start[1], end[1])

    column_end = next(
        last for first, last in _columns(first_row) if first <= start[1] <= last
    )
    end_row, end_word = rows[end[0]], end[1]
    left = min(first_row.words[start[1]].x0, end_row.words[end_word].x0)
    right = max(w.x1 for w in first_row.words[start[1]:column_end + 1])
    more: list[tuple[TextRun, int, int]] = []
    for row in rows[start[0] + 1 : end[0]]:
        column = _column_under(row, left, right)
        if column:
            more.append((row, *column))
    first, _last = next(
        (f, l) for f, l in _columns(end_row) if f <= end_word <= l
    )
    more.append((end_row, first, end_word))
    return Selection(page, first_row, start[1], column_end, more)


# A currency word between label and figure belongs to the value's formatting,
# not to the label: "Balance Due Rs. 12,450" is printed "Balance Due ₹12,450"
# by the next release of the same software.
_TRAILING_CURRENCY = re.compile(r"(?:\s+(?:rs|inr|₹))+\.?$", re.IGNORECASE)


_SEPARATOR_CHARS = " \t:.-–#="


def _clean_label(text: str) -> str:
    text = text.strip(_SEPARATOR_CHARS)
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


def label_from(chunk: str) -> str:
    """The label a run of text ends with, or "".

    Usually the whole run. "140.00 Paymode" is a value and the next label set
    as one block, and only the words after the last figure are the label.
    """
    label = _clean_label(chunk)
    if label:
        return label
    words: list[str] = []
    for word in reversed(chunk.split()):
        if re.search(r"\d", word):
            break
        words.insert(0, word)
    return _clean_label(" ".join(words))


def label_at(row: TextRun, word: int) -> str:
    """The label someone pointed at: its column on this row, up to that word."""
    first = next(f for f, l in _columns(row) if f <= word <= l)
    return label_from(_slice(row, first, word))


def label_beside(selection: Selection) -> str:
    """The label printed to the left of the selection on the same row."""
    start, _end = selection.span
    before = selection.row.text[:start]
    for chunk in reversed(_COLUMN_GAP.split(before)):
        if not chunk.strip(_SEPARATOR_CHARS):
            continue  # nothing, or a ":" printed as a block of its own
        return label_from(chunk)  # "" when what sits between is not a label
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


def label_after(selection: Selection, label: str = "") -> str:
    """The first label printed under the selection's last line, or "".

    What ends a value that runs over several lines: "Paid Amt" is printed
    below the payment details however many lines of them a print has. Looked
    for from where this value's own label starts, because on the next row the
    label is to the left and what sits under the value is that row's value.
    """
    row, first, last = selection.parts[-1]
    left = min(w.x0 for w in row.words[first : last + 1])
    right = max(w.x1 for w in row.words[first : last + 1])
    found = _label_re(label).search(selection.row.text) if label else None
    if found:
        left = min(w.x0 for w in selection.row.words_covering(found.start(), found.end()))
    below = sorted((r for r in selection.page.rows if r.y0 >= row.y1 - 1), key=lambda r: r.y0)
    for other in below[:MAX_VALUE_LINES]:
        for column_first, column_last in _columns(other):
            if other.words[column_last].x1 < left or other.words[column_first].x0 >= right:
                continue
            text = _slice(other, column_first, column_last)
            head = text.split(":", 1)[0] if ":" in text else " ".join(
                word for word in text.split()[:_MAX_LABEL_WORDS] if not re.search(r"\d", word)
            )
            head = _clean_label(head)
            if head:
                return head
    return ""


def literal_only(rule: FieldRule) -> bool:
    """Whether this rule can only ever find the exact words it was taught on.

    With no label and no figures, the pattern is the clicked word itself:
    taught on "Cash", it finds nothing on a print that says "UPI".
    """
    return not rule.label and rule.kind != "phone" and r"\d" not in rule.pattern


def rule_from_selection(
    selection: Selection,
    name: str,
    kind: str | None = None,
    country_code: str = "91",
    multiline: bool = False,
    optional: bool = False,
    label: str | None = None,
    where: str = "right",
) -> FieldRule:
    """Turn a click on a sample into a rule that finds the same thing later.

    `label` is one someone pointed at, which beats looking for one.
    """
    value = selection.value
    multiline = (multiline or bool(selection.more)) and kind != "phone"
    if multiline or name in ALWAYS_TEXT:
        kind = "text"
    kind = kind or guess_kind(value, country_code)
    if label is None:
        label, where = label_beside(selection), "right"
        if not label:
            label = label_above(selection)
            where = "below" if label else "right"
    # A label says where the value is, so its shape can vary: one payment's
    # reference is "T9981234" and the next is "UTR/4471". A code with no figures
    # in it has nothing else to tell it from the words around it.
    pattern = ""
    if (not label and kind != "phone") or (kind == "code" and not re.search(r"\d", value)):
        pattern = infer_pattern(value)
    return FieldRule(
        name=name, label=label, kind=kind, pattern=pattern, where=where,
        multiline=multiline and bool(label),
        until=label_after(selection, label) if multiline and label else "",
        optional=optional,
    )


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
        token = column.split()[0]
        # What follows the label is only a code if it looks like one. A heading
        # such as "Bill No Details" moves on to the next place the label appears.
        return token if re.search(r"\d", token) else None
    if rule.kind == "phone":
        m = re.search(r"\+?\d[\d \-]{8,}\d", column)
        return m.group().strip() if m else None
    return column


def _rows(doc: Document) -> list[tuple[Page, TextRun]]:
    return [(page, row) for page in doc.pages for row in page.rows]


def _read_lines(doc: Document, rule: FieldRule) -> str | None:
    """A value that may run over several lines, down to the label that ends it."""
    label = _label_re(rule.label)
    stop = _label_re(rule.until) if rule.until else None
    for page in doc.pages:
        rows = page.rows
        for index, row in enumerate(rows):
            m = label.search(row.text)
            if not m:
                continue
            found = row.words_covering(m.start(), m.end())
            left = min(w.x0 for w in found)
            parts: list[str] = []
            if rule.where == "below":
                right = max(w.x1 for w in found)
            else:
                rest = row.text[m.end():]
                value = value_from(rest, rule)
                if not value:
                    continue
                parts.append(value)
                # The right edge of the value's column, which is how far in a
                # following line may start and still be part of it.
                start = m.end() + len(rest) - len(_SEPARATORS.sub("", rest))
                gap = _COLUMN_GAP.search(row.text, start)
                column = row.words_covering(start, gap.start() if gap else len(row.text))
                right = max(w.x1 for w in column)
            above = row
            for following in rows[index + 1:]:
                height = (above.y1 - above.y0) or 10.0
                column = _column_under(following, left, right)
                if (
                    column is None
                    or following.y0 - above.y1 > height * 2.2
                    or len(parts) >= MAX_VALUE_LINES
                ):
                    break
                text = _slice(following, *column)
                if stop and stop.search(text):
                    break
                parts.append(text)
                above = following
            if parts:
                return " ".join(parts).strip(" .,:-–") or None
    return None


def read_rule(doc: Document, rule: FieldRule) -> str | None:
    """The first value this rule finds in the document, in reading order."""
    if rule.multiline and rule.label:
        return _read_lines(doc, rule)
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
