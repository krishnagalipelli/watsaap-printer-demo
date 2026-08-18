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
