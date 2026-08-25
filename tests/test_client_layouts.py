"""Extraction against the shapes real clients actually print.

The synthetic tax invoice in the rest of the suite is not what the first client
prints. This file covers the chit fund receipt layout, and exists to keep both
readable from one set of defaults — the whole point of profile.py.

Data here is invented. The real receipts carry a member's name and personal
mobile number and are kept out of the repository.
"""

from __future__ import annotations

import pytest
from invoice_factory import ChitReceiptSpec, InvoiceSpec, build_chit_receipt

from waprinter.extract import extract_fields
from waprinter.extract.profile import DocumentProfile
from waprinter.models import Confidence

# The chit fund's own office landline, which appears in the letterhead.
OFFICE = "08782251999"


@pytest.fixture
def receipt(tmp_path):
    def _make(spec: ChitReceiptSpec | None = None, name: str = "receipt.pdf"):
        return build_chit_receipt(spec or ChitReceiptSpec(), tmp_path / name)

    return _make


class TestChitFundReceipt:
    def test_the_members_mobile_is_found(self, receipt):
        fields = extract_fields(receipt())
        assert fields.best is not None
        assert fields.best.e164 == "+919000012345"

    def test_the_members_mobile_beats_the_office_number(self, receipt):
        # Both are labelled phone numbers. The office one is in the letterhead,
        # so it must not be the one offered to the operator.
        fields = extract_fields(receipt())
        high = [c for c in fields.candidates if c.confidence is Confidence.HIGH]
        assert [c.e164 for c in high] == ["+919000012345"]

    def test_the_unlabelled_receipt_number_is_read(self, receipt):
        # "CR1747/26" sits alone with no "Receipt No:" in front of it.
        assert extract_fields(receipt()).invoice_number == "CR1747/26"

    def test_the_alphabetic_month_date_is_read(self, receipt):
        assert extract_fields(receipt()).invoice_date == "13-Aug-26"

    def test_a_street_address_is_not_mistaken_for_a_date(self, receipt):
        # Regression: "H.No. 2-7-384" parsed as a date and would have gone out
        # in the customer's message.
        assert extract_fields(receipt()).invoice_date != "2-7-384"

    def test_the_member_name_is_read_from_the_sri_smt_anchor(self, receipt):
        assert extract_fields(receipt()).customer_name == "ANITHA RAMESH"

    def test_the_name_does_not_swallow_the_phone_number(self, receipt):
        # Name and mobile share a line; without trimming, the message greets
        # the customer with their own phone number.
        name = extract_fields(receipt()).customer_name
        assert "9000012345" not in name
        assert "Mobile" not in name

    def test_a_receipt_with_no_mobile_yields_no_recipient(self, receipt):
        fields = extract_fields(receipt(ChitReceiptSpec(member_phone=None)))
        high = [c for c in fields.candidates if c.confidence is Confidence.HIGH]
        assert high == []

    def test_the_receipt_has_a_text_layer_so_ocr_never_runs(self, receipt):
        fields = extract_fields(receipt())
        assert fields.has_text_layer is True
        assert fields.used_ocr is False


class TestOneProfileReadsBothLayouts:
    """A single default profile must handle every client seen so far."""

    def test_tax_invoice_still_works(self, make_invoice):
        fields = extract_fields(make_invoice(InvoiceSpec()))
        assert fields.invoice_number == "INV-2291"
        assert fields.customer_name == "Meghana Enterprises"
        assert fields.total_amount == "18,450.00"
        assert fields.best.e164 == "+919876543210"

    def test_chit_receipt_works_with_the_same_defaults(self, receipt):
        fields = extract_fields(receipt())
        assert fields.invoice_number == "CR1747/26"
        assert fields.customer_name == "ANITHA RAMESH"
        assert fields.best.e164 == "+919000012345"


class TestProfileConfiguration:
    def test_a_client_can_add_their_own_label(self, receipt, tmp_path):
        spec = ChitReceiptSpec(member_label="Chandadaru :", phone_label="Cell No :")
        # Unknown anchor, so the stock profile cannot find the name.
        assert extract_fields(receipt(spec, "odd.pdf")).customer_name != "ANITHA RAMESH"

        profile = DocumentProfile(
            name="telugu-chits",
            customer_anchors=[*DocumentProfile().customer_anchors, "chandadaru"],
        )
        fields = extract_fields(receipt(spec, "odd2.pdf"), profile=profile)
        assert fields.customer_name == "ANITHA RAMESH"

    def test_a_client_can_add_an_unlabelled_number_pattern(self, receipt, tmp_path):
        spec = ChitReceiptSpec(receipt_number="RCPT-99-2026")
        profile = DocumentProfile(
            name="custom", document_number_patterns=[r"\b(RCPT-\d+-\d+)\b"]
        )
        fields = extract_fields(receipt(spec, "custom.pdf"), profile=profile)
        assert fields.invoice_number == "RCPT-99-2026"

    def test_a_profile_round_trips_through_disk(self, tmp_path):
        path = tmp_path / "profiles.json"
        DocumentProfile(name="acme", phone_labels=["mobile", "cell"]).save(path)
        loaded = DocumentProfile.load(path)
        assert loaded.name == "acme"
        assert loaded.phone_labels == ["mobile", "cell"]
        # Unspecified keys fall back to the built-in defaults.
        assert "bill to" in loaded.customer_anchors

    def test_a_broken_profile_file_does_not_stop_the_printer(self, tmp_path):
        path = tmp_path / "profiles.json"
        path.write_text("{ not valid json", encoding="utf-8")
        assert DocumentProfile.load(path).name == "default"

    def test_a_missing_profile_file_uses_defaults(self, tmp_path):
        assert DocumentProfile.load(tmp_path / "nope.json").name == "default"


class TestPreprintedStationery:
    """A form whose wording is printed on the paper, not by the software.

    The client's receipts are like this: the PDF text layer holds only the
    filled-in fields, so the pre-printed words ("Received from", "an amount of
    Rupees") appear only when the page is read by OCR. That made the bug
    invisible on the text path and guaranteed on the scanned one.
    """

    def test_the_name_is_read_when_two_anchors_share_a_row(self, receipt):
        # "Received from" and "Sri/Smt/M/s" both match. The first has nothing
        # after it; the last has the name.
        fields = extract_fields(receipt(ChitReceiptSpec(preprinted_wording=True)))
        assert fields.customer_name == "ANITHA RAMESH"

    def test_boilerplate_below_the_anchor_is_not_a_name(self, receipt):
        # Regression: the message greeted the member as "an amount of Rupees".
        fields = extract_fields(receipt(ChitReceiptSpec(preprinted_wording=True)))
        assert "amount" not in (fields.customer_name or "").lower()
        assert "rupees" not in (fields.customer_name or "").lower()

    def test_the_name_is_read_when_it_is_a_separate_line_at_the_same_height(
        self, receipt
    ):
        # At 300 dpi Tesseract split "Sri/Smt/M/s . NAME" into two lines, which
        # left the anchor with nothing beside it. Rows put them back together.
        spec = ChitReceiptSpec(preprinted_wording=True, split_name_line=True)
        assert extract_fields(receipt(spec, "split.pdf")).customer_name == "ANITHA RAMESH"

    def test_the_recipient_is_still_correct(self, receipt):
        spec = ChitReceiptSpec(preprinted_wording=True, split_name_line=True)
        fields = extract_fields(receipt(spec, "split2.pdf"), excluded_numbers={"+91" + OFFICE[1:]})
        assert fields.best.e164 == "+919000012345"


class TestTwoColumnInvoiceStillWorks:
    def test_the_invoice_number_is_not_mistaken_for_the_customer(self, make_invoice):
        # The "Bill To" row also carries "Invoice No: INV-2291" from the right
        # column, joined into one row. Reading the row must not return that.
        fields = extract_fields(make_invoice(InvoiceSpec()))
        assert fields.customer_name == "Meghana Enterprises"
        assert "INV-2291" not in fields.customer_name


class TestGarbledOcrNames:
    """A poor scan mangles the anchor, and the fragment lands in the greeting."""

    def test_debris_in_front_of_a_name_is_dropped(self):
        # Tesseract reads "Sri/Smt/M/s" as "'M’s" at low resolution. Without
        # this the member is greeted as "Dear 'M’s . ANITHA RAMESH".
        from waprinter.extract.fields import _strip_leading_junk

        assert _strip_leading_junk("'M’s   . ANITHA RAMESH") == "ANITHA RAMESH"

    def test_a_clean_name_is_untouched(self):
        from waprinter.extract.fields import _strip_leading_junk

        assert _strip_leading_junk("ANITHA RAMESH") == "ANITHA RAMESH"

    def test_initials_survive(self):
        # A period is not junk; "R. K. SHARMA" is a name.
        from waprinter.extract.fields import _strip_leading_junk

        assert _strip_leading_junk("R. K. SHARMA") == "R. K. SHARMA"
