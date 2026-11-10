"""Issue a key, paste it on a fresh counter, and print through the API path."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from invoice_factory import ChitReceiptSpec, build_chit_receipt
from waprinter import activation, licence
from waprinter.config import Settings, paths
from waprinter.models import JobStatus
from waprinter.pipeline import build_default
from waprinter.secrets import load_token

BASE = "https://graph.facebook.com/v21.0"
TOKEN = "test-only-access-token-for-this-account"
SECRET = bytes(range(32))


@pytest.fixture
def issued(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    import issue_licence

    monkeypatch.setattr(licence, "PUBLIC_KEY", licence.public_key_of(SECRET).hex())
    key = tmp_path / "signing.key"
    key.write_text(SECRET.hex())
    config = tmp_path / "issuer.json"
    config.write_text(json.dumps({"access_token": TOKEN, "phone_number_id": "123",
                                  "business_account_id": "456"}))
    out = tmp_path / "issued.txt"
    request = activation.request_code()
    assert activation.request_code() == request
    assert issue_licence.main(["--key", str(key), "issue", "--machine", request,
                               "--licensee", "Test branch", "--connection", str(config),
                               "-o", str(out)]) == 0
    text = out.read_text()
    assert TOKEN not in text and TOKEN not in (tmp_path / "issued.csv").read_text()
    entitlement = licence.parse(text)
    assert TOKEN.encode() not in licence._unb64(entitlement.connection_box)
    return text.splitlines()[-1]


def mock_meta(header="IMAGE"):
    respx.get(f"{BASE}/456/phone_numbers").respond(json={"data": [
        {"id": "123", "verified_name": "Demo branch", "display_phone_number": "+919888811111"}
    ]})
    respx.get(f"{BASE}/456/message_templates").respond(json={"data": [{
        "name": "chit_details1", "language": "en", "status": "APPROVED",
        "parameter_format": "NAMED", "components": [
            {"type": "HEADER", "format": header},
            {"type": "BODY", "text": "Receipt {{receipt_no}}, paid by {{payment_mode}}."},
        ],
    }]})


def check_print(path, pipeline, expected_mode, upload, send, expected_line):
    job, = pipeline.process_document(path)
    assert job.status is JobStatus.SENT, job.hold_reason
    assert job.fields.payment_method == expected_mode
    assert job.recipient == job.fields.best.e164
    media = upload.calls.last.request.read()
    assert b"image/jpeg" in media and b"\xff\xd8\xff" in media
    msg = json.loads(send.calls.last.request.read())
    assert msg["to"] == job.recipient.lstrip("+")
    header = msg["template"]["components"][0]
    assert header["parameters"] == [{"type": "image", "image": {"id": "media1"}}]
    body = next(c for c in msg["template"]["components"] if c["type"] == "body")
    sent = {p.get("parameter_name"): p["text"] for p in body["parameters"]}
    # The member is told which payment this was, number and all.
    assert sent["payment_mode"] == expected_line
    return job


@respx.mock
def test_pasting_one_key_enables_automatic_cash_and_upi_image_sends(issued, tmp_path):
    mock_meta()
    licence.install(issued)
    assert licence.status().licensed
    settings = Settings.load()
    assert settings.send_mode == "api" and not settings.dry_run
    assert settings.receipt_attachment == "image"
    from waprinter.send.readiness import problems
    assert problems(settings) == []
    assert load_token() == TOKEN and TOKEN not in paths().settings.read_text()
    upload = respx.post(f"{BASE}/123/media").respond(json={"id": "media1"})
    send = respx.post(f"{BASE}/123/messages").respond(json={"messages": [{"id": "wamid.test"}]})
    pipeline = build_default()
    for number, cell, mode, line in [
        ("CR11/26", "Cash", "Cash", "Cash"),
        ("CHQ12/26", "Transaction Number\n919876543210 dated 06/10/2026\nThrough ONLINE-UPI", "UPI",
         "UPI Transaction No. 919876543210 dated 06/10/2026"),
    ]:
        path = build_chit_receipt(ChitReceiptSpec(receipt_number=number, payment_mode=cell),
                                  tmp_path / f"{mode}.pdf")
        job = check_print(path, pipeline, mode, upload, send, line)
        assert job.recipient == "+919000012345"
    assert upload.call_count == send.call_count == 2


@respx.mock
def test_failed_template_check_can_retry_without_changing_the_counter(issued):
    mock_meta(header="DOCUMENT")
    before = Settings.load()
    with pytest.raises(licence.LicenceError, match="image-header"):
        licence.install(issued)
    assert Settings.load() == before and load_token() is None
    assert not licence.status().licensed
    mock_meta()
    licence.install(issued)
    assert not Settings.load().dry_run


def test_another_install_cannot_decrypt_a_key_even_with_the_same_machine_code(issued, monkeypatch, tmp_path):
    monkeypatch.setenv("WAPRINTER_HOME", str(tmp_path / "other"))
    with pytest.raises(licence.LicenceError, match="new activation request"):
        licence.install(issued)
    assert load_token() is None and not licence.status().licensed
