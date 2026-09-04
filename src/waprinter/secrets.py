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

# Meta's tokens are long -- a system user token runs past 200 characters. This
# floor exists to catch a token that was never really entered, not to police
# the format.
_PLAUSIBLE_TOKEN_CHARS = 30


def token_problem(token: str | None) -> str | None:
    """Why this token cannot work, or None if it looks usable.

    A client spent a day on failing sends because Ctrl+V in a Windows Command
    Prompt does not paste -- it types a literal \x16 -- and getpass took that
    single control character as the whole token. It survived .strip(), because
    \x16 is not whitespace, and every later check only asked whether a token
    was present. Meta then rejected the header in a way that surfaced as a
    dropped connection, which looks like a network fault and is not one.
    """
    if not token:
        return "No access token is stored."
    odd = sum(1 for c in token if not (32 <= ord(c) < 127))
    if odd:
        return (
            f"The stored access token contains {odd} character(s) that cannot "
            f"appear in a token. Ctrl+V does not paste in a Command Prompt -- "
            f"it types a control character. Run `waprinter set-token` again "
            f"and paste with a right-click."
        )
    if len(token) < _PLAUSIBLE_TOKEN_CHARS:
        return (
            f"The stored access token is only {len(token)} characters. Meta's "
            f"tokens are far longer, so this one was probably not pasted in "
            f"full."
        )
    return None


def _win32_protect(data: bytes) -> bytes:
    import win32crypt  # type: ignore[import-not-found]

    return win32crypt.CryptProtectData(data, "waprinter", None, None, None, 0x4)


def _win32_unprotect(data: bytes) -> bytes:
    import win32crypt  # type: ignore[import-not-found]

    return win32crypt.CryptUnprotectData(data, None, None, None, 0x4)[1]


def save_token(token: str) -> None:
    """Store the token. Refuses one that cannot possibly work.

    Refusing here rather than at send time is deliberate: the alternative is a
    counter that reports itself ready and silently fails every receipt.
    """
    problem = token_problem(token)
    if problem:
        raise ValueError(problem)
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
