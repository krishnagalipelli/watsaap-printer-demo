"""Baileys WhatsApp sender.

Talks to a local Node.js microservice running on port 8732.
"""

from __future__ import annotations

from pathlib import Path
import httpx

from ..models import SendResult
from .templates import RenderedMessage


class BaileysSender:
    def __init__(self, port: int = 8732, timeout: float = 30.0):
        self.url = f"http://127.0.0.1:{port}/send"
        self._client = httpx.Client(timeout=timeout)

    def send(
        self,
        recipient: str,
        pdf_path: Path,
        message: RenderedMessage,
    ) -> SendResult:
        
        # Build the message text from the template parts (like the Cloud API does)
        # We can just send the text alongside the PDF caption.
        # Alternatively, we could send two messages (document, then text), but 
        # the Node server expects `message` as caption for simplicity.
        
        text = ""
        if message.parameters:
            text = "\n".join(message.parameters)

        try:
            response = self._client.post(
                self.url,
                json={
                    "recipient": recipient.lstrip("+"),
                    "pdf_path": str(pdf_path.absolute()),
                    "message": text
                }
            )
        except httpx.HTTPError as exc:
            return SendResult(
                ok=False, 
                error=f"Could not connect to Baileys service: {exc}", 
                retryable=True
            )

        try:
            payload = response.json()
        except ValueError:
            return SendResult(
                ok=False, 
                error=f"HTTP {response.status_code}: unreadable response",
                retryable=response.status_code >= 500
            )

        if not response.is_success or not payload.get("ok"):
            error_msg = payload.get("error", "Unknown error")
            return SendResult(ok=False, error=error_msg, retryable=False)

        wamid = payload.get("wamid", "unknown")
        return SendResult(ok=True, wamid=wamid)

    def get_status(self) -> dict:
        try:
            url = self.url.replace("/send", "/status")
            response = self._client.get(url)
            if response.is_success:
                return response.json()
            return {"state": "error", "error": f"HTTP {response.status_code}"}
        except Exception as exc:
            return {"state": "error", "error": str(exc)}
