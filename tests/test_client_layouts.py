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
