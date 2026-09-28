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
        state = vm.device_state(Settings(dry_run=False, send_mode="api"), 3, [])
        assert "3 document(s) need attention" in state.text
        assert state.tone == "warn"

    def test_ready_when_nothing_is_outstanding(self):
        state = vm.device_state(Settings(dry_run=False, send_mode="api"), 0, [])
        assert state.text == "Ready"
        assert state.tone == "ok"

    def test_link_mode_never_reads_as_sending_on_its_own(self):
        """A bare "Ready" in link mode is how an operator comes to believe a
        receipt was delivered while it is still waiting on their screen."""
        state = vm.device_state(Settings(dry_run=False, send_mode="link"), 0, [])
        assert state.text != "Ready"
        assert "press send" in state.text
        assert state.tone == "warn"


class TestHeader:
    """The header badge is one word; the line beside it one sentence."""

    def test_the_badge_is_the_state_and_the_line_says_why(self):
        state = vm.device_state(Settings(dry_run=True), 0, [])
        assert state.label == "Test mode"
        assert vm.header_line(state) == "Documents are read but nothing is sent."

    def test_a_long_problem_is_cut_to_its_first_sentence(self):
        state = vm.device_state(Settings(dry_run=False), 0, [
            "The WhatsApp phone number ID is not set. Open Setup → Connect "
            "WhatsApp and choose the number to send from."])
        assert vm.header_line(state) == "The WhatsApp phone number ID is not set."

    def test_plain_ready_still_says_something(self):
        state = vm.device_state(Settings(dry_run=False, send_mode="api"), 0, [])
        assert vm.header_line(state) == "Printed documents are sent on their own."

    @pytest.mark.parametrize("settings, waiting, problems", [
        (Settings(dry_run=True), 0, []),
        (Settings(dry_run=False), 0, ["No access token is stored."]),
        (Settings(dry_run=False, send_mode="link"), 0, []),
        (Settings(dry_run=False, send_mode="api"), 2, []),
        (Settings(dry_run=False, send_mode="api"), 0, []),
    ])
    def test_every_state_has_a_line_for_the_mode_card(self, settings, waiting, problems):
        assert vm.device_state(settings, waiting, problems).summary


class TestSetupNeeds:
    """Each problem that stops sending is a card leading to the step that
    fixes it -- which only helps if it leads to the right one."""

    @pytest.mark.parametrize("problem, page", [
        ("The WhatsApp phone number ID is not set. Open Setup → Connect "
         "WhatsApp and choose the number to send from.", "connect"),
        ("No access token is stored.", "connect"),
        ("The stored access token is only 12 characters. Meta's tokens are "
         "far longer, so this one was probably not pasted in full.", "connect"),
        ("Message 'invoice_document' is pending, not yet approved by Meta.", "templates"),
        ("Message 'receipt_msg' is not configured.", "templates"),
        ("The 'invoice_document' message has {{1}}, {{2}} with nothing to fill "
         "them. Open Setup → Fill in messages and choose what goes in each.", "messages"),
        ("Member statement documents have no message yet. Open Setup → Fill "
         "in messages.", "messages"),
        ("Your own numbers are not listed, so a number printed in your "
         "letterhead could be treated as a customer.", "preferences"),
    ])
    def test_each_problem_leads_to_its_step(self, problem, page):
        assert vm.fixed_by(problem) == page

    def test_what_readiness_reports_is_all_recognised(self):
        """Guards the word lists against a rewording in send/readiness.py.

        A fresh install has most of them at once. None may fall through to
        the default, which would send the clerk to the wrong step.
        """
        from waprinter.send.readiness import problems

        found = problems(Settings(dry_run=False, send_mode="api", own_numbers=[]))
        assert len(found) >= 3
        for problem in found:
            lowered = problem.lower()
            assert any(word in lowered for _page, words in vm._FIXED_BY for word in words), problem

    def test_problems_are_grouped_by_step_in_the_order_they_came(self):
        needs = vm.setup_needs([
            "Your own numbers are not listed.",
            "The WhatsApp phone number ID is not set.",
            "No access token is stored.",
        ])
        assert [n.page for n in needs] == ["preferences", "connect"]
        assert [n.title for n in needs] == ["Counter settings", "Connect WhatsApp"]
        assert len(needs[1].lines) == 2

    def test_waiting_on_meta_is_tagged_as_such(self):
        [need] = vm.setup_needs(["Message 'a' is pending, not yet approved by Meta.",
                                 "Message 'b' is pending, not yet approved by Meta."])
        assert need.tag == "Pending approval"
        assert need.action == "View templates"
        [need] = vm.setup_needs(["Message 'a' is not configured."])
        assert need.tag == "Required"


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
