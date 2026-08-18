"""What the window shows.

Tested without a display, because that is where the wording lives and wording is
what the operator actually relies on. The rule being enforced throughout:
internal status names never reach the screen.
"""

from __future__ import annotations

import pytest
from invoice_factory import InvoiceSpec

from waprinter.config import Settings
from waprinter.models import JobStatus
from waprinter.ui import viewmodel as vm


@pytest.fixture
def sent_job(pipeline, make_invoice):
    job = pipeline.process(make_invoice())
    assert job.status is JobStatus.DRY_RUN
    return job


@pytest.fixture
def waiting_job(pipeline, make_invoice):
    job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
    assert job.status is JobStatus.HELD
    return job


class TestStatusLabels:
    @pytest.mark.parametrize("status", list(JobStatus))
    def test_every_status_has_a_human_label(self, status, sent_job):
        sent_job.status = status
        label, tone = vm.label_of(sent_job)
        assert label and label[0].isupper()
        # No enum names, no underscores.
        assert "_" not in label
        assert tone in {"ok", "warn", "bad", "muted"}

    def test_a_test_send_is_not_described_as_sent(self, sent_job):
        label, _tone = vm.label_of(sent_job)
        assert label == "Test only"


class TestDeviceState:
    def test_test_mode_is_stated_first(self):
        state = vm.device_state(Settings(dry_run=True), 0, ["something missing"])
        assert "Test mode" in state.text
        assert state.tone == "warn"

    def test_missing_configuration_blocks_ready(self):
        state = vm.device_state(Settings(dry_run=False), 0, ["No access token."])
        assert state.text.startswith("Not ready")
        assert state.tone == "bad"

    def test_waiting_documents_are_surfaced(self):
        state = vm.device_state(Settings(dry_run=False), 3, [])
        assert "3 document(s) need attention" in state.text
        assert state.tone == "warn"

    def test_ready_when_nothing_is_outstanding(self):
        state = vm.device_state(Settings(dry_run=False), 0, [])
        assert state.text == "Ready"
        assert state.tone == "ok"


class TestCounters:
    def test_it_counts_the_day(self, pipeline, make_invoice):
        pipeline.process(make_invoice())
        pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
        counters = vm.counters_for_today(pipeline.store, pipeline.settings)
        assert counters.printed == 2
        assert counters.sent == 1
        assert counters.waiting == 1
        assert counters.failed == 0

    def test_the_caption_follows_test_mode(self):
        assert vm.sent_caption(Settings(dry_run=True)) == "test sends today"
        assert vm.sent_caption(Settings(dry_run=False)) == "sent today"


class TestRows:
    def test_a_history_row_reads_in_plain_words(self, sent_job):
        _time, status, sent_to, document, _detail = vm.history_row(sent_job)
        assert status == "Test only"
        assert sent_to == "+919876543210"
        assert document == "INV-2291"

    def test_a_queue_caption_names_the_customer(self, waiting_job, pipeline):
        waiting_job.fields.customer_name = "Anitha Ramesh"
        assert "Anitha Ramesh" in vm.queue_caption(waiting_job)

    def test_a_document_without_a_number_still_has_a_title(self, pipeline, tmp_path):
        broken = tmp_path / "x.pdf"
        broken.write_bytes(b"not a pdf")
        job = pipeline.process(broken, doc_title="Chit Receipt")
        assert vm.document_of(job) == "Chit Receipt"


def test_the_instructions_name_the_printer():
    assert "File → Print" in vm.HOW_TO_USE
    assert "WhatsApp Printer" in vm.HOW_TO_USE
