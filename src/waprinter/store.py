"""SQLite job store.

Every capture gets a row the moment the PDF lands, before anything else can
fail. That row is the audit trail: with silent sending there is no operator
memory of what went out, so the database has to be able to answer "what did we
send, to whom, and why did we think that was right".
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .models import (
    Confidence,
    ExtractedFields,
    JobStatus,
    PhoneCandidate,
    PrintJob,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
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
    chat_url        TEXT,
    error           TEXT,
    sent_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created    ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe     ON jobs(dedupe_key, sent_at);
CREATE INDEX IF NOT EXISTS idx_jobs_recipient  ON jobs(recipient);

-- Append-only trail of every state change, for support and dispute handling.
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, at);
"""

log = logging.getLogger(__name__)


def _columns_of(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}


def _schema_columns() -> dict[str, dict[str, str]]:
    """The shape SCHEMA describes, read back from a throwaway database.

    Derived rather than declared so there is nothing to keep in step: SCHEMA
    stays the single description of the tables, and whatever it gains next is
    migrated onto installed machines without anyone remembering to say so.
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(SCHEMA)
        return {
            table: _columns_of(probe, table) for table in ("jobs", "events")
        }
    finally:
        probe.close()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _fields_to_json(f: ExtractedFields) -> str:
    payload = asdict(f)
    # Enums and tuples do not survive a JSON round trip untouched.
    for c in payload["candidates"]:
        c["confidence"] = str(c["confidence"])
        c["bbox"] = list(c["bbox"])
    return json.dumps(payload)


def _fields_from_json(raw: str) -> ExtractedFields:
    payload = json.loads(raw or "{}")
    candidates = [
        PhoneCandidate(
            raw=c["raw"],
            e164=c["e164"],
            score=c["score"],
            confidence=Confidence(c["confidence"]),
            page=c["page"],
            bbox=tuple(c["bbox"]),
            label=c.get("label"),
            reasons=c.get("reasons", []),
            from_ocr=c.get("from_ocr", False),
        )
        for c in payload.get("candidates", [])
    ]
    return ExtractedFields(
        candidates=candidates,
        invoice_number=payload.get("invoice_number"),
        customer_name=payload.get("customer_name"),
        invoice_date=payload.get("invoice_date"),
        total_amount=payload.get("total_amount"),
        amount_words=payload.get("amount_words"),
        payment_mode=payload.get("payment_mode"),
        document_kind=payload.get("document_kind"),
        # Written since the field existed but never read back, so a held scan
        # reopened from the queue looked verified whatever the two OCR passes
        # had said about it.
        document_kind_verified=payload.get("document_kind_verified", True),
        classification_error=payload.get("classification_error", ""),
        page_count=payload.get("page_count", 0),
        extra={
            str(k): str(v)
            for k, v in (payload.get("extra") or {}).items()
            if v is not None
        },
        has_text_layer=payload.get("has_text_layer", True),
        used_ocr=payload.get("used_ocr", False),
        ocr_error=payload.get("ocr_error"),
    )


def _locked(method):
    """Serialise one Store method against the others.

    One connection is shared by the watcher thread and the window's thread.
    SQLite serialises individual statements, but not a statement and the
    commit that follows it: the window's commit() could land between the
    watcher's upsert and its own commit, and the two halves of "a job and its
    event" went to disk as separate transactions.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        """Bring a database created by an older build up to the current shape.

        CREATE TABLE IF NOT EXISTS does nothing at all to a table that already
        exists, so a column added in a later release never reached a machine
        that had already printed something. `chat_url` was the one that bit:
        every install predates it, the agent updates itself overnight, and the
        next morning every print died in upsert() on "table jobs has no column
        named chat_url" -- silently, because the agent is frozen --windowed.

        Only additions are handled, which is all SQLite does cheaply and all
        this schema has ever done. Indexes look after themselves: SCHEMA
        creates those IF NOT EXISTS, which works on an existing table.
        """
        for table, expected in _schema_columns().items():
            have = _columns_of(self.conn, table)
            if not have:
                continue  # the table itself is new; executescript just made it
            for name, declared in expected.items():
                if name in have:
                    continue
                # Every column added since the first release is a nullable
                # TEXT, which ALTER TABLE can add to a populated table.
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declared}"
                )
                log.warning(
                    "migrated %s: added missing column %s", table, name
                )

    def close(self) -> None:
        self.conn.close()

    # -- writes ------------------------------------------------------------

    @_locked
    def upsert(self, job: PrintJob) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs (id, created_at, pdf_path, status, doc_title,
                              windows_user, fields_json, recipient, confidence,
                              hold_reason, dedupe_key, template_name,
                              message_preview, wamid, chat_url, error, sent_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status          = excluded.status,
                doc_title       = excluded.doc_title,
                windows_user    = excluded.windows_user,
                fields_json     = excluded.fields_json,
                recipient       = excluded.recipient,
                confidence      = excluded.confidence,
                hold_reason     = excluded.hold_reason,
                dedupe_key      = excluded.dedupe_key,
                template_name   = excluded.template_name,
                message_preview = excluded.message_preview,
                wamid           = excluded.wamid,
                chat_url        = excluded.chat_url,
                error           = excluded.error,
                sent_at         = excluded.sent_at
            """,
            (
                job.id,
                _iso(job.created_at),
                str(job.pdf_path),
                str(job.status),
                job.doc_title,
                job.windows_user,
                _fields_to_json(job.fields),
                job.recipient,
                str(job.confidence) if job.confidence else None,
                job.hold_reason,
                job.dedupe_key,
                job.template_name,
                job.message_preview,
                job.wamid,
                job.chat_url,
                job.error,
                _iso(job.sent_at),
            ),
        )
        self.conn.commit()

    @_locked
    def log(self, job_id: str, kind: str, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, at, kind, detail) VALUES (?,?,?,?)",
            (job_id, datetime.now().isoformat(), kind, detail),
        )
        self.conn.commit()

    # -- reads -------------------------------------------------------------

    @_locked
    def get(self, job_id: str) -> PrintJob | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    @_locked
    def by_status(self, status: JobStatus, limit: int = 200) -> list[PrintJob]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (str(status), limit),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @_locked
    def pending(self, limit: int = 200) -> list[PrintJob]:
        """Everything waiting on a person.

        Includes failures for manual retry and review, as well as documents
        waiting for a recipient or for WhatsApp to be opened.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM jobs WHERE status IN (?, ?, ?, ?)
            ORDER BY created_at DESC LIMIT ?
            """,
            (
                str(JobStatus.AWAITING),
                str(JobStatus.HELD),
                str(JobStatus.READY),
                str(JobStatus.FAILED),
                limit,
            ),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @_locked
    def pending_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?, ?, ?)",
            tuple(str(s) for s in (JobStatus.AWAITING, JobStatus.HELD, JobStatus.READY, JobStatus.FAILED)),
        ).fetchone()[0]

    @_locked
    def unresolved_pdf_paths(self) -> set[Path]:
        """All working files that must survive cleanup, without a UI limit."""
        rows = self.conn.execute(
            "SELECT DISTINCT pdf_path FROM jobs WHERE status IN (?, ?, ?, ?, ?, ?)",
            tuple(str(s) for s in (JobStatus.CAPTURED, JobStatus.QUEUED, JobStatus.AWAITING,
                                   JobStatus.HELD, JobStatus.READY, JobStatus.FAILED)),
        ).fetchall()
        return {Path(row[0]) for row in rows}

    @_locked
    def status_counts(self, since: datetime) -> dict[str, int]:
        """How many jobs landed in each status since `since`."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE created_at >= ? "
            "GROUP BY status",
            (since.isoformat(),),
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    @_locked
    def recent(self, limit: int = 100) -> list[PrintJob]:
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @_locked
    def events(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY at", (job_id,)
        ).fetchall()

    # -- rules support -----------------------------------------------------

    @_locked
    def find_duplicate(
        self,
        dedupe_key: str,
        window_hours: int,
        now: datetime | None = None,
        *,
        dry_run: bool = False,
        send_mode: str = "api",
    ) -> PrintJob | None:
        """A prior *successful* send of the same invoice to the same recipient.

        Match only the current sending mode. Tests, real API sends and manual
        WhatsApp handoffs are separate histories; none proves another happened.
        """
        now = now or datetime.now()
        cutoff = (now - timedelta(hours=window_hours)).isoformat()
        status = (JobStatus.HANDED_OFF if send_mode == "link" else
                  JobStatus.DRY_RUN if dry_run else JobStatus.SENT)
        row = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE dedupe_key = ? AND status = ? AND sent_at IS NOT NULL
              AND sent_at >= ?
            ORDER BY sent_at DESC LIMIT 1
            """,
            (
                dedupe_key,
                str(status),
                cutoff,
            ),
        ).fetchone()
        return self._row_to_job(row) if row else None

    @_locked
    def count_sent_since(self, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = ? AND sent_at >= ?",
            (str(JobStatus.SENT), since.isoformat()),
        ).fetchone()
        return int(row["n"])

    @_locked
    def interrupted(self) -> list[PrintJob]:
        """Jobs that were mid-send when the previous process stopped.

        QUEUED is written just before the sender is called and replaced the
        moment it answers, so a QUEUED row at startup means the answer never
        came: a crash, a power cut, or a send that raised. Nothing listed
        those rows -- the queue shows only what waits on a person -- so the
        receipt was simply gone from every screen.
        """
        return self.by_status(JobStatus.QUEUED)

    # -- mapping -----------------------------------------------------------

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> PrintJob:
        return PrintJob(
            id=row["id"],
            created_at=_dt(row["created_at"]),
            pdf_path=Path(row["pdf_path"]),
            status=JobStatus(row["status"]),
            doc_title=row["doc_title"],
            windows_user=row["windows_user"],
            fields=_fields_from_json(row["fields_json"]),
            recipient=row["recipient"],
            confidence=Confidence(row["confidence"]) if row["confidence"] else None,
            hold_reason=row["hold_reason"],
            dedupe_key=row["dedupe_key"],
            template_name=row["template_name"],
            message_preview=row["message_preview"],
            wamid=row["wamid"],
            chat_url=row["chat_url"],
            error=row["error"],
            sent_at=_dt(row["sent_at"]),
        )
