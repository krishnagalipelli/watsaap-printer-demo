"""One print job holding several receipts becomes one job per subscriber.

A chit fund counter prints a run of receipts as a single job: one PDF, a
different subscriber on every page. That used to arrive as one job with two
numbers above the send threshold, which the gate correctly refused to choose
between -- so every batch sat in "Needs attention" saying "2 equally likely
numbers found".

The hold was the visible symptom. The dangerous part was the obvious way round
it: the send path uploads the job's PDF whole, so an operator picking one of
the offered numbers would have posted that subscriber a document carrying
everyone else's name, mobile and amount paid. test_a_receipt_never_carries_
another_subscriber is the one that matters.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz
import pytest
from invoice_factory import ChitReceiptSpec, build_chit_receipt


@pytest.fixture
def receipt(tmp_path):
    def _make(number: str, name: str, phone: str) -> Path:
        return build_chit_receipt(
            ChitReceiptSpec(
                receipt_number=number, member_name=name, member_phone=phone
            ),
            tmp_path / f"{number.replace('/', '-')}.pdf",
        )

    return _make


@pytest.fixture
def merge(tmp_path):
    """Staple PDFs into one print job, the way the spooler would."""
    counter = {"n": 0}

    def _merge(*parts: Path) -> Path:
        counter["n"] += 1
        out = fitz.open()
        for part in parts:
            with fitz.open(part) as src:
                out.insert_pdf(src)
        target = tmp_path / f"batch_{counter['n']}.pdf"
        out.save(target)
        out.close()
        return target

    return _merge


def _text_of(pdf: Path) -> str:
    with fitz.open(pdf) as doc:
        return "".join(page.get_text() for page in doc)


def _mobiles_in(pdf: Path) -> set[str]:
    return set(re.findall(r"\b[6-9]\d{9}\b", _text_of(pdf).replace(" ", "")))


class TestBatchOfReceipts:
    def test_each_receipt_becomes_its_own_job(self, link_pipeline, receipt, merge):
        batch = merge(
            receipt("CHQ6511/26", "SHAHNAVAZDANISH MOHAMMAD", "7032893588"),
            receipt("CHQ6488/26", "LAVANYA ANUMASU", "9492204498"),
        )
        jobs = link_pipeline.process_document(batch)

        assert len(jobs) == 2
        assert [j.recipient for j in jobs] == ["+917032893588", "+919492204498"]
        assert [j.fields.invoice_number for j in jobs] == ["CHQ6511/26", "CHQ6488/26"]

    def test_a_receipt_never_carries_another_subscriber(
        self, link_pipeline, receipt, merge
    ):
        """The whole point. What is attached is what the customer receives."""
        batch = merge(
            receipt("CHQ6511/26", "SHAHNAVAZDANISH MOHAMMAD", "7032893588"),
            receipt("CHQ6488/26", "LAVANYA ANUMASU", "9492204498"),
        )
        jobs = link_pipeline.process_document(batch)

        for job in jobs:
            assert job.recipient is not None
            mobiles = _mobiles_in(job.pdf_path)
            assert mobiles == {job.recipient[len("+91") :]}, (
                f"{job.pdf_path.name} carries numbers other than its own "
                f"subscriber's: {sorted(mobiles)}"
            )

        first, second = jobs
        assert "LAVANYA" not in _text_of(first.pdf_path)
        assert "SHAHNAVAZDANISH" not in _text_of(second.pdf_path)

    def test_a_batch_no_longer_holds_for_ambiguity(
        self, link_pipeline, receipt, merge
    ):
        """The symptom the operator reported: every batch stuck in the queue."""
        batch = merge(
            receipt("CHQ6511/26", "SHAHNAVAZDANISH MOHAMMAD", "7032893588"),
            receipt("CHQ6488/26", "LAVANYA ANUMASU", "9492204498"),
        )
        jobs = link_pipeline.process_document(batch)

        assert not any(
            "equally likely" in (job.hold_reason or "") for job in jobs
        )


class TestWhatMustNotBeSplit:
    def test_a_single_receipt_is_one_job_against_the_original_file(
        self, link_pipeline, receipt
    ):
        """Nearly every print. It must not grow a copy on disk."""
        one = receipt("CHQ6511/26", "SHAHNAVAZDANISH MOHAMMAD", "7032893588")
        jobs = link_pipeline.process_document(one)

        assert len(jobs) == 1
        assert jobs[0].pdf_path == one
        assert not list(one.parent.glob("*-r1.pdf"))

    def test_two_copies_of_one_receipt_stay_one_job(
        self, link_pipeline, receipt, merge
    ):
        """Subscriber copy then office copy is one receipt printed twice."""
        one = receipt("CHQ6511/26", "SHAHNAVAZDANISH MOHAMMAD", "7032893588")
        jobs = link_pipeline.process_document(merge(one, one))

        assert len(jobs) == 1
        assert jobs[0].recipient == "+917032893588"

    def test_a_continuation_page_stays_with_its_receipt(
        self, link_pipeline, receipt, merge, tmp_path
    ):
        """A page carrying no number of its own belongs to the page before it."""
        one = receipt("CHQ6511/26", "SHAHNAVAZDANISH MOHAMMAD", "7032893588")
        spill = fitz.open()
        page = spill.new_page(width=595, height=842)
        page.insert_text((60, 100), "3   Subscription instalment    1,000.00", fontsize=9)
        spill.save(tmp_path / "spill.pdf")
        spill.close()

        jobs = link_pipeline.process_document(merge(one, tmp_path / "spill.pdf"))

        assert len(jobs) == 1
        with fitz.open(jobs[0].pdf_path) as doc:
            assert doc.page_count == 2


class TestSplittingNeverLosesAPrint:
    def test_an_unreadable_pdf_still_produces_a_job(self, link_pipeline, tmp_path):
        """Splitting sits on top of a working path; it must not become a way to lose one."""
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4 this is not a PDF")

        jobs = link_pipeline.process_document(broken)

        assert len(jobs) == 1
        assert jobs[0].error is not None
