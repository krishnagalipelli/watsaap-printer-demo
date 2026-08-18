"""The popup that appears after a print.

On a normal print this is the only thing the operator sees, so what it says has
to be right and readable without training. Skipped where there is no display.
"""

from __future__ import annotations

import pytest
from invoice_factory import InvoiceSpec

from waprinter.models import JobStatus
from waprinter.ui.result_popup import describe

tk = pytest.importorskip("tkinter")


@pytest.fixture(scope="session")
def tk_root():
    """One Tk root for the session.

    Creating and destroying several segfaults macOS Tk 9, so every test shares
    this one and puts its popup in a Toplevel beneath it.
    """
    try:
        root = tk.Tk()
    except Exception as exc:  # headless CI
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def root(tk_root):
    yield tk_root
    for child in tk_root.winfo_children():
        try:
            child.destroy()
        except tk.TclError:
            pass


@pytest.fixture
def sent_job(pipeline, make_invoice):
    job = pipeline.process(make_invoice())
    assert job.status is JobStatus.DRY_RUN
    return job


class TestWording:
    def test_a_real_send_says_it_went(self, sent_job):
        sent_job.status = JobStatus.SENT
        tone, headline, detail = describe(sent_job)
        assert tone == "ok"
        assert headline == "Sent on WhatsApp"
        assert "+919876543210" in detail
        assert "INV-2291" in detail

    def test_a_test_send_does_not_claim_to_have_sent(self, sent_job):
        # The operator must not believe a customer received something.
        tone, headline, detail = describe(sent_job)
        assert tone == "wait"
        assert "Not sent" in headline
        assert "test mode" in headline.lower()

    def test_a_failure_says_why(self, pipeline, make_invoice):
        job = pipeline.process(make_invoice())
        job.status = JobStatus.FAILED
        job.error = "Message undeliverable"
        tone, headline, detail = describe(job)
        assert tone == "bad"
        assert headline == "Could not send"
        assert "Message undeliverable" in detail

    def test_an_unreadable_number_asks_for_attention(self, pipeline, make_invoice):
        job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
        assert job.status is JobStatus.HELD
        tone, headline, detail = describe(job)
        assert tone == "wait"
        assert headline == "Needs your attention"
        assert "phone number" in detail.lower()

    def test_a_reprint_says_it_was_already_sent(self, pipeline, make_invoice):
        pipeline.process(make_invoice())
        job = pipeline.process(make_invoice())
        assert job.status is JobStatus.DUPLICATE
        _tone, headline, _detail = describe(job)
        assert headline == "Already sent"

    def test_no_internal_status_names_leak(self, sent_job):
        joined = " ".join(describe(sent_job))
        for internal in ("dry_run", "JobStatus", "awaiting"):
            assert internal not in joined


class TestWindow:
    def test_it_opens(self, root, sent_job):
        from waprinter.ui.result_popup import ResultPopup

        popup = ResultPopup(root, sent_job)
        assert popup.win.winfo_exists()

    def test_a_success_closes_itself(self, root, sent_job):
        from waprinter.ui.result_popup import AUTO_CLOSE_MS, ResultPopup

        sent_job.status = JobStatus.SENT
        popup = ResultPopup(root, sent_job)
        root.update()
        # Fast-forward the scheduled close rather than waiting for it.
        popup.close()
        assert AUTO_CLOSE_MS > 0
        assert not popup.win.winfo_exists()

    def test_a_failure_offers_a_way_into_the_queue(self, root, pipeline, make_invoice):
        from waprinter.ui.result_popup import ResultPopup

        opened = []
        job = pipeline.process(make_invoice(InvoiceSpec(customer_phone=None)))
        popup = ResultPopup(root, job, on_open_queue=lambda: opened.append(True))
        root.update_idletasks()
        popup._open_queue()
        assert opened == [True]


class TestPopupHost:
    def test_submit_is_safe_from_the_watcher_thread(self, pipeline, sent_job):
        import threading

        from waprinter.ui.result_popup import PopupHost

        host = PopupHost(pipeline)
        thread = threading.Thread(target=host.submit, args=(sent_job.id,))
        thread.start()
        thread.join()
        assert host.incoming.get_nowait() == sent_job.id

    def test_a_missing_job_is_ignored(self, pipeline):
        from waprinter.ui.result_popup import PopupHost

        PopupHost(pipeline)._show("does-not-exist")  # must not raise
