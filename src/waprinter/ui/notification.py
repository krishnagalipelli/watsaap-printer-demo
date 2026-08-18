"""The panel that appears in the corner after a print.

On a normal print this is the only thing the operator sees, so it has to be
readable at a glance and get out of the way on its own.

Successes close themselves after a few seconds. Anything the operator has to act
on stays until they dismiss it — a receipt that did not reach a member has to be
noticed, and a panel that vanishes after four seconds is a panel that gets
missed.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..models import PrintJob
from .result import describe, needs_action

WIDTH = 340
MARGIN = 22
TASKBAR_ALLOWANCE = 64
AUTO_CLOSE_MS = 5000

TONES = {
    "ok": ("#0f7b43", "✓"),
    "bad": ("#b3261e", "!"),
    "wait": ("#a35a00", "i"),
}


class Notification:
    """One borderless panel reporting one job."""

    def __init__(
        self,
        master: tk.Misc,
        job: PrintJob,
        on_open: Callable[[], None] | None = None,
    ):
        self.on_open = on_open
        tone, headline, detail = describe(job)
        accent, glyph = TONES[tone]
        actionable = needs_action(job)

        self.win = tk.Toplevel(master)
        self.win.title("WhatsApp Printer")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        # A notification, not a window to manage.
        self.win.overrideredirect(True)

        outer = tk.Frame(self.win, bg=accent)      # coloured edge
        outer.pack(fill="both", expand=True)
        body = tk.Frame(outer, bg="white", padx=15, pady=12)
        body.pack(fill="both", expand=True, padx=(4, 1), pady=1)

        head = tk.Frame(body, bg="white")
        head.pack(fill="x")
        tk.Label(
            head, text=glyph, bg=accent, fg="white", width=2,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(0, 9), ipady=1)
        tk.Label(
            head, text=headline, bg="white", fg="#1a1a1a", anchor="w",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")

        tk.Label(
            body, text=detail, bg="white", fg="#444", anchor="w",
            justify="left", wraplength=WIDTH - 46, font=("Segoe UI", 9),
        ).pack(fill="x", pady=(6, 0))

        if actionable:
            row = tk.Frame(body, bg="white")
            row.pack(fill="x", pady=(10, 0))
            ttk.Button(row, text="Dismiss", width=10, command=self.close).pack(
                side="right"
            )
            if on_open is not None:
                ttk.Button(row, text="Open", width=10, command=self._open).pack(
                    side="right", padx=(0, 6)
                )
        else:
            self.win.after(AUTO_CLOSE_MS, self.close)

        self._place()

    def _place(self) -> None:
        """Bottom right, clear of the taskbar, like a system notification."""
        self.win.update_idletasks()
        width = max(self.win.winfo_width(), WIDTH)
        height = self.win.winfo_height()
        x = self.win.winfo_screenwidth() - width - MARGIN
        y = self.win.winfo_screenheight() - height - TASKBAR_ALLOWANCE
        self.win.geometry(f"{width}x{height}+{x}+{y}")

    def _open(self) -> None:
        if self.on_open:
            self.on_open()
        self.close()

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass  # already gone

    @property
    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False
