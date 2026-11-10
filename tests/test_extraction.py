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


class TestDriverPunctuationIsFoldedBack:
    """The Windows 7 queue renders plain ASCII as typographic punctuation.

    There is no inbox PDF driver before Windows 10, so that queue is built on
    the XPS Document Writer, whose font substitutes for plain ASCII. Printing
    an invoice through it on a Windows runner brought the number back as
    "INV<something>2291" rather than "INV-2291". Unfolded, the same document
    would extract differently depending on which Windows printed it.

    There is no page-level test here on purpose: PyMuPDF's base-14 fonts
    cannot carry these characters -- insert_text turns every one of them into
    U+00B7 -- so a synthetic PDF would prove nothing about a real driver. The
    faithful check is packaging/check_xps_capture.py, which prints through the
    actual queue on a Windows runner and reports what it sees.
    """

    def test_the_soft_hyphen_the_xps_driver_emits_reads_as_a_hyphen(self):
        """U+00AD is the one actually observed coming off the XPS queue.

        Dropping it instead -- which is what a soft hyphen usually warrants,
        being an invisible hint about where a word may break -- would read the
        number as "INV2291". What is on the page is a hyphen.
        """
        from waprinter.extract.pdf_text import fold_punctuation

        assert fold_punctuation("INV\u00ad2291") == "INV-2291"

    def test_a_non_breaking_hyphen_reads_as_a_hyphen(self):
        from waprinter.extract.pdf_text import fold_punctuation

        assert fold_punctuation("INV\u20112291") == "INV-2291"

    def test_every_dash_the_drivers_use_folds(self):
        from waprinter.extract.pdf_text import fold_punctuation

        for dash in "\u00ad\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d":
            assert fold_punctuation("INV{}2291".format(dash)) == "INV-2291"

    def test_plain_ascii_is_untouched(self):
        from waprinter.extract.pdf_text import fold_punctuation

        text = "INV-2291 Mobile: 9876543210 Total: 18,450.00"
        assert fold_punctuation(text) == text

    def test_reading_a_page_applies_the_fold(self):
        """The fold has to be in the path read() takes, not just available."""
        from waprinter.extract.pdf_text import _words_from

        raw = [(0.0, 0.0, 10.0, 10.0, "INV\u20112291", 0, 0, 0)]
        grouped = _words_from(raw, page_number=1, from_ocr=False)
        words = [w for ws in grouped.values() for w in ws]
        assert [w.text for w in words] == ["INV-2291"]


class TestPaymentModeInATallBox:
    """Form XVI prints the mode at the top of a box taller than its label.

    CR2009/26 was held for want of a payment mode although "Cash" sits
    plainly beside "Mode of Payment". Raised far enough, the value joins the
    row above when rows are grouped by height, and is lost in the middle of
    "Sub Total c/d   Cash   2.00   Other Charges   0.00".
    """

    @staticmethod
    def form(tmp_path, mode="Cash", raised=20, labels=True, above="Sub Total c/d"):
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 80), "SRINIDHI CHITS (HYDERABAD) PVT. LTD.", fontsize=13)
        page.insert_text((60, 100), "CR2009/26   29-Oct-26   SPT27E-4", fontsize=10)
        if labels:
            page.insert_text((60, 470), above, fontsize=10, fontname="hebo")
            page.insert_text((330, 470), "Other Charges", fontsize=10, fontname="hebo")
            page.insert_text((60, 492), "Mode of Payment", fontsize=10, fontname="hebo")
            page.insert_text((330, 512), "Grand Total Rs.", fontsize=10, fontname="hebo")
        page.insert_text((250, 470), "2.00", fontsize=10)
        page.insert_text((500, 470), "0.00", fontsize=10)
        if mode:
            page.insert_text((180, 492 - raised), mode, fontsize=9)
        page.insert_text((500, 512), "2.00", fontsize=10)
        path = tmp_path / "form.pdf"
        doc.save(path)
        doc.close()
        return extract_fields(path)

    def test_a_mode_level_with_its_label_is_read(self, tmp_path):
        assert self.form(tmp_path, raised=0).payment_mode == "Cash"

    def test_a_mode_raised_into_the_row_above_is_read(self, tmp_path):
        assert self.form(tmp_path, raised=20).payment_mode == "Cash"

    def test_a_raised_cheque_is_read(self, tmp_path):
        f = self.form(tmp_path, mode="Cheque Number 351768", raised=20)
        assert f.payment_method == "Cheque"

    def test_a_form_without_its_labels_in_the_text_still_reads_it(self, tmp_path):
        """The chit software can leave the stationery wording out of the text."""
        assert self.form(tmp_path, raised=20, labels=False).payment_mode == "Cash"

    def test_cash_inside_another_label_is_not_a_payment_mode(self, tmp_path):
        f = self.form(tmp_path, mode=None, above="Cash Discount")
        assert f.payment_mode is None


class TestPaymentModeReadByOcr:
    """The counter's print queue saves a Crystal Reports receipt as a picture.

    OCR read CR2009/26's payment cell at 300 dpi as "=f] Mode   of Payment =»
    Cash": the label spaced out, and specks of the box's border between it and
    the value. The receipt was held for want of a payment mode on every print.
    """

    def test_the_line_ocr_actually_produced_is_read(self):
        from waprinter.extract.profile import DocumentProfile

        m = DocumentProfile().payment_mode_labelled_re.search(
            "=f] Mode   of Payment =» Cash"
        )
        assert m and m.group(1) == "Cash"

    def test_a_spaced_out_label_on_the_page_is_read(self, tmp_path):
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 80), "SRINIDHI CHITS (HYDERABAD) PVT. LTD.", fontsize=13)
        page.insert_text((60, 100), "Receipt No CR2009/26 Date 29-Oct-26", fontsize=10)
        # Two runs on one baseline, as OCR returns them: joined with a gap.
        page.insert_text((60, 300), "Mode", fontsize=10)
        page.insert_text((100, 300), "of Payment =» Cash", fontsize=10)
        path = tmp_path / "ocr-like.pdf"
        doc.save(path)
        doc.close()
        assert extract_fields(path).payment_mode == "Cash"

    def test_the_word_cashier_is_not_a_payment_mode(self):
        from waprinter.extract.profile import DocumentProfile

        assert not DocumentProfile().payment_mode_labelled_re.search(
            "Mode of Payment   Cashier / Receiving Clerk"
        )
