"""Meta WhatsApp Cloud API sender.

Two calls per invoice:

1. Upload the PDF to /{phone_number_id}/media, which returns a media id. Using
   the media endpoint rather than a public link means the PDF never has to be
   hosted anywhere reachable from the internet — worth it for documents that
   carry a customer's billing details.
2. Send a template message whose header component references that media id.

Media ids expire (Meta documents 30 days), which does not matter here because
the id is used seconds after it is minted.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ..models import SendResult
from .templates import RenderedMessage

GRAPH_HOST = "https://graph.facebook.com"

# Failures worth retrying: transient server trouble and throttling. Everything
# else (bad number, unapproved template, expired token) will fail identically on
# a retry, so it goes straight to the operator instead.
RETRYABLE_CODES = {
    130429,  # rate limit hit
    131056,  # pair rate limit
    133016,  # temporary account restriction
    368,     # temporarily blocked for policy violations
}


class WhatsAppCloudSender:
    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        api_version: str = "v21.0",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.api_version = api_version
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def _base(self) -> str:
        return f"{GRAPH_HOST}/{self.api_version}"

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    # -- public API --------------------------------------------------------

    def send(
        self,
        recipient: str,
        pdf_path: Path,
        message: RenderedMessage,
    ) -> SendResult:
        if not message.template.usable:
            return SendResult(
                ok=False,
                error=(
                    f"Template '{message.template.name}' is "
                    f"{message.template.status}, not approved for sending."
                ),
                retryable=False,
            )

        try:
            media_id = self._upload(pdf_path, message.filename)
        except _ApiError as exc:
            return SendResult(ok=False, error=f"Upload failed: {exc}", retryable=exc.retryable)
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"Upload failed: {exc}", retryable=True)

        try:
            wamid = self._send_template(recipient, media_id, message)
        except _ApiError as exc:
            return SendResult(ok=False, error=str(exc), retryable=exc.retryable)
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=str(exc), retryable=True)

        return SendResult(ok=True, wamid=wamid)

    # -- internals ---------------------------------------------------------

    def _upload(self, pdf_path: Path, filename: str) -> str:
        with pdf_path.open("rb") as fh:
            response = self._client.post(
                f"{self._base}/{self.phone_number_id}/media",
                headers=self._auth,
                data={"messaging_product": "whatsapp", "type": "application/pdf"},
                files={"file": (filename, fh, "application/pdf")},
            )
        payload = _parse(response)
        media_id = payload.get("id")
        if not media_id:
            raise _ApiError("Media upload returned no id", retryable=False)
        return media_id

    def _send_template(
        self,
        recipient: str,
        media_id: str,
        message: RenderedMessage,
    ) -> str:
        components: list[dict] = [
            {
                "type": "header",
                "parameters": [
                    {
                        "type": "document",
                        "document": {"id": media_id, "filename": message.filename},
                    }
                ],
            }
        ]
        if message.parameters:
            components.append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": value} for value in message.parameters
                    ],
                }
            )

        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            # The Graph API takes the number without a leading "+".
            "to": recipient.lstrip("+"),
            "type": "template",
            "template": {
                "name": message.template.name,
                "language": {"code": message.template.language},
                "components": components,
            },
        }

        response = self._client.post(
            f"{self._base}/{self.phone_number_id}/messages",
            headers={**self._auth, "Content-Type": "application/json"},
            json=body,
        )
        payload = _parse(response)
        messages = payload.get("messages") or []
        if not messages or "id" not in messages[0]:
            raise _ApiError("Send returned no message id", retryable=False)
        return messages[0]["id"]


class _ApiError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _parse(response: httpx.Response) -> dict:
    """Turn a Graph API response into a payload or a classified error."""
    try:
        payload = response.json()
    except ValueError:
        raise _ApiError(
            f"HTTP {response.status_code}: unreadable response",
            retryable=response.status_code >= 500,
        ) from None

    if response.is_success and "error" not in payload:
        return payload

    error = payload.get("error", {})
    code = error.get("code")
    detail = error.get("error_data", {}).get("details") or error.get("message")
    retryable = (
        response.status_code >= 500
        or response.status_code == 429
        or code in RETRYABLE_CODES
    )
    raise _ApiError(f"[{code}] {detail}", retryable=retryable)
