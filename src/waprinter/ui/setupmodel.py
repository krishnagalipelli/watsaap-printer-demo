"""What the setup screens say and decide, without any widgets.

The same split as viewmodel.py: the Tk code in setup.py only lays things out
and calls into here, so every rule about mapping a template, what counts as
set up, and how the PIN is checked is tested on a machine with no display.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

from ..extract.rules import BUILTIN_FIELDS, display_name
from ..models import ExtractedFields
from ..send.templates import (
    FIXED_TEXT_PREFIX,
    LEAVE_BLANK,
    SHARED_MAP_WRITTEN_FOR,  # noqa: F401  (re-exported for the screens)
    STATUS_WORDS,
    MessageTemplate,
    render,
    shared_variables_for,
)

FIXED_LABEL = "Fixed text…"
BLANK_LABEL = "Leave blank (sends -)"
BUSINESS_FIELD = "business_name"

# Other words a template author uses for each built-in field. Checked after
# an exact name match, before guessing from shared words.
_SYNONYMS = {
    "customer_name": {"name", "customer", "member", "member_name", "subscriber",
                      "customer_name", "full_name"},
    "invoice_number": {"receipt_no", "receipt", "receipt_number", "invoice_no",
                       "invoice", "invoice_number", "bill_no", "notice_no",
                       "letter_no", "document_no", "ref", "reference", "number", "no"},
    "invoice_date": {"date", "receipt_date", "invoice_date", "dated", "notice_date",
                     "letter_date"},
    "total_amount": {"total", "total_amount", "amount_figures", "amount_paid_figures"},
    "amount_words": {"amount", "amount_words", "amount_in_words", "amount_paid"},
    "payment_mode": {"mode", "payment_mode", "payment", "paid_by"},
    BUSINESS_FIELD: {"business", "business_name", "company", "company_name", "firm",
                     "branch_name"},
}


# -- templates ---------------------------------------------------------------


def template_problem(template: MessageTemplate | None, send_mode: str) -> str:
    """Why this template cannot carry a PDF from this install, or ""."""
    if template is None:
        return "Not on this computer. Refresh the templates from WhatsApp."
    if send_mode != "api":
        # Link mode opens WhatsApp with the text typed out; Meta's approval
        # and header are not involved on that route.
        return ""
    if template.status != "approved":
        return f"Cannot be used: {STATUS_WORDS.get(template.status, template.status)}."
    if not template.header_document:
        kind = (template.header_format or "no").lower()
        return (
            f"Cannot be used: it has {kind} header, so the PDF has nowhere to go."
            if template.header_format
            else "Cannot be used: it has no document header, so the PDF has nowhere to go."
        )
    return ""


def template_choice(template: MessageTemplate, ref: str) -> str:
    """How a template reads in the drop-down."""
    status = STATUS_WORDS.get(template.status, template.status)
    return f"{ref} · {template.language} · {status}"


# -- mapping -----------------------------------------------------------------


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


# One rule for what the shared map fits, used here and at send time.
shared_map_for = shared_variables_for


# Words printed just before a variable that say what goes in it, most
# specific first. Read from the template's own wording, so they work for
# numbered variables, which carry no name to go on.
_CUES: list[tuple[str, str]] = [
    ("mode of payment", "payment_mode"),
    ("payment mode", "payment_mode"),
    ("paid by", "payment_mode"),
    ("receipt no", "invoice_number"),
    ("receipt number", "invoice_number"),
    ("invoice no", "invoice_number"),
    ("invoice number", "invoice_number"),
    ("bill no", "invoice_number"),
    ("notice no", "invoice_number"),
    ("letter no", "invoice_number"),
    ("amount paid", "total_amount"),
    ("total amount", "total_amount"),
    ("member name", "customer_name"),
    ("customer name", "customer_name"),
    ("reference", "invoice_number"),
    ("invoice", "invoice_number"),
    ("receipt", "invoice_number"),
    ("bill", "invoice_number"),
    ("ref", "invoice_number"),
    ("date", "invoice_date"),
    ("dated", "invoice_date"),
    ("amount", "total_amount"),
    ("total", "total_amount"),
    ("rs", "total_amount"),
    ("inr", "total_amount"),
    ("mode", "payment_mode"),
    ("dear", "customer_name"),
    ("hello", "customer_name"),
    ("hi", "customer_name"),
    ("sri", "customer_name"),
    ("smt", "customer_name"),
    ("mr", "customer_name"),
    ("mrs", "customer_name"),
    ("ms", "customer_name"),
    ("name", "customer_name"),
]


def guess_from_wording(template: MessageTemplate, variable: str,
                       available: list[str]) -> str:
    """What the words just before {{variable}} in the body say it holds.

    "Hello {{1}}" is a name, "invoice {{2}}" a number, "₹{{3}}" an amount.
    Only the words immediately in front count, on the same line; anything
    less direct ("payment to {{2}}") says too little and is left for a
    person to choose.
    """
    marker = re.search(r"\{\{\s*%s\s*\}\}" % re.escape(variable), template.body)
    if not marker:
        return ""
    before = template.body[: marker.start()].rsplit("\n", 1)[-1]
    before = before.lower().rstrip(" \t:.-–#,(")
    if before.endswith("₹"):
        return "total_amount" if "total_amount" in available else ""
    words = re.findall(r"[a-z]+", before)[-3:]
    for phrase, field_name in _CUES:
        wanted = phrase.split()
        if words[-len(wanted):] == wanted and field_name in available:
            return field_name
    return ""


def suggest_mapping(
    template: MessageTemplate,
    fields: list[str],
    legacy: dict[str, str] | None = None,
    current: dict[str, str] | None = None,
) -> dict[str, str]:
    """A first guess at filling each variable, for the person to correct.

    In order: what is already saved; a field with the variable's exact name;
    the install's shared map, as far as it fits this template (pass it
    through `shared_map_for`); a known synonym ("receipt_no" is the document
    number); the field sharing most words with it, which is how "id" finds
    "customer_id"; and last, the words printed just before the variable in
    the body -- the only clue a numbered variable has.

    Where none of those says anything, the variable is left empty. An empty
    row cannot be saved, which is better than a wrong guess that can.
    """
    legacy = legacy or {}
    current = current or {}
    available = [*fields, BUSINESS_FIELD]
    guess: dict[str, str] = {}
    for variable in template.placeholders:
        if current.get(variable):
            guess[variable] = current[variable]
            continue
        key = variable.lower()
        if key in available:
            guess[variable] = key
            continue
        old = legacy.get(variable)
        if old and old in available:
            guess[variable] = old
            continue
        by_synonym = next(
            (f for f in available if key in _SYNONYMS.get(f, set())), None
        )
        if by_synonym:
            guess[variable] = by_synonym
            continue
        words = _tokens(key) - {str(n) for n in range(100)}
        best, overlap = "", 0
        for candidate in available:
            shared = len(words & _tokens(candidate))
            if shared > overlap:
                best, overlap = candidate, shared
        if best:
            guess[variable] = best
            continue
        from_wording = guess_from_wording(template, variable, available)
        if from_wording:
            guess[variable] = from_wording
    return guess


def source_choices(fields: list[str]) -> list[tuple[str, str]]:
    """(label, source) pairs for a variable's drop-down.

    Fixed text is represented by its prefix alone; the screen collects the
    words in a box beside it.
    """
    choices = [(display_name(f), f) for f in fields]
    choices.append(("Business name (from settings)", BUSINESS_FIELD))
    choices.append((FIXED_LABEL, FIXED_TEXT_PREFIX))
    choices.append((BLANK_LABEL, LEAVE_BLANK))
    return choices


def label_for_source(source: str, fields: list[str]) -> str:
    if source.startswith(FIXED_TEXT_PREFIX):
        return FIXED_LABEL
    for label, value in source_choices(fields):
        if value == source:
            return label
    return display_name(source) if source else ""


def mapping_problems(template: MessageTemplate, mapping: dict[str, str]) -> list[str]:
    """What stops this mapping being saved."""
    problems = []
    for variable in template.placeholders:
        source = mapping.get(variable, "")
        if not source:
            problems.append("{{%s}} is not filled in." % variable)
        elif source.startswith(FIXED_TEXT_PREFIX) and not source[len(FIXED_TEXT_PREFIX):].strip():
            problems.append("{{%s}} is set to fixed text, but the text is empty." % variable)
    return problems


def fields_from_values(values: dict[str, str]) -> ExtractedFields:
    fields = ExtractedFields()
    for name, value in values.items():
        if name in BUILTIN_FIELDS:
            setattr(fields, name, value)
        elif name != BUSINESS_FIELD:
            fields.extra[name] = value
    return fields


def preview(
    template: MessageTemplate,
    mapping: dict[str, str],
    values: dict[str, str],
    business_name: str,
) -> str:
    """The message a member would read, filled from example values.

    A variable whose field has no example yet shows the field's name in
    brackets, so the preview is readable before anything has been printed.
    """
    shown = dict(values)
    for variable in template.placeholders:
        source = mapping.get(variable, "")
        if source and not source.startswith(FIXED_TEXT_PREFIX) and source != LEAVE_BLANK:
            if not shown.get(source) and source != BUSINESS_FIELD:
                shown[source] = f"[{display_name(source)}]"
    fields = fields_from_values(shown)
    filled = {v: (s or "") for v, s in mapping.items()}
    for variable in template.placeholders:
        if not filled.get(variable):
            filled[variable] = FIXED_TEXT_PREFIX + "[not filled in]"
    message = render(template, filled, fields, extra={BUSINESS_FIELD: business_name})
    return message.preview


# -- the checklist --------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    done: bool
    detail: str = ""


STEP_TITLES = [
    ("connect", "Connect WhatsApp"),
    ("templates", "Message templates"),
    ("documents", "Document types"),
    ("messages", "Fill in messages"),
    ("test", "Try it"),
    ("preferences", "Counter settings"),
    ("share", "Share setup"),
]


def checklist(settings, templates, profile, token_problem: str | None) -> list[Step]:
    """Where setup stands, one line per step, for the side bar."""
    connected = not token_problem and bool(settings.phone_number_id)
    usable = [t for t in templates.all() if t.usable]
    in_use = [settings.default_template, *settings.document_templates.values()]
    unfinished = []
    for ref in in_use:
        template = templates.get(ref)
        if template is None:
            unfinished.append(ref)
            continue
        mapping = settings.template_mappings.get(template.name)
        if mapping is not None and mapping_problems(template, mapping):
            unfinished.append(template.name)
    waiting = [k.name for k in profile.custom_kinds
               if not settings.document_templates.get(k.name)]
    details = {
        "connect": "Connected" if connected else (token_problem or "Choose a phone number"),
        "templates": f"{len(usable)} usable" if usable else "None usable yet",
        # The built-in scan-only "receipt" kind is the default row, not a
        # type of its own, on every screen that lists them.
        "documents": f"{len([k for k in profile.all_kinds if k.name != 'receipt']) + 1} types",
        "messages": "All filled in" if not unfinished and not waiting
        else f"{len(unfinished) + len(waiting)} to finish",
        "test": "Check a PDF before going live",
        "preferences": "Test mode on" if settings.dry_run else "Live",
        "share": "PIN set" if settings.setup_pin else "No PIN",
    }
    done = {
        "connect": connected,
        "templates": bool(usable) or settings.send_mode == "link",
        "documents": True,
        "messages": not unfinished and not waiting,
        "test": False,
        "preferences": True,
        "share": bool(settings.setup_pin),
    }
    return [Step(key, title, done[key], details[key]) for key, title in STEP_TITLES]


# -- the setup PIN ---------------------------------------------------------------

_PIN_ITERATIONS = 120_000


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, _PIN_ITERATIONS)
    return f"pbkdf2${_PIN_ITERATIONS}${salt.hex()}${digest.hex()}"


def check_pin(pin: str, stored: str) -> bool:
    """Whether `pin` matches the stored hash. An empty store has no lock."""
    if not stored:
        return True
    try:
        scheme, rounds, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        attempt = hashlib.pbkdf2_hmac(
            "sha256", pin.encode("utf-8"), bytes.fromhex(salt), int(rounds)
        )
        return hmac.compare_digest(attempt.hex(), digest)
    except (ValueError, TypeError):
        return False


def pin_problem(pin: str) -> str:
    if not re.fullmatch(r"\d{4,8}", pin or ""):
        return "Use 4 to 8 digits."
    return ""
