"""A sender for an install that is not in a state to send.

Wired in when test mode is off but the real sender cannot be built -- no
access token stored, most often. The alternative was to raise while building
the pipeline, which the agent did at startup: a provisioning file whose token
had been refused, with `dry_run: false` beside it, produced an agent that
showed a crash box at every logon and captured nothing.

Every send fails, immediately and with the reason, so a job is recorded as
FAILED with "No WhatsApp access token stored" instead of vanishing. The
status line says the same thing through readiness.problems(). Nothing is
pretended: this is not the dry-run sender, and no job is written down as
tested or sent.
"""

from __future__ import annotations

from pathlib import Path

from ..models import SendResult
from .templates import RenderedMessage


class UnavailableSender:
    """Fails every send with the reason the real sender could not be built."""

    def __init__(self, reason: str):
        self.reason = reason

    def send(
        self,
        recipient: str,
        pdf_path: Path,
        message: RenderedMessage,
    ) -> SendResult:
        return SendResult(ok=False, error=self.reason, retryable=True)
