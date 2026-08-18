"""What the window shows, without any widgets.

Everything the operator reads is decided here — the device status line, the
counters, how a job is described. Keeping it separate from the Tk code means it
can be tested on a build machine with no display, which is most of them.

The one rule this module exists to enforce: internal status names
(`dry_run`, `awaiting`, `held`) are for the database, never for the screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..models import JobStatus, PrintJob

# How each internal status reads on screen, and which colour it takes.
STATUS_LABELS: dict[JobStatus, tuple[str, str]] = {
    JobStatus.SENT: ("Sent", "ok"),
    JobStatus.DRY_RUN: ("Test only", "warn"),
    JobStatus.AWAITING: ("Needs a number", "warn"),
    JobStatus.HELD: ("Waiting", "warn"),
    JobStatus.DUPLICATE: ("Reprint ignored", "muted"),
    JobStatus.FAILED: ("Failed", "bad"),
    JobStatus.QUEUED: ("Sending", "muted"),
    JobStatus.CAPTURED: ("Reading", "muted"),
    JobStatus.DISCARDED: ("Discarded", "muted"),
}


def label_of(job: PrintJob) -> tuple[str, str]:
    """(text, tone) for a job's status."""
    return STATUS_LABELS.get(job.status, (str(job.status), "muted"))


def document_of(job: PrintJob) -> str:
    return job.fields.invoice_number or job.doc_title or "Document"


@dataclass(frozen=True)
class DeviceState:
    """The line under the title, like a printer's own ready/offline state."""

    text: str
    tone: str


def device_state(settings: Settings, waiting: int, problems: list[str]) -> DeviceState:
    if settings.dry_run:
        return DeviceState("Test mode — documents are read but nothing is sent", "warn")
    if problems:
        return DeviceState(f"Not ready — {problems[0]}", "bad")
    if waiting:
        return DeviceState(f"Ready — {waiting} document(s) need attention", "warn")
    return DeviceState("Ready", "ok")


@dataclass(frozen=True)
class Counters:
    printed: int = 0
    sent: int = 0
    waiting: int = 0
    failed: int = 0

    @property
    def sent_label(self) -> str:
        return "sent today"


def counters_for_today(store, settings: Settings, now: datetime | None = None) -> Counters:
    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = store.status_counts(midnight)
    return Counters(
        printed=sum(counts.values()),
        # A test send counts here so the panel means something before go-live;
        # the label changes rather than the number.
        sent=counts.get(JobStatus.SENT, 0) + counts.get(JobStatus.DRY_RUN, 0),
        waiting=counts.get(JobStatus.AWAITING, 0) + counts.get(JobStatus.HELD, 0),
        failed=counts.get(JobStatus.FAILED, 0),
    )


def sent_caption(settings: Settings) -> str:
    return "test sends today" if settings.dry_run else "sent today"


def history_row(job: PrintJob) -> tuple[str, str, str, str, str]:
    """One line of the Recent tab."""
    label, _tone = label_of(job)
    return (
        job.created_at.strftime("%d %b %H:%M"),
        label,
        job.recipient or "—",
        document_of(job),
        job.error or job.hold_reason or "",
    )


def queue_caption(job: PrintJob) -> str:
    """The line under a waiting document's title."""
    bits = [job.created_at.strftime("%d %b, %H:%M")]
    if job.fields.customer_name:
        bits.append(job.fields.customer_name)
    return "  ·  ".join(bits)


HOW_TO_USE = (
    "In any program choose File → Print, pick WhatsApp Printer, and print as "
    "normal.\n\nThe customer's number is read off the page and the document is "
    "sent to them on WhatsApp. A small panel appears in the corner to say "
    "whether it went."
)
