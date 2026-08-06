"""Access-token storage.

On Windows the token is sealed with DPAPI under the machine scope, so it is
readable by the service account but not by copying the file to another PC. On a
dev machine there is no DPAPI, so it falls back to a plain file with an obvious
name — never use that fallback for a real token.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

from .config import paths

_TOKEN_FILE = "whatsapp_token.bin"
_DEV_TOKEN_FILE = "whatsapp_token.INSECURE_DEV_ONLY"


def _win32_protect(data: bytes) -> bytes:
    import win32crypt  # type: ignore[import-not-found]

    return win32crypt.CryptProtectData(data, "waprinter", None, None, None, 0x4)


def _win32_unprotect(data: bytes) -> bytes:
    import win32crypt  # type: ignore[import-not-found]

    return win32crypt.CryptUnprotectData(data, None, None, None, 0x4)[1]


def save_token(token: str) -> None:
    root = paths().root
    root.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        (root / _TOKEN_FILE).write_bytes(_win32_protect(token.encode("utf-8")))
    else:
        (root / _DEV_TOKEN_FILE).write_bytes(base64.b64encode(token.encode("utf-8")))


def load_token() -> str | None:
    root = paths().root
    if sys.platform == "win32":
        blob = root / _TOKEN_FILE
        if not blob.exists():
            return None
        return _win32_unprotect(blob.read_bytes()).decode("utf-8")

    blob = root / _DEV_TOKEN_FILE
    if not blob.exists():
        return None
    return base64.b64decode(blob.read_bytes()).decode("utf-8")


def clear_token() -> None:
    root = paths().root
    for name in (_TOKEN_FILE, _DEV_TOKEN_FILE):
        target = root / name
        if target.exists():
            target.unlink()
