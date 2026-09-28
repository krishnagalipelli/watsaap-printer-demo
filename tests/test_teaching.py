"""Teaching a new kind of document from one sample, and reading it back.

The promise being tested: someone clicks the value beside "Cust ID" on one
sample, calls it Customer ID, and from then on every print of that document
fills {{id}} in the template with the right member's ID -- found by its label,
not by where it happened to sit on the sample.
"""

from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

from waprinter.config import Settings, paths
from waprinter.extract import pdf_text
from waprinter.extract.profile import DocumentKind, DocumentProfile, FieldRule
from waprinter.extract.rules import (
    Selection,
    guess_kind,
    infer_pattern,
    read_rule,
    rule_from_selection,
)


def statement(path: Path, cust_id="SCF-00418", name="ANITHA RAMESH",
              mobile="9000012345", shift=0.0, title="MEMBER STATEMENT") -> Path:
    """A member statement. `shift` moves everything down the page, the way a
    longer letterhead would, to prove rules do not depend on position."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    def text(x, y, value, size=10):
        page.insert_text((x, y + shift), value, fontsize=size)

    text(60, 70, title, 16)
    text(60, 110, "Cust ID :")
    text(140, 110, cust_id)
    text(330, 110, "Statement Date: 13-Aug-26")
    text(60, 130, "Name")
    text(140, 130, name)
    text(330, 130, f"Mobile : {mobile}")
    text(60, 160, "Chit Group")
    text(60, 174, "KRM-12")
    text(60, 200, "Balance Due  Rs. 12,450.00")
    doc.save(path)
    doc.close()
    return path


def select(doc, word: str, through: str | None = None) -> Selection:
    page = doc.pages[0]
    for row in page.rows:
        texts = [w.text for w in row.words]
        if word in texts:
            first = texts.index(word)
            last = texts.index(through) if through else first
            return Selection(page, row, first, last)
    raise AssertionError(f"{word} is not on the page")


@pytest.fixture
def sample(tmp_path):
    return statement(tmp_path / "statement.pdf")


@pytest.fixture
def doc(sample):
    return pdf_text.read(sample)


class TestLearningFromAClick:
    def test_the_label_to_the_left_is_what_is_stored(self, doc):
        rule = rule_from_selection(select(doc, "SCF-00418"), "customer_id")
        assert rule.label == "Cust ID"
        assert rule.kind == "code"
        assert rule.where == "right"

    def test_a_heading_above_is_used_when_nothing_is_beside_it(self, doc):
        rule = rule_from_selection(select(doc, "KRM-12"), "chit_group")
        assert (rule.label, rule.where) == ("Chit Group", "below")

    def test_a_currency_word_is_not_part_of_the_label(self, doc):
        """Printed "Rs." today, "₹" by the next release of the same software."""
        rule = rule_from_selection(select(doc, "12,450.00"), "balance")
        assert rule.label == "Balance Due"
        assert rule.kind == "amount"

    def test_a_name_can_span_words(self, doc):
        sel = select(doc, "ANITHA", through="RAMESH")
        assert sel.value == "ANITHA RAMESH"
        assert rule_from_selection(sel, "customer_name").kind == "text"

    @pytest.mark.parametrize("value,kind", [
        ("9000012345", "phone"), ("13-Aug-26", "date"), ("12,450.00", "amount"),
        ("Rs. 450", "amount"), ("SCF-00418", "code"), ("ANITHA RAMESH", "text"),
        ("418", "code"),
    ])
    def test_what_kind_of_value_it_is(self, value, kind):
        assert guess_kind(value) == kind

    def test_a_pattern_keeps_the_shape_and_frees_the_length(self):
        import re

        pattern = re.compile(infer_pattern("SCF-00418"))
        assert pattern.search("Cust SCF-12 x").group(1) == "SCF-12"
        assert pattern.search("scf-12") is None          # case is part of the shape
        assert re.search(infer_pattern("CR1747/26"), "CR9/2026").group(1) == "CR9/2026"


class TestReadingItBack:
    def test_a_rule_reads_the_same_value_on_another_print(self, doc, tmp_path):
        rules = [
            rule_from_selection(select(doc, "SCF-00418"), "customer_id"),
            rule_from_selection(select(doc, "ANITHA", "RAMESH"), "customer_name"),
            rule_from_selection(select(doc, "KRM-12"), "chit_group"),
            rule_from_selection(select(doc, "13-Aug-26"), "statement_date"),
        ]
        other = pdf_text.read(statement(tmp_path / "other.pdf", cust_id="SCF-7",
                                        name="RAVI KUMAR", shift=45))
        assert [read_rule(other, r) for r in rules] == [
            "SCF-7", "RAVI KUMAR", "KRM-12", "13-Aug-26",
        ]

    def test_a_value_is_never_taken_from_the_next_column(self, doc):
        """"Name   ANITHA RAMESH   Mobile : 9000..." is one row of text."""
        rule = FieldRule(name="customer_name", label="Name", kind="text")
        assert read_rule(doc, rule) == "ANITHA RAMESH"

    def test_an_unlabelled_value_is_found_by_its_shape(self, doc):
        rule = FieldRule(name="balance", kind="code", pattern=infer_pattern("12,450.00"))
        assert read_rule(doc, rule) == "12,450.00"

    def test_literal_prefix_keeps_other_identifiers_out(self, doc):
        rule = FieldRule(name="group", kind="code", pattern=infer_pattern("KRM-12"))
        assert read_rule(doc, rule) == "KRM-12"
        import re
        assert re.search(infer_pattern("SCF-00418"), "RN-317") is None

    def test_a_label_that_is_not_there_reads_nothing(self, doc):
        assert read_rule(doc, FieldRule(name="x", label="Policy No")) is None


class TestTheProfile:
    def test_taught_types_are_tried_before_built_in_ones(self):
        profile = DocumentProfile(custom_kinds=[
            DocumentKind(name="member_statement", match=["member statement"]),
        ])
        # A scan of a statement may well say "received from" somewhere too.
        kind = profile.kind_of("MEMBER STATEMENT ... received from")
        assert kind.name == "member_statement"

    def test_receipts_share_the_default_fields(self):
        profile = DocumentProfile(field_rules={"_default": [FieldRule(name="a", label="A")]})
        assert profile.rules_for(None)[0].name == "a"
        assert profile.rules_for("receipt")[0].name == "a"
        assert profile.rules_for("removal_notice") == []

    def test_a_taught_mobile_label_strengthens_the_scorer(self):
        profile = DocumentProfile(field_rules={
            "_default": [FieldRule(name="customer_mobile", label="Contact Cell", kind="phone")],
        })
        assert "Contact Cell" in profile.for_kind(None).phone_labels
        assert "Contact Cell" not in profile.phone_labels

    def test_one_broken_entry_does_not_lose_the_rest(self, tmp_path):
        """Dropping the whole file would drop every taught *type*, and a
        notice nobody recognises goes out as a receipt."""
        path = tmp_path / "profile.json"
        path.write_text(json.dumps({
            "custom_kinds": [{"name": "member_statement", "match": ["MEMBER STATEMENT"]},
                             {"name": "", "match": []}],
            "field_rules": {"member_statement": [
                {"name": "customer_id", "label": "Cust ID", "kind": "code"},
                {"name": "broken", "label": "X", "pattern": "([unclosed"},
            ]},
        }))
        profile = DocumentProfile.load(path)
        assert [k.name for k in profile.custom_kinds] == ["member_statement"]
        assert [r.name for r in profile.rules_for("member_statement")] == ["customer_id"]

    def test_saving_taught_fields_leaves_other_overrides_alone(self, tmp_path):
        path = tmp_path / "profile.json"
        path.write_text(json.dumps({"phone_labels": ["cell"]}))
        DocumentProfile.write_taught(path, [], {"_default": [FieldRule(name="a", label="A")]})
        raw = json.loads(path.read_text())
        assert raw["phone_labels"] == ["cell"]
        assert raw["field_rules"]["_default"][0]["label"] == "A"
        # The built-in lists were not copied in, so later defaults still arrive.
        assert "not_phone_labels" not in raw


class TestTheWholeWay:
    """From a taught profile.json to a message, through the real pipeline."""

    @pytest.fixture
    def taught(self, sample):
        from waprinter import teaching

        doc = pdf_text.read(sample)
        rules = [rule_from_selection(select(doc, "SCF-00418"), "customer_id"),
                 rule_from_selection(select(doc, "ANITHA", "RAMESH"), "customer_name")]
        teaching.save_teaching(
            "member_statement", rules, sample=sample,
            new_kind=DocumentKind(name="member_statement", match=["MEMBER STATEMENT"]),
        )
        return DocumentProfile.load(paths().profile)

    @pytest.fixture
    def statement_pipeline(self, pipeline, taught):
        from waprinter.send.templates import MessageTemplate

        pipeline.profile = taught
        pipeline.templates.put(MessageTemplate(
            name="member_statement", status="approved", parameter_format="named",
            body="Dear {{name}}, your statement for member {{id}} is attached.",
        ))
        pipeline.settings.document_templates = {"member_statement": "member_statement"}
        pipeline.settings.template_mappings = {
            "member_statement": {"name": "customer_name", "id": "customer_id"},
        }
        return pipeline

    def test_the_taught_value_reaches_the_message(self, statement_pipeline, tmp_path):
        pdf = statement(tmp_path / "print.pdf", cust_id="SCF-991", name="RAVI KUMAR",
                        mobile="9000054321", shift=30)
        job = statement_pipeline.process(pdf)
        assert job.fields.document_kind == "member_statement"
        assert job.fields.extra == {"customer_id": "SCF-991"}
        assert job.template_name == "member_statement"
        assert "member SCF-991" in job.message_preview
        assert "Dear RAVI KUMAR" in job.message_preview

    def test_a_mapped_value_missing_from_a_print_holds_it(self, statement_pipeline, tmp_path):
        """A template someone mapped says the value belongs in the message.
        "member -" is the silent failure the mapping exists to prevent."""
        from waprinter.models import JobStatus

        pdf = statement(tmp_path / "print.pdf", cust_id="")
        job = statement_pipeline.process(pdf)
        assert job.status is JobStatus.HELD
        assert "customer id" in job.hold_reason
        assert "member_statement" in job.hold_reason

    def test_the_taught_field_survives_the_database(self, statement_pipeline, tmp_path):
        job = statement_pipeline.process(statement(tmp_path / "p.pdf"))
        assert statement_pipeline.store.get(job.id).fields.extra["customer_id"] == "SCF-00418"

    def test_a_template_with_no_mapping_behaves_as_it_always_did(self, pipeline, make_invoice):
        job = pipeline.process(make_invoice())
        assert job.template_name == "invoice_document"
        assert job.status.value in ("dry_run", "sent")

    def test_readiness_names_a_taught_type_with_no_message(self, taught):
        from waprinter.send.readiness import problems

        found = " ".join(problems(Settings(), profile=taught))
        assert "Member statement documents have no message yet" in found

    def test_readiness_names_an_unfilled_variable(self, statement_pipeline):
        from waprinter.send.readiness import problems

        statement_pipeline.settings.template_mappings["member_statement"] = {"name": "customer_name"}
        found = " ".join(problems(statement_pipeline.settings, statement_pipeline.templates))
        assert "has {{id}} with nothing to fill it" in found


class TestTheTeachingService:
    def test_titles_are_suggested_from_the_top_of_the_page(self, doc):
        from waprinter import teaching

        assert teaching.suggest_titles(doc, DocumentProfile())[0] == "MEMBER STATEMENT"

    def test_a_title_on_another_types_sample_is_refused(self, doc, tmp_path):
        from waprinter import teaching

        other = statement(tmp_path / "notice.pdf", title="REMOVAL NOTICE MEMBER STATEMENT")
        teaching.save_teaching("removal_notice", [], sample=other)
        problems = teaching.title_problems("MEMBER STATEMENT", doc, DocumentProfile(),
                                           "member_statement")
        assert any("removal notice sample" in p for p in problems)
        assert "MEMBER STATEMENT" not in teaching.suggest_titles(doc, DocumentProfile())

    def test_a_title_not_on_the_sample_is_refused(self, doc):
        from waprinter import teaching

        problems = teaching.title_problems("PAYSLIP", doc, DocumentProfile(), "x")
        assert any("does not appear" in p for p in problems)

    def test_reserved_and_duplicate_names_are_refused(self):
        from waprinter import teaching

        profile = DocumentProfile(custom_kinds=[DocumentKind(name="statement", match=["S T"])])
        assert teaching.name_problems("Receipt", profile)
        assert teaching.name_problems("Statement", profile)
        assert teaching.name_problems("Removal notice", profile)
        assert not teaching.name_problems("Payslip", profile)

    def test_a_rule_that_reads_something_else_is_reported(self, doc):
        from waprinter import teaching

        rule = FieldRule(name="balance", label="Due", kind="amount")
        problems = teaching.rule_problems(doc, [rule], {"balance": "99.00"})
        assert problems and "12,450.00" in problems[0]

    def test_removing_a_type_forgets_its_fields_and_sample(self, sample):
        from waprinter import teaching

        teaching.save_teaching("member_statement", [FieldRule(name="a", label="A")],
                               sample=sample,
                               new_kind=DocumentKind(name="member_statement", match=["M S"]))
        assert teaching.sample_path("member_statement").exists()
        profile = teaching.remove_kind("member_statement")
        assert profile.custom_kinds == [] and profile.rules_for("member_statement") == []
        assert not teaching.sample_path("member_statement").exists()

    def test_a_trial_reads_without_saving_or_sending(self, pipeline, sample, doc):
        from waprinter import teaching

        rules = [rule_from_selection(select(doc, "SCF-00418"), "customer_id")]
        profile = teaching.candidate_profile(
            DocumentProfile(), "member_statement", rules,
            DocumentKind(name="member_statement", match=["MEMBER STATEMENT"]),
        )
        rows = teaching.trial(profile, [sample], Settings())
        assert rows[0].kind == "Member statement"
        assert rows[0].values["customer_id"] == "SCF-00418"
        assert "+919000012345" in rows[0].mobile
        assert not paths().profile.exists()
        assert pipeline.store.recent() == []

    def test_example_values_come_from_the_latest_print_of_that_type(self, pipeline, make_invoice):
        from waprinter import teaching

        pipeline.process(make_invoice())
        values = teaching.example_values(pipeline.store, pipeline.profile, "_default")
        assert values["invoice_number"] == "INV-2291"


def test_a_new_types_title_is_checked_against_the_receipt_sample_too(tmp_path):
    """A blank kind means "the default" everywhere else, and the new type's
    title check used to skip the receipts sample because of it."""
    from waprinter import teaching

    receipt = statement(tmp_path / "receipt.pdf", title="SRINIDHI CHIT FUNDS")
    teaching.save_teaching("_default", [], sample=receipt)
    new = pdf_text.read(statement(tmp_path / "new.pdf", title="SRINIDHI CHIT FUNDS"))
    assert "SRINIDHI CHIT FUNDS" not in teaching.suggest_titles(new, DocumentProfile())
    problems = teaching.title_problems("SRINIDHI CHIT FUNDS", new, DocumentProfile(),
                                       "member_statement")
    assert any("receipts sample" in p for p in problems)
