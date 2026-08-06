"""Core domain types shared by capture, extraction, rules, and sending."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class Confidence(enum.StrEnum):
    """How sure we are that a phone candidate is the *customer's* number.

    Only HIGH is eligible for a silent send. Everything else is held.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class JobStatus(enum.StrEnum):
    CAPTURED = "captured"      # PDF landed, not yet parsed
    AWAITING = "awaiting"      # dialog is up, operator is entering the number
    HELD = "held"              # needs an operator decision (no/ambiguous number)
    QUEUED = "queued"          # cleared the gate, waiting on the sender
    SENT = "sent"
    FAILED = "failed"
    DUPLICATE = "duplicate"    # suppressed reprint
    DISCARDED = "discarded"    # operator threw it away
    DRY_RUN = "dry_run"        # would have sent; nothing left the machine


@dataclass
class PhoneCandidate:
    """A phone number found on the page, with the evidence for its score."""

    raw: str                    # exactly as it appeared on the page
    e164: str                   # normalised, e.g. +919876543210
    score: int                  # 0-100, see extract/phone.py for the rubric
    confidence: Confidence
    page: int
    bbox: tuple[float, float, float, float]
    label: str | None = None    # the anchoring label, e.g. "Mobile"
    reasons: list[str] = field(default_factory=list)  # why it scored this way
    # Read by OCR rather than from a text layer. A misread digit here means a
    # different person, so the gate treats these differently.
    from_ocr: bool = False


@dataclass
class ExtractedFields:
    """Everything we pulled off the printed page."""

    candidates: list[PhoneCandidate] = field(default_factory=list)
    invoice_number: str | None = None
    customer_name: str | None = None
    invoice_date: str | None = None
    total_amount: str | None = None
    page_count: int = 0
    has_text_layer: bool = True   # False => the ERP printed a raster
    used_ocr: bool = False        # a raster page that OCR managed to read
    ocr_error: str | None = None  # why OCR could not run, when it was needed

    @property
    def readable(self) -> bool:
        """Whether anything could be read off the page at all."""
        return self.has_text_layer or self.used_ocr

    @property
    def best(self) -> PhoneCandidate | None:
        return self.candidates[0] if self.candidates else None

    def as_template_vars(self) -> dict[str, str]:
        """Field values available for substitution into the message body."""
        return {
            "invoice_number": self.invoice_number or "",
            "customer_name": self.customer_name or "",
            "invoice_date": self.invoice_date or "",
            "total_amount": self.total_amount or "",
        }


@dataclass
class PrintJob:
    """One capture, from spool file through to delivery receipt."""

    id: str
    created_at: datetime
    pdf_path: Path
    status: JobStatus = JobStatus.CAPTURED

    # From the Windows spooler event log, best effort.
    doc_title: str | None = None
    windows_user: str | None = None

    fields: ExtractedFields = field(default_factory=ExtractedFields)
    recipient: str | None = None          # E.164, resolved by the gate
    confidence: Confidence | None = None
    hold_reason: str | None = None        # populated when status is HELD

    dedupe_key: str | None = None
    template_name: str | None = None
    message_preview: str | None = None    # the body text as the customer sees it

    wamid: str | None = None              # WhatsApp message id, once accepted
    error: str | None = None
    sent_at: datetime | None = None


@dataclass
class SendResult:
    """Outcome of one send attempt. Returned by every Sender implementation."""

    ok: bool
    wamid: str | None = None
    error: str | None = None
    retryable: bool = False
