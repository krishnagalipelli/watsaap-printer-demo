"""Configuration and filesystem layout.

Settings live in a JSON file next to the job database. Secrets (the WhatsApp
access token) never go in here — see `waprinter.secrets`, which uses Windows
DPAPI in production.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` so that a crash leaves either the old file or
    the new one, never a truncated mixture of the two.

    settings.json used to be written straight through write_text. A counter PC
    that lost power mid-write was left with half a file, json could not parse
    it, and the agent refused to start at every logon from then on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def set_aside_corrupt(path: Path) -> Path | None:
    """Move a file that cannot be parsed out of the way, keeping its contents.

    Returns where it went, or None if it could not be moved. The original
    name is then free for a fresh file, and whoever investigates still has
    the bytes.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, target)
        return target
    except OSError:
        return None


def data_root() -> Path:
    """Base directory for spool, database, logs, and settings.

    Overridable with WAPRINTER_HOME, which is how the tests and the dry-run
    harness stay out of the real install.
    """
    override = os.environ.get("WAPRINTER_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        return base / "WAPrinter"
    # Dev machines (macOS/Linux). Production is Windows-only.
    return Path.home() / ".waprinter"


@dataclass
class Paths:
    root: Path

    @property
    def spool(self) -> Path:
        """Where the printer port drops raw PDFs. Watched, emptied immediately."""
        return self.root / "spool"

    @property
    def inbox(self) -> Path:
        """Captured PDFs, moved here under a uuid so the port is freed at once."""
        return self.root / "inbox"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def db(self) -> Path:
        return self.root / "jobs.db"

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def profile(self) -> Path:
        """Per-client document vocabulary. See extract/profile.py."""
        return self.root / "profile.json"

    @property
    def templates(self) -> Path:
        return self.root / "templates.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def samples(self) -> Path:
        """One sample PDF per taught document type, for the setup screens."""
        return self.root / "samples"

    def ensure(self) -> None:
        for p in (self.spool, self.inbox, self.archive, self.logs):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    # --- Sending behaviour -------------------------------------------------
    # dry_run runs the whole pipeline but sends nothing. On until the client
    # has a working provider account.
    dry_run: bool = True
    # Print, read the page, send. The operator sees only a small popup saying
    # whether it went. This is the normal flow because these documents do print
    # the customer's mobile number, and the extractor finds it reliably.
    #
    # Turn this on to make every print stop for confirmation instead — worth it
    # for a client whose paperwork does not carry a number, or while tuning a
    # new document layout.
    confirm_before_send: bool = False

    # --- Safety rails ------------------------------------------------------
    # The client's own numbers. Seeded at install; anything matching is never
    # treated as a recipient (invoice footers carry the seller's number).
    own_numbers: list[str] = field(default_factory=list)
    # Extra numbers never to send to (transporters, internal staff).
    blocklist: list[str] = field(default_factory=list)
    # A reprint of the same invoice inside this window does not re-send.
    dedupe_window_hours: int = 24
    max_sends_per_minute: int = 10
    max_sends_per_day: int = 500

    # --- OCR (for ERPs that print a raster instead of text) ----------------
    ocr_enabled: bool = True
    ocr_dpi: int = 300
    ocr_verify_dpi: int = 400
    ocr_language: str = "eng"
    # Leave blank to auto-detect Tesseract's language data.
    ocr_tessdata: str = ""
    # Whether a number read by OCR may be sent to without anyone looking.
    # Off by default: OCR confuses digits, and on a ten-digit mobile a single
    # wrong digit is a different real person. Turn this on only after measuring
    # scanned invoices with `waprinter corpus --score`.
    ocr_silent_send: bool = False

    # --- Locale ------------------------------------------------------------
    default_country_code: str = "91"

    # --- WhatsApp ----------------------------------------------------------
    phone_number_id: str = ""
    business_account_id: str = ""
    graph_api_version: str = "v21.0"
    # Send over IPv4 only. The sender falls back to this on its own after a
    # dropped connection, which costs one failed upload; setting it here skips
    # that on a counter PC already known to have a broken IPv6 route.
    force_ipv4: bool = False
    default_template: str = "chit_receipt"
    # Which approved template each kind of paperwork goes out under. The chit
    # fund prints removal notices and removal letters through the same queue as
    # receipts, and they are different documents to different people saying
    # different things -- a member sent the receipt wording over a removal
    # notice is worse than one sent nothing. Recognised non-receipt kinds
    # without a mapping are held. Receipts and unrecognised documents use
    # default_template for compatibility with receipt-only installations.
    document_templates: dict[str, str] = field(
        default_factory=lambda: {
            "removal_notice": "removal_notice",
            "removal_letter": "removal_letter",
        }
    )
    # What each kind of paperwork is called on the customer's phone. The
    # member reads the filename before they open anything, so a removal notice
    # must not arrive as "Receipt-RN317-26.pdf". Falls back to document_noun.
    document_nouns: dict[str, str] = field(
        default_factory=lambda: {
            "removal_notice": "Removal Notice",
            "removal_letter": "Removal Letter",
        }
    )
    template_language: str = "en"
    # Maps a template body variable -> extracted field name. Positional
    # templates key on the position ({{1}}); named ones key on the variable
    # name ({{receipt_no}}), and only need an entry where Meta's name differs
    # from ours. Both key sets live here so switching default_template between
    # the two shapes does not need this rewritten.
    template_variables: dict[str, str] = field(
        default_factory=lambda: {
            "1": "customer_name",
            "2": "business_name",
            "3": "invoice_number",
            "4": "invoice_date",
            "5": "total_amount",
            "6": "payment_mode",
            "receipt_no": "invoice_number",
            # The removal notice and removal letter templates. Their bodies
            # name the same two fields differently, which is exactly what this
            # map is for.
            "notice_no": "invoice_number",
            "letter_no": "invoice_number",
            "date": "invoice_date",
            # The amount in words, not figures. A chit receipt prints the
            # figures five times over — dues, sub-totals, interest — and the
            # labels that would tell them apart are pre-printed, so they never
            # reach the PDF's text layer. `total_amount` is therefore blank on
            # this layout, and a blank parameter sends as "-", which on a
            # payment receipt is worse than saying nothing. The words are
            # unambiguous. A client whose paperwork does label its total should
            # point this back at `total_amount`.
            "amount": "amount_words",
        }
    )
    # How each template's variables are filled, per template name, as chosen
    # on the setup screen: {"member_statement": {"id": "customer_id",
    # "branch": "text:Karimnagar", "note": "(blank)"}}. See templates.render
    # for the three kinds of value.
    #
    # A template listed here is filled from its own map only, and a mapped
    # field that comes out blank on a print holds the document rather than
    # sending "-" -- whoever set it up said that value belongs in the
    # message. A template not listed falls back to template_variables above,
    # exactly as before, so an existing install behaves as it always did.
    template_mappings: dict[str, dict[str, str]] = field(default_factory=dict)

    # --- Message ------------------------------------------------------------
    # Free text until the Cloud API account is live; a wa.me link carries the
    # message but cannot carry the PDF, so the operator attaches it.
    send_mode: str = "link"           # "link" or "api"
    # Link mode: open the chat as soon as the receipt is read, instead of
    # waiting for the operator to click. Right for a counter serving one member
    # at a time. Turn it off where receipts are printed in batches, or ten
    # prints will fling open ten chats.
    auto_open_chat: bool = True
    business_name: str = "Srinidhi Chit Funds"
    # What this client's paperwork is called, used to name the attached PDF:
    # "Receipt-CR1747-26.pdf". The customer reads this before they open
    # anything, so a chit fund must not be sending "Invoice-".
    document_noun: str = "Receipt"

    # --- Keeping the printed receipts --------------------------------------
    # Every PDF that comes off the printer is copied here, filed by date and
    # named after the receipt, so the office has its own record without going
    # near ProgramData. Blank means <data root>/archive, which is where they
    # go if nobody chooses anything.
    #
    # A copy, not a move: the working file under inbox is what the queue reopens
    # and what a held job is eventually sent from, and pointing that at a
    # network drive or a USB stick would break sending the moment it went away.
    pdf_folder: str = ""
    keep_printed_pdfs: bool = True
    # How long the working copies under <data root>/inbox are kept. These are
    # what the queue reopens and a held job is sent from, so anything still
    # waiting on a person is kept whatever its age. Zero keeps everything.
    # Without this the folder grew by one PDF per print, forever, on machines
    # nobody administers.
    keep_inbox_days: int = 90

    # --- Updates -----------------------------------------------------------
    # A static JSON file: {"version", "url", "sha256", "notes"}. No server of
    # ours is involved; if it is unreachable, everything else carries on.
    # /releases/latest/download/ always resolves to the newest release's copy
    # of the file, so this address never changes as versions come and go. CI
    # writes latest.json when a v* tag is built; see build-windows.yml.
    update_url: str = (
        "https://github.com/krishnagalipelli/watsaap-printer-demo"
        "/releases/latest/download/latest.json"
    )
    update_check_enabled: bool = True
    last_update_check: str = ""

    # --- This installation -------------------------------------------------
    # Shown in the window and written on every job, so logs can answer "which
    # counter sent this" without a central service.
    device_name: str = ""
    branch_name: str = ""
    # Asked for before the setup screens open, so a counter clerk does not
    # change which message members receive by accident. A hash, never the
    # PIN itself. It is a lock on a door, not a safe: anyone who can edit
    # settings.json can clear it, and that is the whole Users group.
    setup_pin: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or Paths(data_root()).settings
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings.json is not a JSON object")
        except (OSError, ValueError) as exc:
            # A file that cannot be read must not stop the agent starting:
            # the defaults are the safe state (test mode, nothing sent), and
            # the broken file is kept beside them for whoever looks into it.
            moved = set_aside_corrupt(path)
            log.error(
                "could not read %s (%s); using defaults%s",
                path,
                exc,
                f", the file was moved to {moved.name}" if moved else "",
            )
            return cls()
        # Ignore unknown keys so a settings file from a newer build does not
        # crash an older service, and drop values of the wrong type so a hand
        # edit like "dry_run": "false" cannot leave a string where the code
        # tests a bool.
        clean: dict = {}
        for key, value in raw.items():
            if key not in cls.__dataclass_fields__:
                continue
            value, problem = coerce_setting(key, value)
            if problem:
                log.warning("ignoring %s in %s: %s", key, path, problem)
                continue
            clean[key] = value
        settings = cls(**clean)

        # A variable added in a release has to reach a machine that already
        # has a settings.json. Stored dicts replace the default wholesale, so
        # the keys the removal templates need never arrived on an install that
        # had printed anything -- and an unresolved variable does not fail,
        # it sends as "-". A member reading "Notice No.: -" on a removal
        # notice is the same silent breakage as the template that never
        # reached an existing install, and as the database column before it.
        #
        # Only this map is merged. document_templates and document_nouns say
        # which documents a branch actually sends, and an empty one is a real
        # answer -- merging the defaults back would hand every client the chit
        # fund's paperwork. This one is vocabulary: an entry for a template
        # you do not use is inert, and a missing one is a broken message.
        # Stored entries still win, so a remapped variable stays remapped.
        if "template_variables" in raw:
            merged = dict(cls().template_variables)
            merged.update(settings.template_variables)
            settings.template_variables = merged

        return settings

    def save(self, path: Path | None = None) -> None:
        path = path or Paths(data_root()).settings
        atomic_write_text(path, json.dumps(asdict(self), indent=2))

    def ocr(self) -> "OcrSettings":
        """The OCR configuration, in the form the extractor expects."""
        from .extract.ocr import OcrSettings

        return OcrSettings(
            enabled=self.ocr_enabled,
            dpi=self.ocr_dpi,
            verify_dpi=self.ocr_verify_dpi,
            language=self.ocr_language,
            tessdata=self.ocr_tessdata or None,
        )


def coerce_setting(name: str, value: object) -> tuple[object, str | None]:
    """Check one setting's value against the type of its default.

    Returns (value, None) when it fits, or (value, why not) when it does not.
    The shape is read off the dataclass default rather than declared twice,
    so a new field is validated the moment it exists.

    Every route into Settings -- provision.json, settings.json, the CLI --
    hands over whatever JSON held, and JSON does not know that "false" is not
    False. A string is truthy, so `"dry_run": "false"` left test mode on, and
    `"own_numbers": "98765..."` iterated as characters and excluded nothing,
    which made the seller's own number a valid recipient.
    """
    default = getattr(Settings(), name)
    kind = type(default)

    if kind is bool:
        if isinstance(value, bool):
            return value, None
        return value, f"expected true or false, got {value!r}"
    if kind is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value, None
        return value, f"expected a whole number, got {value!r}"
    if kind is str:
        if isinstance(value, str):
            return value, None
        return value, f"expected text, got {value!r}"
    if kind is list:
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value, None
        return value, f"expected a list of text values, got {value!r}"
    if kind is dict:
        # The one nested shape: a map of maps. Told apart by its declared
        # type, because an empty default carries no hint of what goes in it.
        declared = str(Settings.__dataclass_fields__[name].type).replace(" ", "")
        if declared == "dict[str,dict[str,str]]":
            if isinstance(value, dict) and all(
                isinstance(k, str)
                and isinstance(inner, dict)
                and all(isinstance(a, str) and isinstance(b, str) for a, b in inner.items())
                for k, inner in value.items()
            ):
                return value, None
            return value, f"expected a map of maps of text, got {value!r}"
        if isinstance(value, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            return value, None
        return value, f"expected a map of text to text, got {value!r}"
    return value, None


def paths() -> Paths:
    return Paths(data_root())
