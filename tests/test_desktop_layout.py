"""The settings page has to stay usable on the screens it is installed on.

A build shipped with Apply packed below settings groups that were taller than
the window, on a tab that did not scroll. It was not clipped by a few pixels —
it sat about 300px past the bottom edge, so maximising did not reveal it
either. The operator typed a page of settings, pressed Test send, and was told
the fields were empty.

The assertions are structural rather than pixel measurements on a deliberately
shrunken window. That is not squeamishness about geometry: forcing the old
layout into a window smaller than its content makes Tk fight its own geometry
manager inside update(), and the run hangs instead of failing. A regression
test that wedges CI is worse than no test, so what is checked here is the
shape that made the bug impossible — content that scrolls, and an Apply button
outside the part that scrolls.

Needs a display. Windows CI has one; a headless dev box skips.
"""

from __future__ import annotations

import tkinter as tk

import pytest


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    """One window for the whole module.

    Module scope is not an optimisation: Tk on macOS segfaults when a process
    creates a second Tk() instance, so building one per test — or even probing
    for a display with a throwaway root — crashes the run rather than skipping
    it. These tests only measure the layout; none of them mutate it.
    """
    from waprinter.config import Settings
    from waprinter.pipeline import Pipeline
    from waprinter.send.dryrun import DryRunSender
    from waprinter.send.templates import TemplateStore
    from waprinter.store import Store

    tmp = tmp_path_factory.mktemp("desktop")
    store = Store(tmp / "jobs.db")
    pipeline = Pipeline(
        settings=Settings(),
        store=store,
        sender=DryRunSender(tmp / "dry_run.jsonl"),
        templates=TemplateStore(tmp / "templates.json"),
    )

    from waprinter.ui.desktop import DesktopWindow

    try:
        win = DesktopWindow(pipeline)
    except tk.TclError:
        store.close()
        pytest.skip("no display")

    win.tabs.select(win.settings_tab)
    # Map the window before anything measures it. update_idletasks() runs the
    # geometry calculations but not the map, and Windows gives an unmapped
    # window no real geometry at all -- the canvas reports a width of 1, so
    # every measurement below compares against nonsense. macOS happens to fill
    # them in anyway, which is why this held together until the tests were
    # first run on the platform the product actually ships on.
    win.root.deiconify()
    win.root.update()
    yield win
    win.root.destroy()
    store.close()


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _apply_button(window):
    found = [
        w
        for w in _descendants(window.settings_tab)
        if w.winfo_class() == "TButton" and str(w.cget("text")) == "Apply"
    ]
    assert len(found) == 1, "expected exactly one Apply button on the settings tab"
    return found[0]


def _canvas(window):
    found = [w for w in _descendants(window.settings_tab) if w.winfo_class() == "Canvas"]
    assert found, "the settings tab does not scroll"
    return found[0]


def test_apply_cannot_scroll_out_of_sight(window):
    """Apply belongs to the tab, not to the content that scrolls under it."""
    canvas_path = str(_canvas(window))
    assert not str(_apply_button(window)).startswith(f"{canvas_path}.")


def test_the_settings_groups_overflow_and_therefore_must_scroll(window):
    """Guards the premise: if the fields ever fit, this file is testing nothing."""
    canvas = _canvas(window)
    content_height = canvas.bbox("all")[3]
    assert content_height > canvas.winfo_height()


def test_the_last_group_can_be_scrolled_to(window):
    canvas = _canvas(window)
    canvas.yview_moveto(1.0)
    window.root.update_idletasks()
    assert canvas.yview()[1] == pytest.approx(1.0)
    canvas.yview_moveto(0.0)


def test_fields_use_the_full_width(window):
    """The scrolling frame tracks the canvas, so entries are not a thin column."""
    canvas = _canvas(window)
    inner = canvas.winfo_children()[0]
    assert inner.winfo_width() == canvas.winfo_width()

# --- the folder for keeping printed receipts --------------------------------
#
# Browse and Open sit at the right-hand end of a row whose entry expands. This
# tab has form: Apply once ended up about 300px below the bottom edge, and a
# control that cannot be reached is a control that is not there.


def _folder_row(window):
    entries = [
        w
        for w in _descendants(window.settings_tab)
        if w.winfo_class() == "TEntry"
        and str(w.cget("textvariable")) == str(window.fields["pdf_folder"])
    ]
    assert len(entries) == 1, "no folder box for pdf_folder on the settings tab"
    return entries[0].master


def test_the_folder_can_be_chosen_and_opened(window):
    labels = [
        str(w.cget("text"))
        for w in _folder_row(window).winfo_children()
        if w.winfo_class() == "TButton"
    ]
    assert "Browse..." in labels
    assert "Open" in labels


def test_the_folder_buttons_are_inside_the_window(window):
    """Not clipped by the entry expanding past them."""
    canvas = _canvas(window)
    window.root.update_idletasks()

    for button in _folder_row(window).winfo_children():
        if button.winfo_class() != "TButton":
            continue
        right_edge = button.winfo_rootx() + button.winfo_width()
        assert right_edge <= canvas.winfo_rootx() + canvas.winfo_width(), (
            f"{button.cget('text')} runs past the right-hand edge"
        )


def test_the_folder_box_keeps_a_usable_width(window):
    """The two buttons must not squeeze the path down to nothing."""
    window.root.update_idletasks()
    entry = [
        w for w in _folder_row(window).winfo_children() if w.winfo_class() == "TEntry"
    ][0]
    assert entry.winfo_width() > 200


# --- the queue tab: typing a number must survive the refresh timer ----------
#
# refresh() runs every two seconds and _render_queue() used to destroy and
# rebuild every row unconditionally. The operator typing a number into a held
# job's box got it wiped out from under them roughly once a second and a half,
# so the one control that resolves a held document could not be used at all.


def _queue_entries(window):
    return [
        w
        for w in _descendants(window.queue_body)
        if w.winfo_class() == "TEntry"
    ]


def _held_job(window):
    from datetime import datetime
    from pathlib import Path

    from waprinter.models import JobStatus, PrintJob

    job = PrintJob(
        id="held-under-test",
        created_at=datetime.now(),
        pdf_path=Path("receipt.pdf"),
        status=JobStatus.HELD,
        doc_title="CHQ6511/26",
        hold_reason="2 equally likely numbers found. Pick the right one.",
    )
    window.pipeline.store.upsert(job)
    return job


def test_typing_a_number_survives_the_refresh_timer(window):
    job = _held_job(window)
    try:
        window.refresh()
        entries = _queue_entries(window)
        assert entries, "the held job did not render a number box"

        entry = entries[0]
        entry.insert(0, "9492204498")

        # What the two-second timer does, twice.
        window.refresh()
        window.refresh()

        survivor = _queue_entries(window)[0]
        assert survivor.get() == "9492204498"
    finally:
        window.pipeline.store.conn.execute(
            "DELETE FROM jobs WHERE id = ?", (job.id,)
        )
        window.pipeline.store.conn.commit()
        window.refresh()


def test_a_batch_print_does_not_open_a_chat_per_receipt(window, monkeypatch):
    """One receipt goes straight to its chat; ten receipts must not.

    Splitting a batch into one job per subscriber turned a single notification
    into one per receipt. With auto_open_chat on -- it is on by default, and
    link mode is the shipped default -- that would have thrown a WhatsApp
    window per subscriber at whoever was standing at the counter.
    """
    from datetime import datetime
    from pathlib import Path

    from waprinter.models import JobStatus, PrintJob

    opened: list[str] = []
    monkeypatch.setattr(window, "open_whatsapp", lambda jid: opened.append(jid) or True)
    monkeypatch.setattr(window.settings, "auto_open_chat", True)

    job = PrintJob(
        id="ready-under-test",
        created_at=datetime.now(),
        pdf_path=Path("receipt.pdf"),
        status=JobStatus.READY,
        recipient="+917032893588",
        chat_url="https://wa.me/917032893588",
    )
    window.pipeline.store.upsert(job)
    try:
        window._notify(job.id, auto_open=False)
        assert opened == [], "a batch receipt opened a chat window"

        window._notify(job.id, auto_open=True)
        assert opened == [job.id], "a single receipt should still go to its chat"
    finally:
        for panel in window._notifications:
            panel.close()
        window._notifications = []
        window.pipeline.store.conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
        window.pipeline.store.conn.commit()
        window.refresh()


class TestTheDocumentsGroup:
    """Which message each kind of paperwork goes out under, on the page.

    Before this there was no way to see it from the app at all: the Settings
    tab showed `default_template` and nothing else, so an engineer over
    AnyDesk had to open settings.json to find out whether a removal notice
    was pointing at a template Meta had rejected -- or that it was pointing at
    a typo, which holds every notice at the counter.
    """

    @pytest.fixture
    def form(self, window):
        """Restores the window's settings afterwards.

        The window is module-scoped because a second Tk() segfaults on macOS,
        so a test that writes to it has to put it back.
        """
        s = window.settings
        before = (dict(s.document_templates), dict(s.document_nouns))
        window._load_settings_into_form()
        yield window
        s.document_templates, s.document_nouns = before
        window._load_settings_into_form()

    def _kinds(self, window):
        return [k.name for k in window.pipeline.profile.document_kinds]

    def test_every_document_the_profile_knows_has_a_row(self, window):
        for kind in self._kinds(window):
            assert f"doc_template:{kind}" in window.fields
            assert f"doc_noun:{kind}" in window.fields

    def test_a_row_is_labelled_in_words_not_in_settings_keys(self, window):
        labels = {
            w.cget("text")
            for w in _descendants(window.settings_tab)
            if isinstance(w, tk.Widget) and "text" in w.keys()
        }
        assert "Removal notice" in labels
        assert "removal_notice" not in labels

    def test_the_stored_mapping_is_shown(self, form):
        form.settings.document_templates = {"removal_notice": "rn_v2"}
        form.settings.document_nouns = {"removal_notice": "Notice"}

        form._load_settings_into_form()

        assert form.fields["doc_template:removal_notice"].get() == "rn_v2"
        assert form.fields["doc_noun:removal_notice"].get() == "Notice"

    def test_what_is_typed_is_what_is_stored(self, form, monkeypatch):
        from waprinter.ui import desktop

        monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *a, **k: True)
        form.fields["doc_template:removal_letter"].set("removal_letter")
        form.fields["doc_noun:removal_letter"].set("Removal Letter")

        form.apply_settings()

        assert form.settings.document_templates["removal_letter"] == "removal_letter"
        assert form.settings.document_nouns["removal_letter"] == "Removal Letter"

    def test_blank_rows_do_not_become_a_mapping(self, form):
        """An empty map is a real answer, and the gate reads it.

        A client who only sends receipts must be able to say so. If Apply
        wrote a row back for every kind the profile knows, they never could,
        and every unidentifiable scan would start being held on their counter.
        """
        for kind in self._kinds(form):
            form.fields[f"doc_template:{kind}"].set("")
            form.fields[f"doc_noun:{kind}"].set("")

        form.apply_settings()

        assert form.settings.document_templates == {}
        assert form.settings.document_nouns == {}

    def test_a_typo_is_caught_before_it_reaches_the_counter(self, form, monkeypatch):
        from waprinter.ui import desktop

        asked = {}
        monkeypatch.setattr(
            desktop.messagebox,
            "askyesno",
            lambda title, message, **k: asked.setdefault("message", message) and False,
        )
        form.fields["doc_template:removal_notice"].set("removal_notce")

        form.apply_settings()

        assert "removal_notce" in asked["message"]
        assert "not on this computer" in asked["message"]

    def test_declining_the_warning_changes_nothing(self, form, monkeypatch):
        from waprinter.ui import desktop

        monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *a, **k: False)
        before = dict(form.settings.document_templates)
        form.fields["doc_template:removal_notice"].set("removal_notce")

        form.apply_settings()

        assert form.settings.document_templates == before

    def test_an_unapproved_message_is_named_with_its_status(self, form, monkeypatch):
        """Not only "unknown" -- a template Meta rejected is on the computer
        and would still never send."""
        from waprinter.ui import desktop

        asked = {}
        monkeypatch.setattr(
            desktop.messagebox,
            "askyesno",
            lambda title, message, **k: asked.setdefault("message", message) and False,
        )
        form.fields["doc_template:removal_notice"].set("chits_details")  # pending

        form.apply_settings()

        assert "not approved by Meta" in asked["message"]
