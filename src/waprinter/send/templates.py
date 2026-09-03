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
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..models import ExtractedFields

# Meta templates come in two shapes. The older one numbers its variables
# ({{1}}, {{2}}); the ones the Business Manager creates now name them
# ({{customer_name}}). Both are matched here, and `MessageTemplate.named` says
# which shape a template is, because the send payload differs.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# Meta rejects parameters that are empty or whitespace-only.
EMPTY_PARAM_FALLBACK = "-"

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

DEFAULT_TEMPLATES = [
    MessageTemplate(
        name="chits_details",
        language="en",
        body=CHITS_DETAILS_BODY,
        parameter_format="named",
        # Left pending on purpose: the sender refuses a template it has not
        # been told is approved, so nothing goes out until someone has checked
        # this against the Business Manager.
        status="pending",
        category="UTILITY",
    ),
    MessageTemplate(
        name="chit_receipt",
        language="en",
        body=CHIT_RECEIPT_BODY,
        footer="Regards,\nSrinidhi Chit Funds",
        # Link mode sends free text, so nothing needs Meta's approval. The same
        # wording has to be submitted as a template before the Cloud API can
        # use it.
        status="approved",
        category="UTILITY",
    ),
    MessageTemplate(
        name="invoice_document",
        language="en",
        body=(
            "Hello {{1}}, thank you for your business.\n\n"
            "Your invoice {{2}} for ₹{{3}} is attached.\n\n"
            "Please reach out if you have any questions."
        ),
        footer="Sunrise Traders",
        status="pending",
        category="UTILITY",
    ),
]


class TemplateStore:
    """Templates on disk, editable from the local UI."""

    def __init__(self, path: Path):
        self.path = path
        self._templates: dict[str, MessageTemplate] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._templates = {t.name: t for t in DEFAULT_TEMPLATES}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._templates = {
            item["name"]: MessageTemplate(**item) for item in raw.get("templates", [])
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"templates": [asdict(t) for t in self._templates.values()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, name: str) -> MessageTemplate | None:
        return self._templates.get(name)

    def put(self, template: MessageTemplate) -> None:
        self._templates[template.name] = template
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

    for token in template.placeholders:
        field_name = variable_map.get(token) or (token if template.named else "")
        value = (values.get(field_name) or "").strip()
        if not value:
            missing.append(field_name or f"{{{{{token}}}}}")
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
