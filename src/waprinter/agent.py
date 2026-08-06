"""The desktop agent: one process, everything the operator needs.

Replaces the Windows service for v1, for a concrete reason: a service runs in
session 0 with no desktop and *cannot* show a window. The whole flow now hinges
on a dialog appearing when someone prints, so the code that watches the spool
folder has to live in the signed-in user's session.

Three parts, and the thread each runs on matters:

    main thread    Tk main loop — owns every window (Tk requires this)
    watcher        polls the spool folder, runs the pipeline
    web            the dashboard on 127.0.0.1

The watcher never touches a widget. It hands a job id to the DialogHost queue
and the main thread picks it up.
"""

from __future__ import annotations

import logging
import threading

from .capture.spooler import latest_job
from .capture.watcher import SpoolWatcher
from .config import Settings, paths
from .models import JobStatus
from .pipeline import Pipeline, build_default
from .runner import configure_logging
from .ui.send_dialog import DialogHost

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, pipeline: Pipeline | None = None, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.pipeline = pipeline or build_default(self.settings)
        self.paths = paths()
        self.paths.ensure()

        self.host = DialogHost(self.pipeline, on_error=self._notify)
        self.watcher = SpoolWatcher(
            spool=self.paths.spool,
            inbox=self.paths.inbox,
            on_job=self._handle,
        )
        self._threads: list[threading.Thread] = []

    # -- callbacks ---------------------------------------------------------

    def _notify(self, message: str) -> None:
        """Surface a problem the operator needs to know about."""
        log.error(message)
        try:
            from tkinter import messagebox

            messagebox.showerror("WhatsApp Printer", message)
        except Exception:
            pass  # no display, or the dialog host is gone

    def _handle(self, pdf_path) -> None:
        """Runs on the watcher thread. Must not touch Tk."""
        info = latest_job()
        job = self.pipeline.process(
            pdf_path,
            doc_title=info.document,
            windows_user=info.user,
        )
        log.info("job %s -> %s", job.id, job.status)

        if job.status is JobStatus.AWAITING:
            self.host.submit(job.id)

    # -- lifecycle ---------------------------------------------------------

    def _serve_web(self) -> None:
        import uvicorn

        from .ui.app import create_app

        uvicorn.run(
            create_app(self.pipeline),
            host="127.0.0.1",
            port=self.settings.ui_port,
            log_level="warning",
        )

    def run(self) -> None:
        for target, name in ((self.watcher.run, "watcher"), (self._serve_web, "web")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        mode = "DRY RUN — nothing will be sent" if self.settings.dry_run else "LIVE"
        log.info(
            "WhatsApp Printer agent started (%s); dashboard on http://127.0.0.1:%s",
            mode,
            self.settings.ui_port,
        )

        # Blocks until the Tk loop exits.
        self.host.run()
        self.watcher.stop()
        log.info("WhatsApp Printer agent stopped")


def main() -> None:
    configure_logging(paths().logs)
    try:
        Agent().run()
    except Exception:
        log.exception("agent crashed")
        raise


if __name__ == "__main__":
    main()
