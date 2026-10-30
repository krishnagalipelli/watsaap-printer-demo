"""Per-computer licences: what is accepted, the trial, and what it holds back.

The rule that matters most: an unlicensed counter keeps every print. It holds
the send, never the capture, so importing a licence late loses nothing.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from invoice_factory import ChitReceiptSpec, build_chit_receipt

from waprinter import licence as lic
from waprinter.config import paths
from waprinter.models import JobStatus

# A key for these tests only; the real one never leaves the issuing machine.
SECRET = bytes(range(32))
TODAY = date(2026, 10, 30)


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setattr(lic, "PUBLIC_KEY", lic.public_key_of(SECRET).hex())
    lic._cache.clear()


def make(machine=None, until=date(2027, 10, 30), secret=SECRET, licensee="Sri Chits, Kukatpally"):
    licence = lic.Licence(
        licensee=licensee,
        machine=machine or lic.machine_code(),
        issued=date(2026, 10, 30),
        updates_until=until,
        serial="ABC123",
    )
    return lic.describe(licence, lic.issue(licence, secret))


def end_trial():
    """Start the trial far enough back that it is over today."""
    paths().root.mkdir(parents=True, exist_ok=True)
    started = date.today() - timedelta(days=lic.TRIAL_DAYS + 1)
    (paths().root / "trial-started.txt").write_text(started.isoformat())


class TestSignature:
    def test_it_matches_rfc_8032(self):
        secret = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        public = lic.public_key_of(secret)
        signature = lic.sign(secret, b"")
        assert public.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        assert signature.hex() == (
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065"
            "224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24"
            "655141438e7a100b")
        assert lic.verify(public, b"", signature)

    def test_a_changed_message_fails(self):
        public = lic.public_key_of(SECRET)
        assert not lic.verify(public, b"other", lic.sign(SECRET, b"message"))

    def test_the_shipped_key_is_a_real_point(self):
        assert lic._decompress(bytes.fromhex("5b45621fb377b9ec52a264e8b35e4ac149ce3d988f7b550e91e69f7a90717550"))


class TestParsing:
    def test_a_whole_file_reads_back(self):
        licence = lic.parse(make(machine="ABCD-EFGH-JKLM-NPQR-STUV"))
        assert licence.licensee == "Sri Chits, Kukatpally"
        assert licence.machine == "ABCD-EFGH-JKLM-NPQR-STUV"
        assert licence.updates_until == date(2027, 10, 30)

    def test_the_token_line_alone_is_enough(self):
        token = make().splitlines()[-1]
        assert lic.parse(f"  {token}  ").licensee

    def test_another_key_is_refused(self):
        with pytest.raises(lic.LicenceError, match="not issued"):
            lic.parse(make(secret=bytes(32)))

    def test_an_edited_date_is_refused(self):
        token = make().splitlines()[-1]
        payload, signature = token[len(lic.TOKEN_PREFIX):].split(".")
        fields = json.loads(lic._unb64(payload))
        fields["updates_until"] = "2099-01-01"
        forged = lic.TOKEN_PREFIX + lic._b64(json.dumps(fields).encode()) + "." + signature
        with pytest.raises(lic.LicenceError, match="altered"):
            lic.parse(forged)

    def test_text_that_is_not_a_licence(self):
        with pytest.raises(lic.LicenceError, match="WAP1"):
            lic.parse("hello")

    def test_a_cut_off_line(self):
        with pytest.raises(lic.LicenceError, match="incomplete"):
            lic.parse("WAP1.abc")


class TestInstalling:
    def test_it_licenses_this_computer(self):
        lic.install(make())
        state = lic.status()
        assert state.licensed and state.can_send
        assert state.summary() == "Licensed to Sri Chits, Kukatpally"

    def test_another_computers_licence_is_refused_and_nothing_is_kept(self):
        with pytest.raises(lic.LicenceError, match="this one is"):
            lic.install(make(machine="ABCD-EFGH-JKLM-NPQR-STUV"))
        assert not (paths().root / "licence.txt").exists()

    def test_a_copied_licence_file_does_not_license_this_computer(self):
        paths().root.mkdir(parents=True, exist_ok=True)
        (paths().root / "licence.txt").write_text(make(machine="ABCD-EFGH-JKLM-NPQR-STUV"))
        state = lic.status()
        assert not state.licensed
        assert "ABCD-EFGH-JKLM-NPQR-STUV" in state.note

    def test_a_licence_keeps_sending_after_the_trial(self):
        end_trial()
        lic.install(make())
        assert lic.status().can_send


class TestTrial:
    def test_a_fresh_install_sends(self):
        state = lic.status(TODAY)
        assert state.can_send and not state.licensed
        assert state.summary() == f"Trial: {lic.TRIAL_DAYS} days left"

    def test_it_ends(self):
        lic.status(TODAY)
        over = lic.status(TODAY + timedelta(days=lic.TRIAL_DAYS))
        assert not over.can_send
        assert "no licence" in over.hold_reason()

    def test_moving_the_clock_back_ends_it(self):
        lic.status(TODAY)
        assert not lic.status(TODAY - timedelta(days=3)).can_send

    def test_an_altered_record_ends_it(self):
        lic.status(TODAY)
        (paths().root / "trial-started.txt").write_text("soon")
        state = lic.status(TODAY)
        assert not state.can_send
        assert "trial ended and" in state.hold_reason()

    def test_the_status_page_sends_them_to_the_licence_step(self):
        from waprinter.ui.viewmodel import fixed_by

        lic.status(TODAY)
        assert fixed_by(lic.status(TODAY + timedelta(days=30)).hold_reason()) == "licence"


class TestSending:
    @pytest.fixture
    def receipt(self, tmp_path):
        return build_chit_receipt(ChitReceiptSpec(), tmp_path / "receipt.pdf")

    def test_an_unlicensed_counter_keeps_the_print_and_holds_it(self, link_pipeline, receipt):
        end_trial()
        job = link_pipeline.process(receipt, doc_title="Chit Receipt")
        assert job.status is JobStatus.HELD
        assert "no licence" in job.hold_reason
        assert job.chat_url is None

    def test_the_api_route_is_held_too(self, pipeline, make_invoice):
        pipeline.settings.dry_run = False
        end_trial()
        job = pipeline.process(make_invoice())
        assert job.status is JobStatus.HELD

    def test_test_mode_is_not_held(self, pipeline, make_invoice):
        end_trial()
        assert pipeline.process(make_invoice()).status is JobStatus.DRY_RUN

    def test_the_held_print_sends_once_licensed(self, link_pipeline, receipt):
        end_trial()
        job = link_pipeline.process(receipt, doc_title="Chit Receipt")
        with pytest.raises(ValueError, match="no licence"):
            link_pipeline.release(job.id, "9000012345")
        lic.install(make())
        assert link_pipeline.release(job.id, "9000012345").status is JobStatus.READY

    def test_readiness_names_it(self, settings, templates):
        from waprinter.send.readiness import problems

        end_trial()
        assert any("no licence" in p for p in problems(settings, templates))


class TestUpdates:
    def test_a_build_after_the_amc_is_not_installed(self):
        lic.install(make(until=date(2027, 10, 30)))
        refusal = lic.status().update_refusal("2.4.0", date(2027, 11, 2))
        assert "updates for this computer ended on 30 Oct 2027" in refusal

    def test_a_build_inside_the_amc_is(self):
        lic.install(make(until=date(2027, 10, 30)))
        assert lic.status().update_refusal("2.1.0", date(2027, 10, 30)) == ""

    def test_a_trial_takes_updates(self):
        assert lic.status().update_refusal("2.1.0", date(2099, 1, 1)) == ""

    def test_the_manifest_carries_the_release_date(self, tmp_path):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))
        from make_update_manifest import manifest

        for suffix in ("", "-win7-x86"):
            (tmp_path / f"WhatsAppPrinter-Setup-2.1.0{suffix}.exe").write_bytes(b"x")
        assert manifest(tmp_path, "o/r", "v2.1.0", released="2026-11-01")["released"] == "2026-11-01"

    def test_the_check_reads_it(self):
        import httpx
        import respx

        from waprinter import update

        body = {"version": "9.0.0", "url": "https://x/y.exe", "sha256": "a" * 64,
                "released": "2027-01-02"}
        with respx.mock:
            respx.get("https://u/latest.json").mock(return_value=httpx.Response(200, json=body))
            result = update.check("https://u/latest.json", build="x64")
        assert result.release.released == date(2027, 1, 2)


class TestTools:
    def test_the_cli_imports_and_shows(self, tmp_path, capsys):
        from waprinter.cli import main

        path = tmp_path / "licence.txt"
        path.write_text(make())
        assert main(["licence", str(path)]) == 0
        assert main(["licence"]) == 0
        assert "Licensed to Sri Chits" in capsys.readouterr().out

    def test_the_issuer_writes_a_licence_the_app_accepts(self, tmp_path, monkeypatch):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import issue_licence

        key = tmp_path / "key" / "signing-key.txt"
        key.parent.mkdir()
        key.write_text(SECRET.hex())
        out = tmp_path / "out.txt"
        assert issue_licence.main(["--key", str(key), "issue", "--machine", lic.machine_code().lower(),
                                   "--licensee", "Branch 4", "-o", str(out)]) == 0
        assert lic.install(out.read_text()).licensee == "Branch 4"
        assert "Branch 4" in (key.parent / "issued.csv").read_text()
