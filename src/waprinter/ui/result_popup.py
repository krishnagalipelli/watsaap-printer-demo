"""The only thing the operator sees on a normal print.

Print, read the page, send, and a small panel appears in the corner saying
whether it went. It closes itself after a few seconds. Nothing to click, nothing
to fill in — the same feel as a printer's own "printing" notification.

Failures do not auto-close. If a receipt did not reach the member, the operator
has to see that and press Dismiss, because the alternative is a message quietly
not arriving.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from typing import Callable

from ..models import JobStatus, PrintJob

log = logging.getLogger(__name__)

POLL_MS = 300
AUTO_CLOSE_MS = 4500      # successes get out of the way on their own
WIDTH = 340

OK_BG = "#e7f7ef"
OK_FG = "#0f5132"
OK_ACCENT = "#128c7e"
BAD_BG = "#fdecec"
BAD_FG = "#842029"
BAD_ACCENT = "#b91c1c"
WAIT_BG = "#fff6e5"
WAIT_FG = "#7a4b00"
WAIT_ACCENT = "#b45309"
MUTED = "#6b7280"


def describe(job: PrintJob) -> tuple[str, str, str]:
    """(tone, headline, detail) for a finished job, in the operator's words."""
    document = job.fields.invoice_number or job.doc_title or "Document"
    name = job.fields.customer_name

    if job.status is JobStatus.SENT:
        who = f"{name} · {job.recipient}" if name else job.recipient
        return "ok", "Sent on WhatsApp", f"{document} → {who}"

    if job.status is JobStatus.DRY_RUN:
        who = f"{name} · {job.recipient}" if name else job.recipient
        return (
            "wait",
            "Not sent — test mode",
            f"{document} would have gone to {who}. Turn off test mode in "
            f"Settings to send for real.",
        )

    if job.status is JobStatus.DUPLICATE:
        return "wait", "Already sent", job.hold_reason or f"{document} was sent before."

    if job.status is JobStatus.FAILED:
        return "bad", "Could not send", f"{document}: {job.error or 'unknown error'}"

    # AWAITING or HELD — nothing was lost, it needs a person.
    return (
        "wait",
        "Needs your attention",
        f"{document}: {job.hold_reason or 'waiting in the queue'}",
    )


class ResultPopup:
    """A corner panel reporting one job's outcome."""

    PALETTE = {
        "ok": (OK_BG, OK_FG, OK_ACCENT, "✓"),
        "bad": (BAD_BG, BAD_FG, BAD_ACCENT, "!"),
        "wait": (WAIT_BG, WAIT_FG, WAIT_ACCENT, "•"),
    }

    def __init__(
        self,
        master: tk.Tk,
        job: PrintJob,
        on_open_queue: Callable[[], None] | None = None,
    ):
        self.job = job
        self.on_open_queue = on_open_queue
        tone, headline, detail = describe(job)
        background, foreground, accent, glyph = self.PALETTE[tone]

        self.win = tk.Toplevel(master)
        self.win.title("WhatsApp Printer")
        self.win.configure(bg=background)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        # No title bar: this is a notification, not a window to manage.
        try:
            self.win.overrideredirect(True)
        except tk.TclError:
            pass

        outer = tk.Frame(self.win, bg=background, padx=16, pady=13)
        outer.pack(fill="both", expand=True)

        head = tk.Frame(outer, bg=background)
        head.pack(fill="x")
        tk.Label(
            head, text=glyph, bg=background, fg=accent,
            font=("Segoe UI", 15, "bold"),
        ).pack(side="left", padx=(0, 9))
        tk.Label(
            head, text=headline, bg=background, fg=foreground,
            font=("Segoe UI", 11, "bold"), anchor="w",
        ).pack(side="left")

        tk.Label(
            outer, text=detail, bg=background, fg=foreground,
            wraplength=WIDTH - 40, justify="left", anchor="w",
        ).pack(fill="x", pady=(7, 0))

        if tone != "ok":
            row = tk.Frame(outer, bg=background)
            row.pack(fill="x", pady=(11, 0))
            if self.on_open_queue is not None:
                tk.Button(
                    row, text="Open queue", command=self._open_queue,
                    bg=background, fg=accent, relief="flat", padx=0,
                    activebackground=background, cursor="hand2",
                ).pack(side="left")
            tk.Button(
                row, text="Dismiss", command=self.close,
                bg=background, fg=MUTED, relief="flat",
                activebackground=background, cursor="hand2",
            ).pack(side="right")

        self._place()
        # Only good news disappears by itself.
        if tone == "ok":
            self.win.after(AUTO_CLOSE_MS, self.close)

    def _place(self) -> None:
        """Bottom right, above the taskbar, like a system notification."""
        self.win.update_idletasks()
        width = max(self.win.winfo_width(), WIDTH)
        height = self.win.winfo_height()
        x = self.win.winfo_screenwidth() - width - 24
        y = self.win.winfo_screenheight() - height - 72
        self.win.geometry(f"{width}x{height}+{x}+{y}")

    def _open_queue(self) -> None:
        if self.on_open_queue:
            self.on_open_queue()
        self.close()

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class PopupHost:
    """Owns the Tk main loop and shows one popup per finished job.

    The watcher thread calls `submit()`; the popup is built on the main thread,
    because Tk widgets may only be touched by the thread that created them.
    """

    def __init__(self, pipeline, open_queue: Callable[[], None] | None = None):
        self.pipeline = pipeline
        self.open_queue = open_queue
        self.incoming: queue.Queue[str] = queue.Queue()
        self.root: tk.Tk | None = None
        self._open: list[ResultPopup] = []

    def submit(self, job_id: str) -> None:
        """Thread-safe: queue a finished job for display."""
        self.incoming.put(job_id)

    def _pump(self) -> None:
        try:
            job_id = self.incoming.get_nowait()
        except queue.Empty:
            pass
        else:
            self._show(job_id)
        if self.root:
            self.root.after(POLL_MS, self._pump)

    def _show(self, job_id: str) -> None:
        job = self.pipeline.store.get(job_id)
        if job is None:
            return
        # A batch print should not stack popups down the screen; the newest
        # result is the one that matters.
        for existing in self._open:
            existing.close()
        self._open = [ResultPopup(self.root, job, self.open_queue)]

    def run(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.after(POLL_MS, self._pump)
        self.root.mainloop()

    def stop(self) -> None:
        if self.root:
            self.root.quit()
