"""The seam between the capture pipeline and however messages actually leave.

Everything upstream — watcher, extractor, gate — knows only this interface. The
on-premise build talks to Meta directly; the multi-tenant build will post to our
own backend instead. Swapping one for the other must not require touching a line
of capture or parsing code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import SendResult
from .templates import RenderedMessage


class Sender(Protocol):
    """Delivers one PDF to one recipient."""

    def send(
        self,
        recipient: str,
        pdf_path: Path,
        message: RenderedMessage,
    ) -> SendResult:
        """Deliver `pdf_path` to `recipient` (E.164) using `message`.

        Implementations must not raise for ordinary delivery failures — return
        SendResult(ok=False, ...) with `retryable` set appropriately, so the
        caller can distinguish "try again in a minute" from "this will never
        work".
        """
        ...
