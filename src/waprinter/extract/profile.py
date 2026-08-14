"""What to look for on a page, as configuration rather than code.

Every client prints something different. A GST tax invoice says "Bill To" and
"Invoice No:"; a chit fund receipt says "Sri/Smt/M/s" and prints its receipt
number with no label at all. Hardcoding one layout means a new client needs a new
release, which is not a product.

So the varying parts are data:

* **Label lists** — plain words like `mobile`, `bill to`, `receipt no`. This is
  what changes between clients and what a non-programmer can safely edit. The
  regex machinery around them is fixed and stays in phone.py / fields.py.
* **Pattern lists** — full regexes, for the awkward cases labels cannot reach,
  such as an unlabelled `CR1747/26` sitting alone on a line. An escape hatch,
  not the normal route.

The built-in default is the union of every layout we have seen, so a fresh
install reads both a tax invoice and a chit receipt. A per-client profile can
then narrow or extend it; `profiles.json` next to the database wins over the
defaults, key by key, so a client override never has to restate the whole thing.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- built-in defaults ------------------------------------------------------

# Words that mean "the digits after me are a phone number".
PHONE_LABELS = [
    "mobile", "mob", "movil", "phone", "ph", "contact", "cell",
    "whats app", "whatsapp", "tel", "telephone", "mo", "cell no",
]

# Words that mean "the digits after me are definitely NOT a phone number".
# Getting this list wrong is how an account number becomes a recipient.
NOT_PHONE_LABELS = [
    "gstin", "gst", "pan", "hsn", "sac", "ifsc", "a/c", "ac", "account",
    "invoice", "bill", "challan", "e-way", "eway", "vehicle", "lr", "po",
    "order", "purchase", "pin", "pincode", "cin", "tin", "udyam", "msme",
    "date", "dated", "amount", "qty", "quantity", "rate", "cheque", "check",
    "utr", "ref", "reference", "voucher", "document", "irn", "ack",
    "state code", "licence", "license", "dl", "fssai", "serial", "s.no",
    # Chit fund specific: these sit next to numbers on a receipt.
    "chit", "group", "ticket", "instal", "instalment", "installment",
    "member id", "sl", "sl.no",
]

# Headings that introduce the customer's details.
CUSTOMER_ANCHORS = [
    # Tax invoice
    "bill to", "billed to", "buyer", "consignee", "customer",
    "ship to", "shipped to", "party name", "party",
    "details of receiver", "receiver",
    # Chit fund receipt: "Sri/Smt/M/s . ANITHA RAMESH"
    "sri/smt/m/s", "sri/smt", "m/s", "member name", "subscriber",
    "received from", "received with thanks from",
]

# Labels whose value is the document's identifying number.
DOCUMENT_NUMBER_LABELS = [
    "tax invoice", "invoice", "bill", "inv", "voucher", "document", "doc",
    "receipt", "rcpt", "receipt no",
]

# Unlabelled identifiers, as full regexes with one capture group. Needed where a
# number stands alone on a line, e.g. the chit receipt's "CR1747/26".
DOCUMENT_NUMBER_PATTERNS = [
    r"\b([A-Z]{2,4}\d{3,8}/\d{2,4})\b",   # CR1747/26
]

# Labels whose value is the document date.
DOCUMENT_DATE_LABELS = ["invoice date", "bill date", "receipt date", "dated", "date"]

# Date shapes, most distinctive first — they are tried in this order across the
# whole document, not row by row, so an unambiguous match anywhere beats a
# doubtful one higher up the page.
#
# The year is exactly 2 or 4 digits on purpose. Allowing 2-4 made
# "H.No. 2-7-384" in a street address parse as a date, which is precisely the
# kind of thing that ends up in a customer's message.
DATE_PATTERNS = [
    r"\b(\d{1,2}[-/.][A-Za-z]{3,9}[-/.](?:\d{4}|\d{2}))\b",   # 13-Aug-26
    r"\b(\d{4}-\d{2}-\d{2})\b",                               # 2026-05-12
    r"\b(\d{1,2}[-/.]\d{1,2}[-/.](?:\d{4}|\d{2}))\b",         # 12/05/2026
]

# Labels for the payable figure, most specific first.
AMOUNT_LABELS = [
    "grand total", "amount payable", "net payable", "net amount", "net total",
    "total amount", "amount paid", "amount", "total",
]

# Lines that follow a customer anchor but are not the customer's name.
NOT_A_NAME_PREFIXES = [
    "gstin", "gst", "pan", "state", "address", "phone", "mobile", "mob",
    "contact", "email", "place of supply", "code", "ph",
]


@dataclass
class DocumentProfile:
    """The vocabulary of one client's paperwork."""

    name: str = "default"
    phone_labels: list[str] = field(default_factory=lambda: list(PHONE_LABELS))
    not_phone_labels: list[str] = field(
        default_factory=lambda: list(NOT_PHONE_LABELS)
    )
    customer_anchors: list[str] = field(
        default_factory=lambda: list(CUSTOMER_ANCHORS)
    )
    document_number_labels: list[str] = field(
        default_factory=lambda: list(DOCUMENT_NUMBER_LABELS)
    )
    document_number_patterns: list[str] = field(
        default_factory=lambda: list(DOCUMENT_NUMBER_PATTERNS)
    )
    document_date_labels: list[str] = field(
        default_factory=lambda: list(DOCUMENT_DATE_LABELS)
    )
    date_patterns: list[str] = field(default_factory=lambda: list(DATE_PATTERNS))
    amount_labels: list[str] = field(default_factory=lambda: list(AMOUNT_LABELS))
    not_a_name_prefixes: list[str] = field(
        default_factory=lambda: list(NOT_A_NAME_PREFIXES)
    )

    # -- compiled forms, built once per profile ---------------------------

    def __post_init__(self) -> None:
        self._compile()

    def _compile(self) -> None:
        # A label only counts when it sits immediately before the value, which
        # is what the trailing \s*$ enforces: callers match against the text to
        # the left of a number, so "GSTIN: 29AA...   9876543210" does not let
        # GSTIN claim the phone number two columns away.
        opt_suffix = r"(?:no\.?|nos\.?|number|code|#)?"
        self.phone_label_re = re.compile(
            rf"\b(?:{_alt(self.phone_labels)})\b\.?\s*{opt_suffix}\s*[:\-–]?\s*$",
            re.IGNORECASE,
        )
        self.not_phone_label_re = re.compile(
            rf"\b(?:{_alt(self.not_phone_labels)})\b\.?\s*{opt_suffix}\s*"
            rf"[:\-–]?\s*$",
            re.IGNORECASE,
        )
        self.customer_anchor_re = re.compile(
            rf"\b(?:{_alt(self.customer_anchors)})", re.IGNORECASE
        )
        # A colon or dash is required between label and value, which is what
        # keeps "Invoice Date: 12/05/2026" from reading as an invoice number.
        self.document_number_re = re.compile(
            rf"\b(?:{_alt(self.document_number_labels)})\s*(?!\s*date\b)"
            rf"(?:no\.?|number|#)?\s*[:\-–]\s*([A-Za-z0-9][A-Za-z0-9/\-]{{1,24}})",
            re.IGNORECASE,
        )
        self.document_number_pattern_res = [
            re.compile(p) for p in self.document_number_patterns
        ]
        self.date_res = [
            re.compile(
                rf"\b(?:{_alt(self.document_date_labels)})\s*[:\-–]?\s*{p}",
                re.IGNORECASE,
            )
            for p in self.date_patterns
        ]
        # Unlabelled dates, as a fallback.
        self.bare_date_res = [re.compile(p) for p in self.date_patterns]
        self.amount_label_res = [
            (re.compile(rf"\b{re.escape(label)}\b", re.IGNORECASE), rank)
            # Earlier entries are more specific, so they score higher.
            for rank, label in enumerate(reversed(self.amount_labels), start=1)
        ]
        self.not_a_name_re = re.compile(
            rf"^\s*(?:{_alt(self.not_a_name_prefixes)})\b", re.IGNORECASE
        )

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            k: v
            for k, v in asdict(self).items()
            if not k.endswith("_re") and not k.endswith("_res")
        }

    @classmethod
    def load(cls, path: Path | None = None) -> "DocumentProfile":
        """Built-in defaults, with any per-client overrides merged over them.

        Merging key by key means a client file can override just the customer
        anchors without having to restate every other list.
        """
        if path is None or not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A malformed profile must not stop the printer working.
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _alt(words: list[str]) -> str:
    """Regex alternation over labels, longest first so "bill to" beats "bill"."""
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
