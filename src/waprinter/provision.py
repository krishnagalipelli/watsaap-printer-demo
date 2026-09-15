"""Configure an installation from one file, so nothing is typed per machine.

Twenty-odd counters need the same account, the same template and the same
receipts folder. Typing that into each one is slow and gets a digit wrong
somewhere, and the machine that got it wrong looks fine until a receipt does
not arrive.

So the installer can carry a `provision.json` next to it:

    {
      "phone_number_id": "123456789012345",
      "access_token":    "EAAG...",
      "send_mode":       "api",
      "dry_run":         false,
      "business_name":   "Srinidhi Chit Funds",
      "branch_name":     "Karimnagar",
      "pdf_folder":      "D:\\\\Receipts",
      "own_numbers":     ["8782251999"]
    }

Run setup, and the first start of the agent reads it, seals the token with
DPAPI, writes the rest into settings, and deletes the file. Install and leave.

The token is not compiled into the build, and must not be. This repository is
public and the installer is published on GitHub Releases; a token in the binary
is a token on the internet, revoked by Meta's own scanning within days and
usable by anyone until it is. The number it would be used against is the
client's main business line -- the risk this product rejected Baileys to avoid.
A provisioning file keeps the secret with the person doing the installing, on a
USB stick or a share they control, and off the machine again once it is sealed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, paths
from .secrets import save_token, token_problem

log = logging.getLogger(__name__)

FILENAME = "provision.json"
# Where the installer is told to drop it, in the order we look.
ENV_VAR = "WAPRINTER_PROVISION"

# Handled by hand rather than written straight into Settings.
_TOKEN_KEY = "access_token"


@dataclass
class Result:
    source: Path
    applied: list[str] = field(default_factory=list)
    token_stored: bool = False
    # Whether the file is off the machine. Reported rather than assumed: it
    # holds a plaintext token, and being told it was deleted when it was not
    # is how one gets left on a counter.
    removed: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.applied or self.token_stored)

    def summary(self) -> str:
        parts = []
        if self.token_stored:
            parts.append("access token")
        if self.applied:
            parts.append(f"{len(self.applied)} setting(s)")
        if not parts:
            return f"Nothing to apply from {self.source}."
        return f"Applied {' and '.join(parts)} from {self.source}."


def find(explicit: str | Path | None = None) -> Path | None:
    """The provisioning file, if there is one.

    Beside the executable first: that is where the installer puts it, and it is
    the copy the person standing at the machine actually chose.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    for folder in _search_path():
        candidate = folder / FILENAME
        if candidate.is_file():
            return candidate
    return None


def _search_path() -> list[Path]:
    folders: list[Path] = []
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        env_path = Path(from_env).expanduser()
        folders.append(env_path if env_path.is_dir() else env_path.parent)
    # Beside the frozen exe, or beside the source tree when developing.
    folders.append(Path(sys.executable).parent if getattr(sys, "frozen", False)
                   else Path(__file__).resolve().parent.parent.parent)
    folders.append(paths().root)
    return folders


def apply(
    source: Path, settings: Settings | None = None, remove: bool = True
) -> Result:
    """Read `source` into settings and the token store.

    `remove` deletes the file afterwards, which is the point: it holds a
    plaintext access token and has no business staying on twenty counters.
    """
    result = Result(source=source)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Left in place on purpose: whoever is installing has to see it to fix
        # it, and deleting a file we could not even parse may not be ours to
        # delete. It is still a secret sitting on the machine, so say so.
        result.warnings.append(f"Could not read {source}: {exc}")
        result.warnings.append(f"{source} has been left in place. Check it.")
        return result
    if not isinstance(raw, dict):
        result.warnings.append(f"{source} is not a JSON object.")
        result.warnings.append(f"{source} has been left in place. Check it.")
        return result

    settings = settings if settings is not None else Settings.load()

    token = raw.pop(_TOKEN_KEY, None)
    if token is not None:
        problem = token_problem(str(token))
        if problem:
            # Refuse it here rather than let the counter report itself ready
            # and fail every receipt.
            result.warnings.append(f"Access token not stored -- {problem}")
        else:
            try:
                save_token(str(token))
                result.token_stored = True
            except Exception as exc:
                result.warnings.append(f"Access token not stored -- {exc}")

    known = set(Settings.__dataclass_fields__)
    for key, value in raw.items():
        # provision.example.json documents itself in a "_comment" key, and a
        # branch may well leave notes of their own. Not a mistake worth a line.
        if key.startswith("_"):
            continue
        if key not in known:
            result.warnings.append(f"Ignored unknown setting '{key}'.")
            continue
        setattr(settings, key, value)
        result.applied.append(key)

    if result.applied:
        settings.save()

    if remove:
        _remove(source, result)
    return result


def _remove(source: Path, result: Result) -> None:
    """Delete the provisioning file, overwriting the token first.

    Best effort. A file that cannot be deleted is worth saying out loud rather
    than leaving quietly: it still has the token in it.
    """
    try:
        length = source.stat().st_size
        with source.open("r+b") as fh:
            fh.write(b"\0" * length)
            fh.flush()
            os.fsync(fh.fileno())
        source.unlink()
        result.removed = True
    except OSError as exc:
        result.warnings.append(
            f"{source} still holds the access token and could not be "
            f"deleted ({exc}). Remove it by hand."
        )


def apply_if_present(settings: Settings | None = None) -> Result | None:
    """Called at startup. Never raises: a bad file must not stop the printer."""
    try:
        source = find()
        if source is None:
            return None
        result = apply(source, settings)
        log.info("provisioning: %s", result.summary())
        for warning in result.warnings:
            log.warning("provisioning: %s", warning)
        return result
    except Exception:
        log.exception("provisioning failed; carrying on with existing settings")
        return None
