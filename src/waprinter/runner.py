"""Ties the watcher to the pipeline. Shared by the console and the service."""

from __future__ import annotations

import logging
import logging.handlers
import threading
from pathlib import Path

from .capture.spooler import latest_job
from .capture.watcher import SpoolWatcher
from .config import Settings, paths
from .pipeline import Pipeline, build_default

log = logging.getLogger(__name__)


def configure_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "waprinter.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


class Runner:
    """Owns the watcher thread and the pipeline it feeds."""

    def __init__(self, pipeline: Pipeline | None = None, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.pipeline = pipeline or build_default(self.settings)
        self.paths = paths()
        self.paths.ensure()
        self.watcher = SpoolWatcher(
            spool=self.paths.spool,
            inbox=self.paths.inbox,
            on_job=self._handle,
        )
        self._thread: threading.Thread | None = None

    def _handle(self, pdf_path: Path) -> None:
        """Process one captured PDF."""
        info = latest_job()  # empty off Windows, or if the log is disabled
        job = self.pipeline.process(
            pdf_path,
            doc_title=info.document,
            windows_user=info.user,
        )
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
