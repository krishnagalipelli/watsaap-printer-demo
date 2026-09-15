"""The office's own copy of everything that was printed.

Captured PDFs used to be left in `inbox` under the name capture gave them --
`20260907-113601-fee7f6b4.pdf`, flat, forever, inside ProgramData. The client
needs to be able to find a receipt they printed in September, so each one is
copied out into a folder they choose, filed by date and named after itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from invoice_factory import ChitReceiptSpec, build_chit_receipt

from waprinter.archive import folder_for


@pytest.fixture
def receipt(tmp_path):
    def _make(number="CHQ6511/26", name="SHAHNAVAZDANISH MOHAMMAD", phone="7032893588"):
        return build_chit_receipt(
            ChitReceiptSpec(
                receipt_number=number, member_name=name, member_phone=phone
            ),
            tmp_path / "print.pdf",
        )

    return _make


@pytest.fixture
def kept(tmp_path, link_pipeline):
    """Point the copies at a folder of our own and hand it back."""
    folder = tmp_path / "Receipts"
    link_pipeline.settings.pdf_folder = str(folder)
    return folder


def _filed(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.pdf"))


class TestKeepingACopy:
    def test_a_printed_receipt_is_filed_under_its_own_name(
        self, link_pipeline, receipt, kept
    ):
        job = link_pipeline.process_document(receipt())[0]

        filed = _filed(kept)
        assert len(filed) == 1
        assert filed[0].name == "CHQ6511-26 SHAHNAVAZDANISH MOHAMMAD.pdf"
        assert filed[0].parent.name == job.created_at.strftime("%Y-%m-%d")

    def test_the_slash_in_a_receipt_number_does_not_become_a_folder(
        self, link_pipeline, receipt, kept
    ):
        """CHQ6511/26 is a receipt number, not a path. Windows would refuse it."""
        link_pipeline.process_document(receipt())

        filed = _filed(kept)
        assert len(filed) == 1
        assert "/" not in filed[0].stem and "\\" not in filed[0].stem

    def test_every_receipt_of_a_batch_is_kept_separately(
        self, link_pipeline, tmp_path, kept
    ):
        import fitz

        parts = [
            build_chit_receipt(
                ChitReceiptSpec(
                    receipt_number=n, member_name=who, member_phone=phone
                ),
                tmp_path / f"{n.replace('/', '-')}.pdf",
            )
            for n, who, phone in (
                ("CHQ6511/26", "SHAHNAVAZDANISH MOHAMMAD", "7032893588"),
                ("CHQ6488/26", "LAVANYA ANUMASU", "9492204498"),
            )
        ]
        batch = fitz.open()
        for part in parts:
            with fitz.open(part) as src:
                batch.insert_pdf(src)
        batch.save(tmp_path / "batch.pdf")
        batch.close()

        link_pipeline.process_document(tmp_path / "batch.pdf")

        assert [p.name for p in _filed(kept)] == [
            "CHQ6488-26 LAVANYA ANUMASU.pdf",
            "CHQ6511-26 SHAHNAVAZDANISH MOHAMMAD.pdf",
        ]

    def test_a_reprint_does_not_overwrite_the_first_copy(
        self, link_pipeline, receipt, kept
    ):
        printed = receipt()
        link_pipeline.process_document(printed)
        link_pipeline.process_document(printed)

        names = [p.name for p in _filed(kept)]
        assert len(names) == 2
        assert "CHQ6511-26 SHAHNAVAZDANISH MOHAMMAD (2).pdf" in names

    def test_a_receipt_that_could_not_be_read_is_still_kept(
        self, link_pipeline, tmp_path, kept
    ):
        """They printed it, so they want it. The queue is a separate question."""
        blank = tmp_path / "blank.pdf"
        import fitz

        doc = fitz.open()
        doc.new_page(width=595, height=842)
        doc.save(blank)
        doc.close()

        link_pipeline.process_document(blank)

        assert len(_filed(kept)) == 1

    def test_the_default_folder_is_used_when_none_is_chosen(
        self, link_pipeline, receipt
    ):
        link_pipeline.settings.pdf_folder = ""
        link_pipeline.process_document(receipt())

        assert _filed(folder_for(link_pipeline.settings))


class TestWhatFilingMustNotDo:
    def test_the_working_copy_stays_where_the_pipeline_left_it(
        self, link_pipeline, receipt, kept
    ):
        """A copy, not a move.

        The queue reopens this path, and a held receipt is sent from it later.
        If filing moved the file to a share that went offline, sending would
        break with it.
        """
        printed = receipt()
        job = link_pipeline.process_document(printed)[0]

        assert printed.exists()
        assert job.pdf_path.exists()
        assert job.pdf_path == printed

    def test_turning_it_off_keeps_nothing(self, link_pipeline, receipt, kept):
        link_pipeline.settings.keep_printed_pdfs = False
        link_pipeline.process_document(receipt())

        assert not kept.exists() or not _filed(kept)

    def test_an_unusable_folder_does_not_lose_the_print(
        self, link_pipeline, receipt, tmp_path
    ):
        """A receipt that reached the customer but not the folder is a nuisance.

        The other way round is a lost receipt, so filing never raises.
        """
        blocked = tmp_path / "blocked"
        blocked.write_text("this is a file, not a folder")
        link_pipeline.settings.pdf_folder = str(blocked / "receipts")

        jobs = link_pipeline.process_document(receipt())

        assert len(jobs) == 1
        assert jobs[0].recipient == "+917032893588"
