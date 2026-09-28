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
    JobStatus.READY: ("Ready to send", "warn"),
    # Never "Sent": in link mode a person presses send in WhatsApp and the app
    # cannot see it happen.
    JobStatus.HANDED_OFF: ("Opened in WhatsApp", "ok"),
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
    """Where the printer stands, like a printer's own ready/offline light.

    `label` is the word in the header's badge and `detail` the sentence beside
    it; `summary` is the line on the side bar's mode card.
    """

    label: str
    detail: str
    tone: str
    summary: str = ""

    @property
    def text(self) -> str:
        return f"{self.label} — {self.detail}" if self.detail else self.label


def device_state(settings: Settings, waiting: int, problems: list[str]) -> DeviceState:
    if settings.dry_run:
        return DeviceState("Test mode", "documents are read but nothing is sent", "warn",
                           "No messages are being sent.")
    if problems:
        return DeviceState("Not ready", problems[0], "bad",
                           "Nothing can be sent until setup is finished.")
    # Link mode is a working state, but not an automatic one: nothing goes out
    # until a person presses send in WhatsApp. A bare "Ready" here reads as
    # "receipts are going out on their own", which is how an operator ends up
    # believing a message was delivered that is still sitting on their screen.
    if settings.send_mode == "link":
        return DeviceState("Ready", "WhatsApp opens for you to press send", "warn",
                           "Someone presses send in WhatsApp for each document.")
    if waiting:
        return DeviceState("Ready", f"{waiting} document(s) need attention", "warn",
                           "Printed documents are sent on their own.")
    return DeviceState("Ready", "", "ok", "Printed documents are sent on their own.")


def header_line(state: DeviceState) -> str:
    """The sentence beside the header's badge.

    One line, so only the first sentence: the rest of a long problem is on
    the Status page, next to the button that fixes it.
    """
    text = (state.detail or state.summary).split(". ")[0].rstrip(".")
    return f"{text[:1].upper()}{text[1:]}." if text else ""


@dataclass(frozen=True)
class SetupNeed:
    """What stops sending, gathered under the setup step that fixes it: one
    card on the Status page."""

    page: str
    title: str
    action: str
    lines: tuple[str, ...]

    @property
    def tag(self) -> str:
        # Waiting on Meta is not something anyone here can do more about.
        if all("approved by Meta" in line for line in self.lines):
            return "Pending approval"
        return "Required"


# Which setup step fixes each problem send/readiness.py can report, found by
# the words it uses. First match wins.
_FIXED_BY = (
    ("connect", ("phone number id", "access token")),
    ("templates", ("approved by meta", "is not configured")),
    ("messages", ("nothing to fill", "no message yet")),
    ("preferences", ("own numbers",)),
)
SETUP_ACTIONS = {
    "connect": "Open setup",
    "templates": "View templates",
    "messages": "Fill in messages",
    "preferences": "Open counter settings",
}


def fixed_by(problem: str) -> str:
    """The setup page that fixes `problem`. Connect, if nothing says otherwise."""
    lowered = problem.lower()
    for page, words in _FIXED_BY:
        if any(word in lowered for word in words):
            return page
    return "connect"


def setup_needs(problems: list[str]) -> list[SetupNeed]:
    from .setupmodel import STEP_TITLES

    titles = dict(STEP_TITLES)
    grouped: dict[str, list[str]] = {}
    for problem in problems:
        grouped.setdefault(fixed_by(problem), []).append(problem)
    return [
        SetupNeed(page, titles[page], SETUP_ACTIONS[page], tuple(lines))
        for page, lines in grouped.items()
    ]


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
        sent=counts.get(JobStatus.DRY_RUN if settings.dry_run else JobStatus.SENT, 0),
        waiting=sum(counts.get(s, 0) for s in
                    (JobStatus.AWAITING, JobStatus.HELD, JobStatus.READY, JobStatus.FAILED)),
        failed=counts.get(JobStatus.FAILED, 0),
    )


def sent_caption(settings: Settings) -> str:
    return "test sends today" if settings.dry_run else "sent today"


def history_row(job: PrintJob) -> tuple[str, str, str, str, str]:
    """One line of the Recent documents table."""
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
