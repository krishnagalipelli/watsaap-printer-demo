"""The frozen entry points.

These exist because of a real shipped bug: PyInstaller was pointed at
`src/waprinter/agent.py`, which uses relative imports. PyInstaller runs its entry
script as `__main__`, so the very first import raised

    ImportError: attempted relative import with no known parent package

and because the agent is frozen `--windowed`, nothing was shown at all — the
installer produced an executable that appeared to do nothing when double-clicked,
and the build reported success.

These tests run the entry points the same way a frozen build does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
SRC = ROOT / "src"


def run(script: Path, *args: str, home: Path) -> subprocess.CompletedProcess:
    """Execute a script the way PyInstaller does: as __main__, not as a module."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": str(SRC),
        "WAPRINTER_HOME": str(home),
        "SYSTEMROOT": "C:\\Windows",  # harmless off Windows, required on it
    }
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


class TestEntryShims:
    def test_agent_shim_starts_and_self_tests(self, tmp_path):
        result = run(PACKAGING / "waprinter_agent.py", "--selftest", home=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "selftest passed" in result.stdout

    def test_agent_shim_checks_the_real_moving_parts(self, tmp_path):
        result = run(PACKAGING / "waprinter_agent.py", "--selftest", home=tmp_path)
        for part in ("settings", "pipeline", "templates", "spool dir", "dialog host"):
            assert part in result.stdout

    def test_cli_shim_runs(self, tmp_path):
        result = run(PACKAGING / "waprinter_cli.py", "--help", home=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "usage: waprinter" in result.stdout

    def test_selftest_creates_the_data_directories(self, tmp_path):
        home = tmp_path / "fresh"
        run(PACKAGING / "waprinter_agent.py", "--selftest", home=home)
        assert (home / "spool").is_dir()
        assert (home / "inbox").is_dir()


class TestWhyTheShimsExist:
    """Encode the failure so nobody 'simplifies' the entry points back."""

    @pytest.mark.parametrize("module", ["agent.py", "cli.py"])
    def test_package_modules_cannot_be_run_as_scripts(self, module, tmp_path):
        result = run(SRC / "waprinter" / module, "--help", home=tmp_path)
        assert result.returncode != 0
        assert "attempted relative import" in result.stderr


class TestCrashReporting:
    def test_a_startup_failure_is_written_to_a_file(self, tmp_path, monkeypatch):
        # --windowed has no console, so a crash that is not written down is a
        # crash nobody can diagnose.
        monkeypatch.setenv("WAPRINTER_HOME", str(tmp_path))
        from waprinter import agent

        report = agent._write_crash_report(RuntimeError("boom"))
        assert report is not None and report.exists()
        contents = report.read_text(encoding="utf-8")
        assert "RuntimeError: boom" in contents
        assert "frozen=" in contents

    def test_main_returns_nonzero_when_startup_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WAPRINTER_HOME", str(tmp_path))
        from waprinter import agent

        def explode():
            raise RuntimeError("no provider configured")

        monkeypatch.setattr(agent, "Agent", lambda *a, **k: explode())
        assert agent.main(["--selftest"]) == 1
        assert (tmp_path / "logs" / "crash.txt").exists()

    def test_logging_survives_a_missing_stderr(self, tmp_path, monkeypatch):
        # Frozen --windowed sets sys.stderr to None; a StreamHandler on it fails
        # on every record.
        import logging

        from waprinter.runner import configure_logging

        monkeypatch.setattr(sys, "stderr", None)
        root = logging.getLogger()
        original = root.handlers[:]
        try:
            root.handlers = []
            configure_logging(tmp_path / "logs")
            assert not any(
                type(h) is logging.StreamHandler for h in root.handlers
            ), "a StreamHandler was attached to a None stderr"
            logging.getLogger(__name__).info("does not raise")
        finally:
            root.handlers = original
