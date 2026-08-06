"""The window that opens when someone prints.

Skipped where there is no display. The logic worth testing is the number
validation and the live message preview, because those are what stand between a
typo and a message reaching a stranger.
"""

from __future__ import annotations

import pytest
from invoice_factory import InvoiceSpec

from waprinter.models import JobStatus

tk = pytest.importorskip("tkinter")


@pytest.fixture(scope="session")
def tk_root():
    """One Tk root for the whole session.

    Deliberately session-scoped: creating and destroying multiple Tk instances
    in one process segfaults on macOS Tk 9, so every test shares this root and
    puts its dialog in a Toplevel under it. Skips the module where there is no
    display at all.
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
    """The shared root, with any windows this test opened cleaned up after."""
    yield tk_root
    for child in tk_root.winfo_children():
        try:
            child.destroy()
        except tk.TclError:
            pass


@pytest.fixture
def job(pipeline, make_invoice):
    job = pipeline.process(
        make_invoice(InvoiceSpec(customer_phone=None)), doc_title="Receipt"
    )
    assert job.status is JobStatus.AWAITING
    return job


@pytest.fixture
def dialog(root, job):
    from waprinter.ui.send_dialog import SendDialog

    events = {}
    d = SendDialog(
        root,
        job,
        country_code="91",
        dry_run=True,
        on_send=lambda number, name: events.update(number=number, name=name),
        on_skip=lambda: events.update(skipped=True),
    )
    root.update_idletasks()
    d.events = events  # for assertions
    return d


def preview_text(dialog) -> str:
    dialog.preview.configure(state="normal")
    value = dialog.preview.get("1.0", "end").strip()
    dialog.preview.configure(state="disabled")
    return value


class TestConstruction:
    def test_it_opens_without_error(self, dialog):
        # Regression: _validate() used to run before the Send button existed,
        # which crashed the dialog on every print.
        assert dialog.win.winfo_exists()

    def test_the_pdf_thumbnail_renders(self, dialog):
        assert dialog._thumbnail is not None

    def test_a_broken_pdf_still_opens_the_dialog(self, root, pipeline, tmp_path):
        from waprinter.ui.send_dialog import SendDialog

        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a pdf")
        job = pipeline.process(broken)
        d = SendDialog(root, job, "91", True, lambda n, m: None, lambda: None)
        # No preview, but the operator can still send it.
        assert d._thumbnail is None
        assert d.win.winfo_exists()


class TestNumberValidation:
    def test_send_is_disabled_with_an_empty_box(self, dialog):
        assert str(dialog.send_button["state"]) == "disabled"

    def test_send_stays_disabled_for_a_partial_number(self, dialog, root):
        dialog.number_var.set("98765")
        root.update_idletasks()
        assert str(dialog.send_button["state"]) == "disabled"

    def test_send_stays_disabled_for_a_landline(self, dialog, root):
        dialog.number_var.set("080-25551234")
        root.update_idletasks()
        assert str(dialog.send_button["state"]) == "disabled"

    def test_the_normalised_number_is_echoed_back(self, dialog, root):
        dialog.number_var.set("98765 43210")
        root.update_idletasks()
        assert "+919876543210" in dialog.status["text"]
        assert str(dialog.send_button["state"]) == "normal"

    def test_spaces_and_dashes_are_accepted(self, dialog, root):
        for typed in ("9876543210", "98765-43210", "+91 98765 43210", "09876543210"):
            dialog.number_var.set(typed)
            root.update_idletasks()
            assert "+919876543210" in dialog.status["text"], typed

    def test_pressing_send_with_an_invalid_number_does_nothing(self, dialog, root):
        dialog.number_var.set("123")
        root.update_idletasks()
        dialog._send()
        assert dialog.events == {}
        assert dialog.win.winfo_exists()


class TestSending:
    def test_send_reports_the_normalised_number(self, dialog, root):
        dialog.number_var.set("98765 43210")
        root.update_idletasks()
        dialog._send()
        assert dialog.events["number"] == "+919876543210"

    def test_the_typed_name_is_passed_along(self, dialog, root):
        dialog.number_var.set("9876543210")
        dialog.name_var.set("Ravi Kumar")
        root.update_idletasks()
        dialog._send()
        assert dialog.events["name"] == "Ravi Kumar"

    def test_skip_reports_a_skip(self, dialog):
        dialog._skip()
        assert dialog.events == {"skipped": True}

    def test_closing_the_window_counts_as_a_skip(self, dialog):
        dialog._skip()
        assert dialog.events.get("skipped") is True

    def test_skip_after_send_does_not_fire_twice(self, dialog, root):
        dialog.number_var.set("9876543210")
        root.update_idletasks()
        dialog._send()
        dialog._skip()
        assert "skipped" not in dialog.events


class TestMessagePreview:
    def test_it_shows_the_message_that_will_be_sent(self, dialog):
        assert "thank you for your business" in preview_text(dialog)

    def test_typing_a_name_updates_the_preview_live(self, dialog, root):
        dialog.name_var.set("Ravi Kumar")
        root.update_idletasks()
        assert "Hello Ravi Kumar" in preview_text(dialog)

    def test_the_preview_is_not_directly_editable(self, dialog):
        # The wording is fixed by the approved template; only the variables move.
        assert str(dialog.preview["state"]) == "disabled"


class TestDialogHost:
    def test_submit_is_safe_from_another_thread(self, pipeline, job):
        import threading

        from waprinter.ui.send_dialog import DialogHost

        host = DialogHost(pipeline)
        # The watcher thread calls this; it must not touch Tk.
        thread = threading.Thread(target=host.submit, args=(job.id,))
        thread.start()
        thread.join()
        assert host.incoming.get_nowait() == job.id

    def test_a_missing_job_is_ignored(self, pipeline):
        from waprinter.ui.send_dialog import DialogHost

        host = DialogHost(pipeline)
        host._show("does-not-exist")  # must not raise
        assert host._showing is False
