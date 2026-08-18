"""The control panel.

Laid out like a printer's properties window: a device status line, tabs, grouped
settings with Apply. These tests care about what an operator can read and do,
not about markup.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from invoice_factory import InvoiceSpec

from waprinter.models import JobStatus


def flash_of(response) -> str:
    query = parse_qs(urlparse(response.headers["location"]).query)
    return (query.get("msg") or [""])[0]


def is_error(response) -> bool:
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query.get("kind") == ["err"]


@pytest.fixture
def waiting_job(pipeline, make_invoice):
    """A document whose recipient could not be read, so it needs a person."""
    job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
    assert job.status is JobStatus.HELD
    return job


class TestDeviceStatus:
    def test_test_mode_is_stated_on_every_page(self, client):
        for path in ("/", "/queue", "/history", "/settings"):
            assert "Test mode" in client.get(path).text

    def test_it_reports_not_ready_when_unconfigured(self, client, pipeline):
        pipeline.settings.dry_run = False
        assert "Not ready" in client.get("/").text

    def test_it_reports_ready_when_configured(self, client, pipeline, monkeypatch):
        pipeline.settings.dry_run = False
        monkeypatch.setattr("waprinter.send.readiness.problems", lambda *a, **k: [])
        assert "Ready" in client.get("/").text

    def test_waiting_documents_are_counted_in_the_status_line(
        self, client, pipeline, waiting_job, monkeypatch
    ):
        pipeline.settings.dry_run = False
        monkeypatch.setattr("waprinter.send.readiness.problems", lambda *a, **k: [])
        assert "need attention" in client.get("/").text


class TestStatusTab:
    def test_it_counts_the_day(self, client, pipeline, make_invoice):
        pipeline.process(make_invoice())
        body = client.get("/").text
        assert "documents printed" in body
        assert "test sends today" in body  # wording follows test mode

    def test_it_says_how_to_use_the_printer(self, client):
        assert "File → Print" in client.get("/").text


class TestQueueTab:
    def test_empty_queue_explains_itself(self, client):
        assert "Nothing waiting" in client.get("/queue").text

    def test_it_lists_a_waiting_document_with_its_reason(self, client, waiting_job):
        body = client.get("/queue").text
        assert waiting_job.fields.invoice_number in body
        assert "No phone number" in body

    def test_the_tab_shows_a_count(self, client, waiting_job):
        assert 'class="count"' in client.get("/queue").text

    def test_page_body_is_markup_not_escaped_text(self, client, waiting_job):
        # Regression: the outer template autoescaped the already-rendered inner
        # template, so the whole page displayed as literal HTML source.
        body = client.get("/queue").text
        assert '<div class="item">' in body
        assert "&lt;div" not in body

    def test_values_from_the_page_are_still_escaped(self, client, pipeline, waiting_job):
        waiting_job.fields.customer_name = "<script>alert(1)</script>"
        pipeline.store.upsert(waiting_job)
        body = client.get("/queue").text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


class TestSendingFromTheQueue:
    def test_the_operator_can_supply_a_number(self, client, waiting_job, pipeline):
        response = client.post(
            f"/jobs/{waiting_job.id}/send", data={"recipient": "98765 43210"}
        )
        assert response.status_code == 303
        job = pipeline.store.get(waiting_job.id)
        assert job.status is JobStatus.DRY_RUN
        assert job.recipient == "+919876543210"

    def test_a_landline_is_refused(self, client, waiting_job, pipeline):
        response = client.post(
            f"/jobs/{waiting_job.id}/send", data={"recipient": "080-25551234"}
        )
        assert is_error(response)
        assert pipeline.store.get(waiting_job.id).status is JobStatus.HELD

    def test_a_document_cannot_be_sent_twice(self, client, waiting_job):
        client.post(f"/jobs/{waiting_job.id}/send", data={"recipient": "9876543210"})
        response = client.post(
            f"/jobs/{waiting_job.id}/send", data={"recipient": "9876543210"}
        )
        assert is_error(response)

    def test_discard(self, client, waiting_job, pipeline):
        client.post(f"/jobs/{waiting_job.id}/discard")
        assert pipeline.store.get(waiting_job.id).status is JobStatus.DISCARDED

    def test_sending_leaves_an_audit_trail(self, client, waiting_job, pipeline):
        client.post(f"/jobs/{waiting_job.id}/send", data={"recipient": "9876543210"})
        kinds = [row["kind"] for row in pipeline.store.events(waiting_job.id)]
        assert "released" in kinds and "sent" in kinds


class TestRecentTab:
    def test_it_shows_what_was_sent(self, client, pipeline, make_invoice):
        pipeline.process(make_invoice())
        body = client.get("/history").text
        assert "+919876543210" in body
        assert "INV-2291" in body

    def test_statuses_are_shown_in_plain_words(self, client, pipeline, make_invoice):
        # Regression: raw enum names like "dry_run" and "awaiting" leaked to the
        # screen, which mean nothing to an operator.
        pipeline.process(make_invoice())
        body = client.get("/history").text
        assert "Test only" in body
        for internal in ("dry_run", "awaiting", "held"):
            assert internal not in body


class TestSettingsTab:
    def test_it_shows_current_values(self, client, pipeline):
        pipeline.settings.own_numbers = ["9845012345"]
        assert "9845012345" in client.get("/settings").text

    def test_apply_saves(self, client, pipeline):
        client.post(
            "/settings",
            data={
                "own_numbers": "9845012345, 9845067890",
                "phone_number_id": "111222333",
                "default_template": "invoice_document",
                "dedupe_window_hours": "48",
                "max_sends_per_minute": "5",
                "ocr_enabled": "on",
            },
        )
        s = pipeline.settings
        assert s.own_numbers == ["9845012345", "9845067890"]
        assert s.phone_number_id == "111222333"
        assert s.dedupe_window_hours == 48
        assert s.dry_run is False
        assert s.ocr_enabled is True

    def test_going_live_is_called_out(self, client):
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
        assert "Test mode is OFF" in flash_of(response)

    def test_confirmation_can_be_turned_on(self, client, pipeline):
        client.post(
            "/settings",
            data={
                "own_numbers": "",
                "phone_number_id": "",
                "default_template": "invoice_document",
                "dedupe_window_hours": "24",
                "max_sends_per_minute": "10",
                "dry_run": "on",
                "confirm_before_send": "on",
            },
        )
        assert pipeline.settings.confirm_before_send is True

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
