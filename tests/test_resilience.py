"""Things that used to stop the agent starting, or lose data quietly.

The agent is frozen --windowed and starts at logon on machines nobody
administers. A failure at startup there is a crash box the clerk clicks past,
and then a printer that does nothing. Everything in this file is a way that
used to happen, or a way a file grew until the disk was full.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import pytest

from waprinter.config import Settings, atomic_write_text, paths


class TestABrokenSettingsFileDoesNotStopTheAgent:
    def test_a_truncated_file_yields_defaults_and_is_set_aside(self, tmp_path, caplog):
        path = tmp_path / "settings.json"
        path.write_text('{"dry_run": tr', encoding="utf-8")  # power cut mid-write

        with caplog.at_level(logging.ERROR):
            settings = Settings.load(path)

        assert settings.dry_run is True, "the defaults are the safe state"
        assert not path.exists()
        kept = list(tmp_path.glob("settings.json.corrupt-*"))
        assert len(kept) == 1, "the broken file is kept for whoever looks into it"
        assert "could not read" in caplog.text

    def test_a_wrongly_typed_value_is_dropped_not_stored(self, tmp_path, caplog):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"dry_run": "false", "branch_name": "K"}))

        with caplog.at_level(logging.WARNING):
            settings = Settings.load(path)

        assert settings.dry_run is True
        assert settings.branch_name == "K"
        assert "dry_run" in caplog.text

    def test_saving_is_atomic(self, tmp_path):
        """Either the old file or the new one, never half of each."""
        path = tmp_path / "settings.json"
        Settings(branch_name="before").save(path)

        def explode(*a, **k):
            raise OSError("disk full")

        import os

        real = os.replace
        try:
            os.replace = explode
            with pytest.raises(OSError):
                Settings(branch_name="after").save(path)
        finally:
            os.replace = real

        assert Settings.load(path).branch_name == "before"
        assert list(tmp_path.glob("*.tmp")) == [], "no temp file left behind"

    def test_atomic_write_replaces_in_place(self, tmp_path):
        path = tmp_path / "f.txt"
        atomic_write_text(path, "one")
        atomic_write_text(path, "two")
        assert path.read_text() == "two"
        assert list(tmp_path.iterdir()) == [path]


class TestABrokenTemplatesFileDoesNotStopTheAgent:
    def test_an_unknown_key_is_ignored(self, tmp_path):
        """A file from a newer build, or a hand edit, raised TypeError here."""
        from waprinter.send.templates import TemplateStore

        path = tmp_path / "templates.json"
        path.write_text(
            json.dumps(
                {
                    "templates": [
                        {
                            "name": "chit_receipt",
                            "body": "hi {{1}}",
                            "status": "approved",
                            "quality_score": {"score": "GREEN"},
                        }
                    ]
                }
            )
        )
        store = TemplateStore(path, "Biz")
        assert store.get("chit_receipt").status == "approved"

    def test_a_truncated_file_keeps_the_built_ins(self, tmp_path, caplog):
        from waprinter.send.templates import TemplateStore

        path = tmp_path / "templates.json"
        path.write_text('{"templates": [{"na')
        with caplog.at_level(logging.ERROR):
            store = TemplateStore(path, "Biz")
        assert store.get("chit_receipt") is not None
        assert list(tmp_path.glob("templates.json.corrupt-*"))

    def test_built_ins_are_not_approved_until_someone_says_so(self):
        # chit_receipt used to ship as "approved" so that link mode, which
        # needs no approval, did not report itself not ready. That made an
        # API install look ready before Meta had seen the template at all.
        from waprinter.send.templates import default_templates

        assert all(t.status == "pending" for t in default_templates("Biz"))


class TestAMissingTokenDoesNotStopTheAgent:
    def test_the_pipeline_builds_with_a_sender_that_says_why(self, monkeypatch):
        from waprinter.models import ExtractedFields
        from waprinter.pipeline import build_default
        from waprinter.send.templates import RenderedMessage, MessageTemplate
        from waprinter.send.unavailable import UnavailableSender

        monkeypatch.setattr("waprinter.secrets.load_token", lambda: None)
        settings = Settings(dry_run=False, send_mode="api")

        pipeline = build_default(settings)

        assert isinstance(pipeline.sender, UnavailableSender)
        result = pipeline.sender.send(
            "+919876543210",
            Path("/x.pdf"),
            RenderedMessage(template=MessageTemplate(name="t")),
        )
        assert result.ok is False
        assert "access token" in result.error

    def test_rebuild_sender_still_refuses_so_the_settings_page_can_revert(
        self, pipeline, monkeypatch
    ):
        # The window relies on this raising to put test mode back on.
        monkeypatch.setattr("waprinter.secrets.load_token", lambda: None)
        pipeline.settings.dry_run = False
        with pytest.raises(RuntimeError, match="access token"):
            pipeline.rebuild_sender()


class TestGoLiveUsesTheSameChecksAsTheWindow:
    def test_a_token_of_one_control_character_is_refused(self, monkeypatch, capsys):
        """go-live had its own weaker copy of readiness.

        It tested only whether *a* token was stored, so a token of one
        control character -- what Ctrl+V types in a Command Prompt -- passed
        go-live and then failed every send.
        """
        from waprinter.cli import cmd_go_live

        monkeypatch.setattr("waprinter.secrets.load_token", lambda: "\x16")
        Settings(own_numbers=["9845012345"], phone_number_id="1", send_mode="api").save()

        assert cmd_go_live(None) == 1
        out = capsys.readouterr().out
        assert "Cannot go live" in out
        assert "control character" in out
        assert Settings.load().dry_run is True


class TestLoggingIsConfiguredOnce:
    def test_calling_it_twice_does_not_double_every_line(self, tmp_path):
        from waprinter.runner import configure_logging

        root = logging.getLogger()
        before = list(root.handlers)
        try:
            configure_logging(tmp_path)
            configure_logging(tmp_path)
            ours = [h for h in root.handlers if getattr(h, "_waprinter", False)]
            files = [h for h in ours if isinstance(h, logging.FileHandler)]
            assert len(files) == 1
        finally:
            for h in list(root.handlers):
                if h not in before:
                    root.removeHandler(h)
                    h.close()


class TestTemplateParametersAreWhatMetaAccepts:
    def test_tabs_newlines_and_runs_of_spaces_are_flattened(self):
        """Meta rejects a newline, a tab, or five spaces inside a parameter.

        Page text arrives with all three: a row joins its columns with runs
        of spaces.
        """
        from waprinter.models import ExtractedFields
        from waprinter.send.templates import MessageTemplate, render

        template = MessageTemplate(name="t", body="Dear {{1}}, ref {{2}}")
        fields = ExtractedFields(
            customer_name="ANITHA\tRAMESH     X", invoice_number="CR1/26\nextra"
        )
        message = render(template, {"1": "customer_name", "2": "invoice_number"}, fields)

        assert message.parameters == ["ANITHA RAMESH X", "CR1/26 extra"]


class TestHousekeeping:
    @pytest.fixture
    def data(self, tmp_path):
        p = paths()
        p.ensure()
        return p

    def _pdf(self, folder: Path, name: str, age_days: float) -> Path:
        f = folder / name
        f.write_bytes(b"%PDF-1.4\n%%EOF")
        stamp = time.time() - age_days * 86_400
        import os

        os.utime(f, (stamp, stamp))
        return f

    def test_old_captured_pdfs_are_removed_and_waiting_ones_kept(self, data, pipeline):
        from waprinter import housekeeping
        from waprinter.models import JobStatus, PrintJob

        old = self._pdf(data.inbox, "old.pdf", age_days=200)
        fresh = self._pdf(data.inbox, "fresh.pdf", age_days=1)
        waiting = self._pdf(data.inbox, "waiting.pdf", age_days=200)
        pipeline.store.upsert(
            PrintJob(
                id="w",
                created_at=datetime.now(),
                pdf_path=waiting,
                status=JobStatus.HELD,
            )
        )
        settings = Settings(keep_inbox_days=90)

        removed = housekeeping.run(data, settings, pipeline.store)

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()
        assert waiting.exists(), "what Send would upload is never pruned"

    def test_zero_keeps_everything(self, data, pipeline):
        from waprinter import housekeeping

        old = self._pdf(data.inbox, "old.pdf", age_days=2000)
        assert housekeeping.run(data, Settings(keep_inbox_days=0), pipeline.store) == 0
        assert old.exists()

    def test_the_dry_run_log_is_rotated_and_the_crash_log_capped(self, data, pipeline):
        from waprinter import housekeeping

        dry = data.logs / "dry_run.jsonl"
        dry.write_bytes(b"x" * (housekeeping.DRY_RUN_LOG_MAX_BYTES + 1))
        crash = data.logs / "crash.txt"
        crash.write_bytes(b"old line\n" * 200_000)

        housekeeping.run(data, Settings(), pipeline.store)

        assert not dry.exists()
        assert (data.logs / "dry_run.jsonl.1").exists()
        assert crash.stat().st_size <= housekeeping.CRASH_LOG_MAX_BYTES
        assert crash.read_bytes().startswith(b"old line\n")
