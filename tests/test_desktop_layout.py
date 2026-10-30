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

from waprinter.config import Settings
from waprinter.extract.profile import DEFAULT_KIND


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

    # The counter settings are a page of the setup view now, not a tab.
    win.show_setup("preferences")
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


def test_failed_jobs_have_a_retry_action_and_cancel_keeps_them(window, monkeypatch):
    from waprinter.models import JobStatus
    from waprinter.ui import desktop

    job = _held_job(window)
    job.status = JobStatus.FAILED
    job.error = "The response was lost. Check WhatsApp before sending again."
    job.recipient = "+919876543210"
    window.pipeline.store.upsert(job)
    prompts = []
    monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *a, **k: prompts.append(a) or False)
    try:
        window.refresh()
        buttons = [w for w in _descendants(window.queue_body)
                   if w.winfo_class() == "TButton" and w.cget("text") == "Retry…"]
        assert len(buttons) == 1
        buttons[0].invoke()
        assert prompts and "Check WhatsApp" in prompts[0][1]
        assert window.pipeline.store.get(job.id).status == JobStatus.FAILED
        assert job.id not in window._sending
    finally:
        window.pipeline.store.conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
        window.pipeline.store.conn.commit()
        window.refresh()


def test_older_queue_jobs_can_be_loaded_without_losing_typed_numbers(window):
    from dataclasses import replace
    from datetime import timedelta

    first = _held_job(window)
    second = replace(first, id="older-queue-job", created_at=first.created_at - timedelta(seconds=1))
    window.pipeline.store.upsert(second)
    window._queue_limit = 1
    try:
        window.refresh()
        window._queue_rows[first.id].insert(0, "98765")
        assert second.id not in window._queue_rows
        more = [w for w in _descendants(window.queue_body)
                if w.winfo_class() == "TButton" and w.cget("text").startswith("Show more")]
        assert len(more) == 1
        more[0].invoke()
        assert second.id in window._queue_rows
        assert window._queue_rows[first.id].get() == "98765"
    finally:
        window._queue_limit = 200
        window.pipeline.store.conn.execute("DELETE FROM jobs WHERE id IN (?, ?)", (first.id, second.id))
        window.pipeline.store.conn.commit()
        window.refresh()


def test_configuration_button_is_labelled_as_a_check(window, monkeypatch):
    from waprinter.ui import desktop

    found = [w for w in _descendants(window.root)
             if w.winfo_class() == "TButton" and w.cget("text") == "Check configuration"]
    assert len(found) == 1
    reports = []
    monkeypatch.setattr("waprinter.send.readiness.problems", lambda *a: [])
    monkeypatch.setattr(desktop.messagebox, "showinfo", lambda *a, **k: reports.append(a))
    found[0].invoke()
    assert reports and "nothing was sent" in reports[0][1]


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


@pytest.fixture
def sync_tasks(monkeypatch):
    """Run setup's background work inline, so a test can see its result."""
    from waprinter.ui import setup

    def run(self, name, work, done):
        try:
            result = work()
        except Exception as exc:
            result = exc
        done(result)
        return True

    monkeypatch.setattr(setup.Tasks, "run", run)


@pytest.fixture
def restore(window):
    """Put the window's settings back. The window is module-scoped because a
    second Tk() segfaults on macOS, so a test that writes to it must undo it."""
    from copy import deepcopy

    before = deepcopy(window.settings)
    yield window
    for name in before.__dataclass_fields__:
        setattr(window.settings, name, getattr(before, name))
    window.settings.save()
    window.show_setup("preferences")


def _messages(window):
    window.show_setup("messages")
    return window.setup_view.pages["messages"]


def _choose(box, predicate):
    values = list(box.cget("values"))
    index = next(i for i, v in enumerate(values) if predicate(v))
    box.current(index)
    box.event_generate("<<ComboboxSelected>>")
    return values[index]


class TestSetupIsItsOwnPlace:
    """The clerk has three pages; everything that changes what members
    receive is behind Setup, optionally behind a PIN."""

    def test_the_side_bar_offers_the_counter_pages_then_setup(self, window):
        labels = [str(b.cget("text")).split(" (")[0] for b in window.nav_buttons.values()]
        assert labels == ["Status", "Needs attention", "Recent documents",
                          "Setup", "Message templates"]

    def test_the_counter_pages_do_not_show_setup(self, window):
        window.show_counter()
        try:
            assert not window.in_setup
            assert not window.setup_view.winfo_ismapped()
            window.show_view("recent")
            assert window.nav_buttons["recent"].cget("style") == "NavOn.TButton"
            assert window.nav_buttons["status"].cget("style") == "Nav.TButton"
        finally:
            window.show_setup("preferences")

    def test_message_templates_opens_that_setup_step(self, window):
        window.show_counter()
        window.nav_buttons["templates"].invoke()   # no PIN is set here
        assert window.in_setup and window.setup_view.current == "templates"
        assert window.nav_buttons["templates"].cget("style") == "NavOn.TButton"
        window.show_setup("preferences")
        assert window.nav_buttons["setup"].cget("style") == "NavOn.TButton"

    def test_every_step_opens(self, window):
        for key, _title in __import__("waprinter.ui.setupmodel", fromlist=["x"]).STEP_TITLES:
            window.show_setup(key)
            window.root.update_idletasks()
            assert window.setup_view.current == key
        window.show_setup("preferences")

    def test_a_pin_keeps_setup_closed(self, restore, monkeypatch):
        from waprinter.ui import desktop
        from waprinter.ui.setupmodel import hash_pin

        restore.show_counter()
        restore.settings.setup_pin = hash_pin("4321")
        errors = []
        monkeypatch.setattr(desktop.messagebox, "showerror", lambda *a, **k: errors.append(a))
        monkeypatch.setattr(desktop.simpledialog, "askstring", lambda *a, **k: "1111")
        assert restore.open_setup() is False
        assert errors and not restore.in_setup

        monkeypatch.setattr(desktop.simpledialog, "askstring", lambda *a, **k: "4321")
        assert restore.open_setup() is True
        assert restore.in_setup


class TestFillInMessages:
    def test_every_document_type_is_offered_in_words(self, window):
        page = _messages(window)
        labels = list(page.kind_box.cget("values"))
        assert labels[0] == "Receipts"
        assert "Removal notice" in labels
        assert "removal_notice" not in labels

    def test_the_saved_choice_is_shown(self, restore):
        restore.settings.document_templates = {"removal_notice": "removal_notice"}
        restore.settings.document_nouns = {"removal_notice": "Notice"}
        page = _messages(restore)
        page.select_kind("removal_notice")
        assert page.template_ref() == "removal_notice"
        assert page.noun.get() == "Notice"

    def test_a_saved_template_that_is_not_here_is_shown_not_hidden(self, restore):
        restore.settings.document_templates = {"removal_notice": "removal_notce"}
        page = _messages(restore)
        page.select_kind("removal_notice")
        assert page.template_ref() == "removal_notce"
        assert "not on this computer" in page.template_box.get()

    def test_choosing_none_leaves_no_mapping(self, restore, monkeypatch):
        """An empty map is a real answer, and the gate reads it."""
        from waprinter.ui import setup

        monkeypatch.setattr(setup.messagebox, "showinfo", lambda *a, **k: None)
        restore.settings.document_templates = {"removal_notice": "removal_notice"}
        page = _messages(restore)
        page.select_kind("removal_notice")
        _choose(page.template_box, lambda v: v.startswith("— none"))
        page.noun.set("")
        page.save()
        assert "removal_notice" not in restore.settings.document_templates

    def test_choices_are_fixed_and_carry_their_status(self, window):
        page = _messages(window)
        page.select_kind("removal_notice")
        assert str(page.template_box.cget("state")) == "readonly"
        assert any("waiting for Meta's approval" in v for v in page.template_box.cget("values"))

    def test_an_unapproved_template_is_named_and_declining_changes_nothing(
        self, restore, monkeypatch
    ):
        from waprinter.ui import setup

        asked = {}
        monkeypatch.setattr(setup.messagebox, "askyesno",
                            lambda title, message, **k: asked.setdefault("m", message) and False)
        restore.settings.send_mode = "api"
        before = dict(restore.settings.document_templates)
        page = _messages(restore)
        page.select_kind("removal_notice")
        _choose(page.template_box, lambda v: v.startswith("removal_notice"))
        assert "waiting for Meta's approval" in page.status.cget("text")
        page.save()
        assert "Save anyway" in asked["m"]
        assert restore.settings.document_templates == before

    def test_pdf_or_image_is_chosen_beside_the_template(self, restore, monkeypatch):
        """The two halves of one decision: only an image-header template can
        carry an image, so the screen that picks one picks the other."""
        from waprinter.ui import setup

        monkeypatch.setattr(setup.messagebox, "showinfo", lambda *a, **k: None)
        restore.settings.document_templates = {"removal_notice": "removal_notice"}
        page = _messages(restore)
        page.select_kind("removal_notice")
        _choose(page.attachment_box, lambda v: v.startswith("Image"))
        page.save()

        assert restore.settings.document_attachments["removal_notice"] == "image"

    def test_the_receipt_choice_is_the_one_everything_else_follows(
        self, restore, monkeypatch
    ):
        from waprinter.ui import setup

        monkeypatch.setattr(setup.messagebox, "showinfo", lambda *a, **k: None)
        page = _messages(restore)
        page.select_kind(DEFAULT_KIND)
        _choose(page.attachment_box, lambda v: v.startswith("Image"))
        page.save()

        assert restore.settings.attachment_format == "image"

    def test_the_saved_choice_comes_back(self, restore):
        restore.settings.attachment_format = "image"
        page = _messages(restore)
        page.select_kind(DEFAULT_KIND)

        assert page.attachment() == "image"
        assert page.attachment_box.get().startswith("Image")

    def test_choosing_an_image_warns_that_this_template_cannot_carry_one(
        self, restore
    ):
        """The mismatch is said here, not after a member got nothing."""
        from waprinter.send.templates import MessageTemplate

        store = restore.pipeline.templates
        approved = MessageTemplate(name="receipt_pdf", body="Dear {{1}}.",
                                   status="approved", header_format="DOCUMENT")
        store.put(approved)
        try:
            restore.settings.send_mode = "api"
            page = _messages(restore)
            page.select_kind(DEFAULT_KIND)
            _choose(page.template_box, lambda v: v.startswith("receipt_pdf"))
            assert page.status.cget("text") == ""  # fine as a PDF

            _choose(page.attachment_box, lambda v: v.startswith("Image"))

            assert "image has nowhere to go" in page.status.cget("text")
        finally:
            store._templates.pop(store.ref_for(approved), None)

    def test_choosing_a_template_chooses_what_its_header_carries(self, restore):
        """An image-header template has already decided the image; the second
        drop-down follows it instead of waiting to be set wrong."""
        from waprinter.send.templates import MessageTemplate

        store = restore.pipeline.templates
        image = MessageTemplate(name="receipt_image", body="Dear {{1}}.",
                                status="approved", header_document=False,
                                header_format="IMAGE")
        pdf = MessageTemplate(name="receipt_pdf", body="Dear {{1}}.",
                              status="approved", header_format="DOCUMENT")
        store.put(image)
        store.put(pdf)
        try:
            restore.settings.send_mode = "api"
            page = _messages(restore)
            page.select_kind(DEFAULT_KIND)
            assert page.attachment() == "pdf"

            _choose(page.template_box, lambda v: v.startswith("receipt_image"))
            assert page.attachment() == "image"
            assert page.status.cget("text") == ""
            assert ".jpg" in str(page.filename.cget("text"))

            _choose(page.template_box, lambda v: v.startswith("receipt_pdf"))
            assert page.attachment() == "pdf"
            assert page.status.cget("text") == ""
        finally:
            store._templates.pop(store.ref_for(image), None)
            store._templates.pop(store.ref_for(pdf), None)

    def test_an_image_send_says_the_filename_is_not_shown(self, restore):
        page = _messages(restore)
        page.select_kind(DEFAULT_KIND)
        _choose(page.attachment_box, lambda v: v.startswith("Image"))

        hint = str(page.filename.cget("text"))
        assert ".jpg" in hint
        assert "no filename beside it" in hint

    def test_a_variable_is_mapped_to_a_taught_field(self, restore, monkeypatch):
        """The case this screen exists for: the template says {{id}}, the
        page says "Cust ID", and the two are joined here."""
        from waprinter.extract.profile import FieldRule
        from waprinter.send.templates import MessageTemplate
        from waprinter.ui import setup

        monkeypatch.setattr(setup.messagebox, "showinfo", lambda *a, **k: None)
        restore.pipeline.templates.put(MessageTemplate(
            name="member_statement", body="Dear {{name}}, statement for {{id}} from {{branch}}.",
            status="approved", parameter_format="named",
        ))
        restore.pipeline.profile.field_rules["_default"] = [
            FieldRule(name="customer_id", label="Cust ID", kind="code"),
        ]
        try:
            page = _messages(restore)
            page.select_kind("_default")
            _choose(page.template_box, lambda v: v.startswith("member_statement"))
            mapping = page.mapping()
            assert mapping["name"] == "customer_name"
            assert mapping["id"] == "customer_id"          # suggested from shared words
            source, fixed, *_ = page._rows["branch"]
            source.set("Fixed text…")
            fixed.set("Karimnagar")
            page.update_preview()
            assert "Karimnagar" in page.preview.get("1.0", "end")
            page.save()
            saved = restore.settings.template_mappings["member_statement"]
            assert saved == {"name": "customer_name", "id": "customer_id",
                             "branch": "text:Karimnagar"}
            assert restore.settings.default_template == "member_statement"
        finally:
            restore.pipeline.profile.field_rules.pop("_default", None)

    def test_a_numbered_template_is_not_offered_the_receipts_numbers(self, restore):
        """What running the agent turned up: invoice_document was filled from
        the chit receipt's positions, and read "Your invoice Srinidhi Chit
        Funds for ₹INV-2291"."""
        restore.settings.default_template = "invoice_document"
        page = _messages(restore)
        page.select_kind("_default")
        assert page.mapping() == {"1": "customer_name", "2": "invoice_number",
                                  "3": "total_amount"}
        assert "Srinidhi Chit Funds for" not in page.preview.get("1.0", "end")

    def test_the_status_page_says_an_unfillable_message_before_any_print(self, restore):
        from waprinter.config import Settings

        restore.settings.default_template = "invoice_document"
        restore.settings.template_variables = Settings().template_variables
        restore.settings.template_mappings = {}
        # Saved, as provision.json or a hand edit would: refresh() re-reads
        # settings.json before it decides what the Status tab says.
        restore.settings.save()
        restore.show_counter()
        restore.refresh()
        text = " ".join(str(w.cget("text")) for w in _descendants(restore.problems_box)
                        if w.winfo_class() == "TLabel")
        assert "'invoice_document' message has {{1}}, {{2}}, {{3}}" in text
        assert restore.problems_box.winfo_manager() == "pack"
        # ...on a card that leads to the step that fixes it.
        links = [str(w.cget("text")) for w in _descendants(restore.problems_box)
                 if w.winfo_class() == "TButton"]
        assert "Fill in messages" in links

    def test_an_unfilled_variable_cannot_be_saved(self, restore, monkeypatch):
        from waprinter.send.templates import MessageTemplate
        from waprinter.ui import setup

        errors = []
        monkeypatch.setattr(setup.messagebox, "showerror", lambda *a, **k: errors.append(a))
        restore.pipeline.templates.put(MessageTemplate(
            name="odd_one", body="Hi {{zzz_unknowable}}", status="approved",
            parameter_format="named",
        ))
        page = _messages(restore)
        page.select_kind("_default")
        _choose(page.template_box, lambda v: v.startswith("odd_one"))
        page._rows["zzz_unknowable"][0].set("")
        page.save()
        assert errors and "{{zzz_unknowable}} is not filled in" in errors[0][1]
        assert "odd_one" not in restore.settings.template_mappings


class TestConnect:
    def test_a_token_finds_the_number_and_saving_stores_the_ids(
        self, restore, monkeypatch, sync_tasks
    ):
        from waprinter.send.meta_account import BusinessAccount, Connection, PhoneNumber

        stored = {}
        monkeypatch.setattr("waprinter.send.meta_account.inspect",
                            lambda token, version, account: Connection(
                                valid=True,
                                accounts=[BusinessAccount("555", "Srinidhi", [
                                    PhoneNumber("111", "+91 87822 51999", "Srinidhi Chit Funds")])]))
        monkeypatch.setattr("waprinter.secrets.save_token", lambda t: stored.setdefault("token", t))
        monkeypatch.setattr("waprinter.send.sync.sync_templates", lambda *a, **k: 3)

        restore.show_setup("connect")
        page = restore.setup_view.pages["connect"]
        page.token.set("EAAG" + "x" * 60)
        page.look_up()
        assert "+91 87822 51999" in page.numbers.get()
        assert "does not expire" in page.result.cget("text")

        page.save()
        assert stored["token"] == "EAAG" + "x" * 60
        assert restore.settings.phone_number_id == "111"
        assert restore.settings.business_account_id == "555"
        assert "Loaded 3 template(s)" in page.result.cget("text")


class TestTeaching:
    def test_a_new_type_is_taught_by_clicking_and_saved(self, restore, monkeypatch,
                                                        sync_tasks, tmp_path):
        """Upload a sample, click the value beside "Cust ID", call it
        Customer ID, save: the type and the field are in profile.json."""
        import fitz

        from waprinter.config import paths
        from waprinter.extract.profile import DocumentProfile
        from waprinter.ui import setup, teach

        sample = tmp_path / "statement.pdf"
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), "MEMBER STATEMENT", fontsize=16)
        page.insert_text((60, 110), "Cust ID :", fontsize=10)
        page.insert_text((140, 110), "SCF-00418", fontsize=10)
        page.insert_text((60, 130), "Name", fontsize=10)
        page.insert_text((140, 130), "ANITHA RAMESH", fontsize=10)
        doc.save(sample)

        monkeypatch.setattr(setup.messagebox, "showinfo", lambda *a, **k: None)
        saved_as = []
        documents = restore.setup_view.pages["documents"]
        window = teach.TeachWindow(
            restore.setup_view, sample, None,
            on_saved=lambda key: (saved_as.append(key), documents.taught(key)),
        )
        try:
            assert window.title_var.get() == "MEMBER STATEMENT"
            window.name_var.set("Member statement")
            rows = window._rows
            row = next(r for r in rows if "SCF-00418" in r.text)
            index = [w.text for w in row.words].index("SCF-00418")
            from waprinter.extract.rules import Selection

            window.selection = Selection(window.doc.pages[0], row, index, index)
            window._show_selection(guess=True)
            assert "Cust ID" in window.found_label.cget("text")
            window.field_var.set("Customer ID")
            window.add_field()
            assert window.tree.set("customer_id", "reads") == "SCF-00418"
            errors = []
            monkeypatch.setattr(teach.messagebox, "showerror", lambda *a, **k: errors.append(a))
            window.save()
            assert errors and "at least 3" in errors[-1][1]
            assert not saved_as
            from waprinter.validation import Sample
            from test_teaching import statement
            window.validation_samples = [Sample(sample, "member_statement", {"customer_id": "SCF-00418"}, "")]
            for number in (7, 991):
                extra = statement(tmp_path / f"extra-{number}.pdf", cust_id=f"SCF-{number}")
                window.validation_samples.append(Sample(extra, "member_statement", {"customer_id": f"SCF-{number}"}, "+919000012345"))
            window.save()
        finally:
            if window.winfo_exists():
                window.close()

        assert saved_as == ["member_statement"]
        profile = DocumentProfile.load(paths().profile)
        assert [k.name for k in profile.custom_kinds] == ["member_statement"]
        assert profile.custom_kinds[0].match == ["MEMBER STATEMENT"]
        assert profile.rules_for("member_statement")[0].label == "Cust ID"
        assert (paths().samples / "member_statement.pdf").exists()
        # And the hand-over lands on the mapping page with the new type chosen.
        assert restore.setup_view.current == "messages"
        assert restore.setup_view.pages["messages"].kind_key() == "member_statement"

        from waprinter import teaching

        teaching.remove_kind("member_statement")
        restore.pipeline.reload_profile(force=True)


def test_review_samples_collects_corrected_expectations(window, tmp_path, sync_tasks):
    from test_teaching import statement
    from waprinter.config import Settings
    from waprinter.extract.profile import DocumentKind, DocumentProfile
    from waprinter.ui.validation import ReviewSamples
    from waprinter.validation import Reader

    profile = DocumentProfile(custom_kinds=[DocumentKind('statement', ['MEMBER STATEMENT'])])
    pdfs = [statement(tmp_path / f'review-{i}.pdf', cust_id=f'SCF-{i}') for i in range(3)]
    reviewed = []
    dialog = ReviewSamples(window.root, pdfs, 'statement', profile, Settings(), Reader(), reviewed.extend)
    try:
        assert not reviewed
        for number in range(3):
            assert dialog.index == number
            dialog.entries['recipient'].set('9876543210')
            dialog.accept()
        assert len(reviewed) == 3
        assert all(s.recipient == '+919876543210' for s in reviewed)
    finally:
        if dialog.winfo_exists():
            dialog.destroy()


class TestNothingUnderTheTaskbar:
    """The buttons that finish a job must never depend on scrolling to them."""

    def test_the_teaching_buttons_do_not_scroll(self, restore, tmp_path, sync_tasks):
        from test_cash_and_online import receipt
        from waprinter.ui.teach import TeachWindow

        window = TeachWindow(restore.setup_view, receipt(tmp_path / "s.pdf", ["CASH"]), None,
                             on_saved=lambda key: None)
        try:
            canvases = [str(w) for w in _descendants(window) if w.winfo_class() == "Canvas"]
            for wanted in ("Save", "Cancel", "Try on recent prints"):
                button = next(w for w in _descendants(window) if w.winfo_class() == "TButton"
                              and str(w.cget("text")) == wanted)
                assert not any(str(button).startswith(f"{c}.") for c in canvases), wanted
        finally:
            window.close()

    def test_the_review_buttons_do_not_scroll(self, restore, tmp_path, sync_tasks):
        dialog = _review(restore, tmp_path, 1)
        try:
            canvases = [str(w) for w in _descendants(dialog) if w.winfo_class() == "Canvas"]
            assert not any(str(dialog.next).startswith(f"{c}.") for c in canvases)
        finally:
            dialog.destroy()

    def test_a_teaching_window_opens_no_larger_than_the_screen(self, restore, tmp_path, sync_tasks):
        from test_cash_and_online import receipt
        from waprinter.ui.teach import TeachWindow

        window = TeachWindow(restore.setup_view, receipt(tmp_path / "s.pdf", ["CASH"]), None,
                             on_saved=lambda key: None)
        try:
            window.update_idletasks()
            size, _, position = window.geometry().partition("+")
            height = int(size.split("x")[1])
            assert int(position.split("+")[1]) + height <= window.winfo_screenheight()
        finally:
            window.close()


def _review(window, tmp_path, count, **options):
    from test_cash_and_online import KIND, receipt
    from waprinter.extract.profile import DocumentProfile
    from waprinter.ui.validation import ReviewSamples
    from waprinter.validation import Reader

    profile = DocumentProfile(custom_kinds=[KIND])
    pdfs = [receipt(tmp_path / f"r{n}.pdf", ["CASH"], bill=f"I-{n}") for n in range(count)]
    reviewed = options.pop("done", lambda samples: None)
    return ReviewSamples(window.root, pdfs, "lab", profile, Settings(), Reader(), reviewed, **options)


class TestAWrongDocumentCanBeTakenBack:
    def test_going_back_keeps_what_was_typed(self, window, tmp_path, sync_tasks):
        dialog = _review(window, tmp_path, 2)
        try:
            dialog.entries["recipient"].set("9000012345")
            dialog.accept()
            assert dialog.index == 1
            dialog.back()
            assert dialog.index == 0
            assert dialog.entries["recipient"].get() == "+919000012345"
        finally:
            dialog.destroy()

    def test_closing_part_way_keeps_what_was_checked(self, window, tmp_path, sync_tasks):
        got = []
        dialog = _review(window, tmp_path, 3, done=got.extend)
        dialog.accept()
        dialog.finish()
        assert [s.path.name for s in got] == ["r0.pdf"]

    def test_a_document_of_another_type_cannot_be_accepted(self, window, tmp_path, sync_tasks):
        import fitz

        dialog = _review(window, tmp_path, 1)
        try:
            other = fitz.open()
            other.new_page().insert_text((30, 30), "SOMETHING ELSE ENTIRELY, a long sentence here")
            other.save(tmp_path / "other.pdf")
            dialog.pdfs = [tmp_path / "other.pdf"]
            dialog.load()
            assert str(dialog.next.cget("state")) == "disabled"
            assert str(dialog.remove_button.cget("state")) == "normal"
            assert "cannot be a sample" in dialog.warning.cget("text")
        finally:
            dialog.destroy()

    def test_removing_it_moves_on_and_says_so(self, window, tmp_path, sync_tasks):
        rejected, got = [], []
        dialog = _review(window, tmp_path, 2, done=got.extend, on_reject=rejected.append)
        dialog.reject()
        assert [p.name for p in rejected] == ["r0.pdf"]
        assert dialog.index == 0 and dialog.pdfs[0].name == "r1.pdf"
        dialog.accept()
        assert [s.path.name for s in got] == ["r1.pdf"]

    def test_a_blank_field_can_be_marked_as_not_on_every_document(
        self, window, tmp_path, sync_tasks, monkeypatch
    ):
        from test_cash_and_online import KIND, lab_profile, receipt
        from waprinter.ui import validation as screen
        from waprinter.validation import Reader

        marked, got = [], []
        monkeypatch.setattr(screen.messagebox, "askyesno", lambda *a, **k: True)
        dialog = screen.ReviewSamples(
            window.root, [receipt(tmp_path / "c.pdf", ["CASH"])], KIND.name, lab_profile(),
            Settings(), Reader(), got.extend, on_optional=marked.append,
        )
        dialog.accept()
        assert marked == ["transaction_id"]
        assert got[0].values["transaction_id"] == ""


class TestTheSampleList:
    @pytest.fixture
    def teacher(self, restore, tmp_path, sync_tasks, monkeypatch):
        from test_cash_and_online import receipt
        from waprinter.ui import teach

        window = teach.TeachWindow(restore.setup_view, receipt(tmp_path / "taught.pdf", ["CASH"]),
                                   None, on_saved=lambda key: None)
        window.name_var.set("Lab")
        window.title_var.set("LAB RECEIPT")
        window.info = []
        monkeypatch.setattr(teach.messagebox, "showinfo", lambda *a, **k: window.info.append(a[1]))
        yield window
        window.close()

    def _add(self, window, monkeypatch, *paths):
        from waprinter.ui import teach

        monkeypatch.setattr(teach.filedialog, "askopenfilenames", lambda **k: [str(p) for p in paths])
        monkeypatch.setattr(window, "review_samples", lambda: None)
        window.add_samples()

    def test_the_sample_it_was_taught_from_is_listed_first(self, teacher):
        rows = teacher._sample_rows()
        assert [r[1].name for r in rows] == ["taught.pdf"]
        assert rows[0][2] == "Not reviewed yet"
        assert teacher.sample_count.cget("text").startswith("0 of 3")

    def test_a_pdf_picked_by_mistake_can_be_removed(self, teacher, tmp_path, monkeypatch):
        from test_cash_and_online import receipt

        wrong = receipt(tmp_path / "wrong.pdf", ["CASH"], bill="W")
        self._add(teacher, monkeypatch, wrong)
        digest = next(r[0] for r in teacher._sample_rows() if r[1].name == "wrong.pdf")
        teacher.sample_tree.selection_set(digest)
        teacher.remove_sample()
        assert [r[1].name for r in teacher._sample_rows()] == ["taught.pdf"]

    def test_a_reviewed_one_comes_out_of_the_count_when_removed(self, teacher, tmp_path, monkeypatch):
        from test_cash_and_online import receipt
        from waprinter.validation import Sample

        wrong = receipt(tmp_path / "wrong.pdf", ["CASH"], bill="W")
        teacher._samples_reviewed([Sample(wrong, "lab", {}, "")])
        assert teacher.sample_count.cget("text").startswith("1 of 3")
        teacher.sample_tree.selection_set(teacher._digest(wrong))
        teacher.remove_sample()
        assert teacher.sample_count.cget("text").startswith("0 of 3")

    def test_the_pdf_taught_from_stays(self, teacher):
        teacher.sample_tree.selection_set(teacher._sample_rows()[0][0])
        teacher.remove_sample()
        assert len(teacher._sample_rows()) == 1
        assert "Cancel and start again" in teacher.info[0]

    def test_adding_the_same_pdf_again_says_why_the_count_did_not_move(
        self, teacher, tmp_path, monkeypatch
    ):
        import shutil

        copy = tmp_path / "copy-of-taught.pdf"
        shutil.copy(teacher.pdf, copy)
        self._add(teacher, monkeypatch, copy)
        assert len(teacher._sample_rows()) == 1
        assert "already in this list" in teacher.sample_count.cget("text")

    def test_a_reviewed_sample_is_not_reviewed_again_for_a_new_one(self, teacher, tmp_path, monkeypatch):
        from test_cash_and_online import receipt
        from waprinter.ui import validation as screen
        from waprinter.validation import Sample

        opened = []
        monkeypatch.setattr(screen, "ReviewSamples", lambda parent, pdfs, *a, **k: opened.append(pdfs))
        teacher._samples_reviewed([Sample(teacher.pdf, "lab", {}, "")])
        new = receipt(tmp_path / "new.pdf", ["CASH"], bill="N")
        teacher.pending.append(new)
        teacher._refresh_samples()
        teacher.review_samples()
        assert [p.name for p in opened[0]] == ["new.pdf"]

    def test_a_field_added_later_asks_for_that_sample_again(self, teacher):
        from waprinter.extract.profile import FieldRule
        from waprinter.validation import Sample

        teacher._samples_reviewed([Sample(teacher.pdf, "lab", {}, "+919876543210")])
        assert teacher._sample_rows()[0][2] == "Reviewed"
        teacher.rules = [FieldRule(name="bill", label="Bill no", kind="code")]
        teacher.refresh_fields()
        assert teacher._sample_rows()[0][2].startswith("Review again")

    def test_dragging_over_two_lines_selects_both(self, restore, tmp_path, sync_tasks):
        from test_cash_and_online import ONLINE, receipt
        from waprinter.ui.teach import TeachWindow

        window = TeachWindow(restore.setup_view, receipt(tmp_path / "o.pdf", ONLINE), None,
                             on_saved=lambda key: None)
        try:
            def at(text):
                hit = next(h for h in window._hits
                           if window._rows[h[4]].words[h[5]].text == text)
                return hit[4], hit[5]

            window._anchor = at("ONLINE-UPI")
            window._select_to(*at("T9981234"))
            assert window.selection.value == "ONLINE-UPI Txn ID: T9981234"
            assert window.lines_var.get() is True
            assert len(window.canvas.find_withtag("select")) == 2
        finally:
            window.close()

    def test_pointing_at_a_label_is_what_the_field_is_found_by(self, restore, tmp_path, sync_tasks):
        from test_cash_and_online import receipt
        from waprinter.ui.teach import TeachWindow

        window = TeachWindow(restore.setup_view, receipt(tmp_path / "c.pdf", ["CASH"]), None,
                             on_saved=lambda key: None)
        try:
            def at(text):
                hit = next(h for h in window._hits
                           if window._rows[h[4]].words[h[5]].text == text)
                return hit[4], hit[5]

            window._anchor = at("CASH")
            window._select_to(*at("CASH"))
            window.choose_label()
            window._pick_label(*at("Paymode"))
            assert "Paymode" in window.found_label.cget("text")
            window.field_var.set("Payment mode")
            window.add_field()
            assert window.rules[0].label == "Paymode"
        finally:
            window.close()


class TestChangingATemplate:
    @pytest.fixture
    def photo(self, restore):
        from waprinter.send.templates import MessageTemplate

        restore.pipeline.templates.put(MessageTemplate(
            name="photo_receipt", status="approved", header_format="IMAGE",
            header_document=False, body="Hello",
        ))
        yield "photo_receipt"
        restore.pipeline.templates._templates.pop("photo_receipt@en", None)
        restore.pipeline.templates.save()

    def test_a_photo_template_is_saved_with_the_image_setting(self, restore, photo, monkeypatch):
        from waprinter.ui import setup

        monkeypatch.setattr(setup.messagebox, "showinfo", lambda *a, **k: None)
        restore.settings.document_templates = {"removal_notice": "removal_notice"}
        page = _messages(restore)
        page.select_kind("removal_notice")
        _choose(page.template_box, lambda v: v.startswith(photo))
        assert page.attachment() == "image"
        assert page.unsaved.cget("text") == "Not saved yet"
        assert page.save() is True
        assert page.unsaved.cget("text") == ""
        assert restore.settings.document_templates["removal_notice"] == photo
        assert restore.settings.document_attachments["removal_notice"] == "image"

    def test_it_reads_as_usable_on_the_templates_page(self, restore, photo):
        restore.show_setup("templates")
        page = restore.setup_view.pages["templates"]
        page.tree.selection_set(f"{photo}@en")
        page._show_selected()
        assert page.problem.cget("text").startswith("Can be used, with its image header")

    def test_receipts_going_to_images_leave_the_notices_as_pdfs(self, restore, photo, monkeypatch):
        from waprinter.ui import setup

        monkeypatch.setattr(setup.messagebox, "showinfo", lambda *a, **k: None)
        restore.settings.attachment_format = "pdf"
        restore.settings.document_attachments = {}
        restore.settings.document_templates = {"removal_notice": "removal_notice"}
        page = _messages(restore)
        page.select_kind(DEFAULT_KIND)
        _choose(page.template_box, lambda v: v.startswith(photo))
        page.save()
        assert restore.settings.attachment_format == "image"
        assert restore.settings.document_attachments["removal_notice"] == "pdf"

    def test_leaving_with_a_choice_unsaved_asks_first(self, restore, photo, monkeypatch):
        from waprinter.ui import setup

        asked = []
        restore.settings.document_templates = {"removal_notice": "removal_notice"}
        page = _messages(restore)
        page.select_kind("removal_notice")
        _choose(page.template_box, lambda v: v.startswith(photo))
        monkeypatch.setattr(setup.messagebox, "askyesnocancel",
                            lambda *a, **k: asked.append(a) or None)

        restore.setup_view._nav_selected("templates")
        assert restore.setup_view.current == "messages"   # Cancel stays
        assert page.template_ref() == photo

        _choose(page.kind_box, lambda v: v == "Receipts")
        assert page.kind_key() == "removal_notice"        # Cancel again
        assert len(asked) == 2

        monkeypatch.setattr(setup.messagebox, "askyesnocancel", lambda *a, **k: False)
        restore.setup_view._nav_selected("templates")     # discard
        assert restore.setup_view.current == "templates"
        assert restore.settings.document_templates["removal_notice"] == "removal_notice"
        assert not page.dirty()

    def test_changing_send_as_keeps_the_choices_already_made(self, restore, monkeypatch):
        restore.settings.document_templates = {"removal_notice": "removal_notice"}
        page = _messages(restore)
        page.select_kind("removal_notice")
        row = next(iter(page._rows.values()))
        row[0].set(sm_label := "Leave blank (sends -)")
        _choose(page.attachment_box, lambda v: v.startswith("Image"))
        assert next(iter(page._rows.values()))[0].get() == sm_label

    def test_manual_sending_warns_the_image_does_nothing_yet(self, restore):
        restore.settings.send_mode = "link"
        page = _messages(restore)
        page.select_kind(DEFAULT_KIND)
        _choose(page.attachment_box, lambda v: v.startswith("Image"))
        assert "does nothing yet" in page.status.cget("text")


class TestCopyAndPaste:
    @pytest.fixture
    def box(self, window):
        import tkinter as tk
        from tkinter import ttk

        top = tk.Toplevel(window.root)
        yield ttk.Entry(top), ttk.Combobox(top, values=["one"], state="readonly"), tk.Text(top)
        top.destroy()

    @pytest.fixture
    def menu(self, window):
        from waprinter.ui import clipboard

        menu = clipboard._menu(window.root)
        yield menu
        menu.destroy()

    @staticmethod
    def _items(menu):
        return {menu.entrycget(i, "label"): menu.entrycget(i, "state")
                for i in range(menu.index("end") + 1) if menu.type(i) == "command"}

    def test_pasting_takes_the_line_break_off(self, window, box):
        entry = box[0]
        window.root.clipboard_clear()
        window.root.clipboard_append("9876543210\r\n")
        entry.event_generate("<<Paste>>")
        window.root.update()
        assert entry.get() == "9876543210"

    def test_pasting_replaces_what_is_selected(self, window, box):
        entry = box[0]
        entry.insert(0, "old")
        entry.selection_range(0, "end")
        window.root.clipboard_clear()
        window.root.clipboard_append("new")
        entry.event_generate("<<Paste>>")
        window.root.update()
        assert entry.get() == "new"

    def test_a_read_only_drop_down_cannot_be_pasted_into(self, window, box):
        combo = box[1]
        combo.current(0)
        window.root.clipboard_clear()
        window.root.clipboard_append("nope")
        combo.event_generate("<<Paste>>")
        window.root.update()
        assert combo.get() == "one"

    def test_the_menu_on_a_box_offers_what_is_possible(self, box, menu):
        from waprinter.ui import clipboard

        entry = box[0]
        entry.insert(0, "text")
        clipboard._box_items(menu, entry)
        items = self._items(menu)
        assert items["Paste"] == "normal" and items["Select all"] == "normal"
        assert items["Copy"] == "disabled"     # nothing selected yet

    def test_with_a_selection_it_can_be_copied(self, window, box, menu):
        from waprinter.ui import clipboard

        entry = box[0]
        entry.insert(0, "text")
        entry.selection_range(0, "end")
        clipboard._box_items(menu, entry)
        assert self._items(menu)["Copy"] == "normal"
        entry.event_generate("<<Copy>>")
        window.root.update()
        assert window.root.clipboard_get() == "text"

    def test_a_read_only_drop_down_can_still_be_copied(self, window, box, menu):
        from waprinter.ui import clipboard

        combo = box[1]
        combo.current(0)
        clipboard._box_items(menu, combo)
        assert list(self._items(menu)) == ["Copy"]
        menu.invoke(0)
        assert window.root.clipboard_get() == "one"

    def test_a_read_only_text_can_be_selected_and_copied(self, window, box, menu):
        from waprinter.ui import clipboard

        text = box[2]
        text.insert("1.0", "shown")
        text.configure(state="disabled")
        clipboard._select_all(text)
        clipboard._box_items(menu, text)
        items = self._items(menu)
        assert items["Copy"] == "normal" and items["Paste"] == "disabled"

    def test_a_hidden_token_can_be_pasted_but_not_copied(self, window, box, menu):
        from waprinter.ui import clipboard

        entry = box[0]
        entry.configure(show="•")
        clipboard.guard_secret(entry)
        entry.insert(0, "SECRET")
        entry.selection_range(0, "end")
        window.root.clipboard_clear()
        window.root.clipboard_append("kept")
        entry.event_generate("<<Copy>>")
        window.root.update()
        assert window.root.clipboard_get() == "kept"
        clipboard._box_items(menu, entry)
        assert self._items(menu)["Copy"] == "disabled"
        assert self._items(menu)["Paste"] == "normal"

    def test_selected_text_stands_out_from_its_box(self):
        from waprinter.ui import theme

        assert theme.SELECTED_TEXT != theme.WASH
        channel = lambda colour, i: int(colour[i:i + 2], 16)
        red_gap = channel(theme.CARD, 1) - channel(theme.SELECTED_TEXT, 1)
        assert red_gap > 60

    def test_a_table_row_copies_as_tab_separated_text(self, window):
        from tkinter import ttk
        from types import SimpleNamespace

        from waprinter.ui import clipboard

        tree = ttk.Treeview(window.root, columns=("a", "b"))
        try:
            tree.pack()
            tree.insert("", "end", iid="r", text="Name", values=("Lab", "9"))
            window.root.update()
            box = tree.bbox("r")
            assert clipboard._row_text(tree, SimpleNamespace(y=box[1] + 2)) == "Name\tLab\t9"
        finally:
            tree.destroy()
