"""Receipts whose printer draws the text as shapes.

On the client's counter, Microsoft Print To PDF turned every letter of the
chit software's receipt into a filled outline: no fonts, no text layer, 310
shapes. Read like a scan, every receipt was held for a clerk -- on a printed
page as sharp as type. These pages are now told apart from scans and photos,
and a number drawn that way, read the same by both OCR passes, is sent.
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from invoice_factory import InvoiceSpec

from conftest import needs_tesseract
from waprinter.extract.fields import _drawn_at, mark_drawn_numbers
from waprinter.models import JobStatus, PhoneCandidate, Confidence


def drawn_as_shapes(path: Path) -> Path:
    """The same page with every glyph turned into a filled outline, as
    Microsoft Print To PDF delivers the chit software's receipts."""
    with fitz.open(path) as pdf:
        svg = pdf[0].get_svg_image(text_as_path=True)
    with fitz.open("svg", svg.encode()) as picture:
        out = fitz.open("pdf", picture.convert_to_pdf())
    shaped = path.with_name(path.stem + "-shapes.pdf")
    out.save(shaped)
    out.close()
    return shaped


def candidate(bbox, page=1) -> PhoneCandidate:
    return PhoneCandidate(raw="9876543210", e164="+919876543210", score=96,
                          confidence=Confidence.HIGH, page=page, bbox=bbox,
                          from_ocr=True)


class TestTellingDrawnFromScanned:
    BOX = (100, 100, 200, 112)

    def page_with(self, tmp_path, draw) -> Path:
        pdf = fitz.open()
        page = pdf.new_page()
        draw(page)
        path = tmp_path / "page.pdf"
        pdf.save(path)
        return path

    def test_digit_shapes_across_the_number_count(self, tmp_path):
        def digits(page):
            for i in range(10):
                x = 101 + i * 10
                page.draw_polyline([(x, 111), (x + 4, 101), (x + 8, 111)],
                                   fill=(0, 0, 0), closePath=True)
        with fitz.open(self.page_with(tmp_path, digits)) as pdf:
            assert _drawn_at(pdf[0], self.BOX)

    def test_a_shaded_box_under_the_number_does_not(self, tmp_path):
        """A table cell or highlight is a rectangle, not printed digits."""
        def boxes(page):
            page.draw_rect(fitz.Rect(100, 100, 200, 112), fill=(0.9, 0.9, 0.9))
        with fitz.open(self.page_with(tmp_path, boxes)) as pdf:
            assert not _drawn_at(pdf[0], self.BOX)

    def test_a_border_running_past_the_number_does_not(self, tmp_path):
        def border(page):
            page.draw_polyline([(20, 105), (500, 105), (500, 106), (20, 106)],
                               fill=(0, 0, 0), closePath=True)
        with fitz.open(self.page_with(tmp_path, border)) as pdf:
            assert not _drawn_at(pdf[0], self.BOX)

    def test_a_few_shapes_covering_part_of_the_number_do_not(self, tmp_path):
        def two(page):
            for x in (101, 111):
                page.draw_polyline([(x, 111), (x + 4, 101), (x + 8, 111)],
                                   fill=(0, 0, 0), closePath=True)
        with fitz.open(self.page_with(tmp_path, two)) as pdf:
            assert not _drawn_at(pdf[0], self.BOX)

    def test_an_unreadable_file_leaves_the_number_held(self, tmp_path):
        found = candidate(self.BOX)
        mark_drawn_numbers(tmp_path / "missing.pdf", [found])
        assert found.drawn is False

    def test_a_number_from_the_text_layer_is_not_inspected(self, tmp_path):
        found = candidate(self.BOX)
        found.from_ocr = False
        mark_drawn_numbers(tmp_path / "missing.pdf", [found])
        assert found.drawn is False


@needs_tesseract
class TestDrawnReceiptsSend:
    @pytest.fixture
    def shaped(self, make_invoice):
        return drawn_as_shapes(make_invoice(InvoiceSpec()))

    @pytest.fixture
    def one_message(self, pipeline):
        # As the scanned-page tests do: one message, so the unidentified-kind
        # fallback cannot pick a wrong one.
        pipeline.settings.document_templates = {}
        return pipeline

    def test_the_page_really_has_no_text(self, shaped):
        with fitz.open(shaped) as pdf:
            assert not pdf[0].get_text().strip()
            assert not pdf[0].get_fonts()

    def test_it_is_sent_without_anyone_looking(self, one_message, shaped):
        job = one_message.process(shaped)
        assert job.status is JobStatus.DRY_RUN, job.hold_reason
        assert job.recipient == "+919876543210"
        assert job.fields.used_ocr and job.fields.best.drawn

    def test_the_counter_can_switch_it_off(self, one_message, shaped):
        one_message.settings.ocr_drawn_send = False
        job = one_message.process(shaped)
        assert job.status is JobStatus.HELD
        assert "read by OCR" in job.hold_reason

    def test_a_scan_of_the_same_receipt_is_still_held(self, one_message, make_invoice):
        job = one_message.process(make_invoice(InvoiceSpec(raster=True)))
        assert job.status is JobStatus.HELD
        assert job.fields.best.drawn is False
        assert "read by OCR" in job.hold_reason

    def test_the_flag_survives_the_queue(self, one_message, shaped):
        """A held job reopened from the store keeps what was found."""
        one_message.settings.ocr_drawn_send = False
        job = one_message.process(shaped)
        again = one_message.store.get(job.id)
        assert again.fields.best.drawn is True
