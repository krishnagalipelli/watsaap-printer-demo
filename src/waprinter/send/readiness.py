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


def problems(
    settings: Settings,
    templates: "TemplateStore | None" = None,
    profile=None,
) -> list[str]:
    """What still has to be done before this install can send for real.

    Empty means ready. Dry run is not counted as a problem — it is a valid
    state, just not a sending one.

    `templates` must be the store the pipeline is actually using. Loading a
    fresh one from the default path looked equivalent but was not: it reported
    a configured message as missing whenever the two disagreed.
    """
    from ..config import paths
    from ..secrets import load_token, token_problem
    from .templates import TemplateStore

    found: list[str] = []

    if not settings.own_numbers:
        found.append(
            "Your own numbers are not listed, so a number printed in your "
            "letterhead could be treated as a customer."
        )
    if not settings.phone_number_id:
        found.append(
            "The WhatsApp phone number ID is not set. Open Setup → Connect "
            "WhatsApp and choose the number to send from."
        )
    # Not just "is one stored": a token of one control character is stored,
    # and answers truthy, and cannot send anything.
    token_issue = token_problem(load_token())
    if token_issue:
        found.append(token_issue)

    templates = templates or TemplateStore(paths().templates, settings.business_name)
    if profile is None:
        from ..extract.profile import DocumentProfile

        profile = DocumentProfile.load(paths().profile)
    from ..extract.rules import known_fields
    from .templates import unfilled_reason, unfilled_variables, variable_map

    fields = known_fields(profile)
    # Every message this install can send, not only the default one: a
    # removal notice held with "Template 'removal_notice' is not configured"
    # is the same failure as a receipt held that way, and used to be invisible
    # here until the first notice was printed.
    wanted = [settings.default_template]
    for name in settings.document_templates.values():
        if name and name not in wanted:
            wanted.append(name)
    for name in wanted:
        template = templates.get(name)
        if template is None:
            found.append(f"Message '{name}' is not configured.")
        elif settings.send_mode == "api" and not template.usable:
            # Meta's approval matters only where Meta sends the message. Link
            # mode opens WhatsApp with the text typed out, and a person
            # presses send; no template is involved on that route at all.
            found.append(
                f"Message '{template.name}' is {template.status}, not yet "
                f"approved by Meta."
            )
        if template is not None:
            # The same check the pipeline makes before it sends, made here so
            # the Status page says it before anything is printed rather than
            # the queue saying it after.
            unfilled = unfilled_variables(
                template, variable_map(template, settings), fields
            )
            if unfilled:
                found.append(unfilled_reason(template, unfilled))

    # A document type someone taught is recognised on every print from then
    # on, and one with no message is held every time. Say so here, before the
    # first one is printed, rather than in the queue after.
    if profile.custom_kinds:
        for kind in profile.custom_kinds:
            if not settings.document_templates.get(kind.name):
                found.append(
                    f"{kind.name.replace('_', ' ').capitalize()} documents have "
                    f"no message yet. Open Setup → Fill in messages."
                )

    return found


def is_ready(settings: Settings, templates: "TemplateStore | None" = None,
             profile=None) -> bool:
    return not problems(settings, templates, profile)
