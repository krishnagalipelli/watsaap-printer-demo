"""Click-to-chat links, for use before the Cloud API account is live.

The operator prints, the app reads the receipt, and a notification offers to
open WhatsApp already in that member's chat with the message typed out. They
attach the PDF and press send.

This is a deliberate stop-gap so the branches can learn the workflow while Meta
approval is pending. Two things it is not, and the UI has to be honest about
both:

* **It does not send.** A wa.me link opens a chat with text prefilled; a person
  still presses send. The app can never know whether they did, so a job that
  went out this way is recorded as handed over, never as sent.
* **It cannot attach the PDF.** wa.me carries text only — there is no parameter
  for a file, by design. The receipt has to be attached by the operator, which
  is why the notification also puts the PDF on the clipboard.

Switching to the API later changes one setting; the capture, extraction and
message wording are identical.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

# WhatsApp's official click-to-chat endpoint. Opens the desktop app when it is
# installed and falls back to web.whatsapp.com otherwise.
BASE = "https://wa.me/"

# WhatsApp truncates very long prefilled text; a receipt message is nowhere
# near this, but a runaway template should be cut rather than silently mangled.
MAX_TEXT = 4000


def chat_url(recipient: str, message: str) -> str:
    """A link that opens `recipient`'s chat with `message` ready to send."""
    number = "".join(ch for ch in recipient if ch.isdigit())
    if not number:
        raise ValueError("a recipient is required to open a chat")
    text = message[:MAX_TEXT]
    return f"{BASE}{number}?text={quote(text, safe='')}"


def open_chat(url: str) -> None:
    """Hand the link to the operating system."""
    if sys.platform == "win32":
        import os

        os.startfile(url)  # noqa: S606 — a https:// URL, opened by the shell
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


def copy_file_to_clipboard(path: Path) -> bool:
    """Put the receipt on the clipboard so the operator can paste it in.

    WhatsApp Desktop accepts a pasted file, which turns attaching into Ctrl+V
    rather than navigating a file dialog. Best effort: if it does not work the
    notification still offers to open the containing folder.
    """
    if sys.platform != "win32":
        return False
    try:
        import struct

        import win32clipboard  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]

        # CF_HDROP wants a DROPFILES header followed by a double-NUL-terminated
        # list of wide-character paths.
        header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
        payload = header + f"{path}\0\0".encode("utf-16-le")
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_HDROP, payload)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        log.info("could not put the receipt on the clipboard", exc_info=True)
        return False


def reveal(path: Path) -> None:
    """Open the folder holding the receipt, with the file selected."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception:
        log.info("could not open the receipt folder", exc_info=True)
