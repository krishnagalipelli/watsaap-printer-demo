"""One agent per machine, and a way to knock on its door.

Two shortcuts point at the same executable: the Start menu and desktop icons,
and the logon entry that passes --hidden. Without a guard, clicking the icon
while the logon copy is already running starts a *second* agent — two watchers
racing for the same spool folder, while the window the operator actually wanted
still belongs to the invisible first copy. Closing the window only hides it, so
from the operator's side the application simply refuses to open.

The guard is a listening socket on the loopback interface. The process that
owns the window records its port; a later launch connects, says SHOW, and
exits, and the copy that owns the window brings itself to the front.

The port is ephemeral and written to a file rather than fixed, because a fixed
port eventually collides with something else on a client machine. If nothing
answers on the recorded port the file is stale — the previous copy was killed,
or the machine lost power — and this process takes over instead.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
SHOW = b"SHOW"
# Asks only whether anyone is home. The logon copy uses this: finding an agent
# already running is a reason to bow out, not to fling a window open at a
# machine nobody has finished signing in to.
PING = b"PING"
# Long enough for a busy machine to answer, short enough that a launch never
# appears to hang. A wrong answer here costs a duplicate agent, not a delay.
CONNECT_TIMEOUT = 2.0


class SingleInstance:
    """Guards the agent so only one copy owns the spool folder and the window."""

    def __init__(self, port_file: Path):
        self.port_file = port_file
        self._server: socket.socket | None = None
        self._running = False

    # -- the second copy ---------------------------------------------------

    def signal_running(self, show: bool = True) -> bool:
        """Check for an already-running agent, optionally raising its window.

        True means one answered and this process should exit without starting
        anything. False means we are the only copy.
        """
        port = self._read_port()
        if port is None:
            return False
        try:
            with socket.create_connection((HOST, port), CONNECT_TIMEOUT) as sock:
                sock.sendall(SHOW if show else PING)
        except OSError:
            # Nothing is listening: the file outlived the process that wrote
            # it. Take over rather than refusing to start.
            log.info("stale instance file (port %s); starting a new agent", port)
            return False
        log.info(
            "an agent is already running; %s",
            "asked it to show its window" if show else "leaving it alone",
        )
        return True

    def _read_port(self) -> int | None:
        try:
            return int(self.port_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    # -- the copy that owns the window -------------------------------------

    def acquire(self, on_show: Callable[[], None]) -> None:
        """Start listening, so later launches raise this window.

        `on_show` is called from the listener thread and must be thread-safe —
        it may not touch a widget directly.
        """
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind((HOST, 0))
        server.listen(8)
        self._server = server
        self._running = True

        port = server.getsockname()[1]
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        self.port_file.write_text(str(port), encoding="utf-8")
        log.info("this agent owns the window; listening on port %s", port)

        threading.Thread(
            target=self._serve, args=(on_show,), name="instance", daemon=True
        ).start()

    def _serve(self, on_show: Callable[[], None]) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()  # type: ignore[union-attr]
            except OSError:
                return  # the socket was closed under us: we are shutting down
            with conn:
                try:
                    data = conn.recv(64)
                except OSError:
                    continue
            if SHOW in data:
                try:
                    on_show()
                except Exception:
                    # A failure to raise the window must never take the agent
                    # down; printing has to carry on regardless.
                    log.exception("could not raise the window")

    def release(self) -> None:
        """Stop listening and clear the port file. Safe to call twice."""
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            self.port_file.unlink(missing_ok=True)
        except OSError:
            pass
