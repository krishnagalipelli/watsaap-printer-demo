"""The setup screens: everything an engineer does once, away from the counter.

The operator's side bar opens three pages -- Status, Needs attention, Recent
documents -- because that is all a clerk printing receipts needs. Everything
that decides *what* goes out lives here instead, behind an optional PIN, as
seven steps in the order they are done:

    Connect WhatsApp     paste a token; the account and number are looked up
    Message templates    what Meta has approved, and what each one needs
    Document types       teach a new kind of paperwork from a sample PDF
    Fill in messages     which template each type uses, and what fills it
    Try it               check a PDF, or re-read the last thirty prints
    Counter settings     sending behaviour, scanned pages, the receipts folder
    Share setup          copy all of this to the other counters; set the PIN

Widgets and wiring only. What the pages say and decide is in setupmodel.py and
teaching.py, where it is tested without a display. Anything that talks to
Meta or reads a folder of PDFs runs on a worker thread and reports back
through `Tasks`, so the window never freezes while it waits.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from copy import deepcopy
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import teaching
from ..extract.profile import DEFAULT_KIND
from ..extract.rules import display_name
from ..send.templates import FIXED_TEXT_PREFIX, PLACEHOLDER, STATUS_WORDS
from . import icons, theme
from . import setupmodel as sm

log = logging.getLogger(__name__)

TITLE = "WhatsApp Printer setup"

TOKEN_HINT = (
    "Paste the access token for your WhatsApp Business account. The account "
    "and phone number are read from it, so there are no IDs to type.\n\n"
    "Use a permanent token made for a System User in Meta Business Settings. "
    "The temporary token on the API Setup page stops working within a day."
)


class Tasks:
    """Run work off the Tk thread and hand the result back on it."""

    def __init__(self, widget: tk.Misc):
        self.widget = widget
        self._results: queue.Queue = queue.Queue()
        self.busy: set[str] = set()
        self.widget.after(150, self._drain)

    def run(self, name: str, work, done) -> bool:
        """Start `work()` unless a task of this name is already running.

        `done(result)` is called on the Tk thread with the return value, or
        with the exception if it raised.
        """
        if name in self.busy:
            return False
        self.busy.add(name)

        def worker() -> None:
            try:
                result = work()
            except Exception as exc:  # reported, never swallowed
                log.info("setup task %s failed", name, exc_info=True)
                result = exc
            self._results.put((name, result, done))

        threading.Thread(target=worker, name=f"setup-{name}", daemon=True).start()
        return True

    def _drain(self) -> None:
        try:
            while True:
                name, result, done = self._results.get_nowait()
                self.busy.discard(name)
                try:
                    done(result)
                except Exception:
                    log.exception("setup task %s could not report", name)
        except queue.Empty:
            pass
        try:
            self.widget.after(150, self._drain)
        except tk.TclError:
            pass  # the window is gone


def _hint(parent, text: str) -> ttk.Label:
    label = theme.autowrap(ttk.Label(parent, text=text, style="Hint.TLabel",
                                     justify="left"))
    label.pack(anchor="w", fill="x", pady=(0, 10))
    return label


def _wrapping(parent, **options) -> ttk.Label:
    """A label that wraps at the width it is given. The caller packs it."""
    return theme.autowrap(ttk.Label(parent, justify="left", **options))


def _scrolling(frame: ttk.Frame) -> ttk.Frame:
    """The page's content, in a region that scrolls when the window is short.

    For pages whose content has a fixed height. A counter screen can be 768
    pixels tall, and what does not fit would otherwise be cut off or crushed.
    """
    inner = ttk.Frame(theme.scrollable(frame), padding=(0, 0, 14, 4))
    inner.pack(fill="both", expand=True)
    return inner


def _readonly_text(parent, height: int) -> tk.Text:
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    text = theme.style_text(tk.Text(frame, height=height, wrap="word", padx=10, pady=8))
    bar = ttk.Scrollbar(frame, command=text.yview)
    text.configure(yscrollcommand=bar.set, state="disabled")
    bar.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    return text


def _set_text(widget: tk.Text, value: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    widget.configure(state="disabled")


class Page:
    """One step of setup. Subclasses fill `self.frame` in build()."""

    def __init__(self, view: "SetupView"):
        self.view = view
        self.frame = ttk.Frame(view.stack)
        self.build()

    @property
    def window(self):
        return self.view.window

    @property
    def pipeline(self):
        return self.view.window.pipeline

    @property
    def settings(self):
        return self.view.window.pipeline.settings

    @property
    def root(self):
        return self.view.window.root

    def build(self) -> None:
        pass

    def on_show(self) -> None:
        pass

    def apply(self, changed) -> bool:
        """Commit a changed copy of the settings. False, with a message, if not."""
        try:
            self.pipeline.apply_settings(changed)
        except Exception as exc:
            messagebox.showerror(
                TITLE,
                f"Could not save. Your previous settings are still active.\n\n{exc}",
                parent=self.root,
            )
            return False
        self.view.refresh_checklist()
        self.window.refresh()
        return True


# -- 1. connect ------------------------------------------------------------------


class ConnectPage(Page):
    def build(self) -> None:
        f = _scrolling(self.frame)
        self.status = _wrapping(f)
        self.status.pack(anchor="w", fill="x", pady=(0, 14))

        box = theme.card(f, "Access token", padding=(20, 18))
        box.pack(fill="x")
        _hint(box, TOKEN_HINT)
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.token = tk.StringVar()
        self.token_entry = ttk.Entry(row, textvariable=self.token, show="•")
        self.token_entry.pack(side="left", fill="x", expand=True)
        # Pasting a 200-character token blind is how half of one gets saved.
        self.show_button = ttk.Button(row, text="Show", width=6, command=self._toggle_token)
        self.show_button.pack(side="left", padx=(6, 0))
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(
            buttons, text="Look up account", style="Mint.TButton", compound="right",
            image=icons.icon(f, "arrow", theme.CARD, 14, before=6), command=self.look_up,
        ).pack(side="left")
        self.stored_button = ttk.Button(
            buttons, text="Check the token already stored",
            command=lambda: self.look_up(stored=True),
        )
        self.stored_button.pack(side="left", padx=(6, 0))

        self.account_row = ttk.Frame(box)
        ttk.Label(self.account_row, text="Business account ID").pack(side="left")
        self.account = tk.StringVar()
        ttk.Entry(self.account_row, textvariable=self.account, width=24).pack(
            side="left", padx=6
        )
        ttk.Button(self.account_row, text="Look up",
                   command=lambda: self.look_up(stored=self._asked_stored)).pack(side="left")

        self.result = _wrapping(box)
        self.result.pack(anchor="w", fill="x", pady=(10, 0))

        choose = theme.card(f, "Send from", padding=(20, 18))
        choose.pack(fill="x", pady=(12, 0))
        self.numbers = ttk.Combobox(choose, state="readonly")
        self.numbers.pack(fill="x")
        self.save_button = ttk.Button(
            choose, text="Save and load templates", style="Mint.TButton", command=self.save
        )
        self.save_button.pack(anchor="w", pady=(12, 0))

        self._found: list = []
        self._token_used = ""
        self._from_stored = False
        self._asked_stored = False

    def _toggle_token(self) -> None:
        hidden = str(self.token_entry.cget("show")) != ""
        self.token_entry.configure(show="" if hidden else "•")
        self.show_button.configure(text="Hide" if hidden else "Show")

    def on_show(self) -> None:
        from ..secrets import load_token, token_problem

        s = self.settings
        try:
            problem = token_problem(load_token())
        except Exception as exc:
            problem = f"The stored token could not be read ({exc})."
        if not problem and s.phone_number_id:
            self.status.configure(
                text=f"Connected. Sending from phone number ID {s.phone_number_id}"
                f" on account {s.business_account_id or '(unknown)'}. Look up a "
                f"new token below to change it."
            )
        else:
            self.status.configure(text="Not connected. " + (problem or "Choose a number to send from."))
        if problem:
            self.stored_button.state(["disabled"])
        else:
            self.stored_button.state(["!disabled"])

    def look_up(self, stored: bool = False) -> None:
        from ..secrets import load_token, token_problem
        from ..send.meta_account import inspect

        self._asked_stored = stored
        token = load_token() if stored else self.token.get().strip()
        problem = token_problem(token)
        if problem:
            self.result.configure(text=problem)
            return
        account = self.account.get().strip()
        version = self.settings.graph_api_version
        self.result.configure(text="Asking Meta…")
        self.view.tasks.run(
            "connect",
            lambda: inspect(token, version, account),
            lambda outcome: self._looked_up(outcome, token, stored),
        )

    def _looked_up(self, outcome, token: str, stored: bool) -> None:
        if isinstance(outcome, Exception):
            self.result.configure(text=f"Could not connect: {outcome}")
            return
        self._found = [(a, n) for a in outcome.accounts for n in a.phone_numbers]
        self._token_used, self._from_stored = token, stored
        lines = [outcome.expiry_text(), *outcome.problems]
        if not self._found:
            self.account_row.pack(anchor="w", pady=(10, 0), before=self.result)
            lines.append("No phone numbers found yet.")
        else:
            self.account_row.pack_forget()
            lines.append(f"Found {len(self._found)} number(s). Choose one below.")
        self.result.configure(text="\n".join(lines))
        labels = [f"{n.label}   ({a.name or a.id})" for a, n in self._found]
        self.numbers.configure(values=labels)
        current = next(
            (i for i, (_a, n) in enumerate(self._found) if n.id == self.settings.phone_number_id),
            0,
        )
        if labels:
            self.numbers.current(current)

    def save(self) -> None:
        from ..config import paths
        from ..secrets import save_token
        from ..send.sync import sync_templates
        from ..send.templates import TemplateStore

        index = self.numbers.current()
        if index < 0 or not self._found:
            messagebox.showerror(TITLE, "Look up a token and choose a number first.",
                                 parent=self.root)
            return
        account, number = self._found[index]
        if not self._from_stored:
            try:
                save_token(self._token_used)
            except Exception as exc:
                messagebox.showerror(TITLE, f"The token was not stored.\n\n{exc}",
                                     parent=self.root)
                return
        changed = deepcopy(self.settings)
        changed.business_account_id = account.id
        changed.phone_number_id = number.id
        if not self.apply(changed):
            return
        self.token.set("")
        self.result.configure(text=f"Saved. Loading templates for {number.label}…")
        frozen = deepcopy(changed)
        token = self._token_used

        def work():
            store = TemplateStore(paths().templates, frozen.business_name,
                                  frozen.template_language)
            return sync_templates(frozen, store, token)

        def done(outcome) -> None:
            if isinstance(outcome, Exception):
                self.result.configure(
                    text=f"Connected to {number.label}, but the templates could "
                    f"not be loaded: {outcome}"
                )
            else:
                self.pipeline.templates.load()
                self.result.configure(
                    text=f"Connected to {number.label}. Loaded {outcome} template(s)."
                )
            self.on_show()
            self.view.refresh_checklist()
            self.window.refresh()

        self.view.tasks.run("sync", work, done)


# -- 2. templates -------------------------------------------------------------------


class TemplatesPage(Page):
    def build(self) -> None:
        f = self.frame
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(0, 12))
        self.refresh_button = ttk.Button(
            row, text="Refresh from WhatsApp", style="Mint.TButton", compound="left",
            image=icons.icon(f, "refresh", theme.CARD, 14, after=7),
            command=self.refresh_from_meta,
        )
        self.refresh_button.pack(side="left")
        self.when = ttk.Label(row, style="Hint.TLabel")
        self.when.pack(side="left", padx=12)

        self.tree = ttk.Treeview(
            f, columns=("language", "status", "pdf", "fills"),
            show="tree headings", height=7, selectmode="browse",
        )
        theme.headings(self.tree, (
            ("#0", "Template", 150), ("language", "Language", 70),
            ("status", "Status", 130), ("pdf", "Attaches PDF", 90),
            ("fills", "Fills in", 130),
        ))
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())

        self.problem = _wrapping(f, style="Warn.TLabel")
        self.problem.pack(anchor="w", fill="x", pady=(12, 6))
        self.body = _readonly_text(f, height=8)

    def on_show(self) -> None:
        store = self.pipeline.templates
        self.when.configure(
            text=f"Last refreshed {store.refreshed_at.replace('T', ' ')}"
            if store.refreshed_at else "Not refreshed from WhatsApp yet."
        )
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for t in sorted(store.all(), key=lambda t: (not t.usable, t.name, t.language)):
            self.tree.insert(
                "", "end", iid=t.ref, text=t.name,
                values=(
                    t.language,
                    STATUS_WORDS.get(t.status, t.status),
                    "Yes" if t.header_document else "No",
                    ", ".join(t.placeholders) or "—",
                ),
            )
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        self._show_selected()

    def _show_selected(self) -> None:
        selection = self.tree.selection()
        template = self.pipeline.templates.get(selection[0]) if selection else None
        if template is None:
            self.problem.configure(text="Choose a template to see its wording.")
            _set_text(self.body, "")
            return

        def example(m) -> str:
            value = template.examples.get(m.group(1))
            return f"«{value}»" if value else m.group(0)

        text = PLACEHOLDER.sub(example, template.body)
        if template.footer:
            text += f"\n\n{template.footer}"
        _set_text(self.body, text)
        self.problem.configure(
            text=sm.template_problem(template, "api")
            or "Can be used. Values in «» are Meta's examples."
        )

    def refresh_from_meta(self) -> None:
        from ..config import paths
        from ..send.sync import sync_templates
        from ..send.templates import TemplateStore

        frozen = deepcopy(self.settings)

        def work():
            store = TemplateStore(paths().templates, frozen.business_name,
                                  frozen.template_language)
            return sync_templates(frozen, store)

        def done(outcome) -> None:
            self.refresh_button.state(["!disabled"])
            if isinstance(outcome, Exception):
                messagebox.showerror(TITLE, str(outcome), parent=self.root)
                return
            self.pipeline.templates.load()
            self.on_show()
            self.view.refresh_checklist()
            self.window.refresh()

        if self.view.tasks.run("sync", work, done):
            self.refresh_button.state(["disabled"])
            self.when.configure(text="Loading from WhatsApp…")


# -- 3. document types ---------------------------------------------------------------


class DocumentsPage(Page):
    def build(self) -> None:
        f = self.frame
        _hint(f, "Each kind of paperwork is recognised by its title and can go "
                 "out under its own message. Receipts carry no title, so they "
                 "need positive identification. Teach from a PDF, then validate at least three samples: click the "
                 "values that matter and name them.")
        self.tree = ttk.Treeview(
            f, columns=("title", "fields", "message"),
            show="tree headings", height=8, selectmode="browse",
        )
        theme.headings(self.tree, (
            ("#0", "Document type", 170), ("title", "Recognised by", 130),
            ("fields", "Fields taught", 150), ("message", "Message", 120),
        ))
        self.tree.pack(fill="x")
        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Teach a new type from a PDF…", style="Mint.TButton",
                   command=self.teach_new).pack(side="left")
        ttk.Button(buttons, text="Teach fields for this type…",
                   command=self.teach_selected).pack(side="left", padx=6)
        self.remove_button = ttk.Button(buttons, text="Remove type",
                                        command=self.remove)
        self.remove_button.pack(side="right")

    def kinds(self) -> list[tuple[str, str]]:
        """(key, label) for every type worth showing, default first."""
        profile = self.pipeline.profile
        rows = [(DEFAULT_KIND, teaching.DEFAULT_LABEL)]
        for kind in profile.all_kinds:
            if kind.name == "receipt":
                continue  # the default row stands for receipts
            rows.append((kind.name, teaching.kind_label(kind.name)))
        return rows

    def on_show(self) -> None:
        profile = self.pipeline.profile
        s = self.settings
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        by_name = {k.name: k for k in profile.all_kinds}
        for key, label in self.kinds():
            kind = by_name.get(key)
            rules = profile.rules_for(key)
            message = (
                s.default_template if key == DEFAULT_KIND
                else s.document_templates.get(key, "not chosen")
            )
            self.tree.insert(
                "", "end", iid=key, text=label,
                values=(
                    ", ".join(kind.match) if kind else "(no title needed)",
                    ", ".join(display_name(r.name) for r in rules) or "none",
                    message,
                ),
            )
        keep = selected[0] if selected and self.tree.exists(selected[0]) else DEFAULT_KIND
        self.tree.selection_set(keep)

    def _selected(self) -> str:
        selection = self.tree.selection()
        return selection[0] if selection else DEFAULT_KIND

    def teach_new(self) -> None:
        pdf = filedialog.askopenfilename(
            parent=self.root, title="Choose a sample of the new document",
            filetypes=[("PDF documents", "*.pdf")],
        )
        if pdf:
            self._open_teacher(Path(pdf), None)

    def teach_selected(self) -> None:
        key = self._selected()
        sample = teaching.sample_path(key)
        if not sample.exists():
            chosen = filedialog.askopenfilename(
                parent=self.root,
                title=f"Choose a sample: {teaching.kind_label(key)}",
                filetypes=[("PDF documents", "*.pdf")],
            )
            if not chosen:
                return
            sample = Path(chosen)
        self._open_teacher(sample, key)

    def _open_teacher(self, pdf: Path, key: str | None) -> None:
        from .teach import TeachWindow

        TeachWindow(self.view, pdf, key, on_saved=self.taught)

    def taught(self, key: str) -> None:
        self.pipeline.reload_profile(force=True)
        self.on_show()
        self.view.refresh_checklist()
        self.window.refresh()
        self.view.show_page("messages")
        self.view.pages["messages"].select_kind(key)

    def remove(self) -> None:
        key = self._selected()
        if key not in {k.name for k in self.pipeline.profile.custom_kinds}:
            messagebox.showinfo(
                TITLE,
                "Only document types taught on this computer can be removed. "
                "Built-in types can be left without a message instead.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            TITLE,
            f"Remove '{teaching.kind_label(key)}'? These documents will no longer "
            f"be recognised, and will go out as receipts.",
            parent=self.root,
        ):
            return
        teaching.remove_kind(key)
        changed = deepcopy(self.settings)
        changed.document_templates.pop(key, None)
        changed.document_nouns.pop(key, None)
        self.apply(changed)
        self.pipeline.reload_profile(force=True)
        self.on_show()


# -- 4. fill in messages ----------------------------------------------------------------


class MessagesPage(Page):
    NONE_CHOICE = "— none: hold these documents for a person —"

    def build(self) -> None:
        # Save sits under everything, outside the part that scrolls, so a
        # template with many variables cannot push it off the window.
        bottom = ttk.Frame(self.frame)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(bottom, text="Save", style="Mint.TButton", command=self.save).pack(
            side="right")
        self.problems = _wrapping(bottom, style="Bad.TLabel")
        self.problems.pack(side="left", fill="x", expand=True, padx=(0, 12))

        f = _scrolling(self.frame)
        top = ttk.Frame(f)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Document type").grid(row=0, column=0, sticky="w", pady=3)
        self.kind_box = ttk.Combobox(top, state="readonly")
        self.kind_box.grid(row=0, column=1, sticky="ew", pady=3, padx=(10, 0))
        self.kind_box.bind("<<ComboboxSelected>>", lambda _e: self.load_kind())
        ttk.Label(top, text="WhatsApp template").grid(row=1, column=0, sticky="w", pady=3)
        self.template_box = ttk.Combobox(top, state="readonly")
        self.template_box.grid(row=1, column=1, sticky="ew", pady=3, padx=(10, 0))
        self.template_box.bind("<<ComboboxSelected>>", lambda _e: self.build_rows())
        ttk.Label(top, text="PDF named").grid(row=2, column=0, sticky="w", pady=3)
        self.noun = tk.StringVar()
        ttk.Entry(top, textvariable=self.noun).grid(row=2, column=1, sticky="ew",
                                                     pady=3, padx=(10, 0))
        self.noun.trace_add("write", lambda *_: self._update_filename())
        self.filename = ttk.Label(top, style="Hint.TLabel")
        self.filename.grid(row=3, column=1, sticky="w", padx=(10, 0))
        self.status = _wrapping(f, style="Warn.TLabel")
        self.status.pack(anchor="w", fill="x", pady=(6, 6))

        fill = theme.card(f, "Fill in the message", padding=(16, 14))
        fill.pack(fill="x")
        self.vars_box = ttk.Frame(fill)
        self.vars_box.pack(fill="x")
        self.vars_box.columnconfigure(1, weight=1)

        preview_box = theme.card(f, "What the member receives", padding=(16, 14))
        preview_box.pack(fill="x", pady=(12, 0))
        self.preview = _readonly_text(preview_box, height=6)

        self._kinds: list[tuple[str, str]] = []
        self._templates: list[tuple[str, str]] = []
        self._rows: dict[str, tuple] = {}
        self._fields: list[str] = []
        self._values: dict[str, str] = {}
        self._template = None

    # which document, which template

    def on_show(self) -> None:
        current = self.kind_key()
        self._kinds = self.view.pages["documents"].kinds()
        self.kind_box.configure(values=[label for _k, label in self._kinds])
        self.select_kind(current or DEFAULT_KIND)

    def kind_key(self) -> str:
        index = self.kind_box.current()
        return self._kinds[index][0] if 0 <= index < len(self._kinds) else ""

    def select_kind(self, key: str) -> None:
        keys = [k for k, _label in self._kinds]
        if key not in keys:
            self._kinds = self.view.pages["documents"].kinds()
            self.kind_box.configure(values=[label for _k, label in self._kinds])
            keys = [k for k, _label in self._kinds]
        self.kind_box.current(keys.index(key) if key in keys else 0)
        self.load_kind()

    def load_kind(self) -> None:
        key = self.kind_key()
        s = self.settings
        profile = self.pipeline.profile
        self._fields = teaching.available_fields(profile, key)
        try:
            self._values = teaching.example_values(self.pipeline.store, profile, key, s)
        except Exception:
            log.info("no example values for %s", key, exc_info=True)
            self._values = {}

        store = self.pipeline.templates
        choices = [] if key == DEFAULT_KIND else [(self.NONE_CHOICE, "")]
        for t in sorted(store.all(), key=lambda t: (bool(sm.template_problem(t, s.send_mode)), t.name)):
            ref = store.ref_for(t)
            choices.append((sm.template_choice(t, ref), ref))
        chosen = s.default_template if key == DEFAULT_KIND else s.document_templates.get(key, "")
        refs = [ref for _label, ref in choices]
        if chosen and chosen not in refs:
            template = store.get(chosen)
            if template is not None and store.ref_for(template) in refs:
                chosen = store.ref_for(template)
            else:
                # Saved, but not on this computer -- a typo, or a template
                # approved since the last refresh. Shown as what it is rather
                # than quietly as "none", which saving would then make true.
                choices.append((f"{chosen} · not on this computer", chosen))
                refs.append(chosen)
        self._templates = choices
        self.template_box.configure(values=[label for label, _ref in choices])
        self.template_box.current(refs.index(chosen) if chosen in refs else 0)
        self.noun.set(s.document_noun if key == DEFAULT_KIND else s.document_nouns.get(key, ""))
        self.build_rows()

    def template_ref(self) -> str:
        index = self.template_box.current()
        return self._templates[index][1] if 0 <= index < len(self._templates) else ""

    # what fills each variable

    def build_rows(self) -> None:
        for child in self.vars_box.winfo_children():
            child.destroy()
        self._rows = {}
        ref = self.template_ref()
        template = self.pipeline.templates.get(ref) if ref else None
        self._template = template
        if template is None:
            text = ("These documents will wait in Needs attention until a "
                    "message is chosen." if not ref else "That template is not on this computer.")
            ttk.Label(self.vars_box, text=text, style="Hint.TLabel").grid(row=0, column=0, sticky="w")
            self.status.configure(text="")
            _set_text(self.preview, "")
            self.problems.configure(text="")
            self._update_filename()
            return

        self.status.configure(text=sm.template_problem(template, self.settings.send_mode))
        saved = self.settings.template_mappings.get(template.name)
        guess = sm.suggest_mapping(
            template, self._fields,
            legacy=(
                sm.shared_map_for(template, self.settings.template_variables,
                                  self.settings.default_template)
                if saved is None else {}
            ),
            current=saved or {},
        )
        choices = sm.source_choices(self._fields)
        labels = [label for label, _source in choices]
        if not template.placeholders:
            ttk.Label(self.vars_box, text="This template has nothing to fill in.",
                      style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        for row, variable in enumerate(template.placeholders):
            ttk.Label(self.vars_box, text="{{%s}}" % variable).grid(
                row=row, column=0, sticky="w", pady=2)
            source_var = tk.StringVar()
            box = ttk.Combobox(self.vars_box, textvariable=source_var, values=labels,
                               state="readonly", width=30)
            box.grid(row=row, column=1, sticky="ew", pady=2, padx=6)
            fixed_var = tk.StringVar()
            fixed = ttk.Entry(self.vars_box, textvariable=fixed_var, width=18)
            hint = ttk.Label(self.vars_box, style="Hint.TLabel")
            hint.grid(row=row, column=3, sticky="w")
            source = guess.get(variable, "")
            source_var.set(sm.label_for_source(source, self._fields))
            if source.startswith(FIXED_TEXT_PREFIX):
                fixed_var.set(source[len(FIXED_TEXT_PREFIX):])
            self._rows[variable] = (source_var, fixed_var, fixed, hint)
            box.bind("<<ComboboxSelected>>", lambda _e: self.update_preview())
            fixed_var.trace_add("write", lambda *_: self.update_preview())
        self._update_filename()
        self.update_preview()

    def mapping(self) -> dict[str, str]:
        by_label = {label: source for label, source in sm.source_choices(self._fields)}
        mapping = {}
        for variable, (source_var, fixed_var, _fixed, _hint) in self._rows.items():
            source = by_label.get(source_var.get(), "")
            if source == FIXED_TEXT_PREFIX:
                source = FIXED_TEXT_PREFIX + fixed_var.get().strip()
            mapping[variable] = source
        return mapping

    def update_preview(self) -> None:
        template = self._template
        if template is None:
            return
        mapping = self.mapping()
        for variable, (source_var, _fixed_var, fixed, hint) in self._rows.items():
            source = mapping.get(variable, "")
            if source.startswith(FIXED_TEXT_PREFIX):
                fixed.grid(row=list(self._rows).index(variable), column=2, sticky="w")
            else:
                fixed.grid_remove()
            example = template.examples.get(variable)
            sample = self._values.get(source, "") if source else ""
            bits = []
            if sample:
                bits.append(f"reads “{sample[:28]}”")
            if example:
                bits.append(f"Meta's example: {example[:24]}")
            hint.configure(text="  ·  ".join(bits))
        _set_text(self.preview, sm.preview(template, mapping, self._values,
                                           self.settings.business_name))
        problems = sm.mapping_problems(template, mapping)
        self.problems.configure(text=" ".join(problems))

    def _update_filename(self) -> None:
        from ..models import ExtractedFields
        from ..send.templates import _filename

        noun = self.noun.get().strip() or "Document"
        number = self._values.get("invoice_number") or "CR1747/26"
        name = _filename(ExtractedFields(invoice_number=number), None, noun)
        self.filename.configure(text=f"The member sees: {name}")

    def save(self) -> None:
        key = self.kind_key()
        ref = self.template_ref()
        template = self._template
        s = self.settings
        changed = deepcopy(s)
        noun = self.noun.get().strip()

        if template is not None:
            mapping = self.mapping()
            problems = sm.mapping_problems(template, mapping)
            if problems:
                messagebox.showerror(TITLE, "\n".join(problems), parent=self.root)
                return
            blocker = sm.template_problem(template, s.send_mode)
            if blocker and not messagebox.askyesno(
                TITLE, f"{template.name}: {blocker}\n\nSave anyway?", parent=self.root
            ):
                return
            changed.template_mappings = {**changed.template_mappings, template.name: mapping}

        if key == DEFAULT_KIND:
            if not ref:
                messagebox.showerror(TITLE, "Receipts need a message.", parent=self.root)
                return
            changed.default_template = ref
            if noun:
                changed.document_noun = noun
        else:
            if ref:
                changed.document_templates[key] = ref
            else:
                changed.document_templates.pop(key, None)
            if noun:
                changed.document_nouns[key] = noun
            else:
                changed.document_nouns.pop(key, None)

        if self.apply(changed):
            self.view.pages["documents"].on_show()
            messagebox.showinfo(TITLE, f"Saved. {teaching.kind_label(key)} will use "
                                f"{ref or 'no message'} from the next print.",
                                parent=self.root)


# -- 5. try it -------------------------------------------------------------------------


class TestPage(Page):
    def build(self) -> None:
        f = self.frame
        one = theme.card(f, "Check one PDF", padding=(16, 14))
        one.pack(fill="both", expand=True)
        row = ttk.Frame(one)
        row.pack(fill="x", pady=(0, 8))
        ttk.Button(row, text="Choose a PDF…", style="Mint.TButton",
                   command=self.check_one).pack(side="left")
        ttk.Label(row, text="Reads it with the saved setup. Nothing is sent.",
                  style="Hint.TLabel").pack(side="left", padx=12)
        self.output = _readonly_text(one, height=6)

        many = theme.card(f, "Try on recent prints", padding=(16, 14))
        many.pack(fill="both", expand=True, pady=(12, 0))
        row = ttk.Frame(many)
        row.pack(fill="x", pady=(0, 8))
        self.trial_button = ttk.Button(row, text="Read the last 30 prints",
                                       command=self.run_trial)
        self.trial_button.pack(side="left")
        self.trial_note = ttk.Label(row, style="Hint.TLabel")
        self.trial_note.pack(side="left", padx=12)
        self.table = trial_table(many)

    def check_one(self) -> None:
        from ..setup_profile import check_pdf

        chosen = filedialog.askopenfilename(parent=self.root, title="Check a PDF",
                                            filetypes=[("PDF documents", "*.pdf")])
        if not chosen:
            return
        _set_text(self.output, "Reading…")

        def done(outcome) -> None:
            _set_text(self.output, f"Could not read it: {outcome}"
                      if isinstance(outcome, Exception) else str(outcome))

        self.view.tasks.run("check", lambda: check_pdf(self.pipeline, Path(chosen)), done)

    def run_trial(self) -> None:
        pdfs = teaching.recent_prints()
        if not pdfs:
            self.trial_note.configure(text="Nothing has been printed yet.")
            return
        profile, settings = self.pipeline.profile, deepcopy(self.settings)
        self.trial_note.configure(text=f"Reading {len(pdfs)} print(s)…")

        def done(outcome) -> None:
            if isinstance(outcome, Exception):
                self.trial_note.configure(text=f"Could not finish: {outcome}")
                return
            fill_trial_table(self.table, outcome)
            self.trial_note.configure(text=f"{len(outcome)} print(s) read. Nothing was sent.")

        self.view.tasks.run("trial", lambda: teaching.trial(profile, pdfs, settings), done)


def trial_table(parent) -> ttk.Treeview:
    table = ttk.Treeview(parent, columns=("kind", "mobile", "values"),
                         show="tree headings", height=5)
    theme.headings(table, (
        ("#0", "Print", 150), ("kind", "Read as", 110),
        ("mobile", "Mobile", 120), ("values", "Values", 240),
    ))
    table.pack(fill="both", expand=True)
    return table


def fill_trial_table(table: ttk.Treeview, rows) -> None:
    table.delete(*table.get_children())
    for row in rows:
        values = row.error or "  ·  ".join(
            f"{display_name(k)}: {v}" for k, v in row.values.items()
        )
        table.insert("", "end", text=row.file, values=(row.kind, row.mobile, values))


# -- 7. share ------------------------------------------------------------------------------


class SharePage(Page):
    def build(self) -> None:
        f = _scrolling(self.frame)
        copy = theme.card(f, "Copy this setup to other counters", padding=(20, 18))
        copy.pack(fill="x")
        _hint(copy, "Export writes one file with the templates, messages, document "
                    "types and taught fields. The access token is never included, "
                    "and each counter keeps its own branch, computer name and "
                    "receipts folder. Put the file beside the installer as "
                    "provision.json, or import it here on the other counter.")
        row = ttk.Frame(copy)
        row.pack(fill="x")
        ttk.Button(row, text="Export setup…", style="Mint.TButton",
                   command=self.window.export_setup).pack(side="left")
        ttk.Button(row, text="Import setup…", command=self.window.import_setup).pack(
            side="left", padx=6)

        lock = theme.card(f, "Setup PIN", padding=(20, 18))
        lock.pack(fill="x", pady=(12, 0))
        _hint(lock, "Asked for before these screens open, so a change to what "
                    "members receive is never made by accident. It keeps honest "
                    "people out; it is not a password.")
        self.pin_state = ttk.Label(lock, style="Field.TLabel")
        self.pin_state.pack(anchor="w", pady=(0, 10))
        row = ttk.Frame(lock)
        row.pack(fill="x")
        ttk.Button(row, text="Set PIN…", command=self.set_pin).pack(side="left")
        ttk.Button(row, text="Remove PIN", command=self.remove_pin).pack(side="left", padx=6)

    def on_show(self) -> None:
        self.pin_state.configure(
            text="A PIN is set." if self.settings.setup_pin else "No PIN is set."
        )

    def set_pin(self) -> None:
        pin = simpledialog.askstring(TITLE, "New PIN (4 to 8 digits):", show="•",
                                     parent=self.root)
        if pin is None:
            return
        problem = sm.pin_problem(pin)
        if problem:
            messagebox.showerror(TITLE, problem, parent=self.root)
            return
        again = simpledialog.askstring(TITLE, "Type the PIN again:", show="•",
                                       parent=self.root)
        if again != pin:
            messagebox.showerror(TITLE, "The two PINs were different. Nothing changed.",
                                 parent=self.root)
            return
        changed = deepcopy(self.settings)
        changed.setup_pin = sm.hash_pin(pin)
        if self.apply(changed):
            self.on_show()

    def remove_pin(self) -> None:
        if not self.settings.setup_pin:
            return
        if not messagebox.askyesno(TITLE, "Remove the setup PIN?", parent=self.root):
            return
        changed = deepcopy(self.settings)
        changed.setup_pin = ""
        if self.apply(changed):
            self.on_show()


# -- the view ------------------------------------------------------------------------------


class SetupView(ttk.Frame):
    """The steps down the left, and the page for the one chosen beside them."""

    def __init__(self, master, window):
        super().__init__(master, padding=(28, 26, 28, 22))
        self.window = window
        self.tasks = Tasks(self)

        side = ttk.Frame(self)
        side.pack(side="left", fill="y", padx=(0, 24))
        theme.eyebrow(side, "Setup steps").pack(anchor="w", padx=10, pady=(6, 10))
        self.nav: dict[str, ttk.Button] = {}
        for key, title in sm.STEP_TITLES:
            button = ttk.Button(side, text=title, style="Nav.TButton", compound="left",
                                command=lambda k=key: self._nav_selected(k))
            button.pack(fill="x", pady=1)
            self.nav[key] = button

        self.body = ttk.Frame(self)
        self.body.pack(side="left", fill="both", expand=True)
        head = ttk.Frame(self.body)
        head.pack(fill="x")
        self.caption = theme.eyebrow(head, "")
        self.caption.pack(anchor="w")
        self.title = ttk.Label(head, style="Title.TLabel")
        self.title.pack(anchor="w", pady=(4, 0))
        self.detail = ttk.Label(head, style="Hint.TLabel")
        self.detail.pack(anchor="w", pady=(2, 0))
        ttk.Separator(self.body).pack(fill="x", pady=(14, 16))
        self.stack = ttk.Frame(self.body)
        self.stack.pack(fill="both", expand=True)

        self.pages: dict[str, Page] = {
            "connect": ConnectPage(self),
            "templates": TemplatesPage(self),
            "documents": DocumentsPage(self),
            "messages": MessagesPage(self),
            "test": TestPage(self),
            "preferences": Page(self),   # the window builds the form into it
            "share": SharePage(self),
        }
        self.current = ""
        self._steps: dict[str, sm.Step] = {}

    def _nav_selected(self, key: str) -> None:
        if key != self.current:
            self.show_page(key)

    def show_page(self, key: str) -> None:
        if self.current and self.current in self.pages:
            self.pages[self.current].frame.pack_forget()
        self.current = key
        self.pages[key].frame.pack(fill="both", expand=True)
        self.refresh_checklist()
        mark = getattr(self.window, "_mark_nav", None)
        if mark is not None:
            mark()   # Setup or Message templates, in the window's side bar
        try:
            self.pages[key].on_show()
        except Exception:
            log.exception("could not show the %s page", key)

    def refresh_checklist(self) -> None:
        from ..secrets import load_token, token_problem

        pipeline = self.window.pipeline
        try:
            problem = token_problem(load_token())
        except Exception as exc:
            problem = str(exc)
        steps = sm.checklist(pipeline.settings, pipeline.templates, pipeline.profile, problem)
        self._steps = {step.key: step for step in steps}
        for step in steps:
            on = step.key == self.current
            # A tick for a finished step, an amber dot for one still to do.
            image = (icons.icon(self, "check", theme.MINT, 14, after=8) if step.done
                     else icons.icon(self, "dot", theme.WARM, 14, after=8))
            self.nav[step.key].configure(
                text=step.title, image=image,
                style="NavOn.TButton" if on else "Nav.TButton",
            )
        step = self._steps.get(self.current)
        if step:
            number = [key for key, _title in sm.STEP_TITLES].index(step.key) + 1
            self.caption.configure(
                text=f"Setup / {number:02d} of {len(sm.STEP_TITLES):02d}".upper())
            self.title.configure(text=step.title)
            self.detail.configure(text=step.detail)
