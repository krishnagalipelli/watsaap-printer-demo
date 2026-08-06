"""The WhatsApp Cloud API sender and the template layer."""

from __future__ import annotations

import httpx
import pytest
import respx

from waprinter.models import ExtractedFields
from waprinter.send.templates import MessageTemplate, TemplateStore, render
from waprinter.send.whatsapp import WhatsAppCloudSender

PHONE_ID = "123456789"
BASE = f"https://graph.facebook.com/v21.0/{PHONE_ID}"


@pytest.fixture
def approved_template():
    return MessageTemplate(
        name="invoice_document",
        language="en",
        body="Hello {{1}}, invoice {{2}} for ₹{{3}} is attached.",
        footer="Sunrise Traders",
        status="approved",
    )


@pytest.fixture
def fields():
    return ExtractedFields(
        invoice_number="INV-2291",
        customer_name="Meghana Enterprises",
        total_amount="18,450.00",
    )


@pytest.fixture
def message(approved_template, fields):
    return render(
        approved_template,
        {"1": "customer_name", "2": "invoice_number", "3": "total_amount"},
        fields,
    )


@pytest.fixture
def pdf(tmp_path):
    from invoice_factory import InvoiceSpec, build

    return build(InvoiceSpec(), tmp_path / "invoice.pdf")


@pytest.fixture
def sender():
    return WhatsAppCloudSender(PHONE_ID, "test-token", api_version="v21.0")


class TestRendering:
    def test_substitutes_variables_in_order(self, message):
        assert message.parameters == ["Meghana Enterprises", "INV-2291", "18,450.00"]

    def test_preview_is_what_the_customer_reads(self, message):
        assert message.preview == (
            "Hello Meghana Enterprises, invoice INV-2291 for ₹18,450.00 is "
            "attached.\n\nSunrise Traders"
        )

    def test_filename_uses_the_invoice_number(self, message):
        assert message.filename == "Invoice-INV-2291.pdf"

    def test_missing_values_are_flagged_not_left_blank(self, approved_template):
        # Meta rejects empty parameters outright, so a placeholder goes in and
        # the gap is recorded for whoever tunes the extractor.
        m = render(
            approved_template,
            {"1": "customer_name", "2": "invoice_number", "3": "total_amount"},
            ExtractedFields(invoice_number="INV-1"),
        )
        assert "customer_name" in m.missing
        assert "total_amount" in m.missing
        assert "" not in m.parameters

    def test_filename_strips_characters_that_would_look_wrong_on_a_phone(self):
        m = render(
            MessageTemplate(name="t", body="hi", status="approved"),
            {},
            ExtractedFields(invoice_number="INV/2291\\<>:*"),
        )
        assert m.filename == "Invoice-INV2291.pdf"


class TestTemplateStore:
    def test_round_trips_through_disk(self, tmp_path):
        path = tmp_path / "templates.json"
        store = TemplateStore(path)
        tpl = store.get("invoice_document")
        tpl.status = "approved"
        store.put(tpl)

        reloaded = TemplateStore(path)
        assert reloaded.get("invoice_document").status == "approved"

    def test_placeholders_are_read_in_order(self):
        tpl = MessageTemplate(name="t", body="{{2}} then {{1}} then {{2}} again")
        assert tpl.placeholders == ["2", "1"]

    def test_a_template_without_a_document_header_cannot_carry_a_pdf(self):
        tpl = MessageTemplate(name="t", status="approved", header_document=False)
        assert tpl.usable is False


class TestSending:
    @respx.mock
    def test_uploads_then_sends(self, sender, pdf, message):
        upload = respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-abc"})
        )
        send = respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(
                200, json={"messages": [{"id": "wamid.XYZ"}]}
            )
        )

        result = sender.send("+919876543210", pdf, message)

        assert result.ok
        assert result.wamid == "wamid.XYZ"
        assert upload.called and send.called

        body = send.calls[0].request.read()
        payload = __import__("json").loads(body)
        assert payload["to"] == "919876543210"  # no leading +
        assert payload["template"]["name"] == "invoice_document"

        header = payload["template"]["components"][0]
        assert header["parameters"][0]["document"]["id"] == "media-abc"
        assert header["parameters"][0]["document"]["filename"] == "Invoice-INV-2291.pdf"

        body_params = payload["template"]["components"][1]["parameters"]
        assert [p["text"] for p in body_params] == [
            "Meghana Enterprises",
            "INV-2291",
            "18,450.00",
        ]

    @respx.mock
    def test_refuses_an_unapproved_template_before_calling_the_api(
        self, sender, pdf, message
    ):
        message.template.status = "pending"
        route = respx.post(f"{BASE}/media")

        result = sender.send("+919876543210", pdf, message)

        assert not result.ok
        assert not result.retryable
        assert "pending" in result.error
        assert not route.called

    @respx.mock
    def test_rate_limit_is_retryable(self, sender, pdf, message):
        respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-abc"})
        )
        respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(
                429,
                json={"error": {"code": 130429, "message": "Rate limit hit"}},
            )
        )

        result = sender.send("+919876543210", pdf, message)
        assert not result.ok
        assert result.retryable

    @respx.mock
    def test_a_bad_number_is_not_retryable(self, sender, pdf, message):
        respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-abc"})
        )
        respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "code": 131026,
                        "message": "Message undeliverable",
                    }
                },
            )
        )

        result = sender.send("+919876543210", pdf, message)
        assert not result.ok
        assert not result.retryable
        assert "131026" in result.error

    @respx.mock
    def test_server_error_is_retryable(self, sender, pdf, message):
        respx.post(f"{BASE}/media").mock(return_value=httpx.Response(503, text="nope"))

        result = sender.send("+919876543210", pdf, message)
        assert not result.ok
        assert result.retryable

    @respx.mock
    def test_network_failure_does_not_raise(self, sender, pdf, message):
        respx.post(f"{BASE}/media").mock(side_effect=httpx.ConnectError("offline"))

        result = sender.send("+919876543210", pdf, message)
        assert not result.ok
        assert result.retryable
