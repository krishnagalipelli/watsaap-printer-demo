"""Teach the reader a new kind of document from one sample PDF.

The setup screen shows the sample, the person clicks the values that matter
and names them, and this module turns that into configuration: a document
type recognised by its title, and field rules that find each value by the
label printed beside it. No code changes and no release -- a fourth kind of
paperwork is a profile.json edit made by clicking.

It also answers the questions worth asking before saving, while the person
who clicked is still looking:

* Does the title picked for this type appear on any *other* type's sample?
  Then those documents would start going out under this type's message.
* What would the new rules read off the last few dozen real prints? A rule
  that finds the right value on the sample and the wrong one everywhere else
  is caught here instead of at the counter.

Nothing in here touches Tk, so all of it is tested without a display.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, paths
from .extract import pdf_text
from .extract.profile import DEFAULT_KIND, DocumentKind, DocumentProfile, FieldRule
from .extract.rules import BUILTIN_FIELDS, field_name, read_rule

log = logging.getLogger(__name__)

# What the document types are called on screen. Receipts are the default
# rather than a recognised title: see profile.DEFAULT_KIND.
DEFAULT_LABEL = "Receipts"

# How many recent prints a trial reads. Enough to see a pattern, few enough
# to answer while the person waits.
TRIAL_LIMIT = 30

# A title shorter than this matches too much ("to", "no.").
_MIN_TITLE = 5


def kind_label(name: str | None) -> str:
    key = DocumentProfile.rules_key(name)
    if key == DEFAULT_KIND:
        return DEFAULT_LABEL
    return key.replace("_", " ").capitalize()


def sample_path(kind: str | None) -> Path:
    return paths().samples / f"{DocumentProfile.rules_key(kind)}.pdf"


def read_sample(pdf: Path, settings: Settings | None = None) -> pdf_text.Document:
    """The sample's words with their positions, OCR'd if it is a scan."""
    settings = settings or Settings()
    return pdf_text.read(pdf, ocr=settings.ocr())


def document_text(doc: pdf_text.Document) -> str:
    return " ".join(row.text for row in doc.rows)


# -- choosing a title --------------------------------------------------------


# Sample text by path, kept while the file is unchanged. The title check runs
# on every keystroke in the teaching window, and re-reading each stored PDF
# each time made typing lag.
_SAMPLE_TEXT: dict[Path, tuple[int, str]] = {}


def _sample_text(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _SAMPLE_TEXT.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        text = document_text(pdf_text.read(path)).lower()
    except Exception:
        log.info("could not read sample %s", path, exc_info=True)
        return None
    _SAMPLE_TEXT[path] = (stamp, text)
    return text


def _other_samples(profile: DocumentProfile, except_kind: str | None) -> dict[str, str]:
    """Text of every stored sample except the one for `except_kind`.

    A blank `except_kind` means a type that does not exist yet, which has no
    sample of its own to skip -- not the default type, which is what a blank
    kind means everywhere else.
    """
    skip = DocumentProfile.rules_key(except_kind) if except_kind else None
    texts: dict[str, str] = {}
    for kind in [DEFAULT_KIND, *(k.name for k in profile.all_kinds)]:
        key = DocumentProfile.rules_key(kind)
        if key == skip or key in texts:
            continue
        text = _sample_text(sample_path(key))
        if text is not None:
            texts[key] = text
    return texts


def suggest_titles(
    doc: pdf_text.Document, profile: DocumentProfile, except_kind: str = ""
) -> list[str]:
    """Lines near the top of page 1 that could identify this document.

    Taller text first, because a title is set larger than the body. Anything
    that also appears on another type's sample is left out: it would identify
    both.
    """
    if not doc.pages:
        return []
    page = doc.pages[0]
    others = _other_samples(profile, except_kind)
    scored: list[tuple[float, float, str]] = []
    for line in page.lines:
        text = re.sub(r"\s+", " ", line.text).strip(" :,.-")
        if len(text) < _MIN_TITLE or len(text) > 60:
            continue
        if line.y0 > page.height * 0.45:
            continue
        letters = sum(c.isalpha() for c in text)
        if letters < len(text) * 0.6:
            continue  # mostly digits: a number or a date, not a title
        if any(text.lower() in other for other in others.values()):
            continue
        height = line.y1 - line.y0
        scored.append((-height, line.y0, text))
    seen: list[str] = []
    for _h, _y, text in sorted(scored):
        if text.lower() not in (s.lower() for s in seen):
            seen.append(text)
    return seen[:6]


def title_problems(
    phrase: str,
    doc: pdf_text.Document,
    profile: DocumentProfile,
    kind_name: str,
) -> list[str]:
    """Why this title would not identify this document, and only it."""
    phrase = phrase.strip()
    problems: list[str] = []
    if len(phrase) < _MIN_TITLE:
        problems.append("The title is too short to identify a document reliably.")
        return problems
    if phrase.lower() not in document_text(doc).lower():
        problems.append(f"'{phrase}' does not appear on this sample.")
    for key, text in _other_samples(profile, kind_name).items():
        if phrase.lower() in text:
            problems.append(
                f"'{phrase}' also appears on the {kind_label(key).lower()} "
                f"sample, so those would be read as this type."
            )
    for kind in profile.all_kinds:
        if kind.name == kind_name:
            continue
        for existing in kind.match:
            if existing and existing.lower() in phrase.lower():
                problems.append(
                    f"'{existing}' already identifies {kind_label(kind.name).lower()}."
                )
    return problems


def name_problems(display: str, profile: DocumentProfile, editing: str = "") -> list[str]:
    """Why a new type cannot be called this."""
    key = field_name(display)
    if not display.strip():
        return ["Give the document type a name."]
    if key in (DEFAULT_KIND.strip("_"), "receipt", "default"):
        return [f"'{display}' is reserved. Choose another name."]
    if key != editing and any(k.name == key for k in profile.all_kinds):
        return [f"There is already a document type called '{display}'."]
    return []


# -- checking and saving the fields -------------------------------------------


def check_rules(doc: pdf_text.Document, rules: list[FieldRule]) -> dict[str, str]:
    """What each rule reads off the sample, "" where it reads nothing."""
    return {rule.name: read_rule(doc, rule) or "" for rule in rules}


def rule_problems(
    doc: pdf_text.Document, rules: list[FieldRule], clicked: dict[str, str]
) -> list[str]:
    """Rules that would not read back what was clicked on the sample.

    The usual cause is a label that appears twice -- "Date" in a heading and
    again beside the value -- where the first one wins.
    """
    problems = []
    read = check_rules(doc, rules)
    for rule in rules:
        want = clicked.get(rule.name, "")
        got = read.get(rule.name, "")
        if rule.kind == "phone":
            want_digits = re.sub(r"\D", "", want)
            if want_digits and want_digits not in re.sub(r"\D", "", got):
                problems.append(
                    f"The mobile number label '{rule.label}' does not lead to "
                    f"{want} on this page."
                )
            continue
        if want and got != want:
            problems.append(
                f"{rule.name.replace('_', ' ').capitalize()}: the label "
                f"'{rule.label or '(none)'}' reads '{got or 'nothing'}' on this "
                f"page, not '{want}'. Click the value again, or rename the label."
            )
    return problems


def save_teaching(
    kind_name: str,
    rules: list[FieldRule],
    sample: Path | None = None,
    new_kind: DocumentKind | None = None,
) -> DocumentProfile:
    """Store a taught type and its fields. Returns the profile as saved.

    A new type goes in front of the others, so it is tried first: its title
    was chosen to identify it, and a scan of it may also carry phrases an
    older type matches on.
    """
    profile_path = paths().profile
    current = DocumentProfile.load(profile_path)
    customs = list(current.custom_kinds)
    if new_kind is not None:
        replaced = False
        for index, existing in enumerate(customs):
            if existing.name == new_kind.name:
                customs[index] = new_kind
                replaced = True
        if not replaced:
            customs.insert(0, new_kind)

    key = DocumentProfile.rules_key(kind_name)
    rules_by_kind = {k: list(v) for k, v in current.field_rules.items()}
    rules_by_kind[key] = list(rules)
    DocumentProfile.write_taught(profile_path, customs, rules_by_kind)

    if sample is not None:
        target = sample_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(sample).resolve() != target.resolve():
            shutil.copy2(sample, target)
    return DocumentProfile.load(profile_path)


def candidate_profile(
    base: DocumentProfile,
    kind_name: str,
    rules: list[FieldRule],
    new_kind: DocumentKind | None = None,
) -> DocumentProfile:
    """`base` as it would be after saving, without saving anything.

    What "Try on recent prints" reads with, so a rule can be tested against
    real prints before it can affect one.
    """
    from dataclasses import replace

    customs = [k for k in base.custom_kinds
               if new_kind is None or k.name != new_kind.name]
    if new_kind is not None:
        customs.insert(0, new_kind)
    rules_by_kind = {k: list(v) for k, v in base.field_rules.items()}
    rules_by_kind[DocumentProfile.rules_key(kind_name)] = list(rules)
    return replace(base, custom_kinds=customs, field_rules=rules_by_kind)


def remove_kind(kind_name: str) -> DocumentProfile:
    """Forget a taught document type, its fields and its sample."""
    profile_path = paths().profile
    current = DocumentProfile.load(profile_path)
    from .extract.rules import field_name
    if field_name(kind_name) != kind_name:
        raise ValueError("Invalid document type")
    validation_dir = paths().samples / "validation" / kind_name
    if validation_dir.exists():
        shutil.rmtree(validation_dir)
    customs = [k for k in current.custom_kinds if k.name != kind_name]
    rules = {k: v for k, v in current.field_rules.items() if k != kind_name}
    DocumentProfile.write_taught(profile_path, customs, rules)
    try:
        sample_path(kind_name).unlink()
    except OSError:
        pass
    return DocumentProfile.load(profile_path)


def forget_field(kind_name: str, name: str) -> DocumentProfile:
    current = DocumentProfile.load(paths().profile)
    rules = [r for r in current.rules_for(kind_name) if r.name != name]
    return save_teaching(kind_name, rules)


# -- trying it out ------------------------------------------------------------


@dataclass
class TrialRow:
    file: str
    kind: str
    values: dict[str, str] = field(default_factory=dict)
    mobile: str = ""
    error: str = ""


def recent_prints(limit: int = TRIAL_LIMIT) -> list[Path]:
    """The most recent captured PDFs, newest first."""
    inbox = paths().inbox
    if not inbox.is_dir():
        return []
    pdfs = [p for p in inbox.glob("*.pdf") if p.is_file()]
    pdfs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[:limit]


def trial(
    profile: DocumentProfile,
    pdfs: list[Path],
    settings: Settings,
) -> list[TrialRow]:
    """Read each PDF with `profile` and report what came out. Sends nothing.

    Uses the same OCR and classification path as live extraction.
    """
    from .extract import extract_fields
    from .rules.gate import excluded_numbers

    rows: list[TrialRow] = []
    excluded = excluded_numbers(settings)
    for pdf in pdfs:
        try:
            fields = extract_fields(
                pdf,
                excluded_numbers=excluded,
                country_code=settings.default_country_code,
                ocr=settings.ocr(),
                profile=profile,
            )
        except Exception as exc:
            rows.append(TrialRow(file=pdf.name, kind="", error=str(exc)))
            continue
        if not fields.readable:
            rows.append(TrialRow(file=pdf.name, kind="", error=fields.ocr_error or "No readable text"))
            continue
        best = fields.best
        rows.append(
            TrialRow(
                file=pdf.name,
                kind=kind_label(fields.document_kind),
                values={k: v for k, v in fields.as_template_vars().items() if v},
                mobile=f"{best.e164} ({best.confidence})" if best else "",
                error=fields.classification_error or ("Document type unrecognised" if fields.document_kind is None else ""),
            )
        )
    return rows


# -- values to show while mapping ------------------------------------------


def example_values(store, profile: DocumentProfile, kind_name: str,
                   settings: Settings | None = None) -> dict[str, str]:
    """Real values for one document type, to show beside each field.

    The most recent print of that type if there is one -- already read, so
    free -- otherwise the stored sample, read now.
    """
    key = DocumentProfile.rules_key(kind_name)
    for job in store.recent(200):
        if DocumentProfile.rules_key(job.fields.document_kind) == key:
            values = job.fields.as_template_vars()
            if any(values.values()):
                return values
    path = sample_path(key)
    if path.exists():
        from .extract import extract_fields

        try:
            fields = extract_fields(path, profile=profile, ocr=None)
            return fields.as_template_vars()
        except Exception:
            log.info("could not read sample %s", path, exc_info=True)
    return {}


def available_fields(profile: DocumentProfile, kind_name: str) -> list[str]:
    """Every field a template for this document type can be filled from."""
    names = list(BUILTIN_FIELDS)
    for rule in profile.rules_for(kind_name):
        if rule.kind != "phone" and rule.name not in names:
            names.append(rule.name)
    return names
