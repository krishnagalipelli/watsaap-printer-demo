"""Click-to-chat mode, used until the Cloud API account is approved.

The honesty requirement runs through all of it: a wa.me link opens a chat with
text prefilled and a *person* presses send. The app cannot observe that, so it
must never record a job as sent, and the operator must be told the PDF is not
attached for them.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest
from invoice_factory import ChitReceiptSpec, build_chit_receipt

from waprinter.models import JobStatus
from waprinter.send.link import MAX_TEXT, chat_url
from waprinter.ui.result import describe, needs_action


@pytest.fixture
def chit(tmp_path):
    def _make(spec=None, name="receipt.pdf"):
        return build_chit_receipt(spec or ChitReceiptSpec(), tmp_path / name)

    return _make


class TestChatUrl:
    def test_it_targets_the_member(self):
        url = chat_url("+919000012345", "hello")
        assert url.startswith("https://wa.me/919000012345?")
        # WhatsApp wants digits only, no leading plus.
        assert "+" not in urlparse(url).path

    def test_the_message_survives_the_round_trip(self):
        message = "Dear ANITHA RAMESH,\n\nReceipt No.: CR1747/26\nAmount: ₹100"
        url = chat_url("9000012345", message)
        assert unquote(parse_qs(urlparse(url).query)["text"][0]) == message

    def test_ampersands_do_not_truncate_the_message(self):
        url = chat_url("9000012345", "Smith & Co — receipt #12")
        assert "Smith & Co — receipt #12" in unquote(
            parse_qs(urlparse(url).query)["text"][0]
        )

    def test_a_runaway_message_is_cut_rather_than_mangled(self):
        url = chat_url("9000012345", "x" * (MAX_TEXT + 500))
        assert len(parse_qs(urlparse(url).query)["text"][0]) <= MAX_TEXT

    def test_a_missing_recipient_is_refused(self):
        with pytest.raises(ValueError, match="recipient"):
            chat_url("", "hello")


class TestPrintingInLinkMode:
    def test_a_print_produces_a_ready_job_with_a_link(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        assert job.status is JobStatus.READY
        assert job.chat_url.startswith("https://wa.me/919000012345?")

    def test_nothing_is_recorded_as_sent(self, link_pipeline, chit):
        # A person still has to press send in WhatsApp.
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        assert job.status is not JobStatus.SENT
        assert job.sent_at is None

    def test_the_message_is_the_clients_wording(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        body = job.message_preview
        assert body.startswith("Dear ANITHA RAMESH,")
        assert "Receipt No.: CR1747/26" in body
        assert "Date: 13-Aug-26" in body
        assert "Amount Paid: ₹100" in body
        assert "Payment Mode: Cash" in body
        assert "Srinidhi Chit Funds" in body

    def test_the_link_carries_that_message(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        text = unquote(parse_qs(urlparse(job.chat_url).query)["text"][0])
        assert text == job.message_preview

    def test_a_receipt_with_no_number_is_held_not_linked(self, link_pipeline, chit):
        job = link_pipeline.process(
            chit(ChitReceiptSpec(member_phone=None), "nonum.pdf")
        )
        assert job.status is JobStatus.HELD
        assert job.chat_url is None

    def test_it_waits_in_the_queue_until_opened(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        assert job.id in {j.id for j in link_pipeline.store.pending()}


class TestHandingOff:
    def test_opening_the_chat_is_recorded_as_handed_off(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        opened = link_pipeline.hand_off(job.id)
        assert opened.status is JobStatus.HANDED_OFF
        assert opened.sent_at is not None

    def test_handing_off_is_never_called_sent(self, link_pipeline, chit):
        from waprinter.ui.viewmodel import label_of

        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        opened = link_pipeline.hand_off(job.id)
        label, _tone = label_of(opened)
        assert label == "Opened in WhatsApp"
        assert label != "Sent"

    def test_a_job_without_a_link_cannot_be_handed_off(self, link_pipeline, chit):
        job = link_pipeline.process(
            chit(ChitReceiptSpec(member_phone=None), "nonum2.pdf")
        )
        with pytest.raises(ValueError, match="no WhatsApp link"):
            link_pipeline.hand_off(job.id)

    def test_a_reprint_is_suppressed_once_the_chat_was_opened(
        self, link_pipeline, chit
    ):
        first = link_pipeline.process(chit(), doc_title="Chit Receipt")
        link_pipeline.hand_off(first.id)
        again = link_pipeline.process(chit(name="again.pdf"), doc_title="Chit Receipt")
        assert again.status is JobStatus.DUPLICATE

    def test_an_unknown_job_is_refused(self, link_pipeline):
        with pytest.raises(KeyError):
            link_pipeline.hand_off("nope")


class TestWhatTheOperatorIsTold:
    def test_a_ready_job_says_to_attach_the_receipt(self, link_pipeline, chit):
        # The link cannot carry the PDF, so the operator has to be told.
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        tone, headline, detail = describe(job)
        assert headline == "Ready to send"
        assert "attach" in detail.lower()
        assert tone == "wait"

    def test_a_ready_notification_never_closes_itself(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        assert needs_action(job) is True

    def test_a_handed_off_job_does_not_claim_delivery(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        opened = link_pipeline.hand_off(job.id)
        _tone, headline, detail = describe(opened)
        assert headline == "Opened in WhatsApp"
        # It tells them what is still left to do, rather than implying the
        # member has already received anything.
        assert "then send" in detail.lower()
        assert "delivered" not in detail.lower()


class TestAutoOpen:
    """One member at a time at a counter, so the chat opens without a click."""

    def test_it_is_on_by_default(self):
        from waprinter.config import Settings

        assert Settings().auto_open_chat is True

    def test_the_window_opens_the_chat_itself(self, link_pipeline, chit, monkeypatch):
        # Exercised without a display: _notify is what decides, and it asks
        # open_whatsapp rather than waiting for a button.
        from waprinter.ui.desktop import DesktopWindow

        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        opened = []

        window = DesktopWindow.__new__(DesktopWindow)
        window.pipeline = link_pipeline
        window.settings = link_pipeline.settings
        window._notifications = []

        def fake_open(job_id):
            opened.append(job_id)
            link_pipeline.hand_off(job_id)
            return True

        window.open_whatsapp = fake_open
        monkeypatch.setattr(
            "waprinter.ui.desktop.Notification", lambda *a, **k: object()
        )
        window.root = None
        DesktopWindow._notify(window, job.id)

        assert opened == [job.id]
        assert link_pipeline.store.get(job.id).status is JobStatus.HANDED_OFF

    def test_turning_it_off_leaves_the_job_waiting_for_a_click(
        self, link_pipeline, chit, monkeypatch
    ):
        from waprinter.ui.desktop import DesktopWindow

        link_pipeline.settings.auto_open_chat = False
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        opened = []

        window = DesktopWindow.__new__(DesktopWindow)
        window.pipeline = link_pipeline
        window.settings = link_pipeline.settings
        window._notifications = []
        window.open_whatsapp = lambda j: opened.append(j)
        monkeypatch.setattr(
            "waprinter.ui.desktop.Notification", lambda *a, **k: object()
        )
        window.root = None
        DesktopWindow._notify(window, job.id)

        assert opened == []
        assert link_pipeline.store.get(job.id).status is JobStatus.READY

    def test_the_reminder_names_the_paste_shortcut(self, link_pipeline, chit):
        job = link_pipeline.process(chit(), doc_title="Chit Receipt")
        opened = link_pipeline.hand_off(job.id)
        _tone, _headline, detail = describe(opened)
        assert "Ctrl+V" in detail
