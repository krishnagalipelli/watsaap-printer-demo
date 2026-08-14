"""The local operator UI and the release path it drives."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from invoice_factory import InvoiceSpec

from waprinter.models import JobStatus


def flash_of(response) -> str:
    """The decoded flash message from a redirect, whatever the encoding."""
    query = parse_qs(urlparse(response.headers["location"]).query)
    return (query.get("msg") or [""])[0]


def is_error(response) -> bool:
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query.get("kind") == ["err"]


@pytest.fixture
def held_job(pipeline, make_invoice):
    """A job the operator skipped at the print dialog, so it sits in the queue.

    This is how jobs reach the queue in the normal flow — the dialog is the
    first stop, and skipping it defers the job to here.
    """
    job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
    assert job.status is JobStatus.AWAITING
    return pipeline.defer(job.id)


class TestDashboard:
    def test_empty_state_tells_you_what_to_do(self, client):
        body = client.get("/").text
        assert "Nothing printed yet today" in body
        assert "WhatsApp Printer" in body

    def test_counts_a_send(self, client, pipeline, make_invoice):
        job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
        pipeline.release(job.id, "9876543210")
        body = client.get("/").text
        assert "would have been sent" in body  # dry run wording
        assert "+919876543210" in body

    def test_counts_and_links_work_waiting_on_the_operator(self, client, held_job):
        body = client.get("/").text
        assert "waiting on you" in body
        assert "open the queue" in body

    def test_delivery_receipts_are_flagged_as_not_wired_up(self, client):
        assert "Delivery receipts aren't wired up yet" in client.get("/").text

    def test_dry_run_is_stated_prominently(self, client):
        assert "DRY RUN" in client.get("/").text


class TestQueuePage:
    def test_empty_queue_explains_itself(self, client):
        body = client.get("/queue").text
        assert "Nothing waiting" in body

    def test_lists_a_held_job_with_its_reason(self, client, held_job):
        body = client.get("/queue").text
        assert held_job.fields.invoice_number in body
        assert "Skipped at the print dialog" in body

    def test_badge_counts_waiting_jobs(self, client, held_job):
        assert "Queue (1)" in client.get("/queue").text

    def test_a_job_still_awaiting_its_dialog_is_visible(
        self, client, pipeline, make_invoice
    ):
        # If the agent restarts with a dialog open, the job must not vanish.
        job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
        assert job.status is JobStatus.AWAITING
        assert job.fields.invoice_number in client.get("/queue").text

    def test_page_body_is_markup_not_escaped_text(self, client, held_job):
        # Regression: the outer template autoescaped the already-rendered inner
        # template, so the whole page displayed as literal HTML source.
        body = client.get("/queue").text
        assert '<div class="job">' in body
        assert "&lt;div" not in body

    def test_values_from_the_page_are_still_escaped(self, client, pipeline, held_job):
        # Marking the composed body safe must not disable escaping of the
        # extracted fields inside it.
        held_job.fields.customer_name = "<script>alert(1)</script>"
        pipeline.store.upsert(held_job)
        body = client.get("/queue").text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


class TestRelease:
    def test_operator_can_send_to_a_number_they_supply(
        self, client, held_job, pipeline
    ):
        response = client.post(
            f"/jobs/{held_job.id}/send", data={"recipient": "98765 43210"}
        )
        assert response.status_code == 303

        job = pipeline.store.get(held_job.id)
        assert job.status is JobStatus.DRY_RUN
        assert job.recipient == "+919876543210"

    def test_a_bad_number_is_rejected_with_a_message(self, client, held_job, pipeline):
        response = client.post(
            f"/jobs/{held_job.id}/send", data={"recipient": "12345"}
        )
        assert response.status_code == 303
        assert is_error(response)
        assert pipeline.store.get(held_job.id).status is JobStatus.HELD

    def test_a_job_cannot_be_sent_twice(self, client, held_job, pipeline):
        client.post(f"/jobs/{held_job.id}/send", data={"recipient": "9876543210"})
        response = client.post(
            f"/jobs/{held_job.id}/send", data={"recipient": "9876543210"}
        )
        assert is_error(response)

    def test_discard(self, client, held_job, pipeline):
        client.post(f"/jobs/{held_job.id}/discard")
        assert pipeline.store.get(held_job.id).status is JobStatus.DISCARDED

    def test_released_job_leaves_an_audit_trail(self, client, held_job, pipeline):
        client.post(f"/jobs/{held_job.id}/send", data={"recipient": "9876543210"})
        kinds = [row["kind"] for row in pipeline.store.events(held_job.id)]
        assert "released" in kinds
        assert "sent" in kinds


class TestHistory:
    def test_shows_what_was_sent(self, client, pipeline, make_invoice):
        pipeline.process(make_invoice())
        body = client.get("/history").text
        assert "+919876543210" in body
        assert "INV-2291" in body


class TestSettings:
    def test_page_renders_current_values(self, client, pipeline):
        pipeline.settings.own_numbers = ["9845012345"]
        assert "9845012345" in client.get("/settings").text

    def test_saving_updates_settings(self, client, pipeline):
        client.post(
            "/settings",
            data={
                "own_numbers": "9845012345, 9845067890",
                "phone_number_id": "111222333",
                "default_template": "invoice_document",
                "dedupe_window_hours": "48",
                "max_sends_per_minute": "5",
                # dry_run checkbox omitted == unchecked == go live
            },
        )
        s = pipeline.settings
        assert s.own_numbers == ["9845012345", "9845067890"]
        assert s.phone_number_id == "111222333"
        assert s.dedupe_window_hours == 48
        assert s.dry_run is False

    def test_going_live_is_called_out(self, client, pipeline):
        response = client.post(
            "/settings",
            data={
                "own_numbers": "9845012345",
                "phone_number_id": "111",
                "default_template": "invoice_document",
                "dedupe_window_hours": "24",
                "max_sends_per_minute": "10",
            },
        )
        assert "Dry run is OFF" in flash_of(response)

    def test_ocr_toggles_round_trip(self, client, pipeline):
        client.post(
            "/settings",
            data={
                "own_numbers": "",
                "phone_number_id": "",
                "default_template": "invoice_document",
                "dedupe_window_hours": "24",
                "max_sends_per_minute": "10",
                "dry_run": "on",
                "ocr_enabled": "on",
                "ocr_silent_send": "on",
            },
        )
        assert pipeline.settings.ocr_enabled is True
        assert pipeline.settings.ocr_silent_send is True

    def test_unchecked_ocr_boxes_turn_the_settings_off(self, client, pipeline):
        pipeline.settings.ocr_silent_send = True
        client.post(
            "/settings",
            data={
                "own_numbers": "",
                "phone_number_id": "",
                "default_template": "invoice_document",
                "dedupe_window_hours": "24",
                "max_sends_per_minute": "10",
                "dry_run": "on",
                "ocr_enabled": "on",
                # ocr_silent_send omitted == unchecked
            },
        )
        assert pipeline.settings.ocr_silent_send is False

    def test_the_page_says_whether_tesseract_is_installed(self, client, pipeline):
        from waprinter.extract.ocr import available

        body = client.get("/settings").text
        if available(pipeline.settings.ocr()):
            assert "Tesseract is installed" in body
        else:
            assert "Tesseract is not installed" in body

    def test_a_non_numeric_limit_is_rejected(self, client, pipeline):
        response = client.post(
            "/settings",
            data={
                "own_numbers": "",
                "phone_number_id": "",
                "default_template": "invoice_document",
                "dedupe_window_hours": "not a number",
                "max_sends_per_minute": "10",
                "dry_run": "on",
            },
        )
        assert is_error(response)
        assert pipeline.settings.dedupe_window_hours == 24
