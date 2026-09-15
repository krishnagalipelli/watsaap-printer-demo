"""Split one print job into the receipts it actually contains.

A chit fund office prints a run of receipts as a single job: the software sends
one document to the printer and Windows writes one PDF, with a different
subscriber on every page. Treated as one document that is one receipt, the
whole run holds — two subscribers means two numbers scoring above the send
threshold, and the gate refuses to guess between them.

Worse than the hold is the obvious way round it. The send path uploads the
job's PDF whole, so an operator who picks one of the offered numbers sends that
subscriber a document containing everyone else's name, mobile and amount paid.

So the split happens before anything else looks at the page.

The boundary is a change of document number, not a page break. A receipt that
runs onto a second page keeps that page, because the continuation carries no
number of its own; a subscriber copy followed by an office copy of the same
receipt stays one job, because the number is the same on both. Only a page
that names a *different* document starts a new one.
"""

from __future__ import annotations

from pathlib import Path

import fitz

from .fields import _document_number
from .pdf_text import Document
from .profile import DocumentProfile


def receipt_groups(doc: Document, profile: DocumentProfile) -> list[list[int]]:
    """Page numbers grouped into one list per receipt, in page order.

    Always returns at least one group for a document with pages, so callers
    can treat the ordinary single-receipt job as the one-group case rather
    than special-casing it.
    """
    groups: list[list[int]] = []
    current: str | None = None

    for page in doc.pages:
        number = _page_document_number(doc, page.number, profile)
        # A page with no number of its own is a continuation, whatever it
        # looks like. Only a page naming a different document breaks the run.
        starts_new = number is not None and current is not None and number != current

        if not groups or starts_new:
            groups.append([page.number])
            current = number
        else:
            groups[-1].append(page.number)
            if current is None:
                current = number

    return groups


def _page_document_number(
    doc: Document, page_number: int, profile: DocumentProfile
) -> str | None:
    page = doc.page(page_number)
    if page is None:
        return None
    # _document_number reads whole documents; give it one holding this page.
    single = Document(
        pages=[page],
        has_text_layer=doc.has_text_layer,
        ocr_pages={page_number} if page_number in doc.ocr_pages else set(),
    )
    return _document_number(single, profile)


def write_segment(source: Path, pages: list[int], destination: Path) -> Path:
    """Write `pages` of `source` to `destination` as a PDF of its own.

    Page numbers are 1-based, matching Page.number.
    """
    with fitz.open(source) as src:
        out = fitz.open()
        try:
            for number in pages:
                out.insert_pdf(src, from_page=number - 1, to_page=number - 1)
            out.save(destination)
        finally:
            out.close()
    return destination


def segment_path(source: Path, index: int) -> Path:
    """Where the nth receipt of a multi-receipt job is written.

    Beside the original, which is left intact: it is what actually came off
    the printer, and the audit trail should keep saying so.
    """
    return source.with_name(f"{source.stem}-r{index}{source.suffix}")
