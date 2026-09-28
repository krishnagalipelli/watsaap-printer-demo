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
