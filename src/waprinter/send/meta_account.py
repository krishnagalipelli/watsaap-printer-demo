"""Find out what an access token can reach, so nobody types an ID.

Setting up a counter used to mean three things copied out of Meta Business
Manager by hand: the token, the phone number ID and the WhatsApp Business
Account ID. Two of those are fifteen-digit numbers, and a wrong digit looks
exactly like a right one until the first receipt fails.

The token already knows the other two. Meta's token inspection lists the
WhatsApp Business Accounts a token was granted, each account lists its phone
numbers with the name customers see, and the same account lists its message
templates. So the connect screen asks for the token alone, shows what it can
reach in words ("+91 98765 43210 · Your Business Name"), and stores the IDs
itself.

The one thing it cannot always learn is the account: some tokens come back
without granular scopes. Then the account ID is asked for once, and
everything else still follows from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
TIMEOUT = 30.0

# The scopes whose target_ids are WhatsApp Business Accounts.
_WABA_SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")

# A token that runs out sooner than this is almost always the temporary one
# the API Setup page hands out, which lasts a day. Everything works on the
# day it is set up, and stops overnight.
SHORT_LIVED = timedelta(days=7)


class MetaError(Exception):
    """Meta refused, or could not be reached. The message is for a person."""


@dataclass
class PhoneNumber:
    id: str
    display_phone_number: str = ""
    verified_name: str = ""
    quality_rating: str = ""

    @property
    def label(self) -> str:
        parts = [self.display_phone_number or self.id]
        if self.verified_name:
            parts.append(self.verified_name)
        return " · ".join(parts)


@dataclass
class BusinessAccount:
    id: str
    name: str = ""
    phone_numbers: list[PhoneNumber] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})" if self.name else self.id


@dataclass
class Connection:
    """What one token can do, as far as Meta will say."""

    valid: bool
    expires_at: datetime | None = None   # None: does not expire
    accounts: list[BusinessAccount] = field(default_factory=list)
    app_name: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def short_lived(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at - datetime.now(timezone.utc) < SHORT_LIVED

    def expiry_text(self) -> str:
        if self.expires_at is None:
            return "This token does not expire."
        local = self.expires_at.astimezone()
        when = f"{local:%d %b %Y at %H:%M}"
        if self.short_lived:
            return (
                f"This token expires on {when}. It looks like a temporary "
                f"token, which stops working within a day. Create a permanent "
                f"one for a System User in Meta Business Settings."
            )
        return f"This token expires on {when}."


class Graph:
    """The few Graph API calls setup needs, with errors in words."""

    def __init__(
        self,
        token: str,
        api_version: str = "v21.0",
        client: httpx.Client | None = None,
    ):
        self.token = token
        self.base = f"{GRAPH_HOST}/{api_version}"
        self._owns = client is None
        self.client = client or httpx.Client(timeout=TIMEOUT)

    def close(self) -> None:
        if self._owns:
            self.client.close()

    def __enter__(self) -> "Graph":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, path: str, **params) -> dict:
        try:
            response = self.client.get(
                f"{self.base}/{path.lstrip('/')}",
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as exc:
            raise MetaError(
                f"Could not reach Meta ({type(exc).__name__}). Check the "
                f"internet connection and try again."
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            raise MetaError(f"Meta sent an unreadable reply (HTTP {response.status_code}).")
        if response.is_error or not isinstance(payload, dict) or "error" in payload:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = error.get("message") or f"HTTP {response.status_code}"
            code = error.get("code")
            if code == 190:
                message = "Meta says this access token is not valid. It may have expired or been revoked."
            raise MetaError(message)
        return payload

    def pages(self, path: str, **params) -> list[dict]:
        """Every item across Meta's pagination. All or nothing."""
        items: list[dict] = []
        seen: set[str] = set()
        params = {"limit": 100, **params}
        while True:
            payload = self.get(path, **params)
            data = payload.get("data")
            if not isinstance(data, list):
                raise MetaError("Meta's reply did not contain a list.")
            items.extend(d for d in data if isinstance(d, dict))
            paging = payload.get("paging") or {}
            if not paging.get("next"):
                return items
            cursor = (paging.get("cursors") or {}).get("after")
            if not cursor or cursor in seen:
                raise MetaError("Meta returned an incomplete list. Try again.")
            seen.add(cursor)
            params = {**params, "after": cursor}


def _expiry(raw: object) -> datetime | None:
    try:
        stamp = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def inspect(
    token: str,
    api_version: str = "v21.0",
    account_id: str = "",
    client: httpx.Client | None = None,
) -> Connection:
    """Everything this token reaches: accounts, their numbers, and expiry.

    `account_id` is used when the token does not say which accounts it was
    granted, or to look at one account in particular.
    """
    with Graph(token, api_version, client) as graph:
        info = graph.get("debug_token", input_token=token).get("data") or {}
        connection = Connection(
            valid=bool(info.get("is_valid", True)),
            expires_at=_expiry(info.get("expires_at")),
            app_name=str(info.get("application") or ""),
        )
        if not connection.valid:
            detail = (info.get("error") or {}).get("message")
            raise MetaError(
                "Meta says this access token is not valid"
                + (f": {detail}" if detail else ".")
            )

        ids: list[str] = []
        for scope in info.get("granular_scopes") or []:
            if scope.get("scope") in _WABA_SCOPES:
                for target in scope.get("target_ids") or []:
                    if str(target) not in ids:
                        ids.append(str(target))
        if account_id:
            ids = [account_id.strip()]
        if not ids:
            connection.problems.append(
                "This token does not say which WhatsApp Business Account it "
                "belongs to. Enter the account ID from WhatsApp Manager → "
                "Account tools → Overview."
            )
            return connection

        for waba in ids:
            try:
                name = graph.get(waba, fields="id,name").get("name", "")
                numbers = graph.pages(
                    f"{waba}/phone_numbers",
                    fields="id,display_phone_number,verified_name,quality_rating",
                )
            except MetaError as exc:
                connection.problems.append(f"Account {waba}: {exc}")
                continue
            connection.accounts.append(
                BusinessAccount(
                    id=waba,
                    name=str(name or ""),
                    phone_numbers=[
                        PhoneNumber(
                            id=str(n.get("id", "")),
                            display_phone_number=str(n.get("display_phone_number") or ""),
                            verified_name=str(n.get("verified_name") or ""),
                            quality_rating=str(n.get("quality_rating") or ""),
                        )
                        for n in numbers
                        if n.get("id")
                    ],
                )
            )
        if ids and not connection.accounts and not connection.problems:
            connection.problems.append("No WhatsApp Business Account could be read.")
        return connection
