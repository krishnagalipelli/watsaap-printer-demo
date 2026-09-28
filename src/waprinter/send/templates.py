"""Message templates and the operator-editable parts of them.

WhatsApp only allows free-form text inside a 24-hour window that the *customer*
opens by messaging first. An invoice send is business-initiated, so it must use
a template Meta has pre-approved. That makes "editable message" mean two
different things, and the UI has to be honest about which is which:

* **Instantly editable** — which template is used, and what each {{n}} variable
  maps to. No approval needed; takes effect on the next print.
* **Editable with a delay** — the template's fixed wording. Changing it means
  submitting a new template to Meta and waiting for review (typically under a
  day). The old template keeps working meanwhile.

Templates are cached here with the status Meta last reported, so the sender can
refuse to use one that is pending or rejected instead of failing at send time.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..config import atomic_write_text, set_aside_corrupt
from ..models import ExtractedFields

log = logging.getLogger(__name__)

# Meta templates come in two shapes. The older one numbers its variables
# ({{1}}, {{2}}); the ones the Business Manager creates now name them
# ({{customer_name}}). Both are matched here, and `MessageTemplate.named` says
# which shape a template is, because the send payload differs.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# Meta rejects parameters that are empty or whitespace-only.
EMPTY_PARAM_FALLBACK = "-"

# How a template variable can be filled, besides naming a field. Written into
# settings.json by the mapping screen, so plain strings rather than objects:
#   "customer_id"          the value of a field read off the page
#   "text:Karimnagar"      the same fixed words on every message
#   "(blank)"              deliberately nothing; sends as EMPTY_PARAM_FALLBACK
FIXED_TEXT_PREFIX = "text:"
LEAVE_BLANK = "(blank)"

# The template the shipped numbered entries of Settings.template_variables
# were written for. Its body is "Dear {{1}}, ... payment to {{2}} ... Receipt
# No.: {{3}}", so {{2}} is the business name there -- and nothing says what
# {{2}} is anywhere else. invoice_document's {{2}} is the invoice number.
SHARED_MAP_WRITTEN_FOR = "chit_receipt"


def shared_variables_for(
    template: "MessageTemplate",
    template_variables: dict[str, str],
    default_template: str,
) -> dict[str, str]:
    """The part of the install's shared variable map that fits `template`.

    Named entries always fit: "receipt_no" means the receipt number in any
    template that uses the name. Numbered entries do not. "{{2}}" is only a
    position, and the shared map's positions describe one template's body.
    Applied to every numbered template they filled invoice_document as "Your
    invoice <business name> for ₹<invoice number>" -- and sent it.

    So numbered entries go only to the template they were written for: the
    chit receipt while they are as shipped, or this install's own default
    template once someone has changed them -- then they were edited for it.
    """
    from ..config import Settings

    named = {k: v for k, v in template_variables.items() if not k.isdigit()}
    numbered = {k: v for k, v in template_variables.items() if k.isdigit()}
    shipped = {k: v for k, v in Settings().template_variables.items() if k.isdigit()}
    written_for = (
        default_template.split("@", 1)[0]
        if numbered != shipped
        else SHARED_MAP_WRITTEN_FOR
    )
    if template.name == written_for:
        return {**named, **numbered}
    return named


def variable_map(template: "MessageTemplate", settings) -> dict[str, str]:
    """How this install fills this template's variables.

    Its own mapping from the setup screen when it has one; otherwise the
    shared template_variables, as far as they fit it.
    """
    own = settings.template_mappings.get(template.name)
    if own is not None:
        return dict(own)
    return shared_variables_for(
        template, settings.template_variables, settings.default_template
    )


def unfilled_variables(
    template: "MessageTemplate",
    mapping: dict[str, str],
    known_fields: set[str],
) -> list[str]:
    """Variables with nothing that could ever fill them.

    Not "blank on this print" -- that is `RenderedMessage.missing`, and can
    change from one print to the next. These are empty on every print: a
    numbered variable nobody mapped, a named one whose name is no field this
    install reads, or a mapping to a field that no longer exists. Sending
    anyway puts "-" where a receipt number or an amount belongs.
    """
    unfilled = []
    for variable in template.placeholders:
        source = mapping.get(variable)
        if source is None:
            source = variable if template.named else ""
        if source == LEAVE_BLANK or source.startswith(FIXED_TEXT_PREFIX):
            continue
        if not source or source not in known_fields:
            unfilled.append(variable)
    return unfilled


def unfilled_reason(template: "MessageTemplate", unfilled: list[str]) -> str:
    slots = ", ".join("{{%s}}" % v for v in unfilled)
    return (
        f"The '{template.name}' message has {slots} with nothing to fill "
        f"{'it' if len(unfilled) == 1 else 'them'}. Open Setup → Fill in "
        f"messages and choose what goes in each."
    )


# What Meta calls a status, in words for the screen.
STATUS_WORDS = {
    "approved": "approved",
    "pending": "waiting for Meta's approval",
    "rejected": "rejected by Meta",
    "paused": "paused by Meta",
    "disabled": "disabled by Meta",
    "missing": "not on WhatsApp",
}
# It also rejects a newline, a tab, or more than four consecutive spaces
# inside a parameter (error 132018). Page text arrives with all three -- a
# row joins its columns with runs of spaces -- so every value is flattened to
# single spaces before it goes anywhere near the send.
_PARAM_WHITESPACE = re.compile(r"\s+")


def clean_parameter(value: str) -> str:
    """One template parameter as Meta will accept it."""
    return _PARAM_WHITESPACE.sub(" ", value).strip()

# What to call the attached PDF when the client has not said. Deliberately the
# generic word: naming a receipt "Invoice" is worse than naming it nothing.
DEFAULT_DOCUMENT_NOUN = "Document"


@dataclass
class MessageTemplate:
    """A template as it exists in the WhatsApp Business account."""

    name: str
    language: str = "en"
    # The approved body wording, with {{1}}, {{2}} … placeholders.
    body: str = ""
    # Whether the template carries a document header (required to attach a PDF).
    header_document: bool = True
    footer: str | None = None
    # approved | pending | rejected | paused — as last reported by Meta.
    status: str = "pending"
    category: str = "UTILITY"
    # "positional" ({{1}}) or "named" ({{customer_name}}), matching the
    # parameter_format Meta recorded when the template was created. Named
    # templates must carry a parameter_name on every body parameter; sending
    # them positionally is rejected with error 132000.
    parameter_format: str = "positional"
    # Example values Meta holds for each body variable, keyed like
    # `placeholders`. Shown beside the mapping so whoever fills a template in
    # can see what {{id}} was meant to hold when it was written.
    examples: dict[str, str] = field(default_factory=dict)
    # DOCUMENT, IMAGE, TEXT, VIDEO -- or blank for no header. Only a document
    # header can carry the PDF, and saying which other kind a template has is
    # the difference between "cannot be used" and knowing why.
    header_format: str = ""

    @property
    def ref(self) -> str:
        """The key this exact template is stored under: name and language."""
        return f"{self.name}@{self.language}"

    @property
    def named(self) -> bool:
        """Whether body parameters go out with a parameter_name."""
        if self.parameter_format == "named":
            return True
        # A body written with named placeholders is named whatever the field
        # says, so a hand-edited template cannot silently send the wrong shape.
        return any(not p.isdigit() for p in self.placeholders)

    @property
    def placeholders(self) -> list[str]:
        """Variable tokens used in the body, in order, deduplicated.

        Positions ("1", "2") for a positional template; names
        ("customer_name") for a named one.
        """
        seen, out = set(), []
        for m in PLACEHOLDER.finditer(self.body):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(m.group(1))
        return out

    @property
    def usable(self) -> bool:
        return self.status == "approved" and self.header_document


CHIT_RECEIPT_BODY = (
    "Dear {{1}},\n\n"
    "Thank you for your payment to {{2}}.\n\n"
    "Please find your payment receipt attached as a PDF.\n"
    "Receipt No.: {{3}}\n"
    "Date: {{4}}\n"
    "Amount Paid: \u20b9{{5}}\n"
    "Payment Mode: {{6}}\n"
    "Thank you for choosing {{2}}."
)

# The named-parameter version of the same receipt, as created in the Business
# Manager. The wording below has to be kept identical to what Meta approved:
# only the parameters travel with the send, so a body that has drifted changes
# nothing for the customer but makes the operator's preview a lie.
CHITS_DETAILS_BODY = (
    "Dear {{customer_name}},\n\n"
    "Thank you for your payment. Please find your payment receipt attached as "
    "a PDF.\n"
    "Receipt No.: {{receipt_no}}\n"
    "Date: {{date}}\n"
    "Amount Paid: \u20b9{{amount}}\n"
    "Payment Mode: {{payment_mode}}"
)

# The removal notice and removal letter. Same rule as the receipt above: this
# wording has to stay identical to what Meta approved, because only the
# parameters travel with the send -- a body that has drifted changes nothing
# for the member but makes the operator's preview a lie. `waprinter templates
# --sync` is the authority on both the wording and the status.
#
# The business's own name is a `{business}` slot rather than one client's
# registered name typed into the source. It is filled from Settings.
# business_name when the built-in copies are built, so a second client does
# not receive a notice signed by the first. Note that it is deliberately NOT
# a {{variable}}: Meta's approved body carries this as static text, and a
# placeholder here would add a parameter to the send that the approved
# template has no slot for, which Meta rejects outright.
REMOVAL_NOTICE_BODY = (
    "Dear {{customer_name}},\n\n"
    "This is to inform you that a Removal Notice has been issued by "
    "{business}.\n"
    "Please find your removal notice attached as a PDF for your reference.\n\n"
    "Notice No.: {{notice_no}}\n"
    "Date: {{date}}\n\n"
    "Kindly review the attached document for further details.\n\n"
    "Regards,\n"
    "{business}."
)

REMOVAL_LETTER_BODY = (
    "Dear {{customer_name}},\n\n"
    "This is to inform you that a Removal Letter has been issued by "
    "{business}.\n"
    "Please find your removal letter attached as a PDF.\n\n"
    "Letter No.: {{letter_no}}\n"
    "Date: {{date}}\n\n"
    "Kindly review the attached document for further details.\n\n"
    "Thank you for your service with {business}."
)

# The business's own name, wherever it appears as static text in a built-in
# template. `{business}` is filled by str.replace and never by str.format --
# formatting would eat the {{...}} Meta placeholders, turning "{{customer_name}}"
# into "{customer_name}" and breaking every send.
BUSINESS_SLOT = "{business}"


def default_templates(business_name: str) -> list[MessageTemplate]:
    """The templates a fresh install starts with, in this client's name.

    Built per-install rather than as a module constant, because the wording
    belongs to the product and the name in it belongs to whoever bought it.
    One client's registered name typed into the source is how a second client
    ends up signing off as the first.

    What Meta has approved still outranks all of this: `waprinter templates
    --sync` replaces these bodies wholesale, and a stored template always wins
    over a built-in one.
    """

    def named(body: str) -> str:
        return body.replace(BUSINESS_SLOT, business_name)

    return [
        MessageTemplate(
            name="chits_details",
            language="en",
            body=named(CHITS_DETAILS_BODY),
            parameter_format="named",
            # Left pending on purpose: the sender refuses a template it has not
            # been told is approved, so nothing goes out until someone has
            # checked this against the Business Manager.
            status="pending",
            category="UTILITY",
        ),
        MessageTemplate(
            name="chit_receipt",
            language="en",
            body=named(CHIT_RECEIPT_BODY),
            footer=f"Regards,\n{business_name}",
            # Pending, like every built-in: the Cloud API sender refuses a
            # template nobody has confirmed against the Business Manager, and
            # `waprinter templates --sync` is what confirms it. This one used
            # to ship as "approved" so that link mode -- which sends free text
            # and needs no approval -- did not report itself not ready. That
            # made the same install look ready to send by API before Meta had
            # seen the template at all. Readiness now asks for approval only
            # where the API is actually used.
            status="pending",
            category="UTILITY",
        ),
        MessageTemplate(
            name="removal_notice",
            language="en",
            body=named(REMOVAL_NOTICE_BODY),
            parameter_format="named",
            status="pending",
            category="UTILITY",
        ),
        MessageTemplate(
            name="removal_letter",
            language="en",
            body=named(REMOVAL_LETTER_BODY),
            parameter_format="named",
            status="pending",
            category="UTILITY",
        ),
        MessageTemplate(
            name="invoice_document",
            language="en",
            body=(
                "Hello {{1}}, thank you for your business.\n\n"
                "Your invoice {{2}} for \u20b9{{3}} is attached.\n\n"
                "Please reach out if you have any questions."
            ),
            footer=business_name,
            status="pending",
            category="UTILITY",
        ),
    ]


class TemplateStore:
    """Templates on disk, editable from the local UI.

    Keyed by name *and* language. Meta allows one name in several languages
    -- "member_statement" in English and in Telugu -- and keying by name
    alone meant the second one fetched silently replaced the first. A
    reference in settings may still be a bare name; `get` resolves it to the
    preferred language when there is more than one.
    """

    def __init__(
        self,
        path: Path,
        business_name: str | None = None,
        language: str = "en",
    ):
        self.path = path
        # Imported here rather than at module scope: config imports nothing
        # from send, and keeping it that way costs one local import.
        if business_name is None:
            from ..config import Settings

            business_name = Settings().business_name
        self.business_name = business_name
        self.language = language
        self.refreshed_at = ""
        self._templates: dict[str, MessageTemplate] = {}
        self.load()

    def load(self) -> None:
        """Built-in templates, with whatever this machine has stored over them.

        The stored file used to be the whole truth, which meant a template
        added in a release never reached a machine that had already printed
        something -- the same way a new database column never reached one. The
        first removal notice to hit those installs would have been held with
        "Template 'removal_notice' is not configured", on every counter at
        once.

        Stored entries win, always. What Meta reports about a template it has
        seen -- above all its status -- is worth more than what shipped in the
        build, so a rejected template is never quietly made approved again.
        """
        self._templates = {
            t.ref: t for t in default_templates(self.business_name)
        }
        self.refreshed_at = ""
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("templates", []) if isinstance(raw, dict) else None
            if items is None:
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            # The same rule as settings.json: a file that cannot be read is
            # set aside, not allowed to stop the agent starting. The built-in
            # copies are all pending, so nothing is sent on their say-so.
            moved = set_aside_corrupt(self.path)
            log.error(
                "could not read %s (%s); using the built-in templates%s",
                self.path,
                exc,
                f", the file was moved to {moved.name}" if moved else "",
            )
            return
        self.refreshed_at = str(raw.get("refreshed_at") or "")
        known = set(MessageTemplate.__dataclass_fields__)
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                log.warning("skipping a template entry with no name in %s", self.path)
                continue
            # Unknown keys are dropped rather than passed to the constructor:
            # a file written by a newer build, or edited by hand, used to
            # raise TypeError here and take the agent down at startup.
            fields = {k: v for k, v in item.items() if k in known}
            if not isinstance(fields.get("examples", {}), dict):
                fields.pop("examples")
            template = MessageTemplate(**fields)
            self._templates[template.ref] = template

    def save(self) -> None:
        payload = {
            "refreshed_at": self.refreshed_at,
            "templates": [asdict(t) for t in self._templates.values()],
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2))

    def get(self, ref: str) -> MessageTemplate | None:
        """A template by "name@language", or by bare name.

        A bare name with several languages resolves to this store's preferred
        language, then to an approved one, then to whichever comes first.
        """
        if not ref:
            return None
        if ref in self._templates:
            return self._templates[ref]
        if "@" in ref:
            return None
        matches = [t for t in self._templates.values() if t.name == ref]
        if len(matches) <= 1:
            return matches[0] if matches else None
        for template in matches:
            if template.language == self.language:
                return template
        approved = [t for t in matches if t.usable]
        return (approved or matches)[0]

    def ref_for(self, template: MessageTemplate) -> str:
        """The shortest reference that picks out exactly this template."""
        same_name = [t for t in self._templates.values() if t.name == template.name]
        return template.name if len(same_name) == 1 else template.ref

    def put(self, template: MessageTemplate) -> None:
        self._templates[template.ref] = template
        self.save()

    def apply_refresh(self, fetched: list[MessageTemplate], when: str = "") -> None:
        """Make the store say what Meta says, all at once.

        Every template Meta returned replaces ours. Every one it did not is
        kept -- a setting may still name it, and the words are worth keeping
        -- but marked "missing", which is not usable. Adding them one at a
        time used to leave a template deleted in the Business Manager looking
        approved here for ever, and the first send under it failed at Meta.
        """
        from datetime import datetime

        fresh = {t.ref: t for t in fetched}
        for ref, template in self._templates.items():
            if ref not in fresh:
                fresh[ref] = replace(template, status="missing")
        self._templates = fresh
        self.refreshed_at = when or datetime.now().isoformat(timespec="seconds")
        self.save()

    def all(self) -> list[MessageTemplate]:
        return list(self._templates.values())


@dataclass
class RenderedMessage:
    """A template resolved against one invoice, ready to send or preview."""

    template: MessageTemplate
    parameters: list[str] = field(default_factory=list)  # ordered {{1}}, {{2}} …
    # Parallel to `parameters`, and only for a named template: the
    # parameter_name each value has to be labelled with in the send payload.
    parameter_names: list[str] = field(default_factory=list)
    preview: str = ""       # what the customer will actually read
    filename: str = "document.pdf"
    missing: list[str] = field(default_factory=list)  # variables with no value
    # Variables with nothing mapped to them at all -- a setup problem, as
    # opposed to a field that came out blank on this one print.
    unmapped: list[str] = field(default_factory=list)


def render(
    template: MessageTemplate,
    variable_map: dict[str, str],
    fields: ExtractedFields,
    doc_title: str | None = None,
    extra: dict[str, str] | None = None,
    document_noun: str = DEFAULT_DOCUMENT_NOUN,
) -> RenderedMessage:
    """Resolve a template's variables from the extracted invoice fields.

    `variable_map` maps a placeholder token to a field name, e.g.
    {"1": "customer_name", "2": "invoice_number"} for a positional template, or
    {"receipt_no": "invoice_number"} for a named one. A named template's token
    that is not in the map falls back to a field of the same name, so only the
    variables whose names differ from ours need mapping at all. `extra`
    supplies values that are configuration rather than page content — the
    business's own name.
    """
    values = {**fields.as_template_vars(), **(extra or {})}
    parameters: list[str] = []
    missing: list[str] = []
    unmapped: list[str] = []

    for token in template.placeholders:
        source = variable_map.get(token)
        if source is None:
            source = token if template.named else ""
        if source == LEAVE_BLANK:
            # Chosen on purpose, so neither missing nor a reason to hold.
            parameters.append(EMPTY_PARAM_FALLBACK)
            continue
        if source.startswith(FIXED_TEXT_PREFIX):
            value = clean_parameter(source[len(FIXED_TEXT_PREFIX):])
        else:
            value = clean_parameter(values.get(source) or "")
        if not source:
            unmapped.append(token)
        if not value:
            missing.append(source or f"{{{{{token}}}}}")
            value = EMPTY_PARAM_FALLBACK
        parameters.append(value)

    def substitute(m: re.Match[str]) -> str:
        token = m.group(1)
        try:
            return parameters[template.placeholders.index(token)]
        except (ValueError, IndexError):
            return m.group(0)

    preview = PLACEHOLDER.sub(substitute, template.body)
    if template.footer:
        preview = f"{preview}\n\n{template.footer}"

    return RenderedMessage(
        template=template,
        parameters=parameters,
        parameter_names=list(template.placeholders) if template.named else [],
        preview=preview,
        filename=_filename(fields, doc_title, document_noun),
        missing=missing,
        unmapped=unmapped,
    )


def _filename(
    fields: ExtractedFields,
    doc_title: str | None,
    noun: str = DEFAULT_DOCUMENT_NOUN,
) -> str:
    """What the PDF is called on the customer's phone.

    `noun` is what this client's paperwork is called — a chit fund sends
    receipts, not invoices, and the member reads this filename before they open
    anything.
    """
    noun = noun.strip() or DEFAULT_DOCUMENT_NOUN
    if fields.invoice_number:
        # "CR1747/26" is one identifier, not two. Turn the separator into
        # something a filesystem accepts instead of deleting it: a member who
        # quotes "CR174726" back over the phone is quoting a number that
        # appears on no receipt.
        number = re.sub(r"[\\/]+", "-", fields.invoice_number)
        stem = f"{noun}-{number}"
    elif doc_title:
        stem = doc_title
    else:
        stem = noun
    # WhatsApp shows this verbatim; keep it filesystem- and eye-friendly.
    stem = re.sub(r"[^A-Za-z0-9._\- ]+", "", stem).strip() or noun
    return f"{stem[:60]}.pdf"
