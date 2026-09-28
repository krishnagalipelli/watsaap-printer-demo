"""Refresh the local template cache from the configured WhatsApp account."""

from __future__ import annotations

from .meta_account import Graph, MetaError


def _examples(body: dict, placeholders: list[str]) -> dict[str, str]:
    """Meta's example value for each body variable, keyed like placeholders.

    Positional templates carry them as one row of values in order; named
    ones as a list of {param_name, example}.
    """
    example = body.get("example") or {}
    named = example.get("body_text_named_params")
    if isinstance(named, list):
        return {
            str(item.get("param_name")): str(item.get("example"))
            for item in named
            if isinstance(item, dict) and item.get("param_name")
        }
    rows = example.get("body_text")
    if isinstance(rows, list) and rows and isinstance(rows[0], list):
        return {
            token: str(value)
            for token, value in zip(placeholders, rows[0])
            if token.isdigit()
        }
    return {}


def _template_from_meta(item: dict) -> "MessageTemplate":
    """Turn one entry of Meta's message_templates response into ours."""
    from .templates import MessageTemplate

    body, footer, header_format = {}, None, ""
    for component in item.get("components") or []:
        kind = (component.get("type") or "").upper()
        if kind == "BODY":
            body = component
        elif kind == "FOOTER":
            footer = component.get("text")
        elif kind == "HEADER":
            header_format = (component.get("format") or "").upper()

    template = MessageTemplate(
        name=item["name"],
        language=item.get("language", "en"),
        body=body.get("text") or "",
        header_document=header_format == "DOCUMENT",
        header_format=header_format,
        footer=footer,
        # Meta reports APPROVED / PENDING / REJECTED / PAUSED.
        status=(item.get("status") or "pending").lower(),
        category=(item.get("category") or "UTILITY").upper(),
        parameter_format=(item.get("parameter_format") or "positional").lower(),
    )
    template.examples = _examples(body, template.placeholders)
    return template


def fetch_templates(token: str, account_id: str, api_version: str = "v21.0",
                    client=None) -> list["MessageTemplate"]:
    """Every template on the account, or MetaError. Nothing is stored."""
    with Graph(token, api_version, client) as graph:
        items = graph.pages(
            f"{account_id}/message_templates",
            fields="name,language,status,category,components,parameter_format",
        )
    return [_template_from_meta(item) for item in items if item.get("name")]


def sync_templates(settings, store, token=None, client=None) -> int:
    """Replace the store's view of Meta's templates. Returns how many came back.

    Either every page arrives and the store is replaced at once, or nothing
    changes: a refresh that failed on page two used to leave half a list.
    """
    from ..secrets import load_token, token_problem

    token = token or load_token()
    problem = token_problem(token)
    if problem:
        raise ValueError(problem)
    if not settings.business_account_id:
        raise ValueError(
            "Connect a WhatsApp Business Account first (Setup → Connect WhatsApp)."
        )
    try:
        fetched = fetch_templates(
            token, settings.business_account_id, settings.graph_api_version, client
        )
    except MetaError as exc:
        raise ValueError(f"Meta refused template refresh: {exc}") from exc
    store.apply_refresh(fetched)
    return len(fetched)
