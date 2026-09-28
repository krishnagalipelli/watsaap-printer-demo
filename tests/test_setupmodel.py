"""What the setup screens decide, tested without a display."""

from __future__ import annotations

import json

import pytest

from waprinter.config import Settings, coerce_setting
from waprinter.extract.profile import DocumentKind, DocumentProfile, FieldRule
from waprinter.send.templates import MessageTemplate, TemplateStore, render
from waprinter.ui import setupmodel as sm


def named(body: str, **kw) -> MessageTemplate:
    return MessageTemplate(name="t", body=body, status="approved",
                           parameter_format="named", **kw)


class TestSuggestingAMapping:
    def test_id_finds_customer_id(self):
        """The case from the brief: the page says customer id, the template
        says {{id}}."""
        guess = sm.suggest_mapping(named("{{id}}"), ["customer_name", "customer_id"])
        assert guess == {"id": "customer_id"}

    @pytest.mark.parametrize("variable,field", [
        ("receipt_no", "invoice_number"), ("name", "customer_name"),
        ("date", "invoice_date"), ("mode", "payment_mode"), ("company", "business_name"),
        ("customer_name", "customer_name"),
    ])
    def test_the_usual_words(self, variable, field):
        fields = ["customer_name", "invoice_number", "invoice_date", "payment_mode"]
        assert sm.suggest_mapping(named("{{%s}}" % variable), fields)[variable] == field

    def test_a_positional_template_takes_the_old_shared_map(self):
        template = MessageTemplate(name="t", body="{{1}} {{2}}")
        guess = sm.suggest_mapping(template, ["customer_name"],
                                   legacy={"1": "customer_name", "2": "business_name"})
        assert guess == {"1": "customer_name", "2": "business_name"}

    def test_what_was_saved_wins(self):
        guess = sm.suggest_mapping(named("{{id}}"), ["customer_id"],
                                   current={"id": "text:ABC"})
        assert guess == {"id": "text:ABC"}

    def test_nothing_is_guessed_from_nothing(self):
        assert sm.suggest_mapping(named("{{zzz}}"), ["customer_name"]) == {}


class TestTheSharedMapOnlyFitsItsOwnTemplate:
    """The shipped numbered map describes the chit receipt's body.

    Offered to invoice_document it produced "Your invoice Srinidhi Chit Funds
    for ₹INV-2291" -- the business name in the invoice number's place and the
    number in the amount's. Found by running the agent on a test print.
    """

    FIELDS = ["customer_name", "invoice_number", "invoice_date", "total_amount",
              "amount_words", "payment_mode"]

    def _store(self, tmp_path):
        return TemplateStore(tmp_path / "t.json", "Srinidhi Chit Funds")

    def _suggest(self, template, default_template, template_variables=None):
        shared = sm.shared_map_for(
            template, template_variables or Settings().template_variables, default_template
        )
        return sm.suggest_mapping(template, self.FIELDS, legacy=shared)

    def test_invoice_document_is_not_given_the_receipts_numbers(self, tmp_path):
        invoice = self._store(tmp_path).get("invoice_document")
        guess = self._suggest(invoice, default_template="invoice_document")
        assert guess == {"1": "customer_name", "2": "invoice_number", "3": "total_amount"}

    def test_the_message_it_suggests_reads_correctly(self, tmp_path):
        from waprinter.models import ExtractedFields

        invoice = self._store(tmp_path).get("invoice_document")
        guess = self._suggest(invoice, default_template="invoice_document")
        text = render(invoice, guess, ExtractedFields(
            customer_name="Meghana Enterprises", invoice_number="INV-2291",
            total_amount="18,450.00")).preview
        assert "Your invoice INV-2291 for ₹18,450.00 is attached." in text

    def test_the_chit_receipt_keeps_the_map_written_for_it(self, tmp_path):
        receipt = self._store(tmp_path).get("chit_receipt")
        guess = self._suggest(receipt, default_template="chit_receipt")
        assert guess["2"] == "business_name"
        assert guess == {k: v for k, v in Settings().template_variables.items()
                         if k in receipt.placeholders}

    def test_a_map_edited_on_this_install_follows_its_own_template(self):
        """Someone changed the numbers for the template this counter uses;
        they were written for that template, and for no other."""
        mine = MessageTemplate(name="my_receipt", body="{{1}} paid {{2}}")
        other = MessageTemplate(name="other", body="{{1}} and {{2}}")
        edited = {**Settings().template_variables, "2": "amount_words"}
        assert self._suggest(mine, "my_receipt", edited)["2"] == "amount_words"
        assert "2" not in self._suggest(other, "my_receipt", edited)
        # ...and then the chit receipt no longer gets them either.
        assert sm.shared_map_for(MessageTemplate(name="chit_receipt", body="{{2}}"),
                                 edited, "my_receipt") == {
            k: v for k, v in edited.items() if not k.isdigit()}

    def test_named_entries_fit_any_template(self):
        template = named("Receipt {{receipt_no}} on {{date}}")
        shared = sm.shared_map_for(template, Settings().template_variables, "chit_receipt")
        assert "1" not in shared
        assert sm.suggest_mapping(template, self.FIELDS, legacy=shared) == {
            "receipt_no": "invoice_number", "date": "invoice_date"}

    @pytest.mark.parametrize("body,expected", [
        ("Dear {{1}},", "customer_name"),
        ("Receipt No.: {{1}}", "invoice_number"),
        ("Date: {{1}}", "invoice_date"),
        ("Amount Paid: ₹{{1}}", "total_amount"),
        ("Payment Mode: {{1}}", "payment_mode"),
        ("Thank you for your payment to {{1}}.", None),
        ("Line one\n{{1}} starts a line", None),
    ])
    def test_a_numbered_variable_is_guessed_only_from_the_words_before_it(
        self, body, expected
    ):
        template = MessageTemplate(name="t", body=body)
        assert sm.suggest_mapping(template, self.FIELDS).get("1") == expected

    def test_a_digit_in_a_field_name_is_not_a_match_for_a_position(self):
        template = MessageTemplate(name="t", body="Thanks {{2}}")
        assert sm.suggest_mapping(template, ["line_2"]) == {}


class TestFillingIn:
    def test_fixed_text_and_blank_are_filled_as_chosen(self):
        from waprinter.models import ExtractedFields

        template = named("{{name}} at {{branch}}{{note}}")
        message = render(template, {"name": "customer_name", "branch": "text:Karimnagar",
                                    "note": "(blank)"},
                         ExtractedFields(customer_name="Ravi"))
        assert message.parameters == ["Ravi", "Karimnagar", "-"]
        assert message.missing == []

    def test_unfilled_variables_block_saving(self):
        template = named("{{a}} {{b}} {{c}}")
        problems = sm.mapping_problems(template, {"a": "customer_name", "b": "text:  "})
        assert problems == ["{{b}} is set to fixed text, but the text is empty.",
                            "{{c}} is not filled in."]

    def test_the_preview_shows_real_values_or_says_what_goes_there(self):
        template = named("Dear {{name}}, member {{id}} of {{biz}}.")
        text = sm.preview(template, {"name": "customer_name", "id": "customer_id",
                                     "biz": "business_name"},
                          {"customer_name": "Ravi"}, "Srinidhi")
        assert text == "Dear Ravi, member [Customer id] of Srinidhi."

    def test_a_template_that_cannot_carry_a_pdf_says_why(self):
        assert "image header" in sm.template_problem(
            MessageTemplate(name="p", status="approved", header_document=False,
                            header_format="IMAGE"), "api")
        assert "waiting for Meta" in sm.template_problem(MessageTemplate(name="p"), "api")
        assert sm.template_problem(MessageTemplate(name="p"), "link") == ""


class TestTheChecklist:
    def test_a_fresh_install_has_everything_to_do(self, tmp_path):
        steps = {s.key: s for s in sm.checklist(
            Settings(send_mode="api"), TemplateStore(tmp_path / "t.json"),
            DocumentProfile(), "No access token is stored.")}
        assert not steps["connect"].done
        assert steps["connect"].detail == "No access token is stored."
        assert not steps["templates"].done

    def test_a_taught_type_without_a_message_is_unfinished(self, tmp_path):
        profile = DocumentProfile(custom_kinds=[DocumentKind(name="s", match=["STATEMENT"])])
        steps = {s.key: s for s in sm.checklist(
            Settings(), TemplateStore(tmp_path / "t.json"), profile, None)}
        assert not steps["messages"].done


class TestThePin:
    def test_the_right_pin_opens_and_the_wrong_one_does_not(self):
        stored = sm.hash_pin("4321")
        assert "4321" not in stored
        assert sm.check_pin("4321", stored)
        assert not sm.check_pin("1234", stored)
        assert sm.check_pin("anything", "")        # no PIN, no lock
        assert not sm.check_pin("4321", "garbage")

    @pytest.mark.parametrize("pin,ok", [("1234", True), ("12345678", True),
                                        ("123", False), ("12a4", False)])
    def test_what_counts_as_a_pin(self, pin, ok):
        assert (sm.pin_problem(pin) == "") is ok


class TestSettingsShape:
    def test_the_mapping_map_is_checked_all_the_way_down(self):
        _v, problem = coerce_setting("template_mappings", {"t": {"id": "customer_id"}})
        assert problem is None
        _v, problem = coerce_setting("template_mappings", {"t": "customer_id"})
        assert "map of maps" in problem

    def test_export_and_import_carry_taught_types(self, tmp_path, templates):
        from waprinter.config import paths
        from waprinter.provision import apply
        from waprinter.setup_profile import export_setup

        profile = DocumentProfile(
            custom_kinds=[DocumentKind(name="member_statement", match=["MEMBER STATEMENT"])],
            field_rules={"member_statement": [FieldRule(name="customer_id", label="Cust ID",
                                                        kind="code")]},
        )
        settings = Settings(template_mappings={"member_statement": {"id": "customer_id"}},
                            setup_pin=sm.hash_pin("4321"))
        target = tmp_path / "provision.json"
        export_setup(target, settings, templates, profile)
        data = json.loads(target.read_text())
        assert "access_token" not in data
        assert data["document_profile"]["custom_kinds"][0]["name"] == "member_statement"

        result = apply(target, remove=False)
        assert "document types" in result.applied and not result.warnings
        imported = DocumentProfile.load(paths().profile)
        assert imported.rules_for("member_statement")[0].label == "Cust ID"
        assert Settings.load().template_mappings == {"member_statement": {"id": "customer_id"}}

    def test_a_broken_taught_field_leaves_the_setup_file_unapplied(self, tmp_path):
        from waprinter.provision import apply

        target = tmp_path / "provision.json"
        target.write_text(json.dumps({
            "branch_name": "changed",
            "document_profile": {"field_rules": {"x": [{"name": "a", "pattern": "([oops"}]}},
        }))
        settings = Settings(branch_name="before")
        result = apply(target, settings)
        assert any("Invalid document types" in w for w in result.warnings)
        assert settings.branch_name == "before"
        assert target.exists()
