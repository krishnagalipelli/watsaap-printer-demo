"""Review independent expected values before testing a taught profile.

One PDF at a time, with a way back and a way out: a sample picked by mistake is
removed here rather than failing every validation from then on, and closing the
window keeps whatever was already checked.
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from ..extract.profile import DocumentProfile
from ..extract.rules import display_name
from ..validation import Sample
from . import theme
from .setup import Tasks


class ReviewSamples(tk.Toplevel):
    def __init__(self, parent, pdfs, kind, profile, settings, reader, done,
                 on_reject=None, on_optional=None, existing=None):
        super().__init__(parent)
        self.title("Review validation samples")
        theme.fit_window(self, 660, 640, 520, 420)
        self.transient(parent)
        self.pdfs, self.kind, self.profile = list(pdfs), kind, profile
        self.settings, self.reader, self.done = settings, reader, done
        self.on_reject, self.on_optional = on_reject, on_optional
        self.existing = existing or {}
        self.reviewed = {}
        self.entries = {}
        self.index = 0
        self.tasks = Tasks(self)
        self.optional = {r.name for r in profile.rules_for(kind) if r.optional}

        # The buttons first and against the bottom edge, so a short window
        # squeezes the fields, which scroll, and never the way forward.
        bar = ttk.Frame(self, padding=12)
        bar.pack(side="bottom", fill="x")
        self.back_button = ttk.Button(bar, text="Back", command=self.back)
        self.back_button.pack(side="left")
        self.remove_button = ttk.Button(bar, text="Not this type: remove it", command=self.reject)
        self.remove_button.pack(side="left", padx=6)
        self.next = ttk.Button(bar, command=self.accept, style="Mint.TButton")
        self.next.pack(side="right")

        self.label = ttk.Label(self, padding=(14, 14, 14, 4), wraplength=620)
        self.label.pack(fill="x")
        self.warning = ttk.Label(self, style="Bad.TLabel", padding=(14, 0), wraplength=620)
        self.warning.pack(fill="x")
        ttk.Button(self, text="Open this PDF to check the values", command=self.open_pdf).pack(pady=(6, 0))
        ttk.Label(self, text="Compare with the PDF and correct any wrong values below.\n"
                  "These become the expected answers for future checks.",
                  padding=12, wraplength=620).pack(fill="x")
        # Scrollable because a template may teach more fields than fit a screen.
        from .setup import _scrolling
        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True)
        self.form = _scrolling(holder)
        self.protocol("WM_DELETE_WINDOW", self.finish)
        self.load()

    def open_pdf(self):
        webbrowser.open(self.pdfs[self.index].resolve().as_uri())

    def _expected(self) -> str:
        return "receipt" if DocumentProfile.rules_key(self.kind) == "_default" else self.kind

    def _buttons(self, *enabled) -> None:
        for button in (self.back_button, self.remove_button, self.next):
            button.configure(state="normal" if button in enabled else "disabled")

    def load(self):
        pdf = self.pdfs[self.index]
        self._buttons()
        self.warning.configure(text="")
        self.next.configure(text="I checked these values — finish"
                            if self.index == len(self.pdfs) - 1
                            else "I checked these values — next sample")
        heading = f"Sample {self.index + 1} of {len(self.pdfs)}: {pdf.name}"
        self.label.configure(text=f"{heading}\nReading…")
        for child in self.form.winfo_children():
            child.destroy()
        self.entries = {}
        back = [self.back_button] if self.index else []

        def loaded(result):
            if isinstance(result, Exception):
                self.label.configure(text=heading)
                self.warning.configure(text=f"Could not read this sample: {result}")
                self._buttons(self.remove_button, *back)
                return
            read_as = result.document_kind or "unrecognised"
            self.label.configure(text=f"{heading}\nExpected type: {self.kind.replace('_', ' ')}; "
                                 f"read as: {read_as.replace('_', ' ')}")
            if result.document_kind != self._expected():
                self.warning.configure(
                    text=f"This does not read as {self.kind.replace('_', ' ')}, so it cannot "
                         f"be a sample of it. Remove it, or go back to the training window "
                         f"if the title chosen for this type is wrong.")
                self._buttons(self.remove_button, *back)
                return
            self._fill(result)
            self._buttons(self.next, self.remove_button, *back)
        self.tasks.run('read', lambda: self.reader.read(pdf, self.profile, self.settings), loaded)

    def _fill(self, result) -> None:
        prior = self.reviewed.get(self.pdfs[self.index]) or self.existing.get(self.pdfs[self.index])
        values = result.as_template_vars()
        names = {r.name for r in self.profile.rules_for(self.kind) if r.kind != 'phone'}
        names.update(k for k, v in values.items() if v)
        for name in ['recipient', *sorted(names)]:
            if name == 'recipient':
                text = "Customer mobile (blank if absent)"
                value = result.best.e164 if result.best else ''
                if prior is not None:
                    value = prior.recipient or ''
            else:
                text = display_name(name)
                if name in self.optional:
                    text += " (blank if not on this document)"
                value = prior.values.get(name, '') if prior is not None else values.get(name, '')
            ttk.Label(self.form, text=text).pack(anchor="w", padx=12)
            var = tk.StringVar(value=value)
            ttk.Entry(self.form, textvariable=var).pack(fill="x", padx=12, pady=(0, 6))
            self.entries[name] = var

    def _blank_fields_settled(self) -> bool:
        """Whether every taught field left blank is meant to be blank.

        A cash receipt has no transaction ID. Asking here, while the document
        is open, beats a validation that can never pass and does not say why.
        """
        if self.on_optional is None:
            return True
        for rule in self.profile.rules_for(self.kind):
            if rule.kind == 'phone' or rule.name in self.optional or rule.name not in self.entries:
                continue
            if self.entries[rule.name].get().strip():
                continue
            name = display_name(rule.name)
            if not messagebox.askyesno(
                "Blank field",
                f"{name} is blank on this document.\n\nIs that because it is not on every "
                f"document of this type?\n\nYes: it may be blank from now on.\n"
                f"No: stay here and fill it in.",
                parent=self,
            ):
                return False
            self.optional.add(rule.name)
            self.on_optional(rule.name)
        return True

    def accept(self):
        from ..extract.phone import parse_typed_number
        raw = self.entries['recipient'].get().strip()
        recipient = parse_typed_number(raw, self.settings.default_country_code) if raw else ''
        if raw and not recipient:
            messagebox.showerror("Check mobile number", "Enter a valid mobile number, or leave blank if absent.", parent=self)
            return
        if not self._blank_fields_settled():
            return
        self.reviewed[self.pdfs[self.index]] = Sample(
            self.pdfs[self.index], self.kind,
            {k: v.get().strip() for k, v in self.entries.items() if k != 'recipient'},
            recipient)
        self.index += 1
        if self.index == len(self.pdfs):
            self.finish()
        else:
            self.load()

    def back(self):
        self.index -= 1
        self.load()

    def reject(self):
        pdf = self.pdfs.pop(self.index)
        self.reviewed.pop(pdf, None)
        if self.on_reject is not None:
            self.on_reject(pdf)
        if self.index == len(self.pdfs):
            self.finish()
        else:
            self.load()

    def finish(self):
        reviewed = [self.reviewed[pdf] for pdf in self.pdfs if pdf in self.reviewed]
        self.destroy()
        self.done(reviewed)
