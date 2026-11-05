"""Licence-bound WhatsApp setup. Only the requesting PC can open its envelope."""
from __future__ import annotations

import base64
import json
import re
import sys
from dataclasses import replace

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from . import licence
from .config import Settings, atomic_write_text, paths
from .instance import SingleInstance
from .secrets import _win32_protect, _win32_unprotect, save_token, token_problem


def _device_key() -> PrivateKey:
    root = paths().root
    lock = SingleInstance(root / "activation-key.port")
    if not lock.claim():
        raise licence.LicenceError("The activation request is being created. Try again.")
    try:
        path = root / "activation-key.bin"
        if path.exists():
            data = base64.b64decode(path.read_bytes(), validate=True)
            return PrivateKey(_win32_unprotect(data) if sys.platform == "win32" else data)
        key = PrivateKey.generate()
        data = _win32_protect(bytes(key)) if sys.platform == "win32" else bytes(key)
        atomic_write_text(path, base64.b64encode(data).decode("ascii"))
        return key
    finally:
        lock.release()


def request_code() -> str:
    return f"{licence.machine_code()}.{bytes(_device_key().public_key).hex()}"


def _connection(raw):
    if not isinstance(raw, dict):
        raise ValueError("The connection file must contain a JSON object.")
    result = {}
    for field in ("access_token", "phone_number_id", "business_account_id", "default_template"):
        value = raw.get(field, "chit_details1" if field == "default_template" else "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"The connection needs {field}.")
        result[field] = value.strip()
    for field in ("phone_number_id", "business_account_id"):
        if not re.fullmatch(r"[0-9]+", result[field]):
            raise ValueError(f"{field} must contain digits only.")
    problem = token_problem(result["access_token"])
    if problem or any(ch.isspace() for ch in result["access_token"]):
        raise ValueError("The access token is incomplete or contains whitespace.")
    own_numbers = raw.get("own_numbers", [])
    if not isinstance(own_numbers, list) or not all(isinstance(n, str) for n in own_numbers):
        raise ValueError("own_numbers must be a list of phone numbers as text.")
    result["own_numbers"] = own_numbers
    mapping = raw.get("template_mappings", {})
    if not isinstance(mapping, dict):
        raise ValueError("template_mappings must be an object.")
    receipt_mapping = mapping.get(result["default_template"].split("@")[0], {})
    if not isinstance(receipt_mapping, dict) or not all(
        isinstance(source, str) for source in receipt_mapping.values()
    ):
        raise ValueError("Invalid receipt template mapping.")
    result["template_mappings"] = {result["default_template"].split("@")[0]: receipt_mapping}
    return result


def seal_connection(public_hex: str, raw: dict) -> str:
    config = _connection(raw)
    public = PublicKey(bytes.fromhex(public_hex))
    return base64.b64encode(SealedBox(public).encrypt(json.dumps(config).encode())).decode()


def activate(entitlement: licence.Licence, signed_token: str) -> None:
    """Check Meta and the receipt mapping before switching the counter to live."""
    from .extract.profile import DocumentProfile
    from .send.meta_account import Graph, MetaError
    from .send.sync import fetch_templates
    from .send.templates import TemplateStore, unfilled_variables
    from .teaching import available_fields
    from .ui.setupmodel import mapping_problems, suggest_mapping

    try:
        blob = base64.b64decode(entitlement.connection_box, validate=True)
        config = _connection(json.loads(SealedBox(_device_key()).decrypt(blob)))
    except (CryptoError, ValueError, TypeError) as exc:
        raise licence.LicenceError("Copy a new activation request from this counter and reissue the key.") from exc

    settings = Settings.load(raise_if_unreadable=True)
    token = config["access_token"]
    account = config["business_account_id"]
    phone = config["phone_number_id"]
    try:
        with Graph(token, settings.graph_api_version) as graph:
            numbers = graph.pages(f"{account}/phone_numbers", fields="id,display_phone_number,verified_name")
        sender = next((n for n in numbers if str(n.get("id")) == phone), None)
        if not sender or not sender.get("display_phone_number"):
            raise licence.LicenceError("The licensed phone number is unavailable on this WhatsApp account.")
        fetched = fetch_templates(token, account, settings.graph_api_version)
    except MetaError as exc:
        raise licence.LicenceError(f"Could not verify the connection. {exc} Paste the key again to retry.") from exc

    ref = config["default_template"]
    matches = [t for t in fetched if ref in (t.name, t.ref)]
    template = next((t for t in matches if t.language == settings.template_language), None)
    if template is None and len(matches) == 1:
        template = matches[0]
    if template is None or not template.usable_for("image"):
        raise licence.LicenceError(f"WhatsApp needs an approved image-header template named {ref} "
                                  f"in the selected language. Check the template and retry.")
    profile = DocumentProfile.load(paths().profile)
    fields = available_fields(profile, "receipt")
    mapping = suggest_mapping(template, fields, current=config["template_mappings"][template.name])
    if mapping_problems(template, mapping) or unfilled_variables(template, mapping, set(fields) | {"business_name"}):
        raise licence.LicenceError(f"Reissue the licence with template_mappings for {template.name}.")

    # Keep live sends disabled across any interrupted multi-file account switch.
    # All remote/validation errors above leave the existing account untouched.
    first_connection = not settings.phone_number_id
    replace(settings, dry_run=True).save()
    store = TemplateStore(paths().templates, settings.business_name, template.language)
    store.apply_refresh(fetched)
    save_token(token)
    licence._save(entitlement, signed_token)
    settings.phone_number_id = phone
    settings.business_account_id = account
    settings.business_name = sender.get("verified_name") or entitlement.licensee
    settings.own_numbers = list(dict.fromkeys(
        [*settings.own_numbers, *config["own_numbers"], sender["display_phone_number"]]
    ))
    settings.default_template = template.ref if len(matches) > 1 else ref
    if first_connection and settings.document_templates == Settings().document_templates:
        # Only offer the built-in notice routes this account actually has.
        settings.document_templates = {
            kind: name for kind, name in settings.document_templates.items()
            if any(t.name == name for t in fetched)
        }
    settings.document_templates["receipt"] = settings.default_template
    settings.document_attachments["receipt"] = "image"
    settings.template_mappings[template.name] = mapping
    settings.template_language = template.language
    settings.send_mode = "api"
    settings.confirm_before_send = False
    settings.dry_run = False
    settings.save()
