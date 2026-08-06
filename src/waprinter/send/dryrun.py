"""A sender that does everything except send.

This is how extraction accuracy gets measured before a single real message goes
out: run the client's actual print traffic through the full pipeline, then read
back exactly who each invoice *would* have gone to. It is the default until the
corpus clears its precision bar.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from ..models import SendResult
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
            "parameters": message.parameters,
            "missing_variables": message.missing,
            "preview": message.preview,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return SendResult(ok=True, wamid=f"dryrun-{uuid.uuid4().hex[:16]}")
