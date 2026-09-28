"""Ties the watcher to the pipeline. Shared by the console and the service."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

from .capture.spooler import latest_job
from .capture.watcher import SpoolWatcher
from .config import Settings, paths
from .pipeline import Pipeline, build_default

log = logging.getLogger(__name__)


def configure_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """Log to a rotating file, and to the console where there is one.

    Safe to call twice: `waprinter -v run` used to set up logging once for
    -v and again for run, and every line went to the file and the console
    twice over. Handlers this function added are replaced, not duplicated.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_waprinter", False):
            root.removeHandler(existing)
            existing.close()

    handler = logging.handlers.RotatingFileHandler(
        log_dir / "waprinter.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler._waprinter = True  # type: ignore[attr-defined]
    root.setLevel(level)
    root.addHandler(handler)

    # httpx writes every request URL at INFO. Meta's token inspection
    # (debug_token) takes the token as a query parameter, so at the root's
    # INFO level connecting an account wrote the access token into
    # waprinter.log in plain text -- a file every user on the counter can
    # read. Requests are not worth a line each anyway; failures are logged by
    # the code that makes them. `waprinter doctor` still turns tracing on for
    # its own calls, which carry the token only in a header.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Frozen with --windowed there is no console, and sys.stderr is None. A
    # StreamHandler on it fails on every single log record.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console._waprinter = True  # type: ignore[attr-defined]
        root.addHandler(console)


class Runner:
    """Owns the watcher thread and the pipeline it feeds."""

    def __init__(self, pipeline: Pipeline | None = None, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.pipeline = pipeline or build_default(self.settings)
        self.paths = paths()
        self.paths.ensure()
        self.pipeline.recover_interrupted()
        self.watcher = SpoolWatcher(
            spool=self.paths.spool,
            inbox=self.paths.inbox,
            on_job=self._handle,
            operation_lock=self.pipeline.operation_lock,
        )
        self._thread: threading.Thread | None = None

    def _handle(self, pdf_path: Path) -> None:
        """Process one captured PDF."""
        info = latest_job()  # empty off Windows, or if the log is disabled
        jobs = self.pipeline.process_document(
            pdf_path,
            doc_title=info.document,
            windows_user=info.user,
        )
        for job in jobs:
            log.info(
                "job %s -> %s (%s)",
                job.id,
                job.status,
                job.recipient or job.hold_reason or "",
            )

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.watcher.run, name="spool-watcher", daemon=True
        )
        self._thread.start()
        mode = "DRY RUN — nothing will be sent" if self.settings.dry_run else "LIVE"
        log.info("WhatsApp Printer service started (%s)", mode)

    def stop(self) -> None:
        self.watcher.stop()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("WhatsApp Printer service stopped")

    def run_forever(self) -> None:
        self.start()
        try:
            while self._thread and self._thread.is_alive():
                self._thread.join(timeout=1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
