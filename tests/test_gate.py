"""The send/confirm/hold decision.

Two modes, both tested here:

* **Confirm mode** (the default, and what the chit fund client needs): every
  print raises a dialog and the operator types the number, because these
  documents do not carry one.
* **Automatic mode**: the recipient is detected from the page and sent to
  without anyone looking. Off by default. Every test in `TestAutomatic*` is
  about making sure that when it *is* on, nothing ambiguous ever goes out.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from invoice_factory import InvoiceSpec

from waprinter.models import JobStatus
from waprinter.rules import Decision


def run(pipeline, make_invoice, spec=None, **kwargs):
    return pipeline.process(make_invoice(spec), **kwargs)


class TestConfirmMode:
    """The default flow: nothing sends until the operator says so."""

    def test_a_print_waits_for_the_operator(self, pipeline, make_invoice):
        job = run(pipeline, make_invoice)
        assert job.status is JobStatus.AWAITING

    def test_it_waits_even_when_a_number_is_on_the_page(self, pipeline, make_invoice):
        # A detected number is a suggestion to prefill, never an instruction.
        job = run(pipeline, make_invoice)
        assert job.status is JobStatus.AWAITING
        assert job.recipient == "+919876543210"
        assert "check it" in job.hold_reason

    def test_it_waits_when_there_is_no_number_at_all(self, pipeline, make_invoice):
        # The normal case for these documents.
        job = run(pipeline, make_invoice, InvoiceSpec(customer_phone=None))
        assert job.status is JobStatus.AWAITING
        assert job.recipient is None
        assert "Enter the customer's WhatsApp number" in job.hold_reason

    def test_the_message_is_ready_for_the_dialog_to_show(self, pipeline, make_invoice):
        job = run(pipeline, make_invoice)
        assert "INV-2291" in job.message_preview
        assert "18,450.00" in job.message_preview

    def test_the_operator_sends_it(self, pipeline, make_invoice):
        job = run(pipeline, make_invoice, InvoiceSpec(customer_phone=None))
        sent = pipeline.release(job.id, "98765 43210")
        assert sent.status is JobStatus.DRY_RUN
        assert sent.recipient == "+919876543210"

    def test_the_operator_can_correct_the_name(self, pipeline, make_invoice):
        job = run(pipeline, make_invoice, InvoiceSpec(customer_phone=None))
        sent = pipeline.release(job.id, "9876543210", customer_name="Ravi Kumar")
        assert "Ravi Kumar" in sent.message_preview

    def test_skipping_leaves_it_in_the_queue(self, pipeline, make_invoice):
        job = run(pipeline, make_invoice)
        deferred = pipeline.defer(job.id)
        assert deferred.status is JobStatus.HELD
        assert "Skipped at the print dialog" in deferred.hold_reason

    def test_a_skipped_job_can_still_be_sent_later(self, pipeline, make_invoice):
        job = run(pipeline, make_invoice)
        pipeline.defer(job.id)
        sent = pipeline.release(job.id, "9876543210")
        assert sent.status is JobStatus.DRY_RUN

    def test_a_bad_number_is_refused(self, pipeline, make_invoice):
        import pytest

        job = run(pipeline, make_invoice)
        with pytest.raises(ValueError, match="not a valid mobile"):
            pipeline.release(job.id, "12345")

    def test_a_job_cannot_be_sent_twice(self, pipeline, make_invoice):
        import pytest

        job = run(pipeline, make_invoice)
        pipeline.release(job.id, "9876543210")
        with pytest.raises(ValueError, match="already sent"):
            pipeline.release(job.id, "9876543210")

    def test_an_unreadable_file_fails_without_killing_the_service(
        self, pipeline, tmp_path
    ):
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"this is not a pdf")
        job = pipeline.process(broken)
        assert job.status is JobStatus.FAILED
        assert job.error


class TestAutomaticSends:
    def test_a_clean_invoice_is_sent_without_asking(self, auto_pipeline, make_invoice):
        job = run(auto_pipeline, make_invoice)
        assert job.status is JobStatus.DRY_RUN
        assert job.recipient == "+919876543210"

    def test_the_message_preview_is_recorded(self, auto_pipeline, make_invoice):
        job = run(auto_pipeline, make_invoice)
        assert "Meghana Enterprises" in job.message_preview
        assert "INV-2291" in job.message_preview
        assert "18,450.00" in job.message_preview


class TestAutomaticHolds:
    def test_no_number_on_the_page(self, auto_pipeline, make_invoice):
        job = run(auto_pipeline, make_invoice, InvoiceSpec(customer_phone=None))
        assert job.status is JobStatus.HELD
        assert "No phone number" in job.hold_reason

    def test_only_the_sellers_own_number(self, auto_pipeline, make_invoice):
        spec = InvoiceSpec(customer_phone=None, seller_phone="9845012345")
        job = run(auto_pipeline, make_invoice, spec)
        assert job.status is JobStatus.HELD

    def test_two_equally_good_numbers(self, auto_pipeline, make_invoice):
        spec = InvoiceSpec(extra_customer_lines=["Transporter Mobile: 9812345678"])
        job = run(auto_pipeline, make_invoice, spec)
        assert job.status is JobStatus.HELD
        assert "equally likely" in job.hold_reason

    def test_a_merely_plausible_number_is_not_sent(self, auto_pipeline, make_invoice):
        auto_pipeline.settings.own_numbers = []
        job = run(auto_pipeline, make_invoice, InvoiceSpec(customer_phone=None))
        assert job.status is JobStatus.HELD
        assert job.recipient == "+919845012345"  # offered as a suggestion
        assert "confidence" in job.hold_reason

    def test_a_scanned_page_is_read_by_ocr_but_still_held(
        self, auto_pipeline, make_invoice
    ):
        job = run(auto_pipeline, make_invoice, InvoiceSpec(raster=True))
        assert job.status is JobStatus.HELD
        assert job.recipient == "+919876543210"
        assert "read by OCR" in job.hold_reason

    def test_a_scanned_page_can_be_sent_once_ocr_is_trusted(
        self, auto_pipeline, make_invoice
    ):
        auto_pipeline.settings.ocr_silent_send = True
        job = run(auto_pipeline, make_invoice, InvoiceSpec(raster=True))
        assert job.status is JobStatus.DRY_RUN

    def test_a_scanned_page_is_held_when_ocr_is_switched_off(
        self, auto_pipeline, make_invoice
    ):
        auto_pipeline.settings.ocr_enabled = False
        job = run(auto_pipeline, make_invoice, InvoiceSpec(raster=True))
        assert job.status is JobStatus.HELD
        assert "no text layer" in job.hold_reason.lower()
        assert "switched off" in job.hold_reason


class TestDeduplication:
    def test_a_reprint_does_not_send_twice(self, auto_pipeline, make_invoice):
        first = run(auto_pipeline, make_invoice)
        assert first.status is JobStatus.DRY_RUN

        second = run(auto_pipeline, make_invoice)
        assert second.status is JobStatus.DUPLICATE
        assert "Reprint suppressed" in second.hold_reason

    def test_a_different_invoice_to_the_same_customer_still_sends(
        self, auto_pipeline, make_invoice
    ):
        run(auto_pipeline, make_invoice)
        job = run(auto_pipeline, make_invoice, InvoiceSpec(invoice_number="INV-2292"))
        assert job.status is JobStatus.DRY_RUN

    def test_the_same_invoice_sends_again_once_the_window_lapses(
        self, auto_pipeline, make_invoice
    ):
        run(auto_pipeline, make_invoice)
        later = datetime.now() + timedelta(
            hours=auto_pipeline.settings.dedupe_window_hours + 1
        )
        job = run(auto_pipeline, make_invoice, now=later)
        assert job.status is JobStatus.DRY_RUN


class TestVolumeRails:
    def test_a_runaway_batch_is_capped(self, auto_pipeline, make_invoice, monkeypatch):
        auto_pipeline.settings.max_sends_per_minute = 3
        monkeypatch.setattr(auto_pipeline.store, "count_sent_since", lambda _s: 3)
        job = run(auto_pipeline, make_invoice)
        assert job.status is JobStatus.HELD
        assert "Rate limit" in job.hold_reason


class TestGateDirectly:
    def test_decision_enum_round_trips(self):
        assert Decision("send") is Decision.SEND
        assert Decision("confirm") is Decision.CONFIRM
        assert Decision("hold") is Decision.HOLD
