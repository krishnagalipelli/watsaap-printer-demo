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
        document_noun="Invoice",
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
            ExtractedFields(invoice_number="INV2291<>:*"),
            document_noun="Invoice",
        )
        assert m.filename == "Invoice-INV2291.pdf"

    def test_filename_keeps_a_separator_inside_the_number(self):
        # "CR1747/26" is one identifier. Deleting the slash gives a member a
        # number that appears on no receipt when they read it back.
        m = render(
            MessageTemplate(name="t", body="hi", status="approved"),
            {},
            ExtractedFields(invoice_number="CR1747/26"),
            document_noun="Receipt",
        )
        assert m.filename == "Receipt-CR1747-26.pdf"

    def test_filename_says_what_the_client_calls_its_paperwork(self):
        m = render(
            MessageTemplate(name="t", body="hi", status="approved"),
            {},
            ExtractedFields(invoice_number="CR1747/26"),
        )
        # Default is the generic word: calling a receipt an invoice is worse
        # than calling it nothing.
        assert m.filename == "Document-CR1747-26.pdf"


class TestNamedTemplates:
    """Templates whose variables Meta records by name, not by position."""

    @pytest.fixture
    def named_template(self):
        return MessageTemplate(
            name="chits_details",
            language="en",
            body=(
                "Dear {{customer_name}}, receipt {{receipt_no}} dated {{date}} "
                "for ₹{{amount}} paid by {{payment_mode}}."
            ),
            parameter_format="named",
            status="approved",
        )

    @pytest.fixture
    def named_message(self, named_template, fields):
        return render(
            named_template,
            # Only the names that differ from our field names need mapping.
            {"receipt_no": "invoice_number", "date": "invoice_date",
             "amount": "total_amount"},
            ExtractedFields(
                customer_name="Venkat",
                invoice_number="CR1747/26",
                invoice_date="2026-08-14",
                total_amount="1000",
                payment_mode="UPI",
            ),
        )

    def test_a_named_body_is_detected_without_being_told(self):
        tpl = MessageTemplate(name="t", body="Hi {{customer_name}}")
        assert tpl.named is True
        assert MessageTemplate(name="t", body="Hi {{1}}").named is False

    def test_unmapped_names_fall_back_to_the_field_of_the_same_name(
        self, named_message
    ):
        assert named_message.parameters[0] == "Venkat"
        assert named_message.missing == []

    def test_preview_substitutes_named_placeholders(self, named_message):
        assert named_message.preview == (
            "Dear Venkat, receipt CR1747/26 dated 2026-08-14 for ₹1000 paid "
            "by UPI."
        )

    @respx.mock
    def test_body_parameters_carry_their_names(self, sender, pdf, named_message):
        respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-abc"})
        )
        send = respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
        )

        assert sender.send("+916281125979", pdf, named_message).ok

        payload = __import__("json").loads(send.calls[0].request.read())
        assert payload["template"]["name"] == "chits_details"
        body_params = payload["template"]["components"][1]["parameters"]
        assert body_params[0] == {
            "type": "text",
            "text": "Venkat",
            "parameter_name": "customer_name",
        }
        assert [p["parameter_name"] for p in body_params] == [
            "customer_name", "receipt_no", "date", "amount", "payment_mode",
        ]

    @respx.mock
    def test_a_positional_template_still_sends_unlabelled(
        self, sender, pdf, message
    ):
        # Meta rejects a parameter_name on a positional template, so the label
        # must not leak across.
        respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-abc"})
        )
        send = respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
        )

        assert sender.send("+919876543210", pdf, message).ok

        payload = __import__("json").loads(send.calls[0].request.read())
        body_params = payload["template"]["components"][1]["parameters"]
        assert all("parameter_name" not in p for p in body_params)


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


class TestSenderFollowsSettings:
    """Turning test mode off must change what happens, not just what it is called.

    The bug this guards: the sender was chosen once, at startup. Unticking
    "Test mode" in the settings page flipped settings.dry_run, so a job was
    recorded as SENT — while the dry-run sender was still wired in and nothing
    reached Meta or the customer. The operator's own screen said it went.
    """

    def test_leaving_test_mode_rewires_the_sender(self, pipeline, monkeypatch):
        from waprinter.send.dryrun import DryRunSender
        from waprinter.send.whatsapp import WhatsAppCloudSender

        monkeypatch.setattr("waprinter.secrets.load_token", lambda: "a-real-token")
        pipeline.settings.dry_run = True
        pipeline.rebuild_sender()
        assert isinstance(pipeline.sender, DryRunSender)

        pipeline.settings.dry_run = False
        pipeline.settings.phone_number_id = "123456"
        pipeline.rebuild_sender()
        assert isinstance(pipeline.sender, WhatsAppCloudSender)

    def test_going_live_without_a_token_refuses_rather_than_pretending(
        self, pipeline, monkeypatch
    ):
        monkeypatch.setattr("waprinter.secrets.load_token", lambda: None)
        pipeline.settings.dry_run = False
        with pytest.raises(RuntimeError, match="access token"):
            pipeline.rebuild_sender()

    def test_returning_to_test_mode_rewires_back(self, pipeline, monkeypatch):
        from waprinter.send.dryrun import DryRunSender

        monkeypatch.setattr("waprinter.secrets.load_token", lambda: "a-real-token")
        pipeline.settings.dry_run = False
        pipeline.settings.phone_number_id = "123456"
        pipeline.rebuild_sender()
        pipeline.settings.dry_run = True
        pipeline.rebuild_sender()
        assert isinstance(pipeline.sender, DryRunSender)


class TestTransientNetworkFailures:
    """A dropped connection must not lose a receipt — but must not double-send.

    The failure this guards: "Upload failed: Server disconnected without
    sending a response". The sender marked it retryable and nothing retried,
    so one network blip on the counter PC lost the document permanently.
    """

    @respx.mock
    def test_a_dropped_upload_is_retried_and_succeeds(self, message, tmp_path):
        pdf = tmp_path / "receipt.pdf"
        pdf.write_bytes(b"%PDF-1.4 test %%EOF")

        route = respx.post(url__regex=r".*/media$").mock(
            side_effect=[
                httpx.RemoteProtocolError("Server disconnected without sending a response"),
                httpx.Response(200, json={"id": "media-123"}),
            ]
        )
        respx.post(url__regex=r".*/messages$").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
        )

        sender = WhatsAppCloudSender("123", "token")
        sender._client = httpx.Client(timeout=5)
        result = sender.send("+919492787875", pdf, message)

        assert result.ok, result.error
        assert result.wamid == "wamid.X"
        assert route.call_count == 2

    @respx.mock
    def test_it_gives_up_after_the_last_attempt(self, message, tmp_path):
        from waprinter.send.whatsapp import UPLOAD_ATTEMPTS

        pdf = tmp_path / "receipt.pdf"
        pdf.write_bytes(b"%PDF-1.4 test %%EOF")
        route = respx.post(url__regex=r".*/media$").mock(
            side_effect=httpx.RemoteProtocolError("Server disconnected")
        )

        sender = WhatsAppCloudSender("123", "token")
        sender._client = httpx.Client(timeout=5)
        result = sender.send("+919492787875", pdf, message)

        assert not result.ok
        assert "Upload failed" in result.error
        assert route.call_count == UPLOAD_ATTEMPTS

    @respx.mock
    def test_a_rejected_upload_is_not_retried(self, message, tmp_path):
        """An expired token fails identically every time; retrying just stalls."""
        pdf = tmp_path / "receipt.pdf"
        pdf.write_bytes(b"%PDF-1.4 test %%EOF")
        route = respx.post(url__regex=r".*/media$").mock(
            return_value=httpx.Response(
                401, json={"error": {"code": 190, "message": "expired token"}}
            )
        )

        sender = WhatsAppCloudSender("123", "token")
        sender._client = httpx.Client(timeout=5)
        result = sender.send("+919492787875", pdf, message)

        assert not result.ok
        assert route.call_count == 1

    @respx.mock
    def test_a_dropped_template_send_is_never_retried(self, message, tmp_path):
        """Retrying an unanswered send would risk two receipts to one customer."""
        pdf = tmp_path / "receipt.pdf"
        pdf.write_bytes(b"%PDF-1.4 test %%EOF")
        respx.post(url__regex=r".*/media$").mock(
            return_value=httpx.Response(200, json={"id": "media-123"})
        )
        route = respx.post(url__regex=r".*/messages$").mock(
            side_effect=httpx.RemoteProtocolError("Server disconnected")
        )

        sender = WhatsAppCloudSender("123", "token")
        sender._client = httpx.Client(timeout=5)
        result = sender.send("+919492787875", pdf, message)

        assert not result.ok
        assert route.call_count == 1, "an unanswered send must not be repeated"
        assert "may or may not have been sent" in result.error
        assert result.retryable is False


class TestIPv4Fallback:
    """A counter PC with a black-holed IPv6 route must still get the receipt out.

    TCP connects, so every reachability check passes; only a payload large
    enough to need fragmenting dies, and it dies with no response at all.
    """

    @respx.mock
    def test_a_dropped_upload_moves_the_session_to_ipv4(self, message, tmp_path):
        pdf = tmp_path / "receipt.pdf"
        pdf.write_bytes(b"%PDF-1.4 test %%EOF")
        respx.post(url__regex=r".*/media$").mock(
            side_effect=[
                httpx.RemoteProtocolError("Server disconnected without sending a response"),
                httpx.Response(200, json={"id": "media-123"}),
            ]
        )
        respx.post(url__regex=r".*/messages$").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})
        )

        sender = WhatsAppCloudSender("123", "token")
        assert sender._ipv4_only is False
        result = sender.send("+919492787875", pdf, message)

        assert result.ok, result.error
        assert sender._ipv4_only is True, "the session should have moved to IPv4"

    def test_the_setting_starts_on_ipv4_without_a_failed_upload_first(self):
        sender = WhatsAppCloudSender("123", "token", force_ipv4=True)
        assert sender._ipv4_only is True

    def test_a_caller_supplied_client_is_never_swapped(self):
        """The tests' own client, and anything injected, must survive."""
        mine = httpx.Client()
        sender = WhatsAppCloudSender("123", "token", client=mine)
        assert sender._fall_back_to_ipv4() is False
        assert sender._client is mine

    def test_the_fallback_happens_once(self):
        sender = WhatsAppCloudSender("123", "token")
        assert sender._fall_back_to_ipv4() is True
        assert sender._fall_back_to_ipv4() is False


class TestTemplateSync:
    """The stored status decides whether a send is allowed, so it must be Meta's.

    The dead end this removes: `chits_details` shipped as "pending", Meta had
    it approved, and nothing in the app could change that. The template read as
    unusable for ever and the app refused to send a perfectly good message.
    """

    def _meta_entry(self, **over):
        entry = {
            "name": "chits_details",
            "language": "en",
            "status": "APPROVED",
            "category": "UTILITY",
            "parameter_format": "NAMED",
            "components": [
                {"type": "HEADER", "format": "DOCUMENT"},
                {"type": "BODY", "text": "Dear {{customer_name}}, receipt {{receipt_no}}."},
                {"type": "FOOTER", "text": "Srinidhi Chit Funds"},
            ],
        }
        entry.update(over)
        return entry

    def test_an_approved_template_becomes_usable(self):
        from waprinter.cli import _template_from_meta

        t = _template_from_meta(self._meta_entry())
        assert t.status == "approved"
        assert t.header_document is True
        assert t.usable is True
        assert t.named is True
        assert t.placeholders == ["customer_name", "receipt_no"]
        assert t.footer == "Srinidhi Chit Funds"

    def test_a_pending_template_stays_unusable(self):
        from waprinter.cli import _template_from_meta

        assert _template_from_meta(self._meta_entry(status="PENDING")).usable is False

    def test_a_template_without_a_document_header_is_not_usable(self):
        """There would be nowhere to attach the PDF."""
        from waprinter.cli import _template_from_meta

        entry = self._meta_entry(
            components=[{"type": "BODY", "text": "Dear {{1}}."}]
        )
        t = _template_from_meta(entry)
        assert t.status == "approved"
        assert t.header_document is False
        assert t.usable is False

    def test_a_text_header_is_not_a_document_header(self):
        from waprinter.cli import _template_from_meta

        entry = self._meta_entry(
            components=[
                {"type": "HEADER", "format": "TEXT", "text": "Receipt"},
                {"type": "BODY", "text": "Dear {{1}}."},
            ]
        )
        assert _template_from_meta(entry).header_document is False

    def test_syncing_replaces_a_stale_pending_status(self, tmp_path):
        from waprinter.cli import _template_from_meta
        from waprinter.send.templates import TemplateStore

        store = TemplateStore(tmp_path / "templates.json")
        assert store.get("chits_details").usable is False   # as shipped

        store.put(_template_from_meta(self._meta_entry()))

        assert TemplateStore(tmp_path / "templates.json").get("chits_details").usable
