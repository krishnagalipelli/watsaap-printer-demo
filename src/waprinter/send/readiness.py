"""Which sender an install uses, and whether it is actually ready.

One build serves every client, so the choice is configuration. The two paths
have genuinely different trade-offs and different prerequisites, and the
operator should see both stated plainly rather than discovering them when a
send fails.

Shared by the settings page and `waprinter go-live` so they can never disagree
about what "ready" means.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings

BAILEYS = "baileys"
CLOUD = "cloud"


@dataclass(frozen=True)
class SenderInfo:
    key: str
    label: str
    summary: str
    caution: str = ""


SENDERS = [
    SenderInfo(
        key=BAILEYS,
        label="WhatsApp Web (Baileys)",
        summary=(
            "Links to a WhatsApp account by QR code. No per-message cost and "
            "the message wording is fully editable."
        ),
        caution=(
            "Unofficial. Meta can ban a number for using it, and automated "
            "daily sending is the pattern they look for. Do not point this at "
            "a business's main WhatsApp number without telling the client."
        ),
    ),
    SenderInfo(
        key=CLOUD,
        label="Official WhatsApp Business API",
        summary=(
            "Supported by Meta, with delivery receipts and no risk to the "
            "client's number."
        ),
        caution=(
            "Costs per message, and messages must use a pre-approved template, "
            "so the wording can only be changed by submitting it for review."
        ),
    ),
]


def get(key: str) -> SenderInfo | None:
    return next((s for s in SENDERS if s.key == key), None)


def problems(settings: Settings) -> list[str]:
    """What still has to be done before this install can send for real.

    Empty means ready. Dry run is not treated as a problem — it is a valid
    state, just not a sending one.
    """
    found: list[str] = []

    if not settings.own_numbers:
        found.append(
            "Your own numbers are not listed, so a number printed in your "
            "letterhead could be treated as a customer."
        )

    sender = getattr(settings, "sender_type", CLOUD)
    if sender == BAILEYS:
        from .baileys import BaileysSender

        if not BaileysSender().is_connected():
            found.append(
                "The WhatsApp Web service is not connected. Start it and scan "
                "the QR code with WhatsApp on the phone that will send."
            )
    elif sender == CLOUD:
        from ..secrets import load_token
        from .templates import TemplateStore
        from ..config import paths

        if not settings.phone_number_id:
            found.append("The WhatsApp phone number ID is not set.")
        if not load_token():
            found.append("No access token is stored (run: waprinter set-token).")

        templates = TemplateStore(paths().root / "templates.json")
        template = templates.get(settings.default_template)
        if template is None:
            found.append(
                f"Template '{settings.default_template}' is not configured."
            )
        elif not template.usable:
            found.append(
                f"Template '{template.name}' is {template.status}, not approved "
                f"for sending."
            )
    else:
        found.append(f"Unknown sender '{sender}'.")

    return found
