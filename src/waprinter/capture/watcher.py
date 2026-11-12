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

Freeing the port and dealing with the document are two separate jobs, on two
threads. They used to be one: the thread that moved a finished file out of the
spool then read it, ran OCR over it and uploaded it before it looked at the
spool again. A send on a poor connection takes a minute or more, and the port
writes every print to the same filename, so the second receipt printed in that
minute waited in the spool and the third replaced it. Nothing recorded that a
print had been lost, because nothing had ever seen it.

So a finished file is first moved into a staging folder, which takes
milliseconds and waits for nothing, and a second thread works through that
folder in the order the prints were made. The staging folder is on disk rather
than in memory on purpose: whatever is in it when the app stops -- a crash, a
power cut, an update -- is still there, and is processed, when it next starts.
"""

from __future__ import annotations

import logging
import os
import re
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
# What the printer's ports are called: job1.pdf … job4.pdf, or .xps on Windows
# 7. Anything else in the spool folder was saved there by a person -- on
# Windows 7 that is a supported way to send, from billing software that exports
# PDFs -- and its name is the only title it has.
PORT_STEM = re.compile(r"job\d+", re.IGNORECASE)
# Separates the unique part of a claimed name from the name the file was saved
# under. Never produced by the timestamp-and-hex prefix, so the first one found
# is always this one.
SAVED_NAME_MARK = "~"
SAVED_NAME_LIMIT = 80
# Where finished prints wait to be processed, beside the inbox.
STAGING_FOLDER = "claimed"
# How often the processing thread looks at the staging folder unprompted.
STAGED_RECHECK_SECONDS = 2.0


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
    """Move a finished spool file into `inbox` under a unique name.

    The rename doubles as the readiness test: on Windows it fails with a sharing
    violation while the spooler still holds the handle, so a file that moves is
    a file that is done. Returns the new path, or None if it was not ready.
    """
    if not is_complete(path):
        return None

    inbox.mkdir(parents=True, exist_ok=True)
    unique = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    if not PORT_STEM.fullmatch(path.stem):
        # Saved, not printed: carry the name along, since no print job will
        # say what this document was. Without it the title came from whatever
        # had last been printed, and that became the attachment's name.
        unique += SAVED_NAME_MARK + path.stem[:SAVED_NAME_LIMIT]
    target = inbox / f"{unique}{path.suffix.lower()}"
    try:
        os.replace(path, target)
    except OSError as exc:
        log.debug("could not claim %s yet: %s", path, exc)
        return None
    return target


def saved_name(path: Path) -> str | None:
    """The name a person saved this file under, or None for a print.

    Works on the claimed file at every later step -- staged, in the inbox,
    converted from XPS -- because each keeps the stem.
    """
    _, mark, name = path.stem.partition(SAVED_NAME_MARK)
    if not mark:
        return None
    return name.strip() or None


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
        on_unreadable: Callable[[Path, str], None] | None = None,
        staging: Path | None = None,
    ):
        self.spool = spool
        self.inbox = inbox
        # Where a finished print waits its turn. Beside the inbox, so the move
        # out of it is a rename on the same volume.
        self.staging = staging or inbox.parent / STAGING_FOLDER
        self.on_job = on_job
        # Called with the claimed file and the reason when a print cannot be
        # turned into a PDF, so it is recorded rather than only logged.
        self.on_unreadable = on_unreadable
        self.poll_interval = poll_interval
        self.settle_seconds = settle_seconds
        self._sizes: dict[Path, tuple[int, float]] = {}
        self._running = False
        self.operation_lock = operation_lock or threading.RLock()
        # Set when something new is staged, so it is picked up at once rather
        # than on the processing thread's next look.
        self._staged = threading.Event()

    def stop(self) -> None:
        self._running = False
        self._staged.set()

    def run(self) -> None:
        """Loop until stop() is called. Intended to run on its own thread.

        This thread only ever frees the port. Documents are dealt with on a
        second thread, started here, so nothing that takes time -- OCR, an
        upload, an update being installed -- can leave a finished print in
        the spool to be overwritten by the next one.
        """
        self._running = True
        self.spool.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        log.info("watching %s", self.spool)

        threading.Thread(
            target=self._process_loop, name="documents", daemon=True
        ).start()

        while self._running:
            try:
                if self.claim_finished():
                    self._staged.set()
            except Exception:
                # A bad job must never take the service down; the next poll
                # continues with whatever else is waiting.
                log.exception("spool poll failed")
            time.sleep(self.poll_interval)

    def _process_loop(self) -> None:
        while self._running:
            # Cleared before the look, not after it: a print staged while
            # this pass is busy then wakes the next one straight away.
            self._staged.clear()
            try:
                self.process_staged()
            except Exception:
                log.exception("could not process the staged prints")
            # The timeout is for what no claim announces: prints left from
            # the last run, and a file that could not be moved a moment ago.
            self._staged.wait(STAGED_RECHECK_SECONDS)

    def drain_once(self) -> list[Path]:
        """Free the ports and deal with what they held, in one call.

        For anything that drives the watcher by hand. Both halves run under
        the lock the updater shares, so nothing here starts while an
        installation is under way.
        """
        with self.operation_lock:
            self.claim_finished()
            return self.process_staged()

    def process_staged(self) -> list[Path]:
        """Hand every staged print to the pipeline, oldest first.

        The lock is taken for each document rather than for the pass, so an
        update or a settings change can get in between two receipts of a long
        run instead of waiting for all of it.
        """
        captured: list[Path] = []
        waiting = self._staged_files()
        if waiting:
            self.inbox.mkdir(parents=True, exist_ok=True)
        for staged in waiting:
            with self.operation_lock:
                claimed = self.inbox / staged.name
                try:
                    os.replace(staged, claimed)
                except OSError as exc:
                    # Held open by a scanner, or already taken. Still staged,
                    # so the next pass tries again.
                    log.debug("could not take %s yet: %s", staged, exc)
                    continue
                if xps.is_xps(claimed):
                    try:
                        claimed = xps.to_pdf(claimed)
                    except Exception as exc:
                        # Leave it in the inbox to be looked at, and say so
                        # where the operator looks. A log line alone was the
                        # only trace, and nothing on any screen said a print
                        # had been lost.
                        log.exception("could not convert %s", claimed)
                        if self.on_unreadable is not None:
                            try:
                                self.on_unreadable(claimed, str(exc))
                            except Exception:
                                log.exception("could not record %s", claimed)
                        continue
                captured.append(claimed)
                log.info("captured %s", claimed.name)
                try:
                    self.on_job(claimed)
                except Exception:
                    log.exception("pipeline failed for %s", claimed)
        return captured

    def _staged_files(self) -> list[Path]:
        """What is waiting, in the order it was printed."""
        try:
            waiting = [p for p in self.staging.iterdir()
                       if p.suffix.lower() in CAPTURED_SUFFIXES]
        except OSError:
            return []  # nothing has been staged yet

        def printed(path: Path) -> tuple[float, str]:
            # A rename keeps the time the spooler finished writing, which is
            # finer than the second in the name.
            try:
                return (path.stat().st_mtime, path.name)
            except OSError:
                return (0.0, path.name)

        return sorted(waiting, key=printed)

    def claim_finished(self) -> list[Path]:
        """One pass over the spool folder. Returns what was moved to staging.

        Takes no lock and waits for nothing, which is the point of it.
        """
        staged: list[Path] = []
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

            claimed = claim(path, self.staging)
            if claimed is None:
                continue

            self._sizes.pop(path, None)
            staged.append(claimed)

        # Forget files that have gone away.
        for known in list(self._sizes):
            if not known.exists():
                self._sizes.pop(known, None)

        return staged
