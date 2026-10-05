"""Turn what the XPS printer wrote into the PDF the rest of the app expects.

Windows 7 has no "Microsoft Print To PDF" driver -- it arrived with Windows 10
-- so the queue there is built on the inbox XPS Document Writer instead. The
spooler writes XPS to the port file; everything downstream reads PDFs, and
MuPDF, already here for reading invoices, converts one to the other. See
installer/provision.ps1 for the queue itself.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# .xps is what the Windows 7 (v3) driver writes; .oxps is what the v4 driver on
# Windows 8 and later writes. MuPDF reads both.
SUFFIXES = {".xps", ".oxps"}


def is_xps(path: Path) -> bool:
    return path.suffix.lower() in SUFFIXES


def looks_finished(data: bytes) -> bool:
    """True when a tail of the file carries a ZIP end-of-central-directory.

    An XPS package is a ZIP, and a ZIP is only readable once its central
    directory has been written -- which the spooler does last. This is the
    XPS counterpart of looking for %%EOF in a PDF.
    """
    return b"PK\x05\x06" in data


def to_pdf(path: Path) -> Path:
    """Convert a captured XPS to a PDF beside it and remove the original.

    Raises if it cannot be read, leaving the XPS in place: a print that cannot
    be converted has to stay on disk to be looked at, not disappear.
    """
    import pymupdf

    if not zipfile.is_zipfile(str(path)):
        raise ValueError("{} is not a readable XPS package".format(path.name))

    target = path.with_suffix(".pdf")
    doc = pymupdf.open(str(path))
    try:
        if not doc.page_count:
            raise ValueError("{} has no pages".format(path.name))
        data = doc.convert_to_pdf()
    finally:
        doc.close()

    target.write_bytes(data)
    log.info("converted %s to %s (%d bytes)", path.name, target.name, len(data))
    try:
        path.unlink()
    except OSError:
        log.warning("could not remove %s after converting it", path)
    return target
