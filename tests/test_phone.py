"""Unit tests for number normalisation and the digit scanner.

These are the rules that decide who receives a customer's invoice, so the
emphasis is on what must be *rejected*.
"""

from __future__ import annotations

import pytest

from waprinter.extract.phone import (
    _find_raw_numbers,
    _nearest_label,
    normalize,
    parse_typed_number,
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("9876543210", "+919876543210"),
            ("919876543210", "+919876543210"),      # country code
            ("09876543210", "+919876543210"),       # trunk prefix
            ("0919876543210", "+919876543210"),     # both
            ("6123456789", "+916123456789"),        # 6-series is valid
            ("7123456789", "+917123456789"),
            ("8123456789", "+918123456789"),
        ],
    )
    def test_accepts_indian_mobiles(self, raw, expected):
        assert normalize(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "5123456789",       # mobiles never start below 6
            "1234567890",
            "987654321",        # 9 digits
            "98765432101",      # 11 digits, no recognised prefix
            "123456789012345",  # far too long
            "",
        ],
    )
    def test_rejects_non_mobiles(self, raw):
        assert normalize(raw) is None


class TestDigitScanner:
    def test_finds_a_plain_number(self):
        found = _find_raw_numbers("Mobile: 9876543210")
        assert [f[2] for f in found] == ["9876543210"]

    def test_finds_a_number_at_end_of_text(self):
        # Regression: an empty "next character" once tested as a sticky char,
        # which discarded every number ending at a boundary.
        assert _find_raw_numbers("Contact 9876543210")

    def test_joins_across_a_single_space_or_hyphen(self):
        assert [f[2] for f in _find_raw_numbers("98765 43210")] == ["9876543210"]
        assert [f[2] for f in _find_raw_numbers("98765-43210")] == ["9876543210"]

    def test_does_not_join_across_a_date_separator(self):
        assert normalize_all("Invoice Date 12/05/2026") == []

    def test_does_not_join_across_an_amount_separator(self):
        assert normalize_all("Grand Total 1,23,456.78") == []

    def test_does_not_fuse_across_a_column_gap(self):
        # Columns are joined with three spaces precisely so a PIN code and a
        # neighbouring figure cannot merge into a plausible mobile.
        assert normalize_all("Bengaluru 560010   Qty 4567") == []

    @pytest.mark.parametrize(
        "text",
        [
            "Ph: 080-25551234",     # Bengaluru landline
            "Tel 011-23456789",     # Delhi landline
            "Phone: 0422 2345678",  # Coimbatore landline
        ],
    )
    def test_rejects_std_code_landlines(self, text):
        # Stripping the trunk zero from "080-25551234" leaves "8025551234",
        # which passes every mobile format test. The grouping is the giveaway.
        assert normalize_all(text) == []

    def test_still_accepts_a_mobile_written_with_a_trunk_prefix(self):
        assert normalize_all("Mobile: 09876543210") == ["+919876543210"]


class TestTypedNumbers:
    """What an operator is allowed to type into the send dialog."""

    @pytest.mark.parametrize(
        "typed",
        [
            "9876543210",
            "98765 43210",
            "98765-43210",
            "+91 98765 43210",
            "+919876543210",
            "09876543210",
            " 9876543210 ",
        ],
    )
    def test_accepts_the_ways_people_write_a_mobile(self, typed):
        assert parse_typed_number(typed) == "+919876543210"

    @pytest.mark.parametrize(
        "typed",
        [
            "",
            "12345",
            "080-25551234",   # landline: stripping separators would pass it
            "011 23456789",
            "5123456789",
            "not a number",
        ],
    )
    def test_rejects_what_whatsapp_cannot_reach(self, typed):
        assert parse_typed_number(typed) is None

    def test_rejects_two_numbers_at_once(self):
        assert parse_typed_number("9876543210 9812345678") is None

    def test_agrees_with_the_page_scanner(self):
        # The dialog must not accept something the extractor refuses to read.
        assert parse_typed_number("080-25551234") is None
        assert normalize_all("Ph: 080-25551234") == []


class TestNearestLabel:
    def test_phone_label(self):
        assert _nearest_label("Mobile: ") == ("Mobile", True)
        assert _nearest_label("Contact No.: ") == ("Contact No.", True)

    def test_non_phone_label(self):
        label, is_phone = _nearest_label("A/c No: ")
        assert is_phone is False
        label, is_phone = _nearest_label("GSTIN: ")
        assert is_phone is False

    def test_nearest_label_wins_on_a_shared_row(self):
        # "Invoice No: 4471   Mobile: " — the number that follows belongs to
        # the label immediately before it, not the first label on the row.
        _label, is_phone = _nearest_label("Invoice No: 4471   Mobile: ")
        assert is_phone is True

    def test_a_label_further_up_the_row_does_not_attach(self):
        # Nothing immediately precedes the number, so it gets no label bonus.
        assert _nearest_label("GSTIN: 29AACCM9910C1ZQ   ") == (None, False)


def normalize_all(text: str) -> list[str]:
    """Every digit run in `text` that survives normalisation."""
    out = []
    for _start, _end, digits in _find_raw_numbers(text):
        e164 = normalize(digits)
        if e164 and e164 not in out:
            out.append(e164)
    return out
