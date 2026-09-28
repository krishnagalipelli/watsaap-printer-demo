"""Reviewed, repeatable sample checks. Never creates jobs or sends messages."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings, atomic_write_text, paths
from .extract import extract_fields
from .extract.profile import DocumentProfile
from .extract.rules import field_name
from .rules.gate import excluded_numbers

MIN_SAMPLES = 3


@dataclass
class Sample:
    path: Path
    kind: str
    values: dict[str, str] = field(default_factory=dict)
    recipient: str | None = ""


@dataclass
class Result:
    sample: Sample
    errors: list[str] = field(default_factory=list)


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.results) and not self.errors and all(not r.errors for r in self.results)

    def summary(self) -> str:
        lines = list(self.errors)
        lines += [f"{r.sample.path.name}: {error}" for r in self.results for error in r.errors]
        return "\n".join(lines) if lines else f"All {len(self.results)} reviewed samples passed. Nothing sent."


class Reader:
    """Reuse costly OCR only while PDF, rules and extraction settings match."""
    def __init__(self):
        self.cache = {}

    def read(self, path: Path, profile: DocumentProfile, settings: Settings):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = (digest, json.dumps(profile.to_dict(), sort_keys=True),
               repr(settings.ocr()), settings.default_country_code,
               tuple(sorted(excluded_numbers(settings))))
        if key not in self.cache:
            result = extract_fields(path, profile=profile, ocr=settings.ocr(),
                                    country_code=settings.default_country_code,
                                    excluded_numbers=excluded_numbers(settings))
            if len(self.cache) >= 64:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = result
        return deepcopy(self.cache[key])


def validate(samples: list[Sample], profile: DocumentProfile, settings: Settings,
             target: str | None = None, reader: Reader | None = None, templates=None) -> Report:
    reader = reader or Reader()
    report = Report()
    distinct = set()
    examples = set()
    for sample in samples:
        result = Result(sample)
        report.results.append(result)
        try:
            if target is not None and DocumentProfile.rules_key(sample.kind) == DocumentProfile.rules_key(target):
                distinct.add(hashlib.sha256(sample.path.read_bytes()).hexdigest())
                examples.add(json.dumps([sample.values, sample.recipient], sort_keys=True))
            fields = reader.read(sample.path, profile, settings)
            if not fields.readable:
                result.errors.append(fields.ocr_error or "No readable text.")
            if fields.classification_error or not fields.document_kind_verified:
                result.errors.append(fields.classification_error or "The two OCR reads disagree on document type.")
            expected = "receipt" if sample.kind == "_default" else sample.kind
            if fields.document_kind != expected:
                result.errors.append(f"Expected {expected}; read {fields.document_kind or 'unrecognised'}.")
            actual = fields.as_template_vars()
            for name, value in sample.values.items():
                if actual.get(name, "") != value:
                    result.errors.append(f"{name}: expected '{value}', read '{actual.get(name, '')}'.")
            from .models import Confidence
            if sum(c.confidence is Confidence.HIGH for c in fields.candidates) > 1:
                result.errors.append("Multiple equally likely recipients; check the phone rules.")
            best = fields.best
            got = best.e164 if best else ""
            if sample.recipient is not None and got != sample.recipient:
                result.errors.append(f"Recipient: expected {sample.recipient or 'none'}, read {got or 'none'}.")
            # Every taught value must have independent expected output. A blank
            # expected value is explicit, but cannot validate a newly taught rule.
            for rule in (profile.rules_for(sample.kind) if sample.recipient is not None else []):
                if rule.kind != "phone" and not sample.values.get(rule.name):
                    result.errors.append(f"Review an expected value for {rule.name}.")
                if rule.kind == "phone" and not sample.recipient:
                    result.errors.append("Review the expected customer mobile number.")
            if templates is not None:
                name = settings.document_templates.get(fields.document_kind or "")
                if fields.document_kind == "receipt":
                    name = name or settings.default_template
                if name:
                    from .send.templates import render, variable_map
                    template = templates.get(name)
                    if template is None:
                        result.errors.append(f"Message template '{name}' is missing.")
                    else:
                        message = render(template, variable_map(template, settings), fields,
                                         extra={"business_name": settings.business_name})
                        if message.missing:
                            result.errors.append("Message values missing: " + ", ".join(message.missing))
        except Exception as exc:
            result.errors.append(str(exc))
    if target is not None and len(distinct) < MIN_SAMPLES:
        report.errors.append(f"Review at least {MIN_SAMPLES} different PDFs of this type ({len(distinct)} provided).")
    if target is not None and len(distinct) >= MIN_SAMPLES and len(examples) < 2:
        report.errors.append("Use samples with different customer or field values, not repeated copies of one document.")
    return report


def unique_paths(pdfs: list[Path]) -> list[Path]:
    found = {}
    for pdf in pdfs:
        found.setdefault(hashlib.sha256(pdf.read_bytes()).hexdigest(), pdf)
    return list(found.values())


def saved_samples(except_kind: str | None = None) -> list[Sample]:
    samples = []
    root = paths().samples / "validation"
    for record in sorted(root.glob("*/*.json")):
        data = json.loads(record.read_text(encoding="utf-8"))
        if except_kind is not None and DocumentProfile.rules_key(data['kind']) == DocumentProfile.rules_key(except_kind):
            continue
        samples.append(Sample(record.with_suffix('.pdf'), data['kind'], data['values'], data['recipient']))
    # Old installations stored one reference PDF per type without field
    # annotations. Keep those as classification-only negative examples too.
    known = {DocumentProfile.rules_key(s.kind) for s in samples}
    for pdf in sorted(paths().samples.glob("*.pdf")):
        key = pdf.stem
        if key not in known and (except_kind is None or key != DocumentProfile.rules_key(except_kind)):
            samples.append(Sample(pdf, "receipt" if key == "_default" else key, recipient=None))
    return samples


def save_samples(samples: list[Sample]) -> None:
    for sample in samples:
        key = DocumentProfile.rules_key(sample.kind)
        if key != "_default" and field_name(key) != key:
            raise ValueError("Invalid document type for sample storage")
        content = sample.path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        folder = paths().samples / "validation" / key
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (digest + '.pdf')
        if not target.exists():
            target.write_bytes(content)
        data = {"kind": sample.kind, "values": sample.values, "recipient": sample.recipient}
        atomic_write_text(target.with_suffix('.json'), json.dumps(data, indent=2))
