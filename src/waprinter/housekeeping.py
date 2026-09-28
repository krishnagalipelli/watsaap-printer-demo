"""Keep the data folder from growing without bound.

Three things accumulated forever, on machines nobody administers:

* ``inbox`` -- every PDF that ever came off the printer, under the name
  capture gave it. The office's own copies live in the archive folder; these
  are the working files the queue reopens and a held job is sent from.
* ``logs/dry_run.jsonl`` -- one line per would-be send, in test mode.
* ``logs/crash.txt`` -- appended to on every startup failure.

Run once a day by the agent. Nothing here can touch a job that is still
waiting on a person: those PDFs are what "Send" will upload, so they are kept
whatever their age.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .config import Paths, Settings

log = logging.getLogger(__name__)

DRY_RUN_LOG_MAX_BYTES = 5_000_000
CRASH_LOG_MAX_BYTES = 1_000_000


def run(paths: Paths, settings: Settings, store, now: float | None = None) -> int:
    """Do the day's tidying. Returns how many inbox files were removed."""
    protected = store.unresolved_pdf_paths()
    removed = prune_inbox(paths.inbox, settings.keep_inbox_days, protected, now=now)
    rotate(paths.logs / "dry_run.jsonl", DRY_RUN_LOG_MAX_BYTES)
    truncate_head(paths.logs / "crash.txt", CRASH_LOG_MAX_BYTES)
    return removed


def prune_inbox(
    inbox: Path, keep_days: int, protected: set[Path], now: float | None = None
) -> int:
    """Delete captured PDFs older than `keep_days`, except the protected ones.

    Zero or a negative number keeps everything, which is what an install
    that has not said otherwise gets from the setting's default until someone
    changes it.
    """
    if keep_days <= 0 or not inbox.is_dir():
        return 0
    now = time.time() if now is None else now
    cutoff = now - keep_days * 86_400
    removed = 0
    for pdf in inbox.glob("*.pdf"):
        if pdf in protected:
            continue
        try:
            if pdf.stat().st_mtime >= cutoff:
                continue
            pdf.unlink()
            removed += 1
        except OSError:
            log.info("could not remove %s", pdf, exc_info=True)
    if removed:
        log.info("removed %d captured PDF(s) older than %d days", removed, keep_days)
    return removed


def rotate(path: Path, max_bytes: int) -> bool:
    """Move `path` aside as `path.1` once it passes `max_bytes`. One generation."""
    try:
        if not path.is_file() or path.stat().st_size <= max_bytes:
            return False
        os.replace(path, path.with_name(path.name + ".1"))
        return True
    except OSError:
        log.info("could not rotate %s", path, exc_info=True)
        return False


def truncate_head(path: Path, max_bytes: int) -> bool:
    """Keep only the newest `max_bytes` of an append-only text file."""
    try:
        if not path.is_file() or path.stat().st_size <= max_bytes:
            return False
        data = path.read_bytes()[-max_bytes:]
        # Start on a whole line, so the first report is not cut mid-way.
        newline = data.find(b"\n")
        if newline != -1:
            data = data[newline + 1 :]
        path.write_bytes(data)
        return True
    except OSError:
        log.info("could not truncate %s", path, exc_info=True)
        return False
