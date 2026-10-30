"""Per-computer licences, checked offline.

What was sold is a one-time licence per device, with updates covered by the
AMC. So a licence here never stops a counter that has one: it names one
computer, and the date until which that computer takes updates. Renewing the
AMC is issuing the same counter a new file with a later date.

How a counter gets one:

* The app shows this computer's machine code (Setup → Licence, or
  `waprinter licence`).
* On the issuing machine, `tools/issue_licence.py issue` signs a licence for
  that code with the private key, which never leaves that machine.
* The engineer imports the file, or pastes its last line, here.

There is no server. The licence is checked against the public key below, so a
branch with no internet keeps sending, and nothing anyone types into a file in
the data folder can make a licence the key did not sign.

Without a licence the app runs for a trial. When that ends, prints are still
captured, read and kept -- nothing printed is ever lost -- but nothing is sent
until a licence is imported. Test mode is not held: it sends nothing anyway.

Ed25519 is implemented here in plain Python (RFC 8032) rather than taken from
the `cryptography` package. The Windows 7 counters run a frozen 32-bit Python
3.8, and one signature check per change of file costs nothing worth a native
dependency there.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import atomic_write_text, paths

log = logging.getLogger(__name__)

# The issuing key's public half. tools/issue_licence.py keygen prints it.
PUBLIC_KEY = "5b45621fb377b9ec52a264e8b35e4ac149ce3d988f7b550e91e69f7a90717550"

TRIAL_DAYS = 14
TOKEN_PREFIX = "WAP1."
_SIGNED_PREFIX = b"waprinter-licence-v1\n"
_LICENCE_FILE = "licence.txt"
_TRIAL_FILE = "trial-started.txt"


class LicenceError(ValueError):
    """A licence that cannot be used here, with the reason in plain words."""


# -- Ed25519 (RFC 8032) -----------------------------------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    a_ = (y1 - x1) * (y2 - x2) % _P
    b_ = (y1 + x1) * (y2 + x2) % _P
    c_ = 2 * t1 * t2 * _D % _P
    d_ = 2 * z1 * z2 % _P
    e, f, g, h = b_ - a_, d_ - c_, d_ + c_, b_ + a_
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same_point(a, b) -> bool:
    x1, y1, z1, _ = a
    x2, y2, z2, _ = b
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(point) -> bytes:
    x, y, z, _ = point
    zi = pow(z, _P - 2, _P)
    x, y = x * zi % _P, y * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(raw: bytes):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _hash_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _expand(secret: bytes):
    digest = hashlib.sha512(secret).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key_of(secret: bytes) -> bytes:
    scalar, _prefix = _expand(secret)
    return _compress(_mul(scalar, _G))


def sign(secret: bytes, message: bytes) -> bytes:
    scalar, prefix = _expand(secret)
    public = _compress(_mul(scalar, _G))
    r = _hash_int(prefix + message) % _L
    encoded_r = _compress(_mul(r, _G))
    h = _hash_int(encoded_r + public + message) % _L
    return encoded_r + ((r + h * scalar) % _L).to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = _hash_int(signature[:32] + public + message) % _L
    return _same_point(_mul(s, _G), _add(r, _mul(h, a)))


# -- this computer -------------------------------------------------------------------


def _machine_guid() -> str:
    """What identifies this Windows installation.

    MachineGuid is written when Windows is installed and survives everything
    short of reinstalling it -- which, like a replacement PC, is a new
    licence under the AMC. Read from the 64-bit view of the registry: the
    Windows 7 build is a 32-bit process, and on a 64-bit Windows the
    redirected view would answer for a different key.
    """
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0])
    # Dev machines only: stable enough to try the flow, not to sell against.
    return f"dev-{uuid.getnode():012x}"


def format_code(raw: str) -> str:
    """A machine code as typed or pasted, in the form it is shown in."""
    letters = "".join(c for c in str(raw).upper() if c.isalnum())
    return "-".join(letters[i:i + 4] for i in range(0, len(letters), 4))


def machine_code() -> str:
    """This computer's code, short enough to read out over the phone."""
    digest = hashlib.sha256(f"waprinter-machine:{_machine_guid()}".encode("utf-8")).digest()
    return format_code(base64.b32encode(digest).decode("ascii")[:20])


# -- the licence file ---------------------------------------------------------------


@dataclass(frozen=True)
class Licence:
    licensee: str
    machine: str
    issued: date
    updates_until: date
    serial: str

    def payload(self) -> bytes:
        return json.dumps(
            {
                "licensee": self.licensee,
                "machine": self.machine,
                "issued": self.issued.isoformat(),
                "updates_until": self.updates_until.isoformat(),
                "serial": self.serial,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(licence: Licence, secret: bytes) -> str:
    """The signed token for `licence`. Only the issuing tool calls this."""
    payload = licence.payload()
    return TOKEN_PREFIX + _b64(payload) + "." + _b64(sign(secret, _SIGNED_PREFIX + payload))


def describe(licence: Licence, token: str) -> str:
    """The whole file the issuing tool writes: readable lines, then the token."""
    return (
        "WhatsApp Printer licence\n"
        f"Licensed to:   {licence.licensee}\n"
        f"Machine code:  {licence.machine}\n"
        f"Issued:        {licence.issued:%d %b %Y}\n"
        f"Updates until: {licence.updates_until:%d %b %Y}\n"
        f"Serial:        {licence.serial}\n"
        "\n"
        "Import this file in Setup -> Licence, or paste the line below there.\n"
        f"{token}\n"
    )


def _token_in(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip().startswith(TOKEN_PREFIX):
            return line.strip()
    raise LicenceError("This is not a WhatsApp Printer licence: it has no line starting WAP1.")


def parse(text: str, public_key: str | None = None) -> Licence:
    """The licence in `text` -- a whole file, or its last line -- if it is genuine."""
    token = _token_in(text)
    try:
        encoded_payload, encoded_signature = token[len(TOKEN_PREFIX):].split(".")
        payload = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
    except ValueError:
        raise LicenceError("The licence line is incomplete. Copy the whole line, or import the file.")
    key = bytes.fromhex(public_key or PUBLIC_KEY)
    if not verify(key, _SIGNED_PREFIX + payload, signature):
        raise LicenceError("This licence was not issued for WhatsApp Printer, or it has been altered.")
    try:
        fields = json.loads(payload.decode("utf-8"))
        return Licence(
            licensee=str(fields["licensee"]),
            machine=format_code(fields["machine"]),
            issued=date.fromisoformat(fields["issued"]),
            updates_until=date.fromisoformat(fields["updates_until"]),
            serial=str(fields.get("serial", "")),
        )
    except (ValueError, KeyError, TypeError):
        raise LicenceError("This licence could not be read. Ask for it to be issued again.")


def _licence_path() -> Path:
    return paths().root / _LICENCE_FILE


def install(text: str) -> Licence:
    """Check a licence and keep it, or raise LicenceError and keep the old one."""
    licence = parse(text)
    here = machine_code()
    if licence.machine != here:
        raise LicenceError(
            f"This licence is for computer {licence.machine}, and this one is {here}. "
            f"Ask for a licence for {here}."
        )
    path = _licence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, describe(licence, _token_in(text)))
    _cache.clear()
    log.info("licence %s installed for %s", licence.serial, licence.licensee)
    return licence


# -- where this computer stands ------------------------------------------------------


@dataclass(frozen=True)
class Status:
    machine: str
    licence: Licence | None = None
    # None once the trial cannot be trusted: its record was altered, or the
    # clock was moved back past the day it began.
    trial_ends: date | None = None
    # Why a licence file present here was not accepted.
    note: str = ""
    today: date | None = None

    @property
    def licensed(self) -> bool:
        return self.licence is not None

    @property
    def can_send(self) -> bool:
        return self.licensed or (self.trial_ends is not None and self.today < self.trial_ends)

    @property
    def trial_days_left(self) -> int:
        if self.licensed or self.trial_ends is None:
            return 0
        return max(0, (self.trial_ends - self.today).days)

    def summary(self) -> str:
        """One line, for the foot of the Status page and the setup step."""
        if self.licence is not None:
            return f"Licensed to {self.licence.licensee}"
        if self.can_send:
            days = self.trial_days_left
            return f"Trial: {days} day{'s' if days != 1 else ''} left"
        return "Trial ended: not licensed"

    def hold_reason(self) -> str:
        """Why a print was not sent. Empty while this computer may send."""
        if self.can_send:
            return ""
        ended = f" on {self.trial_ends:%d %b %Y}" if self.trial_ends else ""
        reason = (
            f"The {TRIAL_DAYS}-day trial ended{ended} and this computer has no "
            f"licence, so nothing is sent. Prints are still kept: import a "
            f"licence in Setup → Licence, then send them from here."
        )
        if self.note:
            reason += f" ({self.note})"
        return reason

    def update_refusal(self, version: str, released: date | None) -> str:
        """Why a newer build may not be installed here. Empty if it may."""
        if self.licence is None or released is None or released <= self.licence.updates_until:
            return ""
        return (
            f"Version {version} is out, but updates for this computer ended on "
            f"{self.licence.updates_until:%d %b %Y}. Renew the AMC for a licence "
            f"that covers it. Nothing else is affected."
        )


_cache: dict[str, Licence] = {}


def _installed(machine: str) -> tuple[Licence | None, str]:
    path = _licence_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return None, "the licence file could not be read"
    key = hashlib.sha256(f"{PUBLIC_KEY}\n{text}".encode("utf-8")).hexdigest()
    licence = _cache.get(key)
    if licence is None:
        try:
            licence = parse(text)
        except LicenceError as exc:
            return None, str(exc)
        _cache.clear()
        _cache[key] = licence
    if licence.machine != machine:
        return None, f"the licence here is for computer {licence.machine}"
    return licence, ""


def _trial_started(today: date) -> date | None:
    """When this install first ran, recorded the first time anyone asks."""
    path = paths().root / _TRIAL_FILE
    try:
        return date.fromisoformat(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        # Someone has been at the file. Not a fresh trial.
        log.warning("trial record unreadable (%s); treating the trial as over", exc)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, today.isoformat())
    return today


def status(today: date | None = None) -> Status:
    """Where this computer stands. Never raises: a fault here must not stop capture."""
    today = today or date.today()
    try:
        machine = machine_code()
    except Exception as exc:
        log.exception("could not read the machine code")
        machine = f"unknown ({exc})"
    try:
        licence, note = _installed(machine)
        if licence is not None:
            return Status(machine, licence=licence, today=today)
        started = _trial_started(today)
        if started is None or started > today:
            return Status(machine, note=note, today=today)
        return Status(machine, trial_ends=started + timedelta(days=TRIAL_DAYS),
                      note=note, today=today)
    except Exception:
        log.exception("could not work out the licence status")
        # Erring towards sending: a bug here must not silence every counter.
        return Status(machine, trial_ends=today + timedelta(days=1), today=today)
