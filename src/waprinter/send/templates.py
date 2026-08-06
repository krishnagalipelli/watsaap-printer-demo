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

PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")

# Meta rejects parameters that are empty or whitespace-only.
EMPTY_PARAM_FALLBACK = "-"


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

    @property
    def placeholders(self) -> list[str]:
        """Variable positions used in the body, in order, deduplicated."""
        seen, out = set(), []
        for m in PLACEHOLDER.finditer(self.body):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(m.group(1))
        return out

    @property
    def usable(self) -> bool:
        return self.status == "approved" and self.header_document


DEFAULT_TEMPLATES = [
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
    preview: str = ""       # what the customer will actually read
    filename: str = "document.pdf"
    missing: list[str] = field(default_factory=list)  # variables with no value


def render(
    template: MessageTemplate,
    variable_map: dict[str, str],
    fields: ExtractedFields,
    doc_title: str | None = None,
) -> RenderedMessage:
    """Resolve a template's variables from the extracted invoice fields.

    `variable_map` maps a placeholder position to a field name, e.g.
    {"1": "customer_name", "2": "invoice_number"}.
    """
    values = fields.as_template_vars()
    parameters: list[str] = []
    missing: list[str] = []

    for position in template.placeholders:
        field_name = variable_map.get(position, "")
        value = (values.get(field_name) or "").strip()
        if not value:
            missing.append(field_name or f"{{{{{position}}}}}")
            value = EMPTY_PARAM_FALLBACK
        parameters.append(value)

    def substitute(m: re.Match[str]) -> str:
        position = m.group(1)
        try:
            return parameters[template.placeholders.index(position)]
        except (ValueError, IndexError):
            return m.group(0)

    preview = PLACEHOLDER.sub(substitute, template.body)
    if template.footer:
        preview = f"{preview}\n\n{template.footer}"

    return RenderedMessage(
        template=template,
        parameters=parameters,
        preview=preview,
        filename=_filename(fields, doc_title),
        missing=missing,
    )


def _filename(fields: ExtractedFields, doc_title: str | None) -> str:
    """What the PDF is called on the customer's phone."""
    if fields.invoice_number:
        stem = f"Invoice-{fields.invoice_number}"
    elif doc_title:
        stem = doc_title
    else:
        stem = "Document"
    # WhatsApp shows this verbatim; keep it filesystem- and eye-friendly.
    stem = re.sub(r"[^A-Za-z0-9._\- ]+", "", stem).strip() or "Document"
    return f"{stem[:60]}.pdf"
