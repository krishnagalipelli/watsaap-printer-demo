"""The application window.

One process, one toolkit, no browser. The control panel and the after-print
notification are both native windows drawn by WebView2 on Windows (the engine
built into Windows 10 and 11) and WebKit on macOS. The operator sees an
application, not a localhost URL.

Why a webview rather than native widgets: the panel is already HTML and reads
like a printer properties page. Rebuilding it in Tk would look worse, be harder
to change, and gain nothing — while mixing Tk *and* a webview is not an option,
because both want to own the main thread.

Threading, which is the part that bites:

    main thread   webview's own loop, started last and blocks until quit
    watcher       captures prints, runs the pipeline, queues a job id
    notifier      turns queued job ids into notification windows

`webview.create_window` is safe to call from another thread once the loop is
running, which is what lets the notifier work at all.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import webview

log = logging.getLogger(__name__)

PANEL_WIDTH = 780
PANEL_HEIGHT = 720
NOTE_WIDTH = 360
NOTE_HEIGHT = 132
NOTE_HEIGHT_ACTIONS = 172   # taller when it carries buttons
NOTE_MARGIN = 24
TASKBAR_ALLOWANCE = 56
AUTO_CLOSE_SECONDS = 5.0
POLL_SECONDS = 0.3


class NoteApi:
    """What the notification window can call back into.

    Exposed to the page as `window.pywebview.api`.
    """

    def __init__(self, shell: "AppShell"):
        self._shell = shell
        self.window: webview.Window | None = None

    def open_panel(self) -> None:
        self._shell.show_panel()
        self.dismiss()

    def dismiss(self) -> None:
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass  # already closed
            self.window = None


class AppShell:
    """Owns every window in the application."""

    def __init__(self, pipeline, port: int):
        self.pipeline = pipeline
        self.port = port
        self.incoming: queue.Queue[str] = queue.Queue()
        self.panel: webview.Window | None = None
        self._notes: list[NoteApi] = []
        self._running = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- notifications -----------------------------------------------------

    def submit(self, job_id: str) -> None:
        """Thread-safe: queue a finished job to be shown."""
        self.incoming.put(job_id)

    def _corner(self, height: int) -> tuple[int, int]:
        """Bottom right of the primary screen, clear of the taskbar."""
        try:
            screen = webview.screens[0]
            return (
                screen.width - NOTE_WIDTH - NOTE_MARGIN,
                screen.height - height - TASKBAR_ALLOWANCE,
            )
        except Exception:
            return (40, 40)

    def _show_note(self, job_id: str) -> None:
        job = self.pipeline.store.get(job_id)
        if job is None:
            return
        from .result import needs_action

        actionable = needs_action(job)
        height = NOTE_HEIGHT_ACTIONS if actionable else NOTE_HEIGHT
        x, y = self._corner(height)

        api = NoteApi(self)
        api.window = webview.create_window(
            "WhatsApp Printer",
            f"{self.base_url}/note/{job_id}",
            width=NOTE_WIDTH,
            height=height,
            x=x,
            y=y,
            frameless=True,
            easy_drag=False,
            on_top=True,
            resizable=False,
            js_api=api,
        )
        self._notes.append(api)

        if not actionable:
            # Good news gets out of the way by itself. Anything the operator
            # has to act on stays until they dismiss it.
            threading.Timer(AUTO_CLOSE_SECONDS, api.dismiss).start()

    def _notifier(self) -> None:
        """Turns queued job ids into windows. Runs off the main thread."""
        self._running.wait()
        while True:
            try:
                job_id = self.incoming.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            except Exception:
                break
            try:
                # One at a time: a batch print should not tile the screen.
                for note in self._notes:
                    note.dismiss()
                self._notes.clear()
                self._show_note(job_id)
            except Exception:
                log.exception("could not show the notification for %s", job_id)

    # -- panel -------------------------------------------------------------

    def show_panel(self) -> None:
        """Bring the control panel up, creating it if it was closed."""
        try:
            if self.panel is not None:
                self.panel.show()
                self.panel.restore()
                return
        except Exception:
            self.panel = None  # it was destroyed; make a new one

        self.panel = webview.create_window(
            "WhatsApp Printer",
            self.base_url,
            width=PANEL_WIDTH,
            height=PANEL_HEIGHT,
            min_size=(560, 480),
        )

    # -- lifecycle ---------------------------------------------------------

    def run(self, show_panel: bool = True) -> None:
        """Block on the GUI loop. Must be called from the main thread."""
        if show_panel:
            self.show_panel()
        else:
            # Started at logon: sit quietly until something is printed.
            self.panel = None

        threading.Thread(target=self._notifier, name="notifier", daemon=True).start()

        def ready() -> None:
            self._running.set()

        # storage_path keeps the webview's own cache out of the program folder,
        # which is read-only for a standard user under Program Files.
        webview.start(ready, private_mode=False)

    def stop(self) -> None:
        for note in self._notes:
            note.dismiss()
        try:
            if self.panel is not None:
                self.panel.destroy()
        except Exception:
            pass
