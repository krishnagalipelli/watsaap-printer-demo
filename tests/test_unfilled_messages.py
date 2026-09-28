"""A message whose variables have nothing to fill them is never sent.

Found by running the agent: with invoice_document as the message and the
shipped variable map, a test print went out as "Your invoice Srinidhi Chit
Funds for ₹INV-2291". The shared map's numbers describe the chit receipt's
body, where {{2}} is the business name; in invoice_document {{2}} is the
invoice number. Fixing the setup screen's suggestion was not enough on its
own: a template picked through provision.json or settings.json never passes
through that screen, and was filled from the same numbers at send time.

Now a variable with no real source -- a number the shared map was not written
for, a name no field answers to, a mapping to a field that is gone -- holds
the document with the reason, on every path out: automatic, link, the
confirmation dialog and the queue. The Status tab says it before anything is
printed.
"""

from __future__ import annotations

import pytest

from waprinter.config import Settings
from waprinter.models import JobStatus
from waprinter.send.templates import MessageTemplate

HOLD = "with nothing to fill"


@pytest.fixture
def shipped(pipeline):
    """invoice_document as the message, with the shipped shared map --
    the configuration the agent was run with."""
    pipeline.settings.default_template = "invoice_document"
    pipeline.settings.template_variables = Settings().template_variables
    pipeline.settings.template_mappings = {}
    return pipeline


def sent_lines(pipeline) -> list[str]:
    path = pipeline.sender.log_path
    return path.read_text().splitlines() if path.exists() else []


class TestNothingIsSentWithAGuessedNumber:
    def test_the_print_is_held_and_says_why(self, shipped, make_invoice):
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.HELD
        assert "'invoice_document' message has {{1}}, {{2}}, {{3}}" in job.hold_reason
        assert "Setup → Fill in messages" in job.hold_reason
        assert sent_lines(shipped) == []

    def test_link_mode_does_not_prepare_a_chat_either(self, shipped, make_invoice):
        shipped.settings.send_mode = "link"
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.HELD
        assert job.chat_url is None

    def test_confirmation_mode_does_not_ask_for_a_number_it_cannot_use(
        self, shipped, make_invoice
    ):
        shipped.settings.confirm_before_send = True
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.HELD
        assert HOLD in job.hold_reason

    def test_sending_it_from_the_queue_is_refused_and_changes_nothing(
        self, shipped, make_invoice
    ):
        job = shipped.process(make_invoice())
        with pytest.raises(ValueError, match="Setup → Fill in messages"):
            shipped.release(job.id, "9876543210")
        after = shipped.store.get(job.id)
        assert after.status is JobStatus.HELD
        assert after.hold_reason == job.hold_reason
        assert sent_lines(shipped) == []

    def test_once_it_is_mapped_it_sends_the_right_words(self, shipped, make_invoice):
        shipped.settings.template_mappings = {
            "invoice_document": {"1": "customer_name", "2": "invoice_number",
                                 "3": "total_amount"},
        }
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.DRY_RUN
        assert "Your invoice INV-2291 for ₹18,450.00 is attached." in job.message_preview
        assert "Srinidhi Chit Funds for" not in job.message_preview


class TestWhatStillSends:
    def test_the_chit_receipt_keeps_its_shared_numbers(self, shipped, make_invoice):
        shipped.settings.default_template = "chit_receipt"
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.DRY_RUN
        assert f"payment to {shipped.settings.business_name}" in job.message_preview

    def test_numbers_edited_for_this_install_fill_its_own_template(
        self, pipeline, make_invoice
    ):
        """The conftest settings: invoice_document with numbers edited for it."""
        job = pipeline.process(make_invoice())
        assert job.status is JobStatus.DRY_RUN
        assert "INV-2291" in job.message_preview

    def test_a_named_template_fills_by_name_as_before(self, shipped, make_invoice):
        shipped.templates.put(MessageTemplate(
            name="named_receipt", status="approved", parameter_format="named",
            body="Dear {{customer_name}}, receipt {{receipt_no}}.",
        ))
        shipped.settings.default_template = "named_receipt"
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.DRY_RUN
        assert "receipt INV-2291." in job.message_preview

    def test_fixed_text_and_a_deliberate_blank_count_as_filled(
        self, shipped, make_invoice
    ):
        shipped.settings.template_mappings = {
            "invoice_document": {"1": "customer_name", "2": "text:INV", "3": "(blank)"},
        }
        assert shipped.process(make_invoice()).status is JobStatus.DRY_RUN


class TestOtherWaysAVariableHasNoSource:
    def test_a_name_no_field_answers_to(self, shipped, make_invoice):
        shipped.templates.put(MessageTemplate(
            name="statement", status="approved", parameter_format="named",
            body="Dear {{customer_name}}, member {{id}}.",
        ))
        shipped.settings.default_template = "statement"
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.HELD
        assert "has {{id}} with nothing to fill it" in job.hold_reason

    def test_a_mapping_to_a_taught_field_that_was_removed(self, shipped, make_invoice):
        shipped.settings.template_mappings = {
            "invoice_document": {"1": "customer_name", "2": "customer_id",
                                 "3": "total_amount"},
        }
        job = shipped.process(make_invoice())
        assert job.status is JobStatus.HELD
        assert "{{2}}" in job.hold_reason

    def test_a_taught_field_is_a_source(self, shipped, make_invoice):
        from waprinter.extract.profile import FieldRule

        shipped.profile.field_rules["_default"] = [
            FieldRule(name="customer_id", label="Cust ID", kind="code")]
        shipped.settings.template_mappings = {
            "invoice_document": {"1": "customer_name", "2": "customer_id",
                                 "3": "total_amount"},
        }
        assert shipped.unfilled_reason(shipped.templates.get("invoice_document")) == ""


class TestTheStatusTabSaysSoFirst:
    def test_readiness_names_the_message_and_its_variables(self, shipped):
        from waprinter.send.readiness import problems

        found = " ".join(problems(shipped.settings, shipped.templates, shipped.profile))
        assert "'invoice_document' message has {{1}}, {{2}}, {{3}}" in found

    def test_the_shipped_chit_receipt_is_not_reported(self, shipped):
        from waprinter.send.readiness import problems

        shipped.settings.default_template = "chit_receipt"
        found = " ".join(problems(shipped.settings, shipped.templates, shipped.profile))
        assert HOLD not in found

    def test_go_live_refuses_it(self, shipped, monkeypatch, capsys):
        from waprinter import cli

        monkeypatch.setattr("waprinter.secrets.load_token", lambda: "EAAG" + "x" * 60)
        s = shipped.settings
        s.phone_number_id, s.send_mode = "1", "api"
        s.save()
        assert cli.cmd_go_live(None) == 1
        assert HOLD in capsys.readouterr().out
