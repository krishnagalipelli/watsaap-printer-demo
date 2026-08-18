"""What the after-print notification says.

Wording only — the panel itself is a Tk window (ui/notification.py).
Kept separate because this is the text an operator reads a hundred times a day
and it is worth testing on its own.
"""

from __future__ import annotations

from ..models import JobStatus, PrintJob

# Statuses where the operator has to do something, so the notification must not
# disappear on its own.
ACTIONABLE = {JobStatus.FAILED, JobStatus.HELD, JobStatus.AWAITING}


def needs_action(job: PrintJob) -> bool:
    return job.status in ACTIONABLE


def describe(job: PrintJob) -> tuple[str, str, str]:
    """(tone, headline, detail) for a finished job, in the operator's words."""
    document = job.fields.invoice_number or job.doc_title or "Document"
    name = job.fields.customer_name

    if job.status is JobStatus.SENT:
        who = f"{name} · {job.recipient}" if name else job.recipient
        return "ok", "Sent on WhatsApp", f"{document} → {who}"

    if job.status is JobStatus.DRY_RUN:
        who = f"{name} · {job.recipient}" if name else job.recipient
        return (
            "wait",
            "Not sent — test mode",
            f"{document} would have gone to {who}. Turn off test mode in "
            f"Settings to send for real.",
        )

    if job.status is JobStatus.DUPLICATE:
        return "wait", "Already sent", job.hold_reason or f"{document} was sent before."

    if job.status is JobStatus.FAILED:
        return "bad", "Could not send", f"{document}: {job.error or 'unknown error'}"

    # AWAITING or HELD — nothing was lost, it needs a person.
    return (
        "wait",
        "Needs your attention",
        f"{document}: {job.hold_reason or 'waiting in the queue'}",
    )
