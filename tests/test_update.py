"""Version checking and self-update.

The rules being pinned here matter more than the mechanism: a version comparison
that sorts wrongly strands every machine on an old build, an unverified download
is arbitrary code execution, and an unreachable update server must never affect
printing.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from waprinter import update

MANIFEST = "https://example.test/latest.json"


class TestVersionOrder:
    @pytest.mark.parametrize(
        "newer,older",
        [
            ("1.0.1", "1.0.0"),
            ("1.1.0", "1.0.9"),
            # The one string comparison gets wrong, which would strand every
            # machine on 1.2.9 forever.
            ("1.2.10", "1.2.9"),
            ("2.0.0", "1.99.99"),
            ("v1.1.0", "1.0.0"),
        ],
    )
    def test_it_orders_numerically(self, newer, older):
        assert update.is_newer(newer, older)
        assert not update.is_newer(older, newer)

    def test_the_same_version_is_not_newer(self):
        assert not update.is_newer("1.0.0", "1.0.0")

    def test_rubbish_does_not_raise(self):
        assert update.parse_version("not-a-version") == (0,)


class TestCheck:
    @respx.mock
    def test_it_reports_an_available_update(self):
        respx.get(MANIFEST).mock(
            return_value=httpx.Response(
                200, json={"version": "9.9.9", "url": "https://example.test/s.exe"}
            )
        )
        result = update.check(MANIFEST, current="1.0.0")
        assert result.available
        assert result.release.version == "9.9.9"
        assert "9.9.9" in result.message

    @respx.mock
    def test_it_reports_being_up_to_date(self):
        respx.get(MANIFEST).mock(
            return_value=httpx.Response(
                200, json={"version": "1.0.0", "url": "https://example.test/s.exe"}
            )
        )
        result = update.check(MANIFEST, current="1.0.0")
        assert not result.available
        assert "Up to date" in result.message

    @respx.mock
    def test_an_unreachable_server_is_not_an_error_the_operator_owns(self):
        respx.get(MANIFEST).mock(side_effect=httpx.ConnectError("offline"))
        result = update.check(MANIFEST, current="1.0.0")
        assert result.failed
        assert not result.available
        assert "Nothing else is affected" in result.message

    @respx.mock
    def test_a_malformed_manifest_does_not_raise(self):
        respx.get(MANIFEST).mock(return_value=httpx.Response(200, json={"oops": True}))
        assert update.check(MANIFEST, current="1.0.0").failed

    def test_no_configured_url_is_reported_plainly(self):
        assert "No update location" in update.check("").message


class TestDownload:
    @respx.mock
    def test_it_verifies_the_checksum(self, tmp_path, monkeypatch):
        payload = b"pretend installer"
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        respx.get("https://example.test/s.exe").mock(
            return_value=httpx.Response(200, content=payload)
        )
        release = update.Release(
            version="2.0.0",
            url="https://example.test/s.exe",
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        path = update.download(release)
        assert path.read_bytes() == payload

    @respx.mock
    def test_a_mismatched_checksum_is_refused_and_deleted(self, tmp_path, monkeypatch):
        # Corrupted, truncated or substituted — never execute it.
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        respx.get("https://example.test/s.exe").mock(
            return_value=httpx.Response(200, content=b"tampered")
        )
        release = update.Release(
            version="2.0.0", url="https://example.test/s.exe", sha256="00" * 32
        )
        with pytest.raises(RuntimeError, match="checksum"):
            update.download(release)
        assert list(tmp_path.glob("*.exe")) == []


class TestDailySchedule:
    def test_a_first_run_is_due(self):
        assert update.due(None)

    def test_a_recent_check_is_not_due(self):
        assert not update.due(datetime.now().isoformat())

    def test_a_check_from_yesterday_is_due(self):
        assert update.due((datetime.now() - timedelta(days=2)).isoformat())

    def test_a_corrupt_timestamp_is_due_rather_than_stuck(self):
        assert update.due("not a date")


class TestAgentIntegration:
    def test_the_manual_check_reports_without_installing(self, pipeline, monkeypatch):
        """The button must work even when nothing can be installed."""
        from waprinter.agent import Agent

        monkeypatch.setattr(
            update, "check", lambda *a, **k: update.CheckResult(message="Up to date.")
        )
        agent = Agent.__new__(Agent)  # no window, no watcher
        agent.settings = pipeline.settings
        agent.pipeline = pipeline
        import threading

        agent._busy = threading.Event()

        assert agent.check_updates() == "Up to date."

    def test_it_will_not_install_while_a_document_is_being_sent(
        self, pipeline, monkeypatch
    ):
        import threading

        from waprinter.agent import Agent

        monkeypatch.setattr(
            update,
            "check",
            lambda *a, **k: update.CheckResult(
                available=True,
                release=update.Release(version="9.9.9", url="https://x/s.exe"),
                message="available",
            ),
        )
        agent = Agent.__new__(Agent)
        agent.settings = pipeline.settings
        agent.pipeline = pipeline
        agent._busy = threading.Event()
        agent._busy.set()

        message = agent.check_updates()
        assert "being sent" in message
