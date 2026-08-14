"""Configuration and filesystem layout.

Settings live in a JSON file next to the job database. Secrets (the WhatsApp
access token) never go in here — see `waprinter.secrets`, which uses Windows
DPAPI in production.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


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

    def ensure(self) -> None:
        for p in (self.spool, self.inbox, self.archive, self.logs):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    # --- Sending behaviour -------------------------------------------------
    # Which sender to use: "cloud" (Meta API) or "baileys" (local node service).
    sender_type: str = "baileys"
    # dry_run runs the whole pipeline but sends nothing. On until the client
    # has a working provider account.
    dry_run: bool = True
    # Every print raises a window where the operator types the customer's
    # number and confirms before anything is sent.
    #
    # This is the primary flow, not a safety net: these documents do not carry
    # the customer's phone number at all, so there is nothing to detect. Turning
    # it off only makes sense for a client whose documents *do* print a number,
    # and then only after measuring extraction with `waprinter corpus --score`.
    confirm_before_send: bool = True

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
    default_template: str = "invoice_document"
    template_language: str = "en"
    # Maps template body variable position -> extracted field name.
    # e.g. {"1": "customer_name", "2": "invoice_number", "3": "total_amount"}
    template_variables: dict[str, str] = field(
        default_factory=lambda: {
            "1": "customer_name",
            "2": "invoice_number",
            "3": "total_amount",
        }
    )

    # --- Local UI ----------------------------------------------------------
    ui_port: int = 8731

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or Paths(data_root()).settings
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Ignore unknown keys so a settings file from a newer build does not
        # crash an older service.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        path = path or Paths(data_root()).settings
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


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


def paths() -> Paths:
    return Paths(data_root())
