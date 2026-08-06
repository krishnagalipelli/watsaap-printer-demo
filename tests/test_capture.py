"""Spool capture.

The port writes to a fixed filename and the file appears before it is finished,
so the two things that must never happen are: claiming a half-written PDF, and
losing a job when two prints land back to back.
"""

from __future__ import annotations

from invoice_factory import InvoiceSpec, build

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
