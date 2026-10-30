"""One document type whose payment details change with how the member paid.

Cash prints one word. An online payment prints the method, a transaction number
and more, over several lines. Both have to be read by the same taught fields,
whichever of the two the fields were taught on -- and neither may hold the other
up. The layout is the client's: the colon is printed with the value, as its own
block from the label, which is what broke finding the label in the first place.
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from waprinter import teaching, validation
from waprinter.config import Settings
from waprinter.extract import pdf_text
from waprinter.extract.profile import DocumentKind, DocumentProfile, FieldRule
from waprinter.extract.rules import (
    Selection,
    label_at,
    label_beside,
    literal_only,
    read_rule,
    rule_from_selection,
    selection_between,
)

ONLINE = ["ONLINE-UPI", "Txn ID: T9981234", "HDFC Bank, Karimnagar"]


def receipt(path: Path, payment: list[str], name="KRISHNA", mobile="9876543210",
            bill="INV-2026-00192") -> Path:
    doc = fitz.open()
    page = doc.new_page(width=420, height=595)

    def pair(y, label, value):
        page.insert_text((30, y), label, fontsize=10)
        page.insert_text((110, y), f": {value}", fontsize=10)

    page.insert_text((30, 30), "LAB RECEIPT", fontsize=14)
    pair(160, "Name", name)
    pair(175, "Mobile no", mobile)
    pair(190, "Bill no", bill)
    page.insert_text((30, 210), "Blood Group", fontsize=10)
    pair(225, "Paymode", payment[0])
    page.insert_text((260, 225), "Total Amt :", fontsize=10)
    page.insert_text((340, 225), "95.00", fontsize=10)
    y = 225
    for line in payment[1:]:
        y += 14
        page.insert_text((116, y), line, fontsize=10)
    page.insert_text((30, y + 14), "Paid Amt :", fontsize=10)
    page.insert_text((110, y + 14), "95.00", fontsize=10)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def cash(tmp_path):
    return pdf_text.read(receipt(tmp_path / "cash.pdf", ["CASH"]))


@pytest.fixture
def online(tmp_path):
    return pdf_text.read(receipt(tmp_path / "online.pdf", ONLINE))


def where(doc, word):
    rows = doc.pages[0].rows
    for r, row in enumerate(rows):
        for w, found in enumerate(row.words):
            if found.text == word:
                return rows, r, w
    raise AssertionError(f"{word} is not on the page")


def click(doc, word) -> Selection:
    rows, r, w = where(doc, word)
    return Selection(doc.pages[0], rows[r], w, w)


def drag(doc, start, end) -> Selection:
    rows, r0, w0 = where(doc, start)
    _rows, r1, w1 = where(doc, end)
    return selection_between(doc.pages[0], rows, (r0, w0), (r1, w1))


class TestFindingTheLabel:
    def test_a_colon_printed_with_the_value_does_not_hide_the_label(self, cash):
        """It used to fall through to the line above: "Blood Group" read "CBP"."""
        assert label_beside(click(cash, "CASH")) == "Paymode"
        assert rule_from_selection(click(cash, "CASH"), "payment_mode").where == "right"

    def test_a_name_is_found_by_its_label_and_not_by_the_name_itself(self, cash, tmp_path):
        rule = rule_from_selection(click(cash, "KRISHNA"), "customer_name")
        assert rule.label == "Name" and not rule.pattern
        other = pdf_text.read(receipt(tmp_path / "o.pdf", ["CASH"], name="ASHA"))
        assert read_rule(other, rule) == "ASHA"

    def test_a_label_set_in_one_block_with_the_value_before_it(self, tmp_path):
        """"140.00 Paymode" on the bill: only the words after the figure."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((30, 50), "Total Amt", fontsize=10)
        page.insert_text((110, 50), "140.00 Paymode", fontsize=10)
        page.insert_text((260, 50), "Cash", fontsize=10)
        doc.save(tmp_path / "bill.pdf")
        read = pdf_text.read(tmp_path / "bill.pdf")
        assert label_beside(click(read, "Cash")) == "Paymode"

    def test_a_label_someone_points_at_wins(self, cash):
        rows, r, w = where(cash, "Mobile")
        assert label_at(rows[r], w + 1) == "Mobile no"
        rule = rule_from_selection(click(cash, "CASH"), "payment_mode", label="Blood Group",
                                   where="below")
        assert (rule.label, rule.where) == ("Blood Group", "below")


class TestOneRuleForBoth:
    def test_taught_on_cash_it_reads_the_first_line_online(self, cash, online):
        rule = rule_from_selection(click(cash, "CASH"), "payment_mode")
        assert read_rule(cash, rule) == "CASH"
        assert read_rule(online, rule) == "ONLINE-UPI"

    def test_taught_on_online_it_still_reads_cash(self, cash, online):
        rule = rule_from_selection(click(online, "ONLINE-UPI"), "payment_mode")
        assert read_rule(cash, rule) == "CASH"

    def test_several_lines_selected_read_back_whole(self, online):
        selection = drag(online, "ONLINE-UPI", "Karimnagar")
        assert selection.value == " ".join(ONLINE)
        rule = rule_from_selection(selection, "payment_details")
        assert rule.multiline and rule.kind == "text" and rule.until == "Paid Amt"
        assert read_rule(online, rule) == " ".join(ONLINE)

    def test_a_rule_taught_over_lines_reads_one_word_on_cash(self, cash, online):
        rule = rule_from_selection(drag(online, "ONLINE-UPI", "Karimnagar"), "payment_details")
        assert read_rule(cash, rule) == "CASH"

    def test_taught_on_cash_with_more_lines_allowed_it_reads_all_of_online(self, cash, online):
        rule = rule_from_selection(click(cash, "CASH"), "payment_mode", multiline=True)
        assert rule.until == "Paid Amt"
        assert read_rule(cash, rule) == "CASH"
        assert read_rule(online, rule) == " ".join(ONLINE)

    def test_the_total_in_the_next_column_never_comes_along(self, online):
        selection = drag(online, "ONLINE-UPI", "Txn")
        assert "Total" not in selection.value and "95.00" not in selection.value

    def test_a_following_line_that_is_a_label_and_a_value_comes_whole(self, tmp_path):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((30, 50), "Paymode", fontsize=10)
        page.insert_text((110, 50), ": ONLINE-UPI", fontsize=10)
        page.insert_text((30, 64), "Reference", fontsize=10)
        page.insert_text((110, 64), ": T9981234", fontsize=10)
        page.insert_text((30, 78), "Paid Amt", fontsize=10)
        page.insert_text((110, 78), ": 95.00", fontsize=10)
        doc.save(tmp_path / "pairs.pdf")
        read = pdf_text.read(tmp_path / "pairs.pdf")
        rule = rule_from_selection(click(read, "ONLINE-UPI"), "payment_mode", multiline=True)
        assert rule.until == "Reference"
        rule = FieldRule(name="payment_mode", label="Paymode", multiline=True, until="Paid Amt")
        assert read_rule(read, rule) == "ONLINE-UPI Reference : T9981234"

    def test_a_value_with_nothing_to_end_it_stops_at_the_limit(self, tmp_path):
        lines = ["first", *(f"line {n}" for n in range(12))]
        read = pdf_text.read(receipt(tmp_path / "long.pdf", lines))
        rule = FieldRule(name="details", label="Paymode", multiline=True)
        assert len(read_rule(read, rule).split(" line ")) == 6

    def test_a_reference_can_change_shape_between_prints(self, online, tmp_path):
        """"T9981234" taught must not refuse "UTR/4471" on the next one."""
        rule = rule_from_selection(click(online, "T9981234"), "transaction_id")
        assert rule.label == "Txn ID" and not rule.pattern
        other = pdf_text.read(receipt(tmp_path / "o.pdf",
                                      ["ONLINE", "Txn ID: UTR/4471"]))
        assert read_rule(other, rule) == "UTR/4471"

    def test_a_heading_with_the_same_label_is_passed_over_for_a_code(self, tmp_path):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((30, 50), "Bill No Details", fontsize=10)
        page.insert_text((30, 80), "Bill No : 7712", fontsize=10)
        doc.save(tmp_path / "h.pdf")
        rule = FieldRule(name="bill", label="Bill No", kind="code")
        assert read_rule(pdf_text.read(tmp_path / "h.pdf"), rule) == "7712"

    def test_a_payment_mode_stays_text_whatever_was_clicked(self, online):
        rule = rule_from_selection(click(online, "T9981234"), "payment_mode", kind="code")
        assert rule.kind == "text"


class TestReadingBackOnTheSample:
    def test_a_rule_tied_to_one_exact_word_is_reported(self, cash):
        rule = FieldRule(name="payment_mode", kind="text", pattern=r"(?<![A-Za-z0-9])(CASH)(?![A-Za-z0-9])")
        assert literal_only(rule)
        problems = teaching.rule_problems(cash, [rule], {"payment_mode": "CASH"})
        assert problems and "Choose its label" in problems[0]

    def test_a_shape_with_figures_is_not(self):
        assert not literal_only(FieldRule(name="n", kind="code", pattern=r"(CR\d+)"))

    def test_one_word_of_a_longer_value_is_not_refused(self, tmp_path):
        read = pdf_text.read(receipt(tmp_path / "n.pdf", ["CASH"], name="ANITHA RAMESH"))
        rule = rule_from_selection(click(read, "ANITHA"), "customer_name")
        assert read_rule(read, rule) == "ANITHA RAMESH"
        assert teaching.rule_problems(read, [rule], {"customer_name": "ANITHA"}) == []

    def test_old_profiles_still_load(self, tmp_path):
        import json

        path = tmp_path / "profile.json"
        path.write_text(json.dumps({"field_rules": {"lab": [{"name": "a", "label": "A"}]}}))
        rule = DocumentProfile.load(path).rules_for("lab")[0]
        assert (rule.multiline, rule.until, rule.optional) == (False, "", False)

    def test_the_new_settings_survive_being_saved(self, tmp_path):
        path = tmp_path / "profile.json"
        taught = FieldRule(name="a", label="A", multiline=True, until="B", optional=True)
        DocumentProfile.write_taught(path, [], {"lab": [taught]})
        assert DocumentProfile.load(path).rules_for("lab") == [taught]


KIND = DocumentKind(name="lab", match=["LAB RECEIPT"])


@pytest.fixture
def lab_samples(tmp_path):
    return [
        validation.Sample(receipt(tmp_path / "s1.pdf", ONLINE, mobile="9876543211", bill="I-1"),
                          "lab", {"transaction_id": "T9981234"}, "+919876543211"),
        validation.Sample(receipt(tmp_path / "s2.pdf", ["CASH"], mobile="9876543212", bill="I-2"),
                          "lab", {"transaction_id": ""}, "+919876543212"),
        validation.Sample(receipt(tmp_path / "s3.pdf", ["CASH"], mobile="9876543213", bill="I-3"),
                          "lab", {"transaction_id": ""}, "+919876543213"),
    ]


def lab_profile(**rule) -> DocumentProfile:
    field = FieldRule(name="transaction_id", label="Txn ID", kind="code", **rule)
    return teaching.candidate_profile(DocumentProfile(), "lab", [field], KIND)


class TestAFieldNotOnEveryDocument:
    def test_it_used_to_be_impossible_to_validate(self, lab_samples):
        report = validation.validate(lab_samples, lab_profile(), Settings(), target="lab")
        assert not report.passed
        assert "Not on every document" in report.summary()

    def test_marked_optional_cash_and_online_both_pass(self, lab_samples):
        report = validation.validate(lab_samples, lab_profile(optional=True), Settings(),
                                     target="lab")
        assert report.passed, report.summary()

    def test_the_three_sample_minimum_still_holds(self, lab_samples):
        report = validation.validate(lab_samples[:2], lab_profile(optional=True), Settings(),
                                     target="lab")
        assert not report.passed and "at least 3" in report.summary()

    def test_a_wrong_value_on_an_optional_field_still_fails(self, lab_samples):
        lab_samples[0].values["transaction_id"] = "T1"
        report = validation.validate(lab_samples, lab_profile(optional=True), Settings(),
                                     target="lab")
        assert not report.passed and "transaction_id" in report.summary()

    @pytest.fixture
    def lab_pipeline(self, pipeline):
        from waprinter.send.templates import MessageTemplate

        pipeline.profile = lab_profile(optional=True)
        pipeline.templates.put(MessageTemplate(
            name="lab", status="approved", header_document=True, parameter_format="named",
            body="Receipt {{no}}. Reference {{ref}}.",
        ))
        pipeline.settings.document_templates = {"lab": "lab"}
        pipeline.settings.template_mappings = {
            "lab": {"no": "invoice_number", "ref": "transaction_id"},
        }
        return pipeline

    def test_a_cash_receipt_goes_out_with_a_dash(self, lab_pipeline, tmp_path):
        from waprinter.models import JobStatus

        job = lab_pipeline.process(receipt(tmp_path / "print.pdf", ["CASH"]))
        assert job.status is not JobStatus.HELD, job.hold_reason
        assert "Reference -." in job.message_preview

    def test_an_online_receipt_carries_the_reference(self, lab_pipeline, tmp_path):
        job = lab_pipeline.process(receipt(tmp_path / "print.pdf", ONLINE))
        assert "Reference T9981234." in job.message_preview

    def test_a_required_field_missing_still_holds_the_document(self, lab_pipeline, tmp_path):
        from waprinter.models import JobStatus

        lab_pipeline.profile = lab_profile()
        job = lab_pipeline.process(receipt(tmp_path / "print.pdf", ["CASH"]))
        assert job.status is JobStatus.HELD
        assert "transaction id" in job.hold_reason


class TestStoredSamples:
    def test_a_sample_taken_out_of_the_list_stays_out(self, lab_samples):
        validation.save_samples(lab_samples)
        validation.save_samples(lab_samples[:2], replacing="lab")
        assert len(validation.saved_samples()) == 2

    def test_replacing_one_type_leaves_the_others_alone(self, lab_samples, tmp_path):
        other = validation.Sample(receipt(tmp_path / "x.pdf", ["CASH"], bill="X"),
                                  "statement", {}, "+919876543210")
        validation.save_samples([*lab_samples, other])
        validation.save_samples(lab_samples[:1], replacing="lab")
        kinds = sorted(s.kind for s in validation.saved_samples())
        assert kinds == ["lab", "statement"]
