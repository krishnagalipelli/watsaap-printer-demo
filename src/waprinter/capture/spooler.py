"""Recover print job metadata from the Windows spooler.

A Local Port hands us a PDF and nothing else — no document title, no user. That
metadata is useful (the document title becomes the attachment name when there is
no invoice number, and the user shows up in the audit trail), but it is not
required for anything safety-critical, so every failure here degrades to None.

The source is the PrintService operational log, event 307, which Windows writes
once a job finishes rendering. That log is off by default; provision.ps1 enables
it. If it is unavailable, capture still works — jobs just show no title.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

PRINTER_NAME = "WhatsApp Printer"
LOG_CHANNEL = "Microsoft-Windows-PrintService/Operational"
JOB_PRINTED_EVENT_ID = 307

# How far back to look when matching a captured PDF to a spooler event.
MATCH_WINDOW = timedelta(seconds=30)


@dataclass
class SpoolJobInfo:
    document: str | None = None
    user: str | None = None
    printed_at: datetime | None = None


def recent_jobs(within: timedelta = MATCH_WINDOW) -> list[SpoolJobInfo]:
    """Jobs the spooler reported finishing on our printer recently."""
    if sys.platform != "win32":
        return []

    try:
        import win32evtlog  # type: ignore[import-not-found]
    except ImportError:
        log.debug("pywin32 unavailable; no spooler metadata")
        return []

    cutoff_ms = int(within.total_seconds() * 1000)
    query = (
        f"*[System[(EventID={JOB_PRINTED_EVENT_ID}) and "
        f"TimeCreated[timediff(@SystemTime) <= {cutoff_ms}]]]"
    )

    try:
        handle = win32evtlog.EvtQuery(
            LOG_CHANNEL,
            win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
            query,
        )
    except Exception:
        # Channel disabled or access denied — metadata is optional.
        log.debug("could not query %s", LOG_CHANNEL, exc_info=True)
        return []

    jobs: list[SpoolJobInfo] = []
    while True:
        try:
            events = win32evtlog.EvtNext(handle, 10)
        except Exception:
            break
        if not events:
            break
        for event in events:
            try:
                xml = win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml)
            except Exception:
                continue
            info = _parse_event(xml)
            if info and info.document is not None:
                jobs.append(info)

    return jobs


def _parse_event(xml: str) -> SpoolJobInfo | None:
    """Pull document/user/printer out of an event 307 XML payload."""
    if PRINTER_NAME.lower() not in xml.lower():
        return None

    def field(name: str) -> str | None:
        m = re.search(
            rf"<Data Name=['\"]{name}['\"]>(.*?)</Data>", xml, re.IGNORECASE | re.DOTALL
        )
        return m.group(1).strip() if m else None

    timestamp = None
    m = re.search(r"SystemTime=['\"]([^'\"]+)['\"]", xml)
    if m:
        try:
            timestamp = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            timestamp = None

    return SpoolJobInfo(
        document=field("Param1") or field("DocumentName"),
        user=field("Param3") or field("User"),
        printed_at=timestamp,
    )


def latest_job() -> SpoolJobInfo:
    """Best guess at the job we just captured. Empty when unavailable."""
    jobs = recent_jobs()
    return jobs[0] if jobs else SpoolJobInfo()
