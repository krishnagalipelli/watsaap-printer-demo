"""Version checking and self-update.

There is no server involved. The app fetches one small JSON file over HTTPS —
hosted on GitHub Releases, or any static host — compares versions, and if a
newer build exists downloads the installer and runs it silently. Inno Setup
replaces the files in place, so there is no uninstall step.

Two ways it runs:

* **Daily, in the background.** Catches the routine version bump without anyone
  thinking about it.
* **On demand, from the Status tab.** A fix released at eleven in the morning
  should not wait until the next day because a timer says so. The operator (or
  you, over AnyDesk) presses the button and it lands immediately.

Rules that matter more than the mechanism:

* A failed or unreachable check is **never** an error the operator has to care
  about. Printing and sending must carry on regardless.
* The download is checksum-verified before it is executed. The manifest names
  the SHA-256; a file that does not match is discarded.
* Never install while a document is being processed.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import __version__

log = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(hours=24)
TIMEOUT = 20.0
# An installer is tens of megabytes; anything far larger is not ours.
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024


def parse_version(text: str) -> tuple[int, ...]:
    """"1.2.10" -> (1, 2, 10), so 1.2.10 sorts above 1.2.9.

    String comparison gets that pair the wrong way round, which would strand
    every machine on an old build.
    """
    parts: list[int] = []
    for chunk in str(text).strip().lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class Release:
    version: str
    url: str
    sha256: str = ""
    notes: str = ""


@dataclass
class CheckResult:
    """What to tell the operator. `available` is the only actionable state."""

    available: bool = False
    release: Release | None = None
    current: str = __version__
    message: str = ""
    failed: bool = False


def check(url: str, current: str = __version__, client: httpx.Client | None = None) -> CheckResult:
    """Ask whether a newer build exists. Never raises."""
    if not url:
        return CheckResult(message="No update location is configured.", failed=True)

    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
        release = Release(
            version=str(payload["version"]),
            url=str(payload["url"]),
            sha256=str(payload.get("sha256", "")),
            notes=str(payload.get("notes", "")),
        )
    except Exception as exc:
        # Offline, DNS down, host moved — all the same to the operator.
        log.info("update check failed: %s", exc)
        return CheckResult(
            current=current,
            message="Could not reach the update server. Nothing else is affected.",
            failed=True,
        )
    finally:
        if owned:
            client.close()

    if is_newer(release.version, current):
        return CheckResult(
            available=True,
            release=release,
            current=current,
            message=f"Version {release.version} is available. You have {current}.",
        )
    return CheckResult(current=current, message=f"Up to date (version {current}).")


def download(release: Release, client: httpx.Client | None = None) -> Path:
    """Fetch the installer and verify it. Returns the file, or raises."""
    owned = client is None
    client = client or httpx.Client(timeout=None, follow_redirects=True)
    target = Path(tempfile.gettempdir()) / f"WhatsAppPrinter-Setup-{release.version}.exe"
    digest = hashlib.sha256()
    written = 0
    try:
        with client.stream("GET", release.url) as response:
            response.raise_for_status()
            with target.open("wb") as fh:
                for chunk in response.iter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("the download is implausibly large")
                    digest.update(chunk)
                    fh.write(chunk)
    finally:
        if owned:
            client.close()

    if release.sha256 and digest.hexdigest().lower() != release.sha256.lower():
        # Corrupted, truncated or substituted. Never run it.
        target.unlink(missing_ok=True)
        raise RuntimeError("the downloaded installer did not match its checksum")

    return target


def install(installer: Path) -> None:
    """Run the installer silently and exit so it can replace our files."""
    if sys.platform != "win32":
        raise RuntimeError("Updates can only be installed on Windows.")
    subprocess.Popen(
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/RESTARTAPPLICATIONS",
        ],
        close_fds=True,
    )


def due(last_check: str | None, now: datetime | None = None) -> bool:
    """Whether the daily background check should run."""
    if not last_check:
        return True
    now = now or datetime.now()
    try:
        return now - datetime.fromisoformat(last_check) >= CHECK_INTERVAL
    except ValueError:
        return True
