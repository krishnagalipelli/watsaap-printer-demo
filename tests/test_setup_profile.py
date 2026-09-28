from __future__ import annotations

import json

import httpx
import pytest
import respx

from waprinter.config import Settings, paths
from waprinter.provision import apply
from waprinter.send.templates import TemplateStore
from waprinter.setup_profile import check_pdf, export_setup


def test_export_import_carries_three_messages_but_no_token_or_local_folder(tmp_path, templates):
    settings = Settings(default_template="chits_details", dry_run=False,
                        branch_name="Branch A", pdf_folder="D:/Receipts")
    target = tmp_path / "provision.json"
    export_setup(target, settings, templates)
    data = json.loads(target.read_text())
    assert data["dry_run"] is True
    assert "access_token" not in data
    assert "pdf_folder" not in data
    assert "branch_name" not in data
    assert {"chits_details", "removal_notice", "removal_letter"} <= {t["name"] for t in data["templates"]}
    result = apply(target, remove=False)
    assert not result.warnings
    assert target.exists()
    imported = Settings.load()
    assert imported.default_template == "chits_details"
    assert imported.document_templates == settings.document_templates
    assert TemplateStore(paths().templates).get("invoice_document").status == "approved"


def test_bad_template_bundle_does_not_apply_settings(tmp_path):
    target = tmp_path / "bad.json"
    target.write_text(json.dumps({"branch_name": "changed", "templates": [{"name": "bad", "body": 10}]}))
    settings = Settings(branch_name="before")
    result = apply(target, settings)
    assert result.warnings
    assert settings.branch_name == "before"
    assert target.exists()


def test_sample_check_is_read_only(pipeline, make_invoice):
    preview = check_pdf(pipeline, make_invoice())
    assert "invoice_document" in preview
    assert "Nothing sent" in preview
    assert pipeline.store.recent() == []


def test_importing_live_settings_without_a_token_never_keeps_a_test_sender(pipeline, monkeypatch):
    from waprinter.send.unavailable import UnavailableSender

    monkeypatch.setattr("waprinter.secrets.load_token", lambda: None)
    Settings(dry_run=False, send_mode="api").save()
    pipeline.reload_if_changed()
    assert isinstance(pipeline.sender, UnavailableSender)


@respx.mock
def test_template_refresh_fetches_all_pages(tmp_path):
    from waprinter.send.sync import sync_templates

    url = "https://graph.facebook.com/v21.0/123/message_templates"
    route = respx.get(url).mock(side_effect=[
        httpx.Response(200, json={"data": [{"name": "first", "status": "APPROVED"}],
                                  "paging": {"next": "ignored", "cursors": {"after": "page2"}}}),
        httpx.Response(200, json={"data": [{"name": "second", "status": "APPROVED"}]}),
    ])
    store = TemplateStore(tmp_path / "templates.json")
    assert sync_templates(Settings(business_account_id="123"), store, "EAA" + "x" * 60) == 2
    assert route.calls[1].request.url.params["after"] == "page2"
    assert store.get("first").status == "approved"
    assert store.get("second").status == "approved"


@respx.mock
def test_failed_refresh_keeps_the_previous_cache(tmp_path):
    from waprinter.send.sync import sync_templates

    url = "https://graph.facebook.com/v21.0/123/message_templates"
    respx.get(url).mock(side_effect=[
        httpx.Response(200, json={"data": [{"name": "partial"}],
                                  "paging": {"next": "ignored", "cursors": {"after": "page2"}}}),
        httpx.Response(403, json={"error": {"message": "Permission denied"}}),
    ])
    store = TemplateStore(tmp_path / "templates.json")
    with pytest.raises(ValueError, match="Permission denied"):
        sync_templates(Settings(business_account_id="123"), store, "EAA" + "x" * 60)
    assert store.get("partial") is None
