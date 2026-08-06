"""SQLite job store.

Every capture gets a row the moment the PDF lands, before anything else can
fail. That row is the audit trail: with silent sending there is no operator
memory of what went out, so the database has to be able to answer "what did we
send, to whom, and why did we think that was right".
"""

from __future__ import annotations

import json
import sqlite3
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
        page_count=payload.get("page_count", 0),
        has_text_layer=payload.get("has_text_layer", True),
        used_ocr=payload.get("used_ocr", False),
        ocr_error=payload.get("ocr_error"),
    )


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- writes ------------------------------------------------------------

    def upsert(self, job: PrintJob) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs (id, created_at, pdf_path, status, doc_title,
                              windows_user, fields_json, recipient, confidence,
                              hold_reason, dedupe_key, template_name,
                              message_preview, wamid, error, sent_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                job.error,
                _iso(job.sent_at),
            ),
        )
        self.conn.commit()

    def log(self, job_id: str, kind: str, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, at, kind, detail) VALUES (?,?,?,?)",
            (job_id, datetime.now().isoformat(), kind, detail),
        )
        self.conn.commit()

    # -- reads -------------------------------------------------------------

    def get(self, job_id: str) -> PrintJob | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def by_status(self, status: JobStatus, limit: int = 200) -> list[PrintJob]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (str(status), limit),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def pending(self, limit: int = 200) -> list[PrintJob]:
        """Everything waiting on a person.

        Includes AWAITING as well as HELD: if the agent is restarted while a
        dialog is open, that job would otherwise be invisible to everyone.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM jobs WHERE status IN (?, ?)
            ORDER BY created_at DESC LIMIT ?
            """,
            (str(JobStatus.AWAITING), str(JobStatus.HELD), limit),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def status_counts(self, since: datetime) -> dict[str, int]:
        """How many jobs landed in each status since `since`."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE created_at >= ? "
            "GROUP BY status",
            (since.isoformat(),),
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def recent(self, limit: int = 100) -> list[PrintJob]:
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def events(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY at", (job_id,)
        ).fetchall()

    # -- rules support -----------------------------------------------------

    def find_duplicate(
        self,
        dedupe_key: str,
        window_hours: int,
        now: datetime | None = None,
    ) -> PrintJob | None:
        """A prior *successful* send of the same invoice to the same recipient.

        Only delivered sends count. A failed or held job must not suppress a
        genuine retry.
        """
        now = now or datetime.now()
        cutoff = (now - timedelta(hours=window_hours)).isoformat()
        row = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE dedupe_key = ? AND status IN (?, ?) AND sent_at IS NOT NULL
              AND sent_at >= ?
            ORDER BY sent_at DESC LIMIT 1
            """,
            (dedupe_key, str(JobStatus.SENT), str(JobStatus.DRY_RUN), cutoff),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def count_sent_since(self, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = ? AND sent_at >= ?",
            (str(JobStatus.SENT), since.isoformat()),
        ).fetchone()
        return int(row["n"])

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
            error=row["error"],
            sent_at=_dt(row["sent_at"]),
        )
