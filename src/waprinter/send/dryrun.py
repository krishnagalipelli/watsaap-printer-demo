"""A sender that does everything except send.

This is how extraction accuracy gets measured before a single real message goes
out: run the client's actual print traffic through the full pipeline, then read
back exactly who each invoice *would* have gone to. It is the default until the
corpus clears its precision bar.

An image send is the one thing here that is really done rather than recorded:
the JPEG is rendered, measured and deleted, so a document that cannot be made
into one fails during the trial instead of after go-live.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from ..models import SendResult
from .attachment import IMAGE, ConversionError, prepared
from .templates import RenderedMessage


class DryRunSender:
    """Records what would have been sent to a JSONL file. Sends nothing."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def send(
        self,
        recipient: str,
        pdf_path: Path,
        message: RenderedMessage,
    ) -> SendResult:
        entry = {
            "at": datetime.now().isoformat(),
            "recipient": recipient,
            "pdf": str(pdf_path),
            "template": message.template.name,
            "filename": message.filename,
            "attachment": message.attachment,
            "parameters": message.parameters,
            "missing_variables": message.missing,
            "preview": message.preview,
        }
        if message.attachment == IMAGE:
            # Actually rendered, and thrown away. A trial run exists to find
            # out what live sending would do, and "the receipt is too big to
            # go as an image" is exactly the kind of thing worth learning
            # during the trial rather than from a member who got nothing.
            try:
                with prepared(pdf_path, message.attachment, message.filename) as (
                    image_path,
                    _media_type,
                ):
                    entry["image_bytes"] = image_path.stat().st_size
            except ConversionError as exc:
                return SendResult(ok=False, error=str(exc), retryable=False)

        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return SendResult(ok=True, wamid=f"dryrun-{uuid.uuid4().hex[:16]}")
