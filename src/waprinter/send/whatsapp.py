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

import logging
import time
from pathlib import Path

import httpx

from ..models import SendResult
from .templates import RenderedMessage

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"

# How many times to attempt the media upload, and how long to wait between.
# Upload is the one step that can be retried without asking whether it already
# worked: a second upload mints a second media id and costs nothing. A dropped
# connection here is common on a counter PC behind antivirus TLS inspection,
# and losing a customer's receipt to one blip is not acceptable.
UPLOAD_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.5, 2.0)

# Binding the socket to any IPv4 address forces the connection onto IPv4.
# Counter PCs on Indian consumer broadband routinely get an IPv6 route to
# Facebook that completes a TCP handshake and then black-holes anything large
# enough to need fragmenting — the upload dies with no response while every
# small request looks perfectly healthy.
IPV4_ANY = "0.0.0.0"

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
        force_ipv4: bool = False,
    ):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.api_version = api_version
        self._timeout = timeout
        # A caller-supplied client is never replaced: it belongs to whoever
        # passed it, and the tests depend on keeping theirs.
        self._owns_client = client is None
        self._ipv4_only = False
        self._client = client or self._new_client(force_ipv4)
        if force_ipv4:
            self._ipv4_only = True

    def _new_client(self, ipv4_only: bool) -> httpx.Client:
        transport = (
            httpx.HTTPTransport(local_address=IPV4_ANY) if ipv4_only else None
        )
        return httpx.Client(timeout=self._timeout, transport=transport)

    def _fall_back_to_ipv4(self) -> bool:
        """Move this session onto IPv4 after a transport failure. Once only.

        The whole client is swapped, not just the upload, so the template send
        that follows goes the same way — a media id uploaded over a route that
        works is no use if the message referencing it takes the broken one.
        """
        if self._ipv4_only or not self._owns_client:
            return False
        log.info("connection dropped; retrying over IPv4 for the rest of this session")
        try:
            self._client.close()
        except Exception:
            pass
        self._client = self._new_client(ipv4_only=True)
        self._ipv4_only = True
        return True

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
            media_id = self._upload_with_retries(pdf_path, message.filename)
        except _ApiError as exc:
            return SendResult(ok=False, error=f"Upload failed: {exc}", retryable=exc.retryable)
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"Upload failed: {exc}", retryable=True)

        try:
            wamid = self._send_template(recipient, media_id, message)
        except _ApiError as exc:
            return SendResult(ok=False, error=str(exc), retryable=exc.retryable)
        except httpx.HTTPError as exc:
            # Deliberately NOT retried. The connection dropped without an
            # answer, so whether Meta accepted the message is unknown — and a
            # retry that guesses wrong sends a customer their receipt twice.
            # A person decides this one, from the queue.
            return SendResult(
                ok=False,
                error=f"{exc} — the message may or may not have been sent. "
                f"Check WhatsApp before resending.",
                retryable=False,
            )

        return SendResult(ok=True, wamid=wamid)

    def _upload_with_retries(self, pdf_path: Path, filename: str) -> str:
        """Upload, retrying the failures that a retry can actually fix."""
        last: Exception | None = None
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                return self._upload(pdf_path, filename)
            except httpx.HTTPError as exc:
                last = exc
                # A transport-level failure is the symptom of a bad route, not
                # of a bad request. Change the route before trying again.
                self._fall_back_to_ipv4()
            except _ApiError as exc:
                if not exc.retryable:
                    raise
                last = exc
            if attempt < UPLOAD_ATTEMPTS - 1:
                pause = RETRY_BACKOFF_SECONDS[
                    min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)
                ]
                log.info(
                    "media upload attempt %s of %s failed (%s); retrying in %ss",
                    attempt + 1,
                    UPLOAD_ATTEMPTS,
                    last,
                    pause,
                )
                time.sleep(pause)
        assert last is not None
        raise last

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
            # A named template needs every body parameter labelled; a
            # positional one is matched by order alone and rejects the label.
            names = message.parameter_names
            body_params: list[dict] = []
            for index, value in enumerate(message.parameters):
                param = {"type": "text", "text": value}
                if index < len(names):
                    param["parameter_name"] = names[index]
                body_params.append(param)
            components.append({"type": "body", "parameters": body_params})

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
