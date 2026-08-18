from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test out of the real install directory."""
    monkeypatch.setenv("WAPRINTER_HOME", str(tmp_path / "waprinter"))
    yield tmp_path


@pytest.fixture
def settings():
    from waprinter.config import Settings

    return Settings(
        dry_run=True,
        own_numbers=["9845012345"],
        default_country_code="91",
    )


@pytest.fixture
def store(tmp_path):
    from waprinter.store import Store

    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


@pytest.fixture
def templates(tmp_path):
    from waprinter.send.templates import TemplateStore

    ts = TemplateStore(tmp_path / "templates.json")
    tpl = ts.get("invoice_document")
    tpl.status = "approved"  # tests exercise sending, not Meta's review queue
    ts.put(tpl)
    return ts


@pytest.fixture
def make_invoice(tmp_path):
    """Build a synthetic invoice PDF and return its path."""
    import invoice_factory

    counter = {"n": 0}

    def _make(spec=None, name: str | None = None) -> Path:
        counter["n"] += 1
        spec = spec or invoice_factory.InvoiceSpec()
        out = tmp_path / "pdfs" / (name or f"invoice_{counter['n']}.pdf")
        return invoice_factory.build(spec, out)

    return _make


@pytest.fixture
def pipeline(settings, store, templates, tmp_path):
    """The production flow: print, read the page, send."""
    from waprinter.pipeline import Pipeline
    from waprinter.send.dryrun import DryRunSender

    return Pipeline(
        settings=settings,
        store=store,
        sender=DryRunSender(tmp_path / "dry_run.jsonl"),
        templates=templates,
    )


@pytest.fixture
def confirm_pipeline(pipeline):
    """Confirmation mode — every print waits for a person.

    Not the default any more: these documents do print the customer's number.
    Kept for a client whose paperwork does not, and while tuning a new layout.
    """
    pipeline.settings.confirm_before_send = True
    return pipeline
