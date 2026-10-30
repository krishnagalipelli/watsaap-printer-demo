"""The window where a document type is taught from one sample PDF.

The page is drawn on the left exactly as it printed. Clicking a word selects
it; dragging or shift-clicking extends the selection, along the line for a name
and down the following lines for payment details. The right-hand side says which label the
value was found beside, lets it be named -- a built-in field, the customer's
mobile, or anything new such as "Customer ID" -- and lists every field taught
so far with what it reads *on this page*. That last column is the check that
matters: a rule that reads something other than what was clicked is caught
here, not on a member's phone.

Save stores the type and its fields (teaching.save_teaching) and hands over to
Fill in messages, where the new fields become choices for the template.
"""

from __future__ import annotations

import base64
import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from copy import deepcopy

from .. import teaching
from ..extract.profile import DocumentKind, DocumentProfile, FieldRule
from ..extract.rules import (
    BUILTIN_FIELDS,
    RECIPIENT_FIELD,
    Selection,
    display_name,
    field_name,
    guess_kind,
    label_above,
    label_at,
    label_beside,
    rule_from_selection,
    selection_between,
)
from ..validation import MIN_SAMPLES, digest_of, unreviewed
from . import theme
from .setup import TITLE, Tasks, fill_trial_table, trial_table
from .setupmodel import _SYNONYMS

log = logging.getLogger(__name__)

ZOOM = 1.3
SIDE_WRAP = 330
SELECT_COLOUR = theme.MINT
HOVER_COLOUR = theme.blend(theme.MINT, theme.CARD, 0.45)

KIND_CHOICES = [
    ("Text", "text"),
    ("Code or number", "code"),
    ("Date", "date"),
    ("Amount", "amount"),
    ("Mobile number (who to send to)", "phone"),
]


def field_for_label(label: str) -> str:
    """A first guess at what to call a value, from the label beside it."""
    key = field_name(label) if label else ""
    for builtin, words in _SYNONYMS.items():
        if key and key in words and builtin in BUILTIN_FIELDS:
            return builtin
    return key


class TeachWindow(tk.Toplevel):
    def __init__(self, view, pdf: Path, kind_key: str | None, on_saved):
        super().__init__(view.window.root)
        self.view = view
        self.pipeline = view.window.pipeline
        self.pdf = Path(pdf)
        self.kind_key = kind_key
        self.on_saved = on_saved
        self.new = kind_key is None
        self.title("Teach a new document type" if self.new
                   else f"Teach fields: {teaching.kind_label(kind_key)}")
        theme.fit_window(self, 1180, 780, 960, 560)
        self.transient(view.window.root)
        self.configure(background=theme.PAPER)

        self.doc = None
        self._fitz = None
        self.page_index = 0
        self.selection: Selection | None = None
        self._anchor: tuple[int, int] | None = None
        self._dragging = False
        self._picking_label = False
        self._label_choice: tuple[str, str] | None = None
        self._hits: list[tuple[float, float, float, float, int, int]] = []
        self._rows: list = []
        self._image = None
        self.rules: list[FieldRule] = (
            [] if self.new else self.pipeline.profile.rules_for(kind_key)
        )
        self.clicked: dict[str, str] = {}
        self.tasks = Tasks(self)
        from ..validation import Reader, saved_samples
        self.validation_reader = Reader()
        self.validation_samples = [sample for sample in saved_samples()
                                   if sample.recipient is not None and not self.new and DocumentProfile.rules_key(sample.kind) ==
                                   DocumentProfile.rules_key(kind_key)]
        # PDFs added but not reviewed yet, and why the last Save refused any.
        self.pending: list[Path] = []
        self.sample_errors: dict[str, str] = {}
        self._sample_note = ""
        self._digests: dict[Path, str] = {}
        self._reviewer = None
        if self._digest(self.pdf) not in {self._digest(s.path) for s in self.validation_samples}:
            self.pending.append(self.pdf)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._read()

    # -- layout ----------------------------------------------------------------

    def _build(self) -> None:
        from .setup import _scrolling
        right_holder = ttk.Frame(self, width=400, padding=(8, 16, 18, 16))
        right_holder.pack(side="right", fill="y")
        right_holder.pack_propagate(False)
        # Packed before the part that scrolls, against the bottom edge, so a
        # short window takes room from the fields and never from these.
        footer = ttk.Frame(right_holder, padding=(0, 8, 14, 0))
        footer.pack(side="bottom", fill="x")
        self.problems = ttk.Label(footer, style="Bad.TLabel", wraplength=SIDE_WRAP,
                                  justify="left")
        self.problems.pack(anchor="w", pady=(0, 6))
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Try on recent prints", command=self.try_recent).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(buttons, text="Save", style="Mint.TButton", command=self.save).pack(
            side="right", padx=6)
        right = _scrolling(right_holder)
        left = ttk.Frame(self, padding=(18, 16, 8, 16))
        left.pack(side="left", fill="both", expand=True)

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(bar, text="◀", width=3, command=lambda: self.turn(-1)).pack(side="left")
        self.page_label = ttk.Label(bar, text="Reading…")
        self.page_label.pack(side="left", padx=8)
        ttk.Button(bar, text="▶", width=3, command=lambda: self.turn(1)).pack(side="left")
        ttk.Label(bar, text="Click a value. Drag or shift-click to take more words or lines.",
                  style="Hint.TLabel").pack(side="left", padx=12)

        holder = ttk.Frame(left, style="Card.TFrame", padding=1)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, background=theme.WASH, highlightthickness=0,
                                cursor="hand2")
        vbar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        hbar = ttk.Scrollbar(holder, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<Motion>", self.on_hover)
        self.canvas.bind(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"),
        )

        kind = theme.card(right, "Document type", padding=14)
        kind.pack(fill="x")
        if self.new:
            ttk.Label(kind, text="Name").pack(anchor="w")
            self.name_var = tk.StringVar()
            ttk.Entry(kind, textvariable=self.name_var, width=38).pack(fill="x")
            ttk.Label(kind, text="Recognised by").pack(anchor="w", pady=(6, 0))
            self.title_var = tk.StringVar()
            self.title_box = ttk.Combobox(kind, textvariable=self.title_var, width=36)
            self.title_box.pack(fill="x")
            ttk.Label(kind, text="Words printed on every one of these documents and "
                                 "on no other kind, usually the title.",
                      style="Hint.TLabel", wraplength=SIDE_WRAP, justify="left").pack(
                anchor="w", pady=(4, 0))
            self.title_var.trace_add("write", lambda *_: self.check())
        else:
            ttk.Label(kind, text=teaching.kind_label(self.kind_key),
                      style="Head.TLabel").pack(anchor="w")

        picked = theme.card(right, "Selected value", padding=14)
        picked.pack(fill="x", pady=(10, 0))
        self.value_label = ttk.Label(picked, text="Click a value on the page.",
                                     wraplength=SIDE_WRAP, justify="left")
        self.value_label.pack(anchor="w")
        self.found_label = ttk.Label(picked, style="Hint.TLabel",
                                     wraplength=SIDE_WRAP, justify="left")
        self.found_label.pack(anchor="w", pady=(2, 6))
        ttk.Label(picked, text="Call it").pack(anchor="w")
        self.field_var = tk.StringVar()
        self.field_box = ttk.Combobox(picked, textvariable=self.field_var, width=36)
        self.field_box.pack(fill="x")
        ttk.Label(picked, text="It is a").pack(anchor="w", pady=(6, 0))
        self.kind_box = ttk.Combobox(picked, values=[label for label, _k in KIND_CHOICES],
                                     state="readonly", width=36)
        self.kind_box.pack(fill="x")
        self.kind_box.bind("<<ComboboxSelected>>", lambda _e: self._kind_chosen())
        self.lines_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(picked, text="Can carry on over the next lines",
                        variable=self.lines_var).pack(anchor="w", pady=(8, 0))
        self.optional_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(picked, text="Not on every document",
                        variable=self.optional_var).pack(anchor="w")
        row = ttk.Frame(picked)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Choose its label", command=self.choose_label).pack(side="left")
        ttk.Button(row, text="Add field", style="Mint.TButton",
                   command=self.add_field).pack(side="right")

        taught = theme.card(right, "Fields", padding=14)
        taught.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(taught, columns=("label", "reads"), show="tree headings",
                                 height=6, selectmode="browse")
        theme.headings(self.tree, (("#0", "Field", 100), ("label", "Found by", 90),
                                   ("reads", "Reads here", 110)))
        self.tree.pack(fill="both", expand=True)
        row = ttk.Frame(taught)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Not on every document", command=self.toggle_optional).pack(side="left")
        ttk.Button(row, text="Remove field", command=self.remove_field).pack(side="right")

        samples = theme.card(right, "Validation samples", padding=14)
        samples.pack(fill="x", pady=(10, 0))
        self.sample_count = ttk.Label(samples, wraplength=SIDE_WRAP, justify="left")
        self.sample_count.pack(anchor="w", pady=(0, 6))
        self.sample_tree = ttk.Treeview(samples, columns=("state",), show="tree headings",
                                        height=4, selectmode="browse")
        theme.headings(self.sample_tree, (("#0", "PDF", 150), ("state", "Status", 150)))
        self.sample_tree.pack(fill="x")
        row = ttk.Frame(samples)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Add PDFs…", command=self.add_samples).pack(side="left")
        ttk.Button(row, text="Review", command=self.review_samples).pack(side="left", padx=6)
        ttk.Button(row, text="Remove", command=self.remove_sample).pack(side="right")
        self._refresh_field_choices()
        self._refresh_samples()

    # -- reading the sample -------------------------------------------------

    def _read(self) -> None:
        settings = self.pipeline.settings

        def done(outcome) -> None:
            if isinstance(outcome, Exception):
                messagebox.showerror(TITLE, f"Could not read that PDF.\n\n{outcome}",
                                     parent=self)
                self.close()
                return
            self.doc = outcome
            if not any(page.lines for page in self.doc.pages):
                messagebox.showerror(
                    TITLE,
                    "No text could be read from this PDF. If it is a scan, OCR "
                    "must be installed and switched on in Counter settings.",
                    parent=self,
                )
                self.close()
                return
            try:
                import fitz

                self._fitz = fitz.open(self.pdf)
            except Exception as exc:
                messagebox.showerror(TITLE, f"Could not open that PDF.\n\n{exc}", parent=self)
                self.close()
                return
            if self.new:
                suggestions = teaching.suggest_titles(self.doc, self.pipeline.profile)
                self.title_box.configure(values=suggestions)
                if suggestions:
                    self.title_var.set(suggestions[0])
            self.show_page(0)
            self.refresh_fields()

        self.tasks.run("read", lambda: teaching.read_sample(self.pdf, settings), done)

    def turn(self, step: int) -> None:
        if self.doc is None:
            return
        target = self.page_index + step
        if 0 <= target < len(self.doc.pages):
            self.show_page(target)

    def show_page(self, index: int) -> None:
        import fitz

        self.page_index = index
        self.selection = None
        self._picking_label = False
        page = self._fitz[index]
        matrix = fitz.Matrix(ZOOM, ZOOM)
        pixmap = page.get_pixmap(matrix=matrix)
        self._image = tk.PhotoImage(data=base64.b64encode(pixmap.tobytes("png")))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._image)
        self.canvas.configure(scrollregion=(0, 0, pixmap.width, pixmap.height))

        to_screen = page.rotation_matrix * matrix
        self._hits = []
        text_page = self.doc.pages[index]
        # Page.rows regroups on every access. Keep one copy, so a shift-click
        # can tell it is on the same row object as the first click.
        self._rows = text_page.rows
        for r, row in enumerate(self._rows):
            for w, word in enumerate(row.words):
                rect = fitz.Rect(word.bbox) * to_screen
                self._hits.append((rect.x0, rect.y0, rect.x1, rect.y1, r, w))
        self.canvas.create_rectangle(0, 0, 0, 0, outline=HOVER_COLOUR, width=1, tags="hover")
        self.page_label.configure(text=f"Page {index + 1} of {len(self.doc.pages)}")
        self._show_selection()

    def _hit(self, event) -> tuple | None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for hit in self._hits:
            x0, y0, x1, y1 = hit[:4]
            if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                return hit
        return None

    def on_hover(self, event) -> None:
        hit = self._hit(event)
        coords = hit[:4] if hit else (0, 0, 0, 0)
        self.canvas.coords("hover", *coords)

    def on_click(self, event) -> None:
        hit = self._hit(event)
        self._dragging = False
        if hit is None or self.doc is None:
            return
        _x0, _y0, _x1, _y1, r, w = hit
        if self._picking_label:
            self._pick_label(r, w)
            return
        extend = bool(event.state & 0x0001) and self.selection is not None \
            and self._anchor is not None
        if not extend:
            self.selection = None
            self._anchor = (r, w)
            self._label_choice = None
            self.lines_var.set(False)
            self.optional_var.set(False)
        self._dragging = True
        self._select_to(r, w)

    def on_drag(self, event) -> None:
        if not self._dragging:
            return
        hit = self._hit(event)
        if hit is not None:
            self._select_to(hit[4], hit[5])

    def _select_to(self, r: int, w: int) -> None:
        chosen = selection_between(self.doc.pages[self.page_index], self._rows,
                                   self._anchor, (r, w))
        if self.selection is not None and chosen.parts == self.selection.parts:
            return
        self.selection = chosen
        self._show_selection(guess=True)

    def choose_label(self) -> None:
        """Let the next click on the page say what this value is found by."""
        if self.selection is None:
            messagebox.showinfo(TITLE, "Click a value on the page first.", parent=self)
            return
        self._picking_label = True
        self.found_label.configure(text="Now click the label on the page.")

    def _pick_label(self, r: int, w: int) -> None:
        row = self._rows[r]
        label = label_at(row, w)
        if not label:
            self.found_label.configure(text="That is not a label. Click the words printed "
                                            "before the value, such as Paymode.")
            return
        self._picking_label = False
        self._label_choice = (label, "right" if row is self.selection.row else "below")
        self._show_selection()

    def _show_selection(self, guess: bool = False) -> None:
        import fitz

        self.canvas.delete("select")
        sel = self.selection
        if sel is None:
            return
        page = self._fitz[self.page_index]
        for row, first, last in sel.parts:
            words = row.words[first : last + 1]
            rect = fitz.Rect(
                min(w.x0 for w in words), min(w.y0 for w in words),
                max(w.x1 for w in words), max(w.y1 for w in words),
            ) * (page.rotation_matrix * fitz.Matrix(ZOOM, ZOOM))
            self.canvas.create_rectangle(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2,
                                         outline=SELECT_COLOUR, width=2, tags="select")

        if self._label_choice:
            label, side = self._label_choice
            where = "to its left" if side == "right" else "above it"
        else:
            label, where = label_beside(sel), "to its left"
            if not label:
                label, where = label_above(sel), "above it"
        if sel.more:
            self.lines_var.set(True)
        self.value_label.configure(text=f"“{sel.value}”")
        self.found_label.configure(
            text=f"Found by the label “{label}” {where}." if label
            else "No label beside it. It will be found by its shape instead."
        )
        if guess:
            kind = guess_kind(sel.value, self.pipeline.settings.default_country_code)
            self.kind_box.current([k for _l, k in KIND_CHOICES].index(kind))
            name = RECIPIENT_FIELD if kind == "phone" else field_for_label(label)
            self.field_var.set(display_name(name) if name else "")

    # -- fields -------------------------------------------------------------

    def _refresh_field_choices(self) -> None:
        names = [*BUILTIN_FIELDS, RECIPIENT_FIELD]
        names += [r.name for r in self.rules if r.name not in names]
        self.field_box.configure(values=[display_name(n) for n in names])

    def _kind_chosen(self) -> None:
        if KIND_CHOICES[self.kind_box.current()][1] == "phone":
            self.field_var.set(display_name(RECIPIENT_FIELD))

    def _chosen_name(self) -> str:
        typed = self.field_var.get().strip()
        for name in [*BUILTIN_FIELDS, RECIPIENT_FIELD, *(r.name for r in self.rules)]:
            if typed.lower() == display_name(name).lower():
                return name
        return field_name(typed) if typed else ""

    def add_field(self) -> None:
        if self.selection is None:
            messagebox.showinfo(TITLE, "Click a value on the page first.", parent=self)
            return
        index = self.kind_box.current()
        kind = KIND_CHOICES[index][1] if index >= 0 else None
        name = RECIPIENT_FIELD if kind == "phone" else self._chosen_name()
        if not name:
            messagebox.showinfo(TITLE, "Say what this value is called.", parent=self)
            return
        if name == RECIPIENT_FIELD and kind != "phone":
            kind = "phone"
        label, where = self._label_choice or (None, "right")
        try:
            rule = rule_from_selection(
                self.selection, name, kind, self.pipeline.settings.default_country_code,
                multiline=self.lines_var.get(), optional=self.optional_var.get(),
                label=label, where=where,
            )
        except Exception as exc:
            messagebox.showerror(TITLE, f"Could not make a rule from that.\n\n{exc}",
                                 parent=self)
            return
        self.rules = [r for r in self.rules if r.name != name] + [rule]
        self.clicked[name] = self.selection.value
        self._refresh_field_choices()
        self.refresh_fields()

    def remove_field(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.rules = [r for r in self.rules if r.name != selection[0]]
        self.clicked.pop(selection[0], None)
        self.refresh_fields()

    def toggle_optional(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(TITLE, "Choose a field in the list first.", parent=self)
            return
        for rule in self.rules:
            if rule.name == selection[0]:
                rule.optional = not rule.optional
        self.refresh_fields()
        self.tree.selection_set(selection[0])

    def _mark_optional(self, name: str) -> None:
        for rule in self.rules:
            if rule.name == name:
                rule.optional = True
        self.refresh_fields()

    def refresh_fields(self) -> None:
        if self.doc is None:
            return
        reads = teaching.check_rules(self.doc, self.rules)
        self.tree.delete(*self.tree.get_children())
        for rule in self.rules:
            found_by = rule.label or "its shape"
            if rule.label and rule.where == "below":
                found_by += " (above)"
            if rule.multiline:
                found_by += " + lines"
            text = display_name(rule.name) + (" (not on all)" if rule.optional else "")
            self.tree.insert("", "end", iid=rule.name, text=text,
                             values=(found_by, reads.get(rule.name) or "nothing"))
        self._refresh_samples()
        self.check()

    def _title(self) -> str:
        return self.title_var.get().strip() if self.new else ""

    def _problems(self) -> tuple[list[str], list[str]]:
        """(blocking, worth a question) for the current state."""
        if self.doc is None:
            return [], []
        blocking: list[str] = []
        if self.new:
            profile = self.pipeline.profile
            display = self.name_var.get()
            blocking += teaching.name_problems(display, profile)
            blocking += teaching.title_problems(
                self._title(), self.doc, profile, field_name(display)
            )
        return blocking, teaching.rule_problems(self.doc, self.rules, self.clicked)

    def check(self) -> None:
        blocking, doubtful = self._problems()
        lines = [*blocking, *doubtful]
        # Beside Save, which never scrolls away, so a long list must not push
        # it off the window. Save itself lists them all.
        if len(lines) > 3:
            lines = [*lines[:3], f"…and {len(lines) - 3} more."]
        self.problems.configure(text="\n".join(lines))

    # -- trying and saving ------------------------------------------------

    def _candidate(self) -> tuple[str, DocumentKind | None]:
        if not self.new:
            return self.kind_key, None
        name = field_name(self.name_var.get())
        return name, DocumentKind(name=name, match=[self._title()])

    def try_recent(self) -> None:
        pdfs = teaching.recent_prints()
        if not pdfs:
            messagebox.showinfo(TITLE, "Nothing has been printed on this computer yet.",
                                parent=self)
            return
        kind_name, new_kind = self._candidate()
        profile = teaching.candidate_profile(self.pipeline.profile, kind_name,
                                             self.rules, new_kind)
        settings = self.pipeline.settings
        results = tk.Toplevel(self)
        results.title("Recent prints, read with these fields")
        theme.fit_window(results, 820, 360)
        results.configure(background=theme.PAPER)
        note = ttk.Label(results, text=f"Reading {len(pdfs)} print(s)… Nothing is sent.",
                         padding=12)
        note.pack(anchor="w")
        table = trial_table(ttk.Frame(results, padding=(12, 0, 12, 12)))
        table.master.pack(fill="both", expand=True)

        def done(outcome) -> None:
            if isinstance(outcome, Exception):
                note.configure(text=f"Could not finish: {outcome}")
                return
            fill_trial_table(table, outcome)
            mine = sum(1 for row in outcome
                       if row.kind == teaching.kind_label(kind_name))
            note.configure(text=f"{len(outcome)} print(s) read; {mine} read as "
                                f"{teaching.kind_label(kind_name).lower()}. Nothing was sent.")

        self.tasks.run("trial", lambda: teaching.trial(profile, pdfs, settings), done)

    # -- validation samples -------------------------------------------------

    def _digest(self, path: Path) -> str:
        path = Path(path)
        if path not in self._digests:
            self._digests[path] = digest_of(path)
        return self._digests[path]

    def _is_taught_sample(self, path: Path) -> bool:
        return self._digest(path) == self._digest(self.pdf)

    def _sample_rows(self) -> list[tuple[str, Path, str]]:
        """(digest, path, status) for every sample in the list, once each."""
        rows: dict[str, tuple[str, Path, str]] = {}
        for sample in self.validation_samples:
            digest = self._digest(sample.path)
            stale = unreviewed(sample, self.rules)
            status = self.sample_errors.get(digest) or (
                "Review again: a field has changed" if stale else "Reviewed")
            rows[digest] = (digest, Path(sample.path), status)
        for path in self.pending:
            digest = self._digest(path)
            rows.setdefault(digest, (digest, path, "Not reviewed yet"))
        return sorted(rows.values(), key=lambda row: not self._is_taught_sample(row[1]))

    def _refresh_samples(self) -> None:
        self.sample_tree.delete(*self.sample_tree.get_children())
        for digest, path, status in self._sample_rows():
            name = path.name
            if self._is_taught_sample(path):
                name += " (taught from)"
            self.sample_tree.insert("", "end", iid=digest, text=name, values=(status,))
        count = len({self._digest(s.path) for s in self.validation_samples})
        text = f"{count} of {MIN_SAMPLES} different PDFs reviewed."
        if count >= MIN_SAMPLES:
            text = f"{count} different PDFs reviewed. Save checks them, and every other type."
        if self._sample_note:
            text += f"\n{self._sample_note}"
        self.sample_count.configure(text=text)

    def add_samples(self) -> None:
        chosen = filedialog.askopenfilenames(parent=self, title="Choose PDFs of this document type",
                                             filetypes=[("PDF documents", "*.pdf")])
        if not chosen:
            return
        known = {row[0] for row in self._sample_rows()}
        repeats = 0
        for name in chosen:
            path = Path(name).resolve()
            digest = self._digest(path)
            if digest in known:
                repeats += 1
                continue
            known.add(digest)
            self.pending.append(path)
        # The count not moving looks like the app ignoring the file. Say why.
        self._sample_note = (f"{repeats} of those {'is' if repeats == 1 else 'are'} already in "
                             f"this list, so the count did not change." if repeats else "")
        self._refresh_samples()
        if len(chosen) > repeats:
            self.review_samples()

    def remove_sample(self) -> None:
        selection = self.sample_tree.selection()
        if not selection:
            messagebox.showinfo(TITLE, "Choose a PDF in the list first.", parent=self)
            return
        path = next(row[1] for row in self._sample_rows() if row[0] == selection[0])
        if self._is_taught_sample(path):
            messagebox.showinfo(
                TITLE,
                "The fields were taught from this PDF, so it stays. If it is the wrong "
                "document, press Cancel and start again with the right one.",
                parent=self,
            )
            return
        self._forget_sample(path)

    def _forget_sample(self, path: Path) -> None:
        digest = self._digest(path)
        self.validation_samples = [s for s in self.validation_samples
                                   if self._digest(s.path) != digest]
        self.pending = [p for p in self.pending if self._digest(p) != digest]
        self.sample_errors.pop(digest, None)
        self._sample_note = ""
        self._refresh_samples()

    def _sample_rejected(self, path: Path) -> None:
        # The PDF the fields came from cannot be dropped here. It stays in the
        # list as not reviewed, and the message on Remove says what to do.
        if not self._is_taught_sample(path):
            self._forget_sample(path)

    def _samples_reviewed(self, samples) -> None:
        by_digest = {self._digest(s.path): s for s in self.validation_samples}
        for sample in samples:
            by_digest[self._digest(sample.path)] = sample
        self.validation_samples = list(by_digest.values())
        self.pending = [p for p in self.pending if self._digest(p) not in by_digest]
        for digest in by_digest:
            self.sample_errors.pop(digest, None)
        self._sample_note = ""
        self._refresh_samples()
        self.check()

    def review_samples(self) -> None:
        """Review the chosen PDF, or else every one that still needs it."""
        from .validation import ReviewSamples
        blocking, _ = self._problems()
        if blocking:
            messagebox.showerror(TITLE, "\n".join(blocking), parent=self)
            return
        if self._reviewer is not None and self._reviewer.winfo_exists():
            self._reviewer.lift()
            return
        rows = self._sample_rows()
        chosen = self.sample_tree.selection()
        pdfs = [row[1] for row in rows if row[0] in chosen] or [
            row[1] for row in rows if row[2] != "Reviewed"
        ]
        if not pdfs:
            messagebox.showinfo(TITLE, "Everything in the list is reviewed. Use Add PDFs for "
                                       "more, or choose one to review it again.", parent=self)
            return
        kind, new_kind = self._candidate()
        profile = teaching.candidate_profile(self.pipeline.profile, kind, self.rules, new_kind)
        self._reviewer = ReviewSamples(
            self, pdfs, kind, profile, deepcopy(self.pipeline.settings),
            self.validation_reader, self._samples_reviewed,
            on_reject=self._sample_rejected, on_optional=self._mark_optional,
            existing={Path(s.path): s for s in self.validation_samples},
        )

    def save(self) -> None:
        from ..validation import saved_samples, save_samples, validate
        if self.doc is None:
            return
        blocking, doubtful = self._problems()
        if blocking or doubtful:
            messagebox.showerror(TITLE, "\n".join([*blocking, *doubtful]), parent=self)
            return
        kind_name, new_kind = self._candidate()
        profile = teaching.candidate_profile(self.pipeline.profile, kind_name, self.rules, new_kind)
        settings = deepcopy(self.pipeline.settings)
        samples = deepcopy(self.validation_samples)
        base = self.pipeline.profile.to_dict()
        others = saved_samples(except_kind=kind_name)
        self.problems.configure(text="Validating samples and previously taught types…")

        def checked(report):
            if isinstance(report, Exception):
                self.check()
                messagebox.showerror(TITLE, f"Validation failed: {report}", parent=self)
                return
            if not report.passed:
                self.sample_errors = {
                    self._digest(r.sample.path): f"Failed: {r.errors[0]}"
                    for r in report.results if r.errors and r.sample in samples
                }
                self._refresh_samples()
                self.check()
                messagebox.showerror(
                    TITLE,
                    f"{report.summary()}\n\nA PDF that is the wrong document can be taken "
                    f"out under Validation samples: choose it and press Remove.",
                    parent=self,
                )
                return
            current_kind, current_new = self._candidate()
            current = teaching.candidate_profile(self.pipeline.profile, current_kind, self.rules, current_new)
            if (current.to_dict() != profile.to_dict() or self.pipeline.settings != settings
                    or self.pipeline.profile.to_dict() != base or self.validation_samples != samples):
                self.problems.configure(text="The setup changed during validation. Save again to recheck it.")
                return
            try:
                save_samples(samples, replacing=kind_name)
                teaching.save_teaching(kind_name, self.rules, sample=self.pdf, new_kind=new_kind)
            except Exception as exc:
                messagebox.showerror(TITLE, f"Could not save.\n\n{exc}", parent=self)
                return
            key = DocumentProfile.rules_key(kind_name)
            self.close()
            self.on_saved(key)

        self.tasks.run("validate", lambda: validate([*samples, *others], profile, settings,
                                                    target=kind_name, reader=self.validation_reader,
                                                    templates=self.pipeline.templates), checked)

    def close(self) -> None:
        if self._fitz is not None:
            try:
                self._fitz.close()
            except Exception:
                pass
            self._fitz = None
        try:
            self.destroy()
        except tk.TclError:
            pass
