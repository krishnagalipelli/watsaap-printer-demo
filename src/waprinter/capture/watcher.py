"""Watch the printer port's output folder and hand finished PDFs to the pipeline.

The "WhatsApp Printer" queue uses an inbox Windows driver bound to a Local Port
whose name is a file path, so Windows writes the job straight to disk with no
Save-As dialog and no third-party driver. Which driver depends on the version:
Print-to-PDF on Windows 10 and later, and the XPS Document Writer on Windows 7,
which has no PDF driver. An XPS job is converted on the way in -- see xps.py --
so everything past this module reads PDFs either way. See installer/provision.ps1.

Two consequences shape this module:

* **The filename is fixed per port.** A port writes to the same path every time,
  so a job must be moved out before the next one starts. The installer creates
  several ports (job1.pdf … job4.pdf) and this watcher drains all of them.
* **The file appears before it is finished.** A PDF is claimed only once its
  size has stopped changing *and* it ends with %%EOF — a half-written spool file
  would otherwise parse as a blank page and be silently held.

Polling rather than filesystem events: the spool folder holds at most a handful
of files, a 500 ms poll is imperceptible next to the time Windows takes to
render a print job, and polling does not miss events on redirected or network
profiles the way change notifications can.
"""

from __future__ import annotations

import logging
import os
import time
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from . import xps

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.5
# How long a file's size must hold steady before it counts as finished.
SETTLE_SECONDS = 1.0
EOF_MARKER = b"%%EOF"
EOF_TAIL_BYTES = 2048
# What a port may write. The driver decides which: PDF on Windows 10 and later,
# XPS on Windows 7.
CAPTURED_SUFFIXES = {".pdf"} | xps.SUFFIXES


def is_complete(path: Path) -> bool:
    """True when the spooler has finished writing this job.

    The end marker is per format, and getting it wrong is not harmless: an XPS
    checked for %%EOF would never look finished and the print would sit in the
    spool folder for ever.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False

    try:
        with path.open("rb") as fh:
            fh.seek(max(0, size - EOF_TAIL_BYTES))
            tail = fh.read()
    except OSError:
        # Still locked by the spooler.
        return False

    if xps.is_xps(path):
        return xps.looks_finished(tail)
    return EOF_MARKER in tail


def claim(path: Path, inbox: Path) -> Path | None:
    """Move a finished spool file into the inbox under a unique name.

    The rename doubles as the readiness test: on Windows it fails with a sharing
    violation while the spooler still holds the handle, so a file that moves is
    a file that is done. Returns the new path, or None if it was not ready.
    """
    if not is_complete(path):
        return None

    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / (f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
                      f"{path.suffix.lower()}")
    try:
        os.replace(path, target)
    except OSError as exc:
        log.debug("could not claim %s yet: %s", path, exc)
        return None
    return target


class SpoolWatcher:
    """Drains the spool folder, calling `on_job` for each captured PDF."""

    def __init__(
        self,
        spool: Path,
        inbox: Path,
        on_job: Callable[[Path], None],
        poll_interval: float = POLL_INTERVAL,
        settle_seconds: float = SETTLE_SECONDS,
        operation_lock=None,
    ):
        self.spool = spool
        self.inbox = inbox
        self.on_job = on_job
        self.poll_interval = poll_interval
        self.settle_seconds = settle_seconds
        self._sizes: dict[Path, tuple[int, float]] = {}
        self._running = False
        self.operation_lock = operation_lock or threading.RLock()

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        """Loop until stop() is called. Intended to run on its own thread."""
        self._running = True
        self.spool.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        log.info("watching %s", self.spool)

        while self._running:
            try:
                self.drain_once()
            except Exception:
                # A bad job must never take the service down; the next poll
                # continues with whatever else is waiting.
                log.exception("spool poll failed")
            time.sleep(self.poll_interval)

    def drain_once(self) -> list[Path]:
        # The updater shares this lock. Acquire before claiming a spool file,
        # so an installation cannot strand a claimed, not-yet-recorded PDF.
        with self.operation_lock:
            return self._drain_once()

    def _drain_once(self) -> list[Path]:
        """One pass over the spool folder. Returns the jobs handed off."""
        captured: list[Path] = []
        now = time.monotonic()

        for path in sorted(p for p in self.spool.glob("*")
                           if p.suffix.lower() in CAPTURED_SUFFIXES):
            try:
                size = path.stat().st_size
            except OSError:
                self._sizes.pop(path, None)
                continue

            previous = self._sizes.get(path)
            if previous is None or previous[0] != size:
                # Size changed (or first sighting) — restart the settle timer.
                self._sizes[path] = (size, now)
                continue

            if now - previous[1] < self.settle_seconds:
                continue

            claimed = claim(path, self.inbox)
            if claimed is None:
                continue

            self._sizes.pop(path, None)
            if xps.is_xps(claimed):
                try:
                    claimed = xps.to_pdf(claimed)
                except Exception:
                    # Leave it in the inbox to be looked at. A print that
                    # cannot be converted must not vanish without trace.
                    log.exception("could not convert %s", claimed)
                    continue
            captured.append(claimed)
            log.info("captured %s", claimed.name)
            try:
                self.on_job(claimed)
            except Exception:
                log.exception("pipeline failed for %s", claimed)

        # Forget files that have gone away.
        for known in list(self._sizes):
            if not known.exists():
                self._sizes.pop(known, None)

        return captured
