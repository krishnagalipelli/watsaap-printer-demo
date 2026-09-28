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
from . import theme
from .result import describe, needs_action

WIDTH = 340
MARGIN = 22
TASKBAR_ALLOWANCE = 64
AUTO_CLOSE_MS = 5000

TONES = {
    "ok": (theme.MINT, "✓"),
    "bad": (theme.DANGER, "!"),
    "wait": (theme.WARM, "i"),
}


class Notification:
    """One borderless panel reporting one job."""

    def __init__(
        self,
        master: tk.Misc,
        job: PrintJob,
        on_open: Callable[[], None] | None = None,
        on_whatsapp: Callable[[], None] | None = None,
    ):
        self.on_open = on_open
        self.on_whatsapp = on_whatsapp
        self.job = job
        tone, headline, detail = describe(job)
        accent, glyph = TONES[tone]
        actionable = needs_action(job)

        self.win = tk.Toplevel(master)
        self.win.title("WhatsApp Printer")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        # A notification, not a window to manage.
        self.win.overrideredirect(True)

        # A hairline round the whole panel, and a coloured edge on the left.
        border = tk.Frame(self.win, bg=theme.LINE)
        border.pack(fill="both", expand=True)
        outer = tk.Frame(border, bg=accent)
        outer.pack(fill="both", expand=True, padx=(0, 1), pady=1)
        body = tk.Frame(outer, bg=theme.PAPER, padx=16, pady=14)
        body.pack(fill="both", expand=True, padx=(4, 0))

        head = tk.Frame(body, bg=theme.PAPER)
        head.pack(fill="x")
        tk.Label(
            head, text=glyph, bg=accent, fg=theme.CARD, width=2,
            font=theme.BOLD,
        ).pack(side="left", padx=(0, 10), ipady=1)
        tk.Label(
            head, text=headline, bg=theme.PAPER, fg=theme.INK, anchor="w",
            font=theme.CARD_TITLE,
        ).pack(side="left")

        tk.Label(
            body, text=detail, bg=theme.PAPER, fg=theme.SUBTLE, anchor="w",
            justify="left", wraplength=WIDTH - 46, font=theme.BODY,
        ).pack(fill="x", pady=(8, 0))

        if actionable:
            row = tk.Frame(body, bg=theme.PAPER)
            row.pack(fill="x", pady=(10, 0))
            ttk.Button(row, text="Dismiss", width=10, command=self.close).pack(
                side="right"
            )
            if job.chat_url and on_whatsapp is not None:
                # The one button that matters in link mode.
                ttk.Button(
                    row, text="Open WhatsApp", width=15, style="Mint.TButton",
                    command=self._whatsapp,
                ).pack(side="right", padx=(0, 6))
            elif on_open is not None:
                ttk.Button(row, text="Open", width=10, style="Mint.TButton",
                           command=self._open).pack(side="right", padx=(0, 6))
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

    def _whatsapp(self) -> None:
        if self.on_whatsapp:
            self.on_whatsapp()
        self.close()

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
