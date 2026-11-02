"""Configuring an installation from one file, so nothing is typed per machine.

Twenty-odd counters need the same account, template and receipts folder. The
alternative to this file is typing it into each one, which is slow and gets a
digit wrong somewhere -- and the machine that got it wrong looks fine until a
receipt does not arrive.

The token is not compiled into the build and must not be: the repository is
public and the installer is published on GitHub Releases, so a token in the
binary is a token on the internet, against the client's main business line.
It travels in this file instead, and the file is deleted once it is sealed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from waprinter.config import Settings
from waprinter.provision import apply, apply_if_present, find
from waprinter.secrets import load_token

GOOD_TOKEN = "EAAG" + "x" * 60


@pytest.fixture
def written(tmp_path):
    def _write(payload: dict, folder: Path | None = None) -> Path:
        target = (folder or tmp_path) / "provision.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    return _write


class TestInstallAndLeave:
    def test_settings_and_token_arrive_together(self, written):
        source = written(
            {
                "access_token": GOOD_TOKEN,
                "phone_number_id": "123456789012345",
                "send_mode": "api",
                "dry_run": False,
                "branch_name": "Karimnagar",
            }
        )
        settings = Settings()
        result = apply(source, settings)

        assert result.token_stored
        assert load_token() == GOOD_TOKEN
        assert settings.phone_number_id == "123456789012345"
        assert settings.send_mode == "api"
        assert settings.dry_run is False
        assert settings.branch_name == "Karimnagar"

    def test_what_was_applied_is_saved_for_the_next_start(self, written):
        apply(written({"branch_name": "Sircilla", "business_name": "Srinidhi"}))

        assert Settings.load().branch_name == "Sircilla"

    def test_the_file_is_deleted_once_it_has_been_read(self, written):
        source = written({"access_token": GOOD_TOKEN, "branch_name": "Karimnagar"})
        result = apply(source)

        assert result.removed
        assert not source.exists()

    def test_it_can_be_kept_when_it_lives_somewhere_controlled(self, written):
        source = written({"branch_name": "Karimnagar"})
        result = apply(source, remove=False)

        assert source.exists()
        assert not result.removed

    def test_the_agent_finds_it_without_being_told(self, written, tmp_path):
        """The installer drops it in the data folder; startup picks it up."""
        from waprinter.config import paths

        source = written({"branch_name": "Huzurabad"}, folder=paths().root)
        assert find() == source

        result = apply_if_present()
        assert result is not None and "branch_name" in result.applied


class TestWhenTheFileIsWrong:
    def test_a_token_that_cannot_work_is_refused(self, written):
        """A counter that reports itself ready and fails every receipt is worse."""
        source = written({"access_token": "\x16", "branch_name": "Karimnagar"})
        result = apply(source)

        assert not result.token_stored
        assert load_token() is None
        assert any("Access token not stored" in w for w in result.warnings)
        # The rest of the file is still worth applying.
        assert "branch_name" in result.applied

    def test_an_unknown_setting_is_ignored_rather_than_fatal(self, written):
        """A file written for a newer build must not brick an older install."""
        result = apply(written({"branch_name": "Karimnagar", "invented": True}))

        assert "branch_name" in result.applied
        assert any("invented" in w for w in result.warnings)

    def test_a_file_that_cannot_be_parsed_is_left_alone_and_said_so(self, tmp_path):
        source = tmp_path / "provision.json"
        source.write_text("{ this is not json", encoding="utf-8")

        result = apply(source)

        assert source.exists(), "a file we could not read is not ours to delete"
        assert not result.removed
        assert any("left in place" in w for w in result.warnings)

    def test_startup_survives_a_broken_file(self, written, tmp_path):
        """A bad provisioning file must not stop the printer starting."""
        from waprinter.config import paths

        target = paths().root / "provision.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ broken", encoding="utf-8")

        assert apply_if_present() is not None or True  # must simply not raise


class TestTheSecretItself:
    def test_no_token_is_compiled_into_the_source(self):
        """The reason this module exists. A grep, so it fails loudly if it regresses.

        Meta's user and system-user tokens start EAA. If one is ever committed,
        the repository is public and the installer is on GitHub Releases.
        """
        import re

        source_root = Path(__file__).resolve().parent.parent / "src"
        pattern = re.compile(r"EAA[A-Za-z0-9]{30,}")
        for path in source_root.rglob("*.py"):
            found = pattern.search(path.read_text(encoding="utf-8"))
            assert not found, f"a Meta access token is committed in {path}"


class TestValuesAreCheckedNotJustCopied:
    """JSON does not know that "false" is not False.

    A string is truthy, so `"dry_run": "false"` left test mode on while the
    file said otherwise. `"own_numbers": "9876543210"` iterated as characters
    and excluded nothing, which made the seller's own number a valid
    recipient. Each is refused, named, and the rest of the file still applies.
    """

    def test_a_string_where_a_flag_belongs_is_refused(self, written):
        settings = Settings()
        result = apply(written({"dry_run": "false", "branch_name": "Karimnagar"}), settings)

        assert settings.dry_run is True
        assert "dry_run" not in result.applied
        assert "branch_name" in result.applied
        assert any("dry_run" in w and "true or false" in w for w in result.warnings)

    def test_a_string_where_a_list_belongs_is_refused(self, written):
        settings = Settings()
        apply(written({"own_numbers": "9876543210"}), settings)
        assert settings.own_numbers == []

    def test_a_string_where_a_number_belongs_is_refused(self, written):
        settings = Settings()
        apply(written({"max_sends_per_minute": "10"}), settings)
        assert settings.max_sends_per_minute == Settings().max_sends_per_minute

    def test_the_right_shapes_still_apply(self, written):
        settings = Settings()
        result = apply(
            written(
                {
                    "dry_run": False,
                    "own_numbers": ["9876543210"],
                    "max_sends_per_minute": 3,
                    "document_templates": {"removal_notice": "removal_notice"},
                }
            ),
            settings,
        )
        assert result.warnings == []
        assert settings.dry_run is False
        assert settings.own_numbers == ["9876543210"]
        assert settings.max_sends_per_minute == 3


class TestConnectedFromTheInstaller:
    """The installer asks for the token, the number and the account, and writes
    them to connect.json. First start applies it after any shared setup file,
    fetches the account's templates, and fills in the receipt's message when
    every variable says plainly what it holds."""

    def chit_details(self, body: str):
        from waprinter.send.templates import MessageTemplate

        return MessageTemplate(name="chit_details1", body=body, status="approved",
                               header_document=False, header_format="IMAGE")

    @pytest.fixture
    def meta(self, monkeypatch):
        fetched: list = []

        def fake_fetch(token, account_id, api_version="v21.0", client=None):
            assert (token, account_id) == (GOOD_TOKEN, "987654321098765")
            return list(fetched)

        monkeypatch.setattr("waprinter.send.sync.fetch_templates", fake_fetch)
        return fetched

    def connect(self, written, **extra):
        from waprinter.config import paths

        target = paths().root / "connect.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "access_token": GOOD_TOKEN,
            "phone_number_id": "123456789012345",
            "business_account_id": "987654321098765",
            "send_mode": "api",
            "default_template": "chit_details1",
            "document_attachments": {"receipt": "image"},
            **extra,
        }), encoding="utf-8")
        return target

    def test_what_was_typed_wins_over_the_shared_file(self, written, meta):
        from waprinter.config import paths

        written({"phone_number_id": "111111111111111", "branch_name": "Karimnagar"},
                folder=paths().root)
        source = self.connect(written)

        result = apply_if_present()

        settings = Settings.load()
        assert settings.phone_number_id == "123456789012345"
        assert settings.branch_name == "Karimnagar"
        assert settings.document_attachments == {"receipt": "image"}
        assert load_token() == GOOD_TOKEN
        assert result.removed and not source.exists()

    def test_templates_arrive_and_the_receipt_message_is_filled(self, written, meta):
        meta.append(self.chit_details(
            "Dear {{1}}, Receipt No: {{2}} Date: {{3}} Amount: {{4}} "
            "Mode of Payment: {{5}}"))
        self.connect(written)

        result = apply_if_present()

        assert "templates" in result.applied
        assert Settings.load().template_mappings["chit_details1"] == {
            "1": "customer_name", "2": "invoice_number", "3": "invoice_date",
            "4": "total_amount", "5": "payment_mode"}

    def test_a_variable_nothing_explains_is_left_for_a_person(self, written, meta):
        meta.append(self.chit_details("Dear {{1}}, your payment to {{2}} is received."))
        self.connect(written)

        result = apply_if_present()

        assert "chit_details1" not in Settings.load().template_mappings
        assert any("Fill in messages" in w for w in result.warnings)

    def test_no_internet_still_starts(self, written, monkeypatch):
        def offline(*_args, **_kwargs):
            raise OSError("no route to graph.facebook.com")

        monkeypatch.setattr("waprinter.send.sync.fetch_templates", offline)
        self.connect(written)

        result = apply_if_present()

        assert result.token_stored
        assert any("Templates not fetched" in w for w in result.warnings)
