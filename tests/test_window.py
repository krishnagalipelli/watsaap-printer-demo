"""The application window and the after-print notification.

The notification is rendered as a page and shown in its own frameless window,
so its wording is testable without a display. Window creation itself needs a
GUI and is exercised on the build machine by --selftest.
"""

from __future__ import annotations

import pytest
from invoice_factory import InvoiceSpec

from waprinter.models import JobStatus
from waprinter.ui.result import describe, needs_action


@pytest.fixture
def sent_job(pipeline, make_invoice):
    job = pipeline.process(make_invoice())
    assert job.status is JobStatus.DRY_RUN
    return job


class TestWording:
    def test_a_real_send_says_it_went(self, sent_job):
        sent_job.status = JobStatus.SENT
        tone, headline, detail = describe(sent_job)
        assert tone == "ok"
        assert headline == "Sent on WhatsApp"
        assert "+919876543210" in detail and "INV-2291" in detail

    def test_a_test_send_does_not_claim_to_have_sent(self, sent_job):
        # The operator must not believe a customer received something.
        tone, headline, _detail = describe(sent_job)
        assert tone == "wait"
        assert "Not sent" in headline

    def test_a_failure_says_why(self, sent_job):
        sent_job.status = JobStatus.FAILED
        sent_job.error = "Message undeliverable"
        tone, headline, detail = describe(sent_job)
        assert tone == "bad" and headline == "Could not send"
        assert "Message undeliverable" in detail

    def test_no_internal_status_names_leak(self, sent_job):
        joined = " ".join(describe(sent_job))
        for internal in ("dry_run", "JobStatus", "awaiting"):
            assert internal not in joined


class TestWhichNotificationsStay:
    """Good news disappears; anything needing a person does not."""

    def test_a_successful_send_closes_itself(self, sent_job):
        sent_job.status = JobStatus.SENT
        assert needs_action(sent_job) is False

    def test_a_suppressed_reprint_closes_itself(self, pipeline, make_invoice):
        pipeline.process(make_invoice())
        job = pipeline.process(make_invoice())
        assert job.status is JobStatus.DUPLICATE
        assert needs_action(job) is False

    def test_a_failure_stays(self, sent_job):
        sent_job.status = JobStatus.FAILED
        assert needs_action(sent_job) is True

    def test_an_unreadable_number_stays(self, pipeline, make_invoice):
        job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
        assert job.status is JobStatus.HELD
        assert needs_action(job) is True


class TestNotificationPage:
    def test_it_renders_for_a_job(self, client, sent_job):
        body = client.get(f"/note/{sent_job.id}").text
        assert "Not sent" in body

    def test_something_needing_attention_carries_buttons(
        self, client, pipeline, make_invoice
    ):
        job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
        assert job.status is JobStatus.HELD
        body = client.get(f"/note/{job.id}").text
        # The buttons call back into the application, not into a browser.
        assert "pywebview.api.open_panel()" in body
        assert "pywebview.api.dismiss()" in body

    def test_a_successful_send_carries_no_buttons(self, client, sent_job, pipeline):
        sent_job.status = JobStatus.SENT
        pipeline.store.upsert(sent_job)
        body = client.get(f"/note/{sent_job.id}").text
        assert "Sent on WhatsApp" in body
        assert "pywebview.api" not in body  # nothing to do, so nothing to click

    def test_an_unknown_job_is_not_found(self, client):
        assert client.get("/note/nope").status_code == 404

    def test_values_are_escaped(self, client, pipeline, make_invoice):
        job = pipeline.process(make_invoice())
        job.fields.customer_name = "<script>alert(1)</script>"
        pipeline.store.upsert(job)
        body = client.get(f"/note/{job.id}").text
        assert "<script>alert(1)</script>" not in body


class TestShell:
    def test_submit_is_safe_from_the_watcher_thread(self, pipeline, sent_job):
        import threading

        from waprinter.ui.window import AppShell

        shell = AppShell(pipeline, port=8731)
        thread = threading.Thread(target=shell.submit, args=(sent_job.id,))
        thread.start()
        thread.join()
        assert shell.incoming.get_nowait() == sent_job.id

    def test_it_points_at_the_configured_port(self, pipeline):
        from waprinter.ui.window import AppShell

        assert AppShell(pipeline, port=9000).base_url == "http://127.0.0.1:9000"
