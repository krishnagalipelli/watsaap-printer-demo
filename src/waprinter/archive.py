"""Keep a copy of every receipt that goes through the printer.

The office needs its own record of what it printed, and until now there was
none worth having. Captured PDFs were left in `inbox` under the name the
capture gave them -- `20260907-113601-fee7f6b4.pdf` -- flat, forever, inside
ProgramData, which is not a folder anyone browses and not a name anyone can
search. The `archive` folder was created at startup and never written to.

So each processed receipt is copied out into a folder the operator chooses,
filed by the date it was printed and named after the receipt itself:

    D:\\Receipts\\2026-09-07\\CHQ6511-26 SHAHNAVAZDANISH MOHAMMAD.pdf

A copy rather than a move, deliberately. The file under `inbox` is what the
queue reopens, what "View PDF" shows and what a held receipt is eventually
sent from; if that pointed at a network share or a USB stick, sending would
break the moment it was unplugged. Filing is also wrapped so it can never be
the reason a print fails -- a receipt that reached the customer but not the
folder is a nuisance, the other way round is a lost receipt.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from .config import Paths, Settings, data_root
from .models import PrintJob

log = logging.getLogger(__name__)

# Characters Windows will not accept in a file name, plus the control range.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows resolves these to devices whatever the extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{n}" for n in range(1, 10)),
    *(f"lpt{n}" for n in range(1, 10)),
}
# Long enough for a receipt number and a member's full name, short enough to
# stay clear of MAX_PATH once a dated folder is in front of it.
_MAX_STEM = 90


def folder_for(settings: Settings) -> Path:
    """Where printed receipts are kept. The chosen folder, or the default."""
    chosen = (settings.pdf_folder or "").strip()
    if chosen:
        return Path(chosen).expanduser()
    return Paths(data_root()).archive


def file_away(job: PrintJob, settings: Settings) -> Path | None:
    """Copy one processed receipt into the operator's folder.

    Returns where it landed, or None if it was turned off, the source had
    already gone, or the copy failed. Never raises.
    """
    if not settings.keep_printed_pdfs:
        return None
    try:
        source = job.pdf_path
        if not source.exists():
            log.warning("nothing to file for job %s: %s is gone", job.id, source)
            return None

        day = job.created_at.strftime("%Y-%m-%d")
        destination = _free_name(folder_for(settings) / day, _stem_for(job))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination
    except Exception:
        # Worth a line in the log and nothing more. The receipt itself is
        # already captured, recorded and on its way.
        log.exception("could not file the printed copy of job %s", job.id)
        return None


def _stem_for(job: PrintJob) -> str:
    """What to call the file, from the operator's point of view.

    The receipt number is what they are given over the counter and what they
    ring up about, so it leads. The member's name is what makes a folder full
    of them skimmable.
    """
    parts = [job.fields.invoice_number, job.fields.customer_name]
    stem = " ".join(_clean(p) for p in parts if _clean(p))
    if not stem:
        # Nothing was read off the page -- a held scan, or a layout we do not
        # know yet. Time and job id at least make it unique and orderable.
        stem = f"{job.created_at.strftime('%H%M%S')} {job.id}"
    return stem[:_MAX_STEM].strip(" .")


def _clean(value: str | None) -> str:
    if not value:
        return ""
    cleaned = _ILLEGAL.sub("-", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _free_name(folder: Path, stem: str) -> Path:
    """`folder/stem.pdf`, or the next free numbering of it.

    A receipt reprinted later the same day is a real event and both copies are
    worth keeping, so the second one is not allowed to overwrite the first.
    """
    candidate = folder / f"{stem}.pdf"
    if not candidate.exists():
        return candidate
    for n in range(2, 1000):
        candidate = folder / f"{stem} ({n}).pdf"
        if not candidate.exists():
            return candidate
    return folder / f"{stem} ({job_suffix()}).pdf"


def job_suffix() -> str:
    from uuid import uuid4

    return uuid4().hex[:8]
