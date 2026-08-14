"""Whether an install is actually ready to send, and by which route.

One build serves every client, so the sender is configuration. These tests pin
down what each route requires, because the failure mode otherwise is discovering
it when a customer's receipt does not arrive.
"""

from __future__ import annotations

import pytest

from waprinter.config import Settings
from waprinter.send.readiness import BAILEYS, CLOUD, SENDERS, get, problems


class TestSenderCatalogue:
    def test_both_routes_are_offered(self):
        assert {s.key for s in SENDERS} == {BAILEYS, CLOUD}

    def test_each_states_its_trade_off(self):
        # The operator picking this should see the downside, not just the pitch.
        for sender in SENDERS:
            assert sender.summary and sender.caution

    def test_the_ban_risk_is_spelled_out_for_baileys(self):
        assert "ban" in get(BAILEYS).caution.lower()

    def test_the_template_restriction_is_spelled_out_for_the_official_api(self):
        assert "template" in get(CLOUD).caution.lower()

    def test_unknown_keys_are_not_senders(self):
        assert get("carrier-pigeon") is None


class TestProblems:
    def test_an_unconfigured_install_is_not_ready(self):
        assert problems(Settings()) != []

    def test_missing_own_numbers_is_always_a_problem(self):
        # Without it, a number in the client's own letterhead can be treated as
        # a customer.
        found = problems(Settings(own_numbers=[], sender_type=BAILEYS))
        assert any("own numbers" in p.lower() for p in found)

    def test_an_unknown_sender_is_reported(self):
        found = problems(Settings(own_numbers=["9845012345"], sender_type="nope"))
        assert any("Unknown sender" in p for p in found)

    def test_baileys_needs_a_linked_account(self):
        # Nothing is listening on the Baileys port during tests.
        found = problems(Settings(own_numbers=["9845012345"], sender_type=BAILEYS))
        assert any("QR" in p for p in found)

    def test_the_official_api_needs_credentials_and_a_template(self):
        found = problems(
            Settings(own_numbers=["9845012345"], sender_type=CLOUD, phone_number_id="")
        )
        joined = " ".join(found)
        assert "phone number ID" in joined
        assert "access token" in joined

    def test_a_fully_configured_baileys_install_is_ready(self, monkeypatch):
        from waprinter.send import baileys

        monkeypatch.setattr(baileys.BaileysSender, "is_connected", lambda self: True)
        assert problems(Settings(own_numbers=["9845012345"], sender_type=BAILEYS)) == []


class TestSettingsPage:
    def test_both_senders_are_listed_with_their_cautions(self, client):
        body = client.get("/settings").text
        assert "WhatsApp Web (Baileys)" in body
        assert "Official WhatsApp Business API" in body
        assert "ban" in body.lower()

    def test_outstanding_problems_are_shown(self, client):
        assert "Not ready to send" in client.get("/settings").text

    def test_the_sender_can_be_changed(self, client, pipeline):
        client.post(
            "/settings",
            data={
                "own_numbers": "9845012345",
                "phone_number_id": "",
                "default_template": "invoice_document",
                "dedupe_window_hours": "24",
                "max_sends_per_minute": "10",
                "dry_run": "on",
                "sender_type": CLOUD,
            },
        )
        assert pipeline.settings.sender_type == CLOUD

    def test_an_unknown_sender_is_rejected(self, client, pipeline):
        original = pipeline.settings.sender_type
        response = client.post(
            "/settings",
            data={
                "own_numbers": "",
                "phone_number_id": "",
                "default_template": "invoice_document",
                "dedupe_window_hours": "24",
                "max_sends_per_minute": "10",
                "dry_run": "on",
                "sender_type": "carrier-pigeon",
            },
        )
        assert response.status_code == 303
        assert pipeline.settings.sender_type == original


class TestBaileysSender:
    def test_the_caption_is_the_rendered_message_not_the_variables(self, tmp_path):
        # Regression: joining message.parameters sent the customer a caption of
        # bare values ("ANITHA RAMESH\nCR1747/26") rather than the sentence
        # they were shown in the send dialog.
        import httpx
        import respx

        from invoice_factory import InvoiceSpec, build
        from waprinter.extract import extract_fields
        from waprinter.send.baileys import BaileysSender
        from waprinter.send.templates import MessageTemplate, render

        pdf = build(InvoiceSpec(), tmp_path / "inv.pdf")
        message = render(
            MessageTemplate(name="t", body="Hello {{1}}, receipt {{2}}.", status="approved"),
            {"1": "customer_name", "2": "invoice_number"},
            extract_fields(pdf),
        )

        with respx.mock:
            route = respx.post("http://127.0.0.1:8732/send").mock(
                return_value=httpx.Response(200, json={"ok": True, "wamid": "x"})
            )
            BaileysSender().send("+919876543210", pdf, message)

        import json

        sent = json.loads(route.calls[0].request.read())
        assert sent["message"] == message.preview
        assert "Hello Meghana Enterprises" in sent["message"]
