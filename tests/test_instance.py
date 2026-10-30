"""The guard that keeps one agent, and lets a second launch reach its window."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from waprinter.instance import SingleInstance


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_no_agent_running_means_we_are_first(tmp_path):
    guard = SingleInstance(tmp_path / "instance.port")
    assert guard.signal_running() is False


def test_second_launch_raises_the_first_window(tmp_path):
    shown = threading.Event()
    owner = SingleInstance(tmp_path / "instance.port")
    owner.acquire(on_show=shown.set)
    try:
        second = SingleInstance(tmp_path / "instance.port")
        assert second.signal_running() is True
        assert _wait_for(shown.is_set), "the running agent was never asked to show"
    finally:
        owner.release()


def test_logon_start_bows_out_without_flinging_the_window_open(tmp_path):
    """--hidden finds a running agent and exits, leaving the window alone."""
    shown = threading.Event()
    owner = SingleInstance(tmp_path / "instance.port")
    owner.acquire(on_show=shown.set)
    try:
        logon = SingleInstance(tmp_path / "instance.port")
        assert logon.signal_running(show=False) is True
        time.sleep(0.2)
        assert not shown.is_set(), "a logon start should not raise the window"
    finally:
        owner.release()


def test_a_stale_port_file_does_not_block_startup(tmp_path):
    """A copy that was killed leaves its file behind; the next launch takes over."""
    port_file = tmp_path / "instance.port"
    # A port nothing is listening on, written as if by a process since killed.
    dead = SingleInstance(port_file)
    dead.acquire(on_show=lambda: None)
    dead._server.close()  # killed, without the chance to clean up
    assert port_file.exists()

    assert SingleInstance(port_file).signal_running() is False


def test_garbage_in_the_port_file_is_survivable(tmp_path):
    port_file = tmp_path / "instance.port"
    port_file.write_text("not a port", encoding="utf-8")
    assert SingleInstance(port_file).signal_running() is False


def test_release_clears_the_file_and_is_safe_twice(tmp_path):
    port_file = tmp_path / "instance.port"
    guard = SingleInstance(port_file)
    guard.acquire(on_show=lambda: None)
    assert port_file.exists()
    guard.release()
    assert not port_file.exists()
    guard.release()  # must not raise


def test_a_failure_to_raise_never_kills_the_listener(tmp_path):
    """Printing has to carry on even if bringing the window up throws."""
    calls: list[int] = []

    def on_show():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("window is in a bad state")

    owner = SingleInstance(tmp_path / "instance.port")
    owner.acquire(on_show=on_show)
    try:
        assert SingleInstance(tmp_path / "instance.port").signal_running() is True
        assert _wait_for(lambda: len(calls) == 1)
        # The listener survived the exception and still answers.
        assert SingleInstance(tmp_path / "instance.port").signal_running() is True
        assert _wait_for(lambda: len(calls) == 2)
    finally:
        owner.release()


class TestTheLockDecidesWhoRuns:
    """Who owns the machine used to be read off the port file, which got a
    stale file pointing at someone else's port, and two launches at once,
    both wrong."""

    def test_only_one_process_can_claim(self, tmp_path):
        first = SingleInstance(tmp_path / "instance.port")
        second = SingleInstance(tmp_path / "instance.port")
        try:
            assert first.claim() is True
            assert second.claim() is False
        finally:
            first.release()
            second.release()

    def test_releasing_lets_the_next_launch_claim(self, tmp_path):
        first = SingleInstance(tmp_path / "instance.port")
        assert first.claim() is True
        first.release()
        again = SingleInstance(tmp_path / "instance.port")
        try:
            assert again.claim() is True
        finally:
            again.release()

    def test_claiming_twice_from_the_owner_is_harmless(self, tmp_path):
        owner = SingleInstance(tmp_path / "instance.port")
        try:
            assert owner.claim() is True
            assert owner.claim() is True
        finally:
            owner.release()


class TestStartingTheAgent:
    @pytest.fixture
    def started(self, monkeypatch):
        """Run agent.main with the heavy parts stubbed out; record whether an
        agent was started."""
        from waprinter import agent

        runs: list[bool] = []

        class StubAgent:
            def __init__(self, instance=None):
                self.instance = instance

            def run(self, visible=True):
                runs.append(visible)

        monkeypatch.setattr(agent, "Agent", StubAgent)
        monkeypatch.setattr(agent, "configure_logging", lambda *_a, **_k: None)
        monkeypatch.setattr("waprinter.provision.apply_if_present", lambda: None)
        return agent, runs

    def test_a_stale_port_held_by_another_program_does_not_block_startup(
        self, started, tmp_path
    ):
        """After a power cut the port file named a port some other program
        then took. The launch connected to it, thought an agent was running,
        and exited -- every time, until that program let go."""
        from waprinter.config import paths

        agent, runs = started
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        try:
            paths().root.mkdir(parents=True, exist_ok=True)
            (paths().root / "instance.port").write_text(str(other.getsockname()[1]))
            assert agent.main(["--hidden"]) == 0
        finally:
            other.close()
        assert runs == [False], "the agent should have started"

    def test_a_second_launch_bows_out_while_the_first_holds_the_lock(self, started):
        from waprinter.config import paths

        agent, runs = started
        owner = SingleInstance(paths().root / "instance.port")
        try:
            assert owner.claim()
            assert agent.main(["--hidden"]) == 0
        finally:
            owner.release()
        assert runs == [], "a second agent was started beside the first"

    def test_the_lock_is_let_go_when_the_agent_stops(self, started):
        from waprinter.config import paths

        agent, _runs = started
        agent.main(["--hidden"])
        after = SingleInstance(paths().root / "instance.port")
        try:
            assert after.claim() is True
        finally:
            after.release()
