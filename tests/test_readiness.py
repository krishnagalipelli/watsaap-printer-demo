"""Whether this install is ready to send, and how the panel reports it.

The failure mode being guarded against is discovering a misconfiguration when a
customer's receipt does not arrive, rather than on the screen beforehand.
"""

from __future__ import annotations

from waprinter.config import Settings
from waprinter.send.readiness import is_ready, problems


class TestProblems:
    def test_an_unconfigured_install_is_not_ready(self):
        assert problems(Settings()) != []
        assert is_ready(Settings()) is False

    def test_missing_own_numbers_is_reported(self):
        # Without it, a number in the client's own letterhead can be treated as
        # a customer.
        found = problems(Settings(own_numbers=[]))
        assert any("own numbers" in p.lower() for p in found)

    def test_missing_credentials_are_reported(self):
        found = " ".join(problems(Settings(own_numbers=["9845012345"])))
        assert "phone number ID" in found
        assert "access token" in found

    def test_an_unapproved_message_is_reported(self, templates, monkeypatch, tmp_path):
        template = templates.get("invoice_document")
        template.status = "pending"
        templates.put(template)
        monkeypatch.setenv("WAPRINTER_HOME", str(templates.path.parent))
        found = " ".join(problems(Settings(own_numbers=["9845012345"])))
        assert "not yet approved" in found


class TestControlPanel:
    def test_the_status_page_lists_what_is_outstanding(self, client):
        assert "Before this can send" in client.get("/").text

    def test_it_explains_how_to_use_the_printer(self, client):
        body = client.get("/").text
        assert "File → Print" in body
        assert "WhatsApp Printer" in body

    def test_test_send_refuses_when_not_configured(self, client):
        response = client.post("/test-send")
        assert response.status_code == 303
        assert "Cannot send yet" in flash_of(response)

    def test_test_send_reports_test_mode_when_otherwise_ready(
        self, client, pipeline, monkeypatch
    ):
        monkeypatch.setattr(
            "waprinter.send.readiness.problems", lambda *a, **k: []
        )
        assert "Test mode is on" in flash_of(client.post("/test-send"))

    def test_no_trace_of_the_unofficial_sender_remains(self, client):
        # WhatsApp Web/Baileys was removed: it risked a ban on the client's
        # main business number.
        for path in ("/", "/settings", "/queue", "/history"):
            assert "baileys" not in client.get(path).text.lower()


def flash_of(response) -> str:
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(response.headers["location"]).query)
    return (query.get("msg") or [""])[0]
