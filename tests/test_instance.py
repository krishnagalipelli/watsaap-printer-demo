"""The guard that keeps one agent, and lets a second launch reach its window."""

from __future__ import annotations

import threading
import time

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
