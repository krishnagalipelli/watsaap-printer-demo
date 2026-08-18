"""Whether this install is actually ready to send.

One definition, shared by the settings page, the agent and `waprinter go-live`,
so they can never disagree about what "ready" means. The operator should be able
to see what is missing before a customer's receipt fails to arrive, not after.

Messages go out through the official WhatsApp Business Cloud API. That is the
only route: an earlier build also supported WhatsApp Web via Baileys, which was
free but unofficial, and Meta bans numbers for using it. Risking the client's
main business line to save a few hundred rupees a month was not a trade worth
offering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:
    from .templates import TemplateStore


def problems(settings: Settings, templates: "TemplateStore | None" = None) -> list[str]:
    """What still has to be done before this install can send for real.

    Empty means ready. Dry run is not counted as a problem — it is a valid
    state, just not a sending one.

    `templates` must be the store the pipeline is actually using. Loading a
    fresh one from the default path looked equivalent but was not: it reported
    a configured message as missing whenever the two disagreed.
    """
    from ..config import paths
    from ..secrets import load_token
    from .templates import TemplateStore

    found: list[str] = []

    if not settings.own_numbers:
        found.append(
            "Your own numbers are not listed, so a number printed in your "
            "letterhead could be treated as a customer."
        )
    if not settings.phone_number_id:
        found.append(
            "The WhatsApp phone number ID is not set (Meta Business → WhatsApp "
            "→ API Setup)."
        )
    if not load_token():
        found.append("No access token is stored.")

    templates = templates or TemplateStore(paths().templates)
    template = templates.get(settings.default_template)
    if template is None:
        found.append(f"Message '{settings.default_template}' is not configured.")
    elif not template.usable:
        found.append(
            f"Message '{template.name}' is {template.status}, not yet approved "
            f"by Meta."
        )

    return found


def is_ready(settings: Settings, templates: "TemplateStore | None" = None) -> bool:
    return not problems(settings, templates)
