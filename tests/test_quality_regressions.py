"""Regression cases from the code-quality and operator-flow audit."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from waprinter import housekeeping, update
from waprinter.agent import Agent
from waprinter.config import Settings, paths
from waprinter.models import JobStatus, PrintJob, SendResult
from waprinter.ui.viewmodel import counters_for_today


class RecordingSender:
    def __init__(self):
        self.calls = 0

    def send(self, *args):
        self.calls += 1
        return SendResult(ok=True, wamid="recorded")


def test_cleanup_preserves_every_unresolved_file_beyond_queue_limit(store):
    p = paths()
    p.ensure()
    now = datetime.now()
    statuses = (JobStatus.HELD, JobStatus.FAILED, JobStatus.AWAITING,
                JobStatus.READY, JobStatus.CAPTURED, JobStatus.QUEUED)
    expected = []
    for i in range(236):
        pdf = p.inbox / f"{i}.pdf"
        pdf.write_bytes(b"document")
        old = time.time() - 100 * 86400
        os.utime(pdf, (old, old))
        expected.append(pdf)
        store.upsert(PrintJob(id=str(i), created_at=now + timedelta(seconds=i),
                              pdf_path=pdf, status=JobStatus.HELD if i < 230 else statuses[i - 230]))
    resolved = p.inbox / "sent.pdf"
    resolved.write_bytes(b"sent")
    os.utime(resolved, (old, old))
    store.upsert(PrintJob(id="sent", created_at=now, pdf_path=resolved, status=JobStatus.SENT))
    assert len(store.pending()) == 200
    assert store.pending_count() > 200
    assert len(store.unresolved_pdf_paths()) == 236
    assert housekeeping.run(p, Settings(keep_inbox_days=90), store) == 1
    assert all(pdf.exists() for pdf in expected)
    assert not resolved.exists()


@pytest.mark.parametrize("manual", [False, True])
def test_test_send_does_not_suppress_a_real_send(pipeline, make_invoice, manual):
    pdf = make_invoice()
    first = pipeline.process(pdf)
    assert first.status == JobStatus.DRY_RUN
    sender = RecordingSender()
    pipeline.sender = sender
    pipeline.settings.dry_run = False
    live = (pipeline.release(first.id, first.recipient) if manual else pipeline.process(pdf))
    assert live.status == JobStatus.SENT
    assert sender.calls == 1
    # Real duplicate protection must still work after going live.
    assert pipeline.process(pdf).status == JobStatus.DUPLICATE
    assert sender.calls == 1
    assert counters_for_today(pipeline.store, pipeline.settings).sent == 1


def test_failed_job_stays_actionable_and_can_be_retried(pipeline, make_invoice):
    class FailedSender:
        def send(self, *args):
            return SendResult(ok=False, error="Connection lost; check WhatsApp first")

    pipeline.sender = FailedSender()
    job = pipeline.process(make_invoice())
    assert job.status == JobStatus.FAILED
    assert pipeline.store.pending_count() == 1
    assert pipeline.store.pending()[0].error == job.error
    assert job.pdf_path in pipeline.store.unresolved_pdf_paths()
    pipeline.sender = RecordingSender()
    retried = pipeline.release(job.id, job.recipient)
    assert retried.status == JobStatus.DRY_RUN
    assert retried.error is None
    assert pipeline.store.pending_count() == 0


@pytest.mark.parametrize("failure", ["sender", "save"])
def test_rejected_settings_leave_both_disk_and_runtime_unchanged(pipeline, monkeypatch, failure):
    pipeline.settings.save()
    before_disk = paths().settings.read_text()
    before = deepcopy(pipeline.settings)
    sender = pipeline.sender
    candidate = deepcopy(before)
    candidate.dry_run = False

    def fail(*args, **kwargs):
        raise OSError("cannot apply")

    monkeypatch.setattr("waprinter.pipeline.build_sender", fail if failure == "sender" else lambda s: RecordingSender())
    if failure == "save":
        monkeypatch.setattr(Settings, "save", fail)
    with pytest.raises(OSError, match="cannot apply"):
        pipeline.apply_settings(candidate)
    assert pipeline.settings == before
    assert pipeline.sender is sender
    assert paths().settings.read_text() == before_disk


@pytest.fixture
def updater(pipeline, monkeypatch, tmp_path):
    agent = Agent.__new__(Agent)
    agent.pipeline = pipeline
    agent.settings = pipeline.settings
    agent._busy = threading.Event()
    monkeypatch.setattr(update, "check", lambda *a: update.CheckResult(
        available=True, release=update.Release("9.9.9", "https://example.test/setup.exe")))
    monkeypatch.setattr(update, "download", lambda *a: tmp_path / "setup.exe")
    return agent


def test_send_starting_during_download_defers_install(updater, pipeline, make_invoice, monkeypatch):
    sending = threading.Event()
    finish = threading.Event()
    installed = []
    pdf = make_invoice()

    class SlowSender:
        def send(self, *args):
            sending.set()
            assert finish.wait(5)
            return SendResult(ok=True)

    pipeline.sender = SlowSender()
    with ThreadPoolExecutor() as pool:
        futures = []

        def download(*args):
            futures.append(pool.submit(pipeline.process_document, pdf))
            assert sending.wait(5)
            return Path("setup.exe")

        monkeypatch.setattr(update, "download", download)
        monkeypatch.setattr(update, "install", lambda p: installed.append(p))
        try:
            assert "being sent" in updater.check_updates()
            assert installed == []
            before = deepcopy(pipeline.settings)
            with pytest.raises(RuntimeError, match="in progress"):
                pipeline.apply_settings(deepcopy(before))
            assert pipeline.settings == before
        finally:
            finish.set()
        assert futures[0].result(5)[0].status == JobStatus.DRY_RUN


@pytest.mark.parametrize("source", ["queue", "spool"])
def test_installation_excludes_queue_send_and_spool_claim(updater, pipeline, make_invoice, monkeypatch, source):
    from waprinter.capture.watcher import SpoolWatcher

    installing = threading.Event()
    finish = threading.Event()
    attempted = threading.Event()
    sent = threading.Event()

    class Sender:
        def send(self, *args):
            sent.set()
            return SendResult(ok=True)

    pipeline.sender = Sender()
    pdf = make_invoice()
    if source == "queue":
        pipeline.settings.confirm_before_send = True
        job = pipeline.process(pdf)
        work = lambda: pipeline.release(job.id, "+919876543210")
    else:
        p = paths()
        p.ensure()
        spool = p.spool / "job1.pdf"
        spool.write_bytes(pdf.read_bytes())
        watcher = SpoolWatcher(p.spool, p.inbox, pipeline.process_document,
                               operation_lock=pipeline.operation_lock)
        watcher._sizes[spool] = (spool.stat().st_size, time.monotonic() - 2)
        work = watcher.drain_once

    def install(*args):
        installing.set()
        assert finish.wait(5)

    def start_work():
        attempted.set()
        return work()

    monkeypatch.setattr(update, "install", install)
    with ThreadPoolExecutor() as pool:
        installation = pool.submit(updater.check_updates)
        assert installing.wait(5)
        document = pool.submit(start_work)
        try:
            assert attempted.wait(5)
            assert not sent.wait(0.1)
            if source == "spool":
                assert spool.exists(), "the PDF must remain in the spool until installation ends"
            assert "already in progress" in updater.check_updates()
        finally:
            finish.set()
        assert "finished" in installation.result(5)
        document.result(5)
        assert sent.is_set()


def test_cancelled_install_releases_processing_lock(updater, pipeline, make_invoice, monkeypatch):
    def cancelled(*args):
        raise OSError("cancelled")

    monkeypatch.setattr(update, "install", cancelled)
    assert "cancelled" in updater.check_updates()
    with ThreadPoolExecutor() as pool:
        assert pool.submit(pipeline.process, make_invoice()).result(5).status == JobStatus.DRY_RUN


def test_installer_waits_and_reports_failure(monkeypatch):
    from unittest.mock import Mock

    process = Mock()
    process.wait.return_value = 2
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: process)
    with pytest.raises(RuntimeError, match="code 2"):
        update.install(Path("setup.exe"))
    process.wait.assert_called_once()
