"""Spool capture.

The port writes to a fixed filename and the file appears before it is finished,
so the two things that must never happen are: claiming a half-written PDF, and
losing a job when two prints land back to back.
"""

from __future__ import annotations

import pytest

from invoice_factory import InvoiceSpec, build, build_xps

from waprinter.capture import xps
from waprinter.capture.watcher import SpoolWatcher, claim, is_complete


def make_spool(tmp_path):
    spool = tmp_path / "spool"
    inbox = tmp_path / "inbox"
    spool.mkdir()
    inbox.mkdir()
    return spool, inbox


class TestCompletionDetection:
    def test_a_finished_pdf_is_complete(self, tmp_path):
        pdf = build(InvoiceSpec(), tmp_path / "job1.pdf")
        assert is_complete(pdf)

    def test_a_partial_write_is_not_complete(self, tmp_path):
        source = build(InvoiceSpec(), tmp_path / "full.pdf")
        partial = tmp_path / "job1.pdf"
        # Everything except the trailing %%EOF, as a job mid-render would be.
        partial.write_bytes(source.read_bytes()[: -len(b"%%EOF") - 20])
        assert not is_complete(partial)

    def test_an_empty_file_is_not_complete(self, tmp_path):
        empty = tmp_path / "job1.pdf"
        empty.touch()
        assert not is_complete(empty)

    def test_a_missing_file_is_not_complete(self, tmp_path):
        assert not is_complete(tmp_path / "nope.pdf")


class TestClaim:
    def test_moves_the_file_out_of_the_spool(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        pdf = build(InvoiceSpec(), spool / "job1.pdf")

        claimed = claim(pdf, inbox)

        assert claimed is not None
        assert claimed.parent == inbox
        assert not pdf.exists(), "the port must be freed for the next job"
        assert claimed.read_bytes()[:5] == b"%PDF-"

    def test_refuses_a_partial_file(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        partial = spool / "job1.pdf"
        partial.write_bytes(b"%PDF-1.7 partial")

        assert claim(partial, inbox) is None
        assert partial.exists()

    def test_claimed_names_do_not_collide(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        first = claim(build(InvoiceSpec(), spool / "job1.pdf"), inbox)
        second = claim(build(InvoiceSpec(), spool / "job1.pdf"), inbox)

        assert first != second
        assert len(list(inbox.glob("*.pdf"))) == 2


class TestWatcher:
    def test_waits_for_the_file_to_settle(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        seen = []
        watcher = SpoolWatcher(spool, inbox, seen.append, settle_seconds=999)
        build(InvoiceSpec(), spool / "job1.pdf")

        # First pass records the size; with a long settle window nothing is
        # claimed on the pass that follows either.
        assert watcher.drain_once() == []
        assert watcher.drain_once() == []
        assert seen == []

    def test_captures_a_settled_file(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        seen = []
        watcher = SpoolWatcher(spool, inbox, seen.append, settle_seconds=0)
        build(InvoiceSpec(), spool / "job1.pdf")

        watcher.drain_once()          # observe the size
        captured = watcher.drain_once()  # settled, so claim it

        assert len(captured) == 1
        assert seen == captured

    def test_drains_every_port_in_one_pass(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        seen = []
        watcher = SpoolWatcher(spool, inbox, seen.append, settle_seconds=0)
        for n in (1, 2, 3, 4):
            build(InvoiceSpec(), spool / f"job{n}.pdf")

        watcher.drain_once()
        captured = watcher.drain_once()

        assert len(captured) == 4
        assert len(seen) == 4
        assert list(spool.glob("*.pdf")) == []

    def test_a_failing_job_does_not_stop_the_others(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        seen = []

        def explode(path):
            seen.append(path)
            raise RuntimeError("pipeline blew up")

        watcher = SpoolWatcher(spool, inbox, explode, settle_seconds=0)
        build(InvoiceSpec(), spool / "job1.pdf")
        build(InvoiceSpec(), spool / "job2.pdf")

        watcher.drain_once()
        captured = watcher.drain_once()

        assert len(captured) == 2
        assert len(seen) == 2

    def test_forgets_files_that_vanish(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        watcher = SpoolWatcher(spool, inbox, lambda _p: None, settle_seconds=999)
        pdf = build(InvoiceSpec(), spool / "job1.pdf")

        watcher.drain_once()
        assert watcher._sizes
        pdf.unlink()
        watcher.drain_once()
        assert not watcher._sizes


class TestThePortIsFreedWhileADocumentIsBeingSent:
    """Every print lands on the same filename, so the port has to be emptied
    whatever the app is doing with the print before it.

    Freeing the port and sending the document used to be one thread. While a
    receipt was uploading, the next print waited in the spool and the one
    after that replaced it, with nothing to show a print had been lost.
    """

    def test_a_print_is_staged_without_being_processed(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        seen = []
        watcher = SpoolWatcher(spool, inbox, seen.append, settle_seconds=0)
        build(InvoiceSpec(), spool / "job1.pdf")

        watcher.claim_finished()
        staged = watcher.claim_finished()

        assert len(staged) == 1 and staged[0].parent == watcher.staging
        assert list(spool.glob("*.pdf")) == [], "the port was not freed"
        assert seen == [] and list(inbox.glob("*.pdf")) == []

    def test_the_port_is_freed_while_the_lock_is_held(self, tmp_path):
        # The lock is what a send, and an update being installed, hold.
        import threading

        spool, inbox = make_spool(tmp_path)
        lock = threading.RLock()
        watcher = SpoolWatcher(spool, inbox, lambda _p: None, settle_seconds=0,
                               operation_lock=lock)
        build(InvoiceSpec(), spool / "job1.pdf")
        held, release = threading.Event(), threading.Event()

        def hold():
            with lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert held.wait(5)
            watcher.claim_finished()
            assert len(watcher.claim_finished()) == 1
        finally:
            release.set()
            holder.join()

    def test_three_prints_during_one_slow_send_are_all_captured(self, tmp_path):
        import threading
        import time

        spool, inbox = make_spool(tmp_path)
        seen = []
        sending, finish = threading.Event(), threading.Event()

        def slow_send(path):
            seen.append(path)
            sending.set()
            assert finish.wait(10)

        watcher = SpoolWatcher(spool, inbox, slow_send, poll_interval=0.01,
                               settle_seconds=0)
        thread = threading.Thread(target=watcher.run, daemon=True)
        thread.start()
        try:
            build(InvoiceSpec(invoice_number="INV-1"), spool / "job1.pdf")
            assert sending.wait(10), "the first print was never processed"
            # The first is still being sent. Two more arrive on the same port,
            # each only once the one before it has been moved out.
            for number in ("INV-2", "INV-3"):
                build(InvoiceSpec(invoice_number=number), spool / "job1.pdf")
                deadline = time.monotonic() + 10
                while (spool / "job1.pdf").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert not (spool / "job1.pdf").exists(), (
                    f"{number} was left in the spool behind the send")
            assert len(seen) == 1
            finish.set()
            deadline = time.monotonic() + 10
            while len(seen) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            finish.set()
            watcher.stop()
            thread.join(5)

        assert len(seen) == 3
        assert all(path.parent == inbox and path.exists() for path in seen)

    def test_prints_staged_when_the_app_stopped_are_processed_on_the_next_start(
        self, tmp_path
    ):
        spool, inbox = make_spool(tmp_path)
        before = SpoolWatcher(spool, inbox, lambda _p: None, settle_seconds=0)
        build(InvoiceSpec(), spool / "job1.pdf")
        before.claim_finished()
        assert before.claim_finished()
        # ...and the app stops here, before the print was dealt with.

        seen = []
        after = SpoolWatcher(spool, inbox, seen.append, settle_seconds=0)
        captured = after.process_staged()

        assert len(captured) == 1 and seen == captured
        assert captured[0].parent == inbox
        assert list(after.staging.iterdir()) == []

    def test_staged_prints_are_processed_in_the_order_they_were_printed(self, tmp_path):
        import os

        spool, inbox = make_spool(tmp_path)
        seen = []
        watcher = SpoolWatcher(spool, inbox, seen.append, settle_seconds=0)
        watcher.staging.mkdir()
        # Named against their print order, as two prints in one second can be.
        first = build(InvoiceSpec(), watcher.staging / "20260101-090000-ffffffff.pdf")
        second = build(InvoiceSpec(), watcher.staging / "20260101-090000-00000000.pdf")
        os.utime(first, (1_000_000, 1_000_000))
        os.utime(second, (1_000_001, 1_000_001))

        watcher.process_staged()

        assert [p.name for p in seen] == [first.name, second.name]


class TestWindows7CapturesXps:
    """Windows 7 has no PDF driver, so its port writes XPS instead.

    Everything past the watcher reads PDFs, so the conversion happens here or
    not at all -- see waprinter/capture/xps.py.
    """

    def test_a_finished_xps_is_complete(self, tmp_path):
        assert is_complete(build_xps(tmp_path / "job1.xps"))

    def test_a_half_written_xps_is_not_complete(self, tmp_path):
        # A ZIP is unreadable until its central directory lands, which the
        # spooler writes last. Checking for %%EOF would hold the job for ever.
        whole = build_xps(tmp_path / "whole.xps").read_bytes()
        partial = tmp_path / "job1.xps"
        partial.write_bytes(whole[: len(whole) // 2])
        assert not is_complete(partial)

    def test_claiming_keeps_the_suffix(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        claimed = claim(build_xps(spool / "job1.xps"), inbox)
        assert claimed is not None and claimed.suffix == ".xps"

    def test_an_xps_print_is_handed_on_as_a_pdf(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        build_xps(spool / "job1.xps")
        seen = []
        watcher = SpoolWatcher(spool, inbox, seen.append, settle_seconds=0)
        watcher.drain_once()
        captured = watcher.drain_once()

        assert [p.suffix for p in captured] == [".pdf"]
        assert seen == captured
        assert captured[0].exists()
        assert not list(spool.glob("*.xps")), "the spool file was not drained"
        assert not list(inbox.glob("*.xps")), "the xps was left behind"

    def test_pdf_capture_still_works_alongside_it(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        build(InvoiceSpec(), spool / "job1.pdf")
        build_xps(spool / "job2.xps")
        watcher = SpoolWatcher(spool, inbox, lambda _p: None, settle_seconds=0)
        watcher.drain_once()
        captured = watcher.drain_once()
        assert sorted(p.suffix for p in captured) == [".pdf", ".pdf"]

    def test_an_unconvertible_print_is_kept_not_dropped(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        # Right extension, not a ZIP: whatever this is, it must not vanish.
        (spool / "job1.xps").write_bytes(b"rubbish PK\x05\x06")
        seen = []
        watcher = SpoolWatcher(spool, inbox, seen.append, settle_seconds=0)
        watcher.drain_once()
        captured = watcher.drain_once()

        assert captured == [] and seen == []
        assert list(inbox.glob("*.xps")), "the unreadable print was lost"

    def test_an_unconvertible_print_is_reported_not_only_logged(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        (spool / "job1.xps").write_bytes(b"rubbish PK\x05\x06")
        reported = []
        watcher = SpoolWatcher(
            spool, inbox, lambda _p: None, settle_seconds=0,
            on_unreadable=lambda path, reason: reported.append((path, reason)),
        )
        watcher.drain_once()
        watcher.drain_once()

        assert len(reported) == 1
        path, reason = reported[0]
        assert path.parent == inbox and path.suffix == ".xps"
        assert reason

    def test_a_failing_report_does_not_stop_the_watcher(self, tmp_path):
        spool, inbox = make_spool(tmp_path)
        (spool / "job1.xps").write_bytes(b"rubbish PK\x05\x06")
        build(InvoiceSpec(), spool / "job2.pdf")

        def boom(_path, _reason):
            raise RuntimeError("database is locked")

        watcher = SpoolWatcher(spool, inbox, lambda _p: None, settle_seconds=0,
                               on_unreadable=boom)
        watcher.drain_once()
        assert [p.suffix for p in watcher.drain_once()] == [".pdf"]


class TestAnUnconvertiblePrintReachesTheQueue:
    """It used to leave nothing but a log line: no job, nothing in Needs
    attention, and a file the daily tidy-up never looked at."""

    def test_it_is_a_failed_job_waiting_on_a_person(self, pipeline, tmp_path):
        from waprinter.models import JobStatus

        bad = tmp_path / "inbox" / "20261017-101010-abcd1234.xps"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"rubbish")

        job = pipeline.record_unreadable_capture(bad, "cannot recognize zip archive")

        assert job.status is JobStatus.FAILED
        assert "could not be converted" in job.error
        assert str(bad) in job.error
        assert job.id in {j.id for j in pipeline.store.pending()}

    def test_the_tidy_up_keeps_it_while_unresolved_then_prunes_it(
        self, pipeline, tmp_path
    ):
        import os
        import time

        from waprinter import housekeeping

        inbox = tmp_path / "inbox"
        inbox.mkdir(exist_ok=True)
        bad = inbox / "old.xps"
        bad.write_bytes(b"rubbish")
        old = time.time() - 200 * 86_400
        os.utime(bad, (old, old))
        job = pipeline.record_unreadable_capture(bad, "broken")

        protected = pipeline.store.unresolved_pdf_paths()
        assert housekeeping.prune_inbox(inbox, 90, protected) == 0
        assert bad.exists()

        pipeline.discard(job.id)
        protected = pipeline.store.unresolved_pdf_paths()
        assert housekeeping.prune_inbox(inbox, 90, protected) == 1
        assert not bad.exists()


class TestXpsConversion:
    def test_converts_every_page(self, tmp_path):
        import pymupdf

        pdf = xps.to_pdf(build_xps(tmp_path / "job.xps", pages=3))
        doc = pymupdf.open(str(pdf))
        try:
            assert doc.is_pdf and doc.page_count == 3
        finally:
            doc.close()

    def test_removes_the_xps_it_converted(self, tmp_path):
        source = build_xps(tmp_path / "job.xps")
        xps.to_pdf(source)
        assert not source.exists()

    def test_refuses_something_that_is_not_an_xps(self, tmp_path):
        source = tmp_path / "job.xps"
        source.write_bytes(b"not a package")
        with pytest.raises(ValueError):
            xps.to_pdf(source)
        assert source.exists(), "the original must be left to look at"
