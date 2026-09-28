"""A database created by an older build has to survive the update.

The agent updates itself overnight without asking. `chat_url` was added to
SCHEMA in a release that reached machines whose jobs.db predated it, and
CREATE TABLE IF NOT EXISTS does nothing whatsoever to a table that already
exists -- so the column never arrived. The next morning every print died in
upsert() on "table jobs has no column named chat_url", before extraction,
before the gate, before anything was written down.

It was invisible from the counter: the agent is frozen --windowed, so the
traceback went nowhere. The queue simply stopped growing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from waprinter.models import JobStatus, PrintJob
from waprinter.store import Store

# jobs.db exactly as an installed machine has it: everything up to the release
# before chat_url. Written out in full rather than generated, because the point
# of the test is a shape this code no longer produces.
SCHEMA_BEFORE_CHAT_URL = """
CREATE TABLE jobs (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    pdf_path        TEXT NOT NULL,
    status          TEXT NOT NULL,
    doc_title       TEXT,
    windows_user    TEXT,
    fields_json     TEXT NOT NULL DEFAULT '{}',
    recipient       TEXT,
    confidence      TEXT,
    hold_reason     TEXT,
    dedupe_key      TEXT,
    template_name   TEXT,
    message_preview TEXT,
    wamid           TEXT,
    error           TEXT,
    sent_at         TEXT
);
CREATE TABLE events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT
);
"""


@pytest.fixture
def old_database(tmp_path):
    """A pre-chat_url database with a job already in it."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_BEFORE_CHAT_URL)
    conn.execute(
        "INSERT INTO jobs (id, created_at, pdf_path, status, recipient) "
        "VALUES ('printed-in-august','2026-08-07T10:00:00','receipt.pdf','sent','+919492204498')"
    )
    conn.commit()
    conn.close()
    return db


def _columns(store: Store, table: str = "jobs") -> set[str]:
    return {row[1] for row in store.conn.execute(f"PRAGMA table_info({table})")}


def test_opening_an_old_database_adds_the_missing_column(old_database):
    store = Store(old_database)
    try:
        assert "chat_url" in _columns(store)
    finally:
        store.close()


def test_a_print_after_the_update_no_longer_fails(old_database):
    """The actual crash: the first write of the morning."""
    store = Store(old_database)
    try:
        store.upsert(
            PrintJob(
                id="printed-in-september",
                created_at=datetime.now(),
                pdf_path=Path("receipt.pdf"),
                status=JobStatus.READY,
                chat_url="https://wa.me/917032893588",
            )
        )
        assert store.get("printed-in-september").chat_url is not None
    finally:
        store.close()


def test_migrating_keeps_what_was_already_there(old_database):
    """An audit trail that loses August is not an audit trail."""
    store = Store(old_database)
    try:
        kept = store.get("printed-in-august")
        assert kept is not None
        assert kept.recipient == "+919492204498"
    finally:
        store.close()


def test_opening_the_same_database_twice_is_harmless(old_database):
    first = Store(old_database)
    first.close()
    second = Store(old_database)
    try:
        assert "chat_url" in _columns(second)
    finally:
        second.close()


def test_a_new_database_needs_no_migrating(tmp_path):
    """The common case must not be disturbed by any of this."""
    store = Store(tmp_path / "fresh.db")
    try:
        assert "chat_url" in _columns(store)
        assert "job_id" in _columns(store, "events")
    finally:
        store.close()


def test_every_column_schema_describes_reaches_an_old_database(old_database):
    """Guards the mechanism rather than the one column that caused the outage.

    Whatever SCHEMA gains next must reach installed machines too, without
    anyone having to remember to write a migration for it.
    """
    fresh = Store(old_database.parent / "fresh.db")
    migrated = Store(old_database)
    try:
        assert _columns(fresh) == _columns(migrated)
        assert _columns(fresh, "events") == _columns(migrated, "events")
    finally:
        fresh.close()
        migrated.close()


class TestSettingsThatReachAMachineAlreadyRunning:
    """A variable added in a release has to arrive on an existing install.

    Same failure as the template that never reached a machine with a
    templates.json, and as the database column added in a release that no
    installed machine ever got. Here the carrier is settings.json: a stored
    dict replaces the shipped default entirely, so `notice_no` -- added when
    the removal templates were -- was absent on every branch that had printed
    anything. It does not fail. It sends "Notice No.: -", which on a notice
    telling a member they are being removed is worse than sending nothing.
    """

    def _written_before(self, tmp_path, variables):
        import json

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"template_variables": variables}), encoding="utf-8")
        return path

    def test_a_new_variable_reaches_an_existing_install(self, tmp_path):
        from waprinter.config import Settings

        # settings.json as written before the removal templates existed.
        path = self._written_before(
            tmp_path,
            {"1": "customer_name", "receipt_no": "invoice_number",
             "date": "invoice_date"},
        )

        loaded = Settings.load(path)

        assert loaded.template_variables["notice_no"] == "invoice_number"
        assert loaded.template_variables["letter_no"] == "invoice_number"

    def test_what_the_machine_has_mapped_still_wins(self, tmp_path):
        """A client who remapped a variable keeps their mapping."""
        from waprinter.config import Settings

        path = self._written_before(tmp_path, {"amount": "total_amount"})

        loaded = Settings.load(path)

        assert loaded.template_variables["amount"] == "total_amount"

    def test_the_notice_number_actually_renders(self, tmp_path):
        """The end of it: the number reaches the member, not a dash."""
        import sys

        sys.path.insert(0, "tests")
        from invoice_factory import RemovalNoticeSpec, build_removal_notice

        from waprinter.config import Settings
        from waprinter.extract import extract_fields
        from waprinter.send.templates import TemplateStore, render

        path = self._written_before(
            tmp_path, {"receipt_no": "invoice_number", "date": "invoice_date"}
        )
        settings = Settings.load(path)
        templates = TemplateStore(tmp_path / "t.json")

        fields = extract_fields(
            build_removal_notice(
                RemovalNoticeSpec(notice_number="RN901/26"), tmp_path / "rn.pdf"
            )
        )
        message = render(
            templates.get("removal_notice"), settings.template_variables, fields
        )

        assert "Notice No.: RN901/26" in message.preview
        assert message.missing == []

    def test_which_documents_a_branch_sends_is_not_merged_back(self, tmp_path):
        """document_templates is a choice, not vocabulary.

        An empty map is a real answer -- a client who only sends receipts --
        and it is what the gate reads to decide whether an unidentifiable scan
        could be the wrong document. Merging defaults into it would hand every
        client the chit fund's paperwork.
        """
        import json

        from waprinter.config import Settings

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"document_templates": {}}), encoding="utf-8")

        assert Settings.load(path).document_templates == {}


class TestWhatSurvivesTheDatabase:
    def _job(self, **fields) -> PrintJob:
        from waprinter.models import ExtractedFields

        return PrintJob(
            id="j1",
            created_at=datetime(2026, 9, 7, 11, 36),
            pdf_path=Path("/tmp/x.pdf"),
            fields=ExtractedFields(**fields),
        )

    def test_the_verified_flag_comes_back_as_it_went_in(self, tmp_path):
        """Written since the field existed, never read back.

        So a held scan reopened from the queue looked verified whatever the
        two OCR passes had said about what kind of document it was.
        """
        store = Store(tmp_path / "jobs.db")
        store.upsert(self._job(document_kind="removal_notice", document_kind_verified=False))
        assert store.get("j1").fields.document_kind_verified is False

    def test_an_older_row_without_the_flag_reads_as_verified(self, tmp_path):
        store = Store(tmp_path / "jobs.db")
        store.upsert(self._job())
        store.conn.execute("UPDATE jobs SET fields_json = '{}' WHERE id = 'j1'")
        store.conn.commit()
        assert store.get("j1").fields.document_kind_verified is True


class TestInterruptedSends:
    """QUEUED is written just before the sender is called.

    A QUEUED row at startup means the answer never came -- a crash, a power
    cut -- and nothing listed those rows, so the receipt was gone from every
    screen. They go back on the queue for a person.
    """

    def test_a_queued_job_is_held_for_review_at_startup(self, pipeline):
        job = PrintJob(
            id="stuck",
            created_at=datetime.now(),
            pdf_path=Path("/tmp/x.pdf"),
            status=JobStatus.QUEUED,
            recipient="+919876543210",
        )
        pipeline.store.upsert(job)
        assert pipeline.store.pending() == []

        recovered = pipeline.recover_interrupted()

        assert [j.id for j in recovered] == ["stuck"]
        held = pipeline.store.get("stuck")
        assert held.status is JobStatus.HELD
        assert "Check WhatsApp" in held.hold_reason
        assert [j.id for j in pipeline.store.pending()] == ["stuck"]

    def test_nothing_else_is_touched(self, pipeline):
        for status in (JobStatus.SENT, JobStatus.HELD, JobStatus.FAILED):
            pipeline.store.upsert(
                PrintJob(
                    id=str(status),
                    created_at=datetime.now(),
                    pdf_path=Path("/tmp/x.pdf"),
                    status=status,
                )
            )
        assert pipeline.recover_interrupted() == []
        for status in (JobStatus.SENT, JobStatus.HELD, JobStatus.FAILED):
            assert pipeline.store.get(str(status)).status is status
