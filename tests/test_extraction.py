"""Extraction against realistic invoice layouts."""

from __future__ import annotations

from invoice_factory import InvoiceSpec

from waprinter.extract import extract_fields
from waprinter.models import Confidence

SELLER = {"+919845012345"}


def fields_for(make_invoice, spec=None, excluded=SELLER):
    return extract_fields(make_invoice(spec), excluded_numbers=excluded)


class TestHappyPath:
    def test_reads_every_field(self, make_invoice):
        f = fields_for(make_invoice)
        assert f.invoice_number == "INV-2291"
        assert f.customer_name == "Meghana Enterprises"
        assert f.invoice_date == "12/05/2026"
        assert f.total_amount == "18,450.00"
        assert f.page_count == 1
        assert f.has_text_layer is True

    def test_finds_the_customer_number_with_high_confidence(self, make_invoice):
        f = fields_for(make_invoice)
        assert f.best.e164 == "+919876543210"
        assert f.best.confidence is Confidence.HIGH

    def test_customer_name_is_not_taken_from_the_opposite_column(self, make_invoice):
        # "Bill To:" and "Invoice No:" sit at the same height. The name must
        # come from the Bill To column, not from whichever line the PDF
        # happened to emit next.
        f = fields_for(make_invoice)
        assert "Invoice" not in (f.customer_name or "")

    def test_grand_total_wins_over_line_item_amounts(self, make_invoice):
        # 13,440.00 and 3,450.00 appear earlier on the page as line items.
        assert fields_for(make_invoice).total_amount == "18,450.00"

    def test_alternative_customer_headings(self, make_invoice):
        for heading in ("Consignee", "Buyer", "Billed To", "Customer"):
            spec = InvoiceSpec(customer_block_label=heading)
            f = fields_for(make_invoice, spec)
            assert f.best is not None, heading
            assert f.best.confidence is Confidence.HIGH, heading

    def test_alternative_phone_labels(self, make_invoice):
        for label in ("Mobile", "Mob", "Phone", "Ph", "Contact No", "Cell", "WhatsApp"):
            spec = InvoiceSpec(customer_phone_label=label)
            f = fields_for(make_invoice, spec)
            assert f.best is not None, label
            assert f.best.e164 == "+919876543210", label
            assert f.best.confidence is Confidence.HIGH, label


class TestSellerNumbersAreNotRecipients:
    def test_own_numbers_are_excluded_outright(self, make_invoice):
        f = fields_for(make_invoice, InvoiceSpec(customer_phone=None))
        assert f.candidates == []

    def test_letterhead_number_is_never_high_confidence(self, make_invoice):
        # Even with the blocklist misconfigured, the seller's own letterhead
        # number must not be good enough to send to silently.
        f = fields_for(make_invoice, InvoiceSpec(customer_phone=None), excluded=set())
        assert f.candidates, "expected the seller number to be seen, just not trusted"
        assert all(c.confidence is not Confidence.HIGH for c in f.candidates)

    def test_footer_number_is_low_confidence(self, make_invoice):
        spec = InvoiceSpec(customer_phone=None, seller_phone=None)
        f = fields_for(make_invoice, spec, excluded=set())
        footer = next(c for c in f.candidates if c.e164 == "+919845012345")
        assert footer.confidence is Confidence.LOW
        assert any("footer" in r for r in footer.reasons)


class TestNumbersThatAreNotPhoneNumbers:
    def test_bank_account_number_is_not_a_recipient(self, make_invoice):
        spec = InvoiceSpec(
            customer_phone=None,
            extra_body_lines=["Bank: HDFC  A/c No: 9876501234  IFSC: HDFC0001234"],
        )
        f = fields_for(make_invoice, spec)
        assert "+919876501234" not in [c.e164 for c in f.candidates]

    def test_gstin_digits_are_not_a_recipient(self, make_invoice):
        f = fields_for(make_invoice, InvoiceSpec(customer_phone=None))
        assert f.candidates == []

    def test_invoice_number_that_looks_like_a_mobile(self, make_invoice):
        spec = InvoiceSpec(customer_phone=None, invoice_number="9876543211")
        f = fields_for(make_invoice, spec)
        assert "+919876543211" not in [c.e164 for c in f.candidates]

    def test_a_landline_in_the_customer_block_is_not_offered(self, make_invoice):
        spec = InvoiceSpec(customer_phone="080-25551234")
        f = fields_for(make_invoice, spec)
        assert f.candidates == []


class TestAmbiguity:
    def test_transporter_number_produces_a_second_candidate(self, make_invoice):
        spec = InvoiceSpec(
            extra_customer_lines=["Transporter Mobile: 9812345678"],
        )
        f = fields_for(make_invoice, spec)
        highs = [c for c in f.candidates if c.confidence is Confidence.HIGH]
        # Both are labelled and both sit in the customer block, so neither can
        # be preferred. The gate turns this into a hold.
        assert len(highs) >= 2


class TestRasterPrint:
    def test_image_only_page_is_flagged(self, make_invoice):
        f = fields_for(make_invoice, InvoiceSpec(raster=True))
        assert f.has_text_layer is False
        assert f.candidates == []
