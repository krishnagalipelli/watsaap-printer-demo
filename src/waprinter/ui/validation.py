"""Review independent expected values before testing a taught profile."""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from ..extract.rules import display_name
from ..validation import Sample
from . import theme
from .setup import Tasks


class ReviewSamples(tk.Toplevel):
    def __init__(self, parent, pdfs, kind, profile, settings, reader, done):
        super().__init__(parent)
        self.title("Review validation samples")
        self.geometry("660x640")
        self.transient(parent)
        self.pdfs, self.kind, self.profile = pdfs, kind, profile
        self.settings, self.reader, self.done = settings, reader, done
        self.reviewed = []
        self.index = 0
        self.tasks = Tasks(self)
        self.label = ttk.Label(self, padding=14, wraplength=620)
        self.label.pack(fill="x")
        ttk.Button(self, text="Open this PDF to check the values", command=self.open_pdf).pack()
        ttk.Label(self, text="Compare with the PDF and correct any wrong values below.\n"
                  "These become the expected answers for future checks.",
                  padding=12, wraplength=620).pack(fill="x")
        # Scrollable because a template may teach more fields than fit a screen.
        from .setup import _scrolling
        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True)
        self.form = _scrolling(holder)
        self.next = ttk.Button(self, text="I checked these values — next sample",
                               command=self.accept, style="Mint.TButton")
        self.next.pack(pady=12)
        self.load()

    def open_pdf(self):
        webbrowser.open(self.pdfs[self.index].resolve().as_uri())

    def load(self):
        self.next.configure(state="disabled")
        self.label.configure(text=f"Sample {self.index + 1} of {len(self.pdfs)}: "
                             f"{self.pdfs[self.index].name}\nReading…")
        for child in self.form.winfo_children():
            child.destroy()
        def loaded(result):
            if isinstance(result, Exception):
                self.label.configure(text=f"Could not read this sample: {result}")
                return
            self.label.configure(text=f"Sample {self.index + 1} of {len(self.pdfs)}: "
                                 f"{self.pdfs[self.index].name}\nExpected type: {self.kind.replace('_', ' ')}; "
                                 f"read as: {result.document_kind or 'unrecognised'}")
            values = result.as_template_vars()
            names = {r.name for r in self.profile.rules_for(self.kind) if r.kind != 'phone'}
            names.update(k for k, v in values.items() if v)
            self.entries = {}
            for name in ['recipient', *sorted(names)]:
                ttk.Label(self.form, text="Customer mobile (blank if absent)" if name == 'recipient'
                          else display_name(name)).pack(anchor="w", padx=12)
                value = (result.best.e164 if result.best else '') if name == 'recipient' else values.get(name, '')
                var = tk.StringVar(value=value)
                ttk.Entry(self.form, textvariable=var).pack(fill="x", padx=12, pady=(0, 6))
                self.entries[name] = var
            self.next.configure(state="normal")
        self.tasks.run('read', lambda: self.reader.read(self.pdfs[self.index], self.profile, self.settings), loaded)

    def accept(self):
        from ..extract.phone import parse_typed_number
        raw = self.entries['recipient'].get().strip()
        recipient = parse_typed_number(raw, self.settings.default_country_code) if raw else ''
        if raw and not recipient:
            messagebox.showerror("Check mobile number", "Enter a valid mobile number, or leave blank if absent.", parent=self)
            return
        self.reviewed.append(Sample(self.pdfs[self.index], self.kind,
                                   {k: v.get().strip() for k, v in self.entries.items() if k != 'recipient'},
                                   recipient))
        self.index += 1
        if self.index == len(self.pdfs):
            reviewed = self.reviewed
            self.destroy()
            self.done(reviewed)
        else:
            self.load()
