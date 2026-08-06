"""OCR fallback for invoices printed as an image.

Tesseract is a hard dependency of these tests. Where it is absent they skip,
except for the tests that specifically cover the absent case.
"""

from __future__ import annotations

import pytest
from invoice_factory import InvoiceSpec

from waprinter.extract import pdf_text
from waprinter.extract.fields import extract_fields
from waprinter.extract.ocr import OcrSettings, available, find_tessdata
from waprinter.extract.phone import apply_ocr_verification, scan_numbers
from waprinter.models import Confidence, PhoneCandidate

OCR = OcrSettings()
needs_tesseract = pytest.mark.skipif(
    not available(OCR), reason="Tesseract is not installed"
)

SELLER = {"+919845012345"}


@pytest.fixture
def scanned(make_invoice):
    return make_invoice(InvoiceSpec(raster=True))


class TestAvailability:
    def test_reports_whether_tesseract_is_installed(self):
        assert available(OCR) is (find_tessdata(None) is not None)

    def test_disabled_setting_means_unavailable(self):
        assert available(OcrSettings(enabled=False)) is False

    def test_a_bad_tessdata_path_is_not_available(self):
        assert available(OcrSettings(tessdata="/nonexistent/tessdata")) is False


class TestWithoutOcr:
    def test_a_scanned_page_yields_nothing(self, scanned):
        # ocr=None — the behaviour before this feature existed.
        fields = extract_fields(scanned, excluded_numbers=SELLER)
        assert fields.has_text_layer is False
        assert fields.used_ocr is False
        assert fields.readable is False
        assert fields.candidates == []

    def test_the_reason_is_reported_for_the_operator(self, scanned):
        fields = extract_fields(scanned, ocr=OcrSettings(enabled=False))
        assert fields.ocr_error
        assert "switched off" in fields.ocr_error

    def test_a_missing_tesseract_explains_itself(self, scanned):
        fields = extract_fields(scanned, ocr=OcrSettings(tessdata="/nope"))
        assert fields.readable is False
        assert "Tesseract" in fields.ocr_error


@needs_tesseract
class TestReadingAScannedPage:
    def test_text_is_recovered(self, scanned):
        doc = pdf_text.read(scanned, ocr=OCR)
        assert doc.used_ocr
        assert doc.ocr_pages == {1}
        assert doc.has_text_layer is False  # there was no native text
        assert len(doc.pages[0].words) > 20

    def test_geometry_survives_so_scoring_still_works(self, scanned):
        # The point of OCRing through PyMuPDF: boxes come back in PDF
        # coordinates, so the Bill To block and footer penalties still apply.
        doc = pdf_text.read(scanned, ocr=OCR)
        page = doc.pages[0]
        assert page.width == pytest.approx(595, abs=2)   # A4
        assert page.height == pytest.approx(842, abs=2)
        assert all(0 <= w.x0 <= page.width for w in page.words)

    def test_fields_are_extracted(self, scanned):
        fields = extract_fields(scanned, excluded_numbers=SELLER, ocr=OCR)
        assert fields.used_ocr is True
        assert fields.readable is True
        assert fields.invoice_number == "INV-2291"
        assert fields.customer_name == "Meghana Enterprises"

    def test_the_customer_number_is_found(self, scanned):
        fields = extract_fields(scanned, excluded_numbers=SELLER, ocr=OCR)
        assert fields.best is not None
        assert fields.best.e164 == "+919876543210"

    def test_candidates_are_marked_as_ocr_derived(self, scanned):
        fields = extract_fields(scanned, excluded_numbers=SELLER, ocr=OCR)
        assert fields.best.from_ocr is True
        assert any("OCR" in r for r in fields.best.reasons)

    def test_a_confirmed_number_says_so(self, scanned):
        fields = extract_fields(scanned, excluded_numbers=SELLER, ocr=OCR)
        assert any("confirmed by a second OCR pass" in r for r in fields.best.reasons)

    def test_the_sellers_number_is_still_excluded(self, scanned):
        fields = extract_fields(scanned, excluded_numbers=SELLER, ocr=OCR)
        assert "+919845012345" not in [c.e164 for c in fields.candidates]

    def test_a_page_with_real_text_is_not_ocred(self, make_invoice):
        doc = pdf_text.read(make_invoice(), ocr=OCR)
        assert doc.used_ocr is False
        assert doc.ocr_pages == set()
        assert doc.has_text_layer is True


class TestDoubleReadVerification:
    def _candidate(self, e164: str, confidence=Confidence.HIGH, score=96):
        return PhoneCandidate(
            raw=e164,
            e164=e164,
            score=score,
            confidence=confidence,
            page=1,
            bbox=(0, 0, 10, 10),
            from_ocr=True,
        )

    def test_a_number_both_reads_agree_on_keeps_its_confidence(self):
        candidate = self._candidate("+919876543210")
        apply_ocr_verification([candidate], {"+919876543210"})
        assert candidate.confidence is Confidence.HIGH

    def test_a_number_the_second_read_missed_is_demoted(self):
        # This is the digit-misread case: 5 read as 6, say. One read saw
        # ...543210, the other ...643210, so neither can be trusted.
        candidate = self._candidate("+919876543210")
        apply_ocr_verification([candidate], {"+919876643210"})
        assert candidate.confidence is Confidence.LOW
        assert any("misread" in r for r in candidate.reasons)

    def test_demotion_makes_it_ineligible_for_a_silent_send(self):
        from waprinter.extract.phone import THRESHOLD_MEDIUM

        candidate = self._candidate("+919876543210")
        apply_ocr_verification([candidate], set())
        assert candidate.score < THRESHOLD_MEDIUM

    def test_text_layer_candidates_are_left_alone(self):
        candidate = self._candidate("+919876543210")
        candidate.from_ocr = False
        apply_ocr_verification([candidate], set())
        assert candidate.confidence is Confidence.HIGH

    @needs_tesseract
    @pytest.mark.parametrize("dpi", [60, 80])
    def test_a_poor_scan_never_produces_a_confident_wrong_number(
        self, make_invoice, dpi
    ):
        """The whole reason double-reading exists.

        At these resolutions Tesseract genuinely misreads digits — observed
        outputs include +919876549210 (3 read as 9) and +919845012945, both of
        which are valid-looking Indian mobiles belonging to someone else. What
        must never happen is one of them arriving as HIGH confidence, because
        that is what a silent send acts on.
        """
        fields = extract_fields(
            make_invoice(InvoiceSpec(raster=True, raster_dpi=dpi)),
            excluded_numbers=SELLER,
            ocr=OCR,
        )
        wrong = [
            c
            for c in fields.candidates
            if c.e164 != "+919876543210" and c.confidence is Confidence.HIGH
        ]
        assert not wrong, f"would have silently sent to {[c.e164 for c in wrong]}"

    @needs_tesseract
    def test_a_clean_scan_still_reads_correctly(self, make_invoice):
        # The verification must not be so strict that legible scans stop working.
        fields = extract_fields(
            make_invoice(InvoiceSpec(raster=True, raster_dpi=150)),
            excluded_numbers=SELLER,
            ocr=OCR,
        )
        assert fields.best.e164 == "+919876543210"
        assert fields.best.confidence is Confidence.HIGH

    @needs_tesseract
    def test_scan_numbers_finds_everything_regardless_of_score(self, scanned):
        doc = pdf_text.read(scanned, ocr=OCR)
        numbers = scan_numbers(doc)
        # Both the customer's number and the seller's are present; scan_numbers
        # ignores labels and position by design.
        assert "+919876543210" in numbers
