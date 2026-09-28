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
        found = " ".join(
            problems(
                Settings(
                    own_numbers=["9845012345"],
                    default_template="invoice_document",
                    send_mode="api",
                ),
                templates,
            )
        )
        assert "not yet approved" in found

    def test_link_mode_does_not_need_metas_approval(self, templates):
        # A wa.me link carries the text itself and a person presses send; no
        # template is involved on that route, so "pending" is not a problem.
        template = templates.get("chit_receipt")
        template.status = "pending"
        templates.put(template)
        found = " ".join(
            problems(
                Settings(
                    own_numbers=["9845012345"],
                    default_template="chit_receipt",
                    send_mode="link",
                ),
                templates,
            )
        )
        assert "approved" not in found

    def test_every_document_kinds_message_is_checked(self, templates):
        # A removal notice held with "Template 'removal_notce' is not
        # configured" is the same failure as a receipt held that way, and used
        # to be invisible here until the first notice was printed.
        found = " ".join(
            problems(
                Settings(
                    own_numbers=["9845012345"],
                    default_template="invoice_document",
                    send_mode="api",
                    document_templates={"removal_notice": "removal_notce"},
                ),
                templates,
            )
        )
        assert "'removal_notce' is not configured" in found
