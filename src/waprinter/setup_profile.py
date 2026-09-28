"""Portable counter setup and read-only document checks."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .config import Settings, atomic_write_text
from .send.templates import MessageTemplate


def read_templates(items: object) -> list[MessageTemplate]:
    if not isinstance(items, list):
        raise ValueError("templates must be a list")
    result = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError("Every template needs a name")
        for key in ("name", "body", "language", "status", "category", "parameter_format"):
            if key in item and not isinstance(item[key], str):
                raise ValueError(f"Template {key} must be text")
        if "header_document" in item and not isinstance(item["header_document"], bool):
            raise ValueError("Template header_document must be true or false")
        if item.get("footer") is not None and not isinstance(item["footer"], str):
            raise ValueError("Template footer must be text")
        known = MessageTemplate.__dataclass_fields__
        result.append(MessageTemplate(**{k: v for k, v in item.items() if k in known}))
    return result


def read_document_profile(item: object):
    """Taught document types and fields from a setup file, or ValueError.

    Checked strictly here, unlike profile.json at startup: a setup file is
    being applied on purpose, by someone who can fix it, and applying half of
    it would leave a counter recognising a document it has no fields for.
    """
    from .extract.profile import DocumentProfile, _valid_kinds, _valid_rules

    if not isinstance(item, dict):
        raise ValueError("document_profile must be an object")
    kinds_in = item.get("custom_kinds", [])
    rules_in = item.get("field_rules", {})
    if not isinstance(kinds_in, list) or not isinstance(rules_in, dict):
        raise ValueError("document_profile has the wrong shape")
    kinds = _valid_kinds(kinds_in)
    rules = _valid_rules(rules_in)
    if len(kinds) != len(kinds_in):
        raise ValueError("a taught document type is incomplete")
    if sum(len(v) for v in rules.values()) != sum(
        len(v) for v in rules_in.values() if isinstance(v, list)
    ):
        raise ValueError("a taught field is incomplete or has a broken pattern")
    DocumentProfile(custom_kinds=kinds, field_rules=rules)  # compiles, or raises
    return kinds, rules


def export_setup(path: Path, settings: Settings, templates, profile=None) -> None:
    """Export reusable settings, templates and taught documents; never credentials.

    A new counter starts in test mode, with its own branch/folder choices.
    Taught document types and fields travel too, so a layout is taught once
    and every counter reads it -- not taught again at each one.
    """
    payload = asdict(settings)
    for key in ("last_update_check", "branch_name", "device_name", "pdf_folder"):
        payload.pop(key, None)
    payload["dry_run"] = True
    payload["templates"] = [asdict(t) for t in templates.all()]
    if profile is not None and (profile.custom_kinds or profile.field_rules):
        payload["document_profile"] = {
            "custom_kinds": [asdict(k) for k in profile.custom_kinds],
            "field_rules": {
                key: [asdict(r) for r in rules]
                for key, rules in profile.field_rules.items()
            },
        }
    payload["_comment"] = (
        "Reusable counter setup. Access token excluded. Enter the token on the "
        "new counter, refresh templates, check one PDF of each kind, then turn "
        "off test mode. Branch, computer and archive folder remain local."
    )
    atomic_write_text(path, json.dumps(payload, indent=2))


def check_pdf(pipeline, pdf_path: Path) -> str:
    """Exercise extraction and routing without storing or sending a job."""
    from datetime import datetime

    from .extract import extract_fields
    from .models import PrintJob
    from .rules.gate import excluded_numbers

    groups = pipeline._receipt_groups(pdf_path)
    if len(groups) > 1:
        return "This PDF contains several documents. Choose one receipt, notice or letter for this check."
    fields = extract_fields(
        pdf_path, excluded_numbers=excluded_numbers(pipeline.settings),
        country_code=pipeline.settings.default_country_code,
        ocr=pipeline.settings.ocr(), profile=pipeline.profile,
    )
    job = PrintJob(id="preview", created_at=datetime.now(), pdf_path=pdf_path, fields=fields)
    name, message = pipeline.compose(job)
    kind = (fields.document_kind or "receipt / unrecognised").replace("_", " ").capitalize()
    lines = [f"Detected: {kind}", f"WhatsApp template: {name}"]
    if message is None:
        lines.append("No message configured. Choose a template in Messages.")
    else:
        lines.extend([f"Template status: {message.template.status}", f"Attachment: {message.filename}"])
        if message.missing:
            lines.append("Missing values: " + ", ".join(message.missing))
        lines.extend(["", message.preview])
    if not fields.document_kind_verified:
        lines.append("\nThe document type was not confirmed by OCR; automatic sending will wait for review.")
    lines.append("\nPreview only. Nothing sent or added to the queue.")
    return "\n".join(lines)
