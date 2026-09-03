"""The application window.

A printer properties sheet, not a dashboard: a device status line at the top, a
Test send button where "Print Test Page" would be, tabs, and grouped settings
with Apply. Native ttk widgets, so it looks like the rest of Windows rather than
like a web page in a frame.

Everything the operator reads comes from ui/viewmodel.py, which is where the
wording and the status labels live and where they are tested. This file is
widgets and wiring only.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .. import __version__
from ..models import JobStatus
from . import viewmodel as vm
from .notification import Notification

log = logging.getLogger(__name__)

REFRESH_MS = 2000
POLL_MS = 400

# Settings that are a fixed choice rather than free text: the plain-English
# label the operator picks, paired with the value stored in settings.json.
SEND_MODES = [
    ("Send automatically", "api"),
    ("Open WhatsApp for me to send", "link"),
]

TONE_COLOURS = {
    "ok": "#0f7b43",
    "warn": "#a35a00",
    "bad": "#b3261e",
    "muted": "#5c5c5c",
}


class DesktopWindow:
    """The whole user interface."""

    def __init__(self, pipeline, on_check_updates=None):
        self.pipeline = pipeline
        self.settings = pipeline.settings
        self.on_check_updates = on_check_updates
        self.incoming: queue.Queue[str] = queue.Queue()
        # Set by another launch of the app asking this copy to come forward.
        # An Event rather than a queue: ten impatient double-clicks should
        # raise the window once, not stack ten raises behind each other.
        self._show_requested = threading.Event()
        self._notifications: list[Notification] = []
        self._queue_rows: dict[str, tk.Widget] = {}

        self.root = tk.Tk()
        self.root.title("WhatsApp Printer")
        self.root.geometry("820x660")
        self.root.minsize(700, 560)
        self._use_native_theme()

        self._build_header()
        self._build_tabs()

        # Closing the window leaves the app running: prints still have to be
        # captured. Only Exit actually quits.
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self.root.after(POLL_MS, self._pump)
        self.root.after(REFRESH_MS, self._refresh)
        self.refresh()
        self._load_settings_into_form()

    # -- chrome ------------------------------------------------------------

    def _use_native_theme(self) -> None:
        style = ttk.Style(self.root)
        for candidate in ("vista", "winnative", "aqua", "clam"):
            if candidate in style.theme_names():
                style.theme_use(candidate)
                break
        style.configure("Big.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Head.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Hint.TLabel", foreground=TONE_COLOURS["muted"])

    def _build_header(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill="x")

        ttk.Label(header, text="🖨", font=("Segoe UI", 22)).pack(side="left", padx=(0, 12))

        text = ttk.Frame(header)
        text.pack(side="left", fill="x", expand=True)
        ttk.Label(text, text="WhatsApp Printer", style="Head.TLabel").pack(anchor="w")
        self.state_label = ttk.Label(text, text="", style="Hint.TLabel")
        self.state_label.pack(anchor="w")

        ttk.Button(header, text="Test send", command=self.test_send).pack(side="right")
        ttk.Separator(self.root).pack(fill="x")

    def _build_tabs(self) -> None:
        self.tabs = ttk.Notebook(self.root, padding=8)
        self.tabs.pack(fill="both", expand=True)

        self.status_tab = ttk.Frame(self.tabs, padding=12)
        self.queue_tab = ttk.Frame(self.tabs, padding=12)
        self.recent_tab = ttk.Frame(self.tabs, padding=12)
        self.settings_tab = ttk.Frame(self.tabs, padding=12)

        self.tabs.add(self.status_tab, text="Status")
        self.tabs.add(self.queue_tab, text="Needs attention")
        self.tabs.add(self.recent_tab, text="Recent")
        self.tabs.add(self.settings_tab, text="Settings")

        self._build_status()
        self._build_queue()
        self._build_recent()
        self._build_settings()

    # -- status tab --------------------------------------------------------

    def _build_status(self) -> None:
        counters = ttk.Frame(self.status_tab)
        counters.pack(fill="x", pady=(0, 14))

        self.counter_values: dict[str, ttk.Label] = {}
        self.counter_captions: dict[str, ttk.Label] = {}
        for key, caption in (
            ("sent", "sent today"),
            ("printed", "documents printed"),
            ("waiting", "need attention"),
            ("failed", "failed"),
        ):
            cell = ttk.Frame(counters, relief="solid", borderwidth=1, padding=(14, 10))
            cell.pack(side="left", fill="both", expand=True, padx=(0, 8))
            value = ttk.Label(cell, text="0", style="Big.TLabel")
            value.pack(anchor="w")
            label = ttk.Label(cell, text=caption, style="Hint.TLabel")
            label.pack(anchor="w")
            self.counter_values[key] = value
            self.counter_captions[key] = label

        self.problems_box = ttk.LabelFrame(
            self.status_tab, text="Before this can send", padding=12
        )
        self.problems_label = ttk.Label(
            self.problems_box, text="", justify="left", wraplength=720,
            foreground=TONE_COLOURS["warn"],
        )
        self.problems_label.pack(anchor="w")

        how = ttk.LabelFrame(self.status_tab, text="How to use", padding=12)
        how.pack(fill="x", pady=(0, 12))
        ttk.Label(how, text=vm.HOW_TO_USE, justify="left", wraplength=720).pack(anchor="w")

        updates = ttk.LabelFrame(self.status_tab, text="This installation", padding=12)
        updates.pack(fill="x")
        row = ttk.Frame(updates)
        row.pack(fill="x")
        self.version_label = ttk.Label(row, text=f"Version {__version__}")
        self.version_label.pack(side="left")
        self.update_button = ttk.Button(
            row, text="Check for updates", command=self.check_updates
        )
        self.update_button.pack(side="right")
        self.update_status = ttk.Label(updates, text="", style="Hint.TLabel")
        self.update_status.pack(anchor="w", pady=(6, 0))

    # -- queue tab ---------------------------------------------------------

    def _build_queue(self) -> None:
        canvas = tk.Canvas(self.queue_tab, highlightthickness=0)
        scroll = ttk.Scrollbar(self.queue_tab, orient="vertical", command=canvas.yview)
        self.queue_body = ttk.Frame(canvas)
        self.queue_body.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        window = canvas.create_window((0, 0), window=self.queue_body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.queue_empty = ttk.Label(
            self.queue_body,
            text="Nothing waiting.\n\nDocuments appear here only when the "
                 "customer's number could not be read off the page.",
            style="Hint.TLabel",
            justify="left",
        )

    def _render_queue(self) -> None:
        for child in self.queue_body.winfo_children():
            child.destroy()

        jobs = self.pipeline.store.pending()
        if not jobs:
            ttk.Label(
                self.queue_body,
                text="Nothing waiting.\n\nDocuments appear here only when the "
                     "customer's number could not be read off the page.",
                style="Hint.TLabel",
                justify="left",
            ).pack(anchor="w", pady=20)
            return

        for job in jobs:
            self._queue_row(job)

    def _queue_row(self, job) -> None:
        box = ttk.LabelFrame(self.queue_body, text=vm.document_of(job), padding=10)
        box.pack(fill="x", pady=(0, 8))

        ttk.Label(box, text=vm.queue_caption(job), style="Hint.TLabel").pack(anchor="w")
        ttk.Label(
            box, text=job.hold_reason or job.error or "", justify="left",
            wraplength=640, foreground=TONE_COLOURS["warn"],
        ).pack(anchor="w", pady=(4, 8))

        row = ttk.Frame(box)
        row.pack(fill="x")
        entry = ttk.Entry(row, width=22)
        entry.insert(0, job.recipient or "")
        entry.pack(side="left")
        ttk.Button(
            row, text="Send", command=lambda j=job.id, e=entry: self.send_job(j, e.get())
        ).pack(side="left", padx=6)
        if job.chat_url:
            ttk.Button(
                row,
                text="Open WhatsApp",
                command=lambda j=job.id: self.open_whatsapp(j),
            ).pack(side="left", padx=(0, 6))
        ttk.Button(
            row, text="View PDF", command=lambda j=job.id: self.open_pdf(j)
        ).pack(side="left")
        ttk.Button(
            row, text="Discard", command=lambda j=job.id: self.discard_job(j)
        ).pack(side="right")

    # -- recent tab --------------------------------------------------------

    def _build_recent(self) -> None:
        columns = ("time", "status", "sent_to", "document", "detail")
        self.recent = ttk.Treeview(
            self.recent_tab, columns=columns, show="headings", height=18
        )
        for column, heading, width in (
            ("time", "Time", 110),
            ("status", "Status", 110),
            ("sent_to", "Sent to", 140),
            ("document", "Document", 140),
            ("detail", "Detail", 260),
        ):
            self.recent.heading(column, text=heading)
            self.recent.column(column, width=width, anchor="w")

        scroll = ttk.Scrollbar(
            self.recent_tab, orient="vertical", command=self.recent.yview
        )
        self.recent.configure(yscrollcommand=scroll.set)
        self.recent.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _render_recent(self) -> None:
        self.recent.delete(*self.recent.get_children())
        for job in self.pipeline.store.recent(80):
            self.recent.insert("", "end", values=vm.history_row(job))

    # -- settings tab ------------------------------------------------------

    def _build_settings(self) -> None:
        self.fields: dict[str, tk.Variable] = {}
        # For fields whose stored value is not what the operator reads.
        self.choices: dict[str, list[tuple[str, str]]] = {}

        # Apply belongs to the tab, not to the scrolling content. The groups
        # below run past the bottom edge on a laptop screen, and a button that
        # has scrolled out of sight reads as a button that is not there — an
        # operator typed a whole page of settings, pressed Test send, and was
        # told the fields were empty.
        buttons = ttk.Frame(self.settings_tab)
        buttons.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Apply", command=self.apply_settings).pack(
            side="right"
        )

        body = self._scrollable(self.settings_tab)

        account = ttk.LabelFrame(body, text="WhatsApp account", padding=12)
        account.pack(fill="x", pady=(0, 10))
        self._entry(account, "phone_number_id", "Phone number ID",
                    "Meta Business → WhatsApp → API Setup.")
        self._entry(account, "own_numbers", "Our own numbers",
                    "Comma separated. Never treated as a customer, so the number "
                    "on your own letterhead cannot be sent its own receipt.")
        self._entry(account, "default_template", "Message",
                    "An approved template name. Meta requires this for messages "
                    "you start.")
        self._entry(account, "document_noun", "Attachment name",
                    "What your paperwork is called. Names the PDF on the "
                    "customer's phone: Receipt-CR1747-26.pdf.")

        sending = ttk.LabelFrame(body, text="Sending", padding=12)
        sending.pack(fill="x", pady=(0, 10))
        self._choice(
            sending, "send_mode", "After printing", SEND_MODES,
            "Send automatically needs an approved message and a working "
            "WhatsApp Business account. Otherwise the receipt is read and "
            "WhatsApp opens with the message ready, and you press send and "
            "attach the PDF yourself.",
        )
        self._check(sending, "dry_run", "Test mode — process everything, send nothing")
        self._check(
            sending,
            "auto_open_chat",
            "Open WhatsApp automatically after printing",
        )
        self._check(sending, "confirm_before_send", "Ask before every send")
        self._entry(sending, "dedupe_window_hours", "Ignore reprints for (hours)", "")
        self._entry(sending, "max_sends_per_minute", "Maximum per minute",
                    "Per computer. Stops one runaway batch print.")

        scanned = ttk.LabelFrame(body, text="Scanned documents", padding=12)
        scanned.pack(fill="x", pady=(0, 10))
        self._check(scanned, "ocr_enabled", "Read documents printed as an image (OCR)")
        self._check(scanned, "ocr_silent_send",
                    "Send to numbers read by OCR without asking")

        install = ttk.LabelFrame(body, text="This computer", padding=12)
        install.pack(fill="x", pady=(0, 10))
        self._entry(install, "branch_name", "Branch", "")
        self._entry(install, "device_name", "Computer", "")
        self._entry(install, "update_url", "Update location",
                    "A link to the version file. Leave blank to disable updates.")

    def _scrollable(self, parent: ttk.Frame) -> ttk.Frame:
        """A vertically scrolling region filling `parent`, returning its content
        frame. Tk has no scrolling container, so it is a canvas with a frame
        inside it."""
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        # The content decides how tall the scroll region is; the canvas decides
        # how wide the content is, so the groups stretch instead of sitting in
        # a narrow column.
        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>", lambda e: canvas.itemconfigure(window, width=e.width)
        )

        # Tk delivers the wheel to the widget under the pointer, which is
        # normally an entry inside the frame rather than the canvas. Bind on
        # the window and match by widget path, so the wheel still works over a
        # field but leaves the other tabs' lists alone.
        def wheel(event: "tk.Event") -> None:
            path = str(event.widget)
            if path == str(canvas) or path.startswith(f"{canvas}."):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        parent.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
        return inner

    def _entry(self, parent, name: str, label: str, hint: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=24, anchor="e").pack(side="left", padx=(0, 10))
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        self.fields[name] = var
        if hint:
            ttk.Label(
                parent, text=hint, style="Hint.TLabel", wraplength=560, justify="left"
            ).pack(anchor="w", padx=(184, 0))

    def _choice(
        self,
        parent,
        name: str,
        label: str,
        options: list[tuple[str, str]],
        hint: str,
    ) -> None:
        """A setting with a fixed set of values, picked by its plain label.

        Read-only, so the operator cannot type a value the code does not
        understand — the failure that leaves would be silent, at the worst
        moment.
        """
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=24, anchor="e").pack(side="left", padx=(0, 10))
        var = tk.StringVar()
        ttk.Combobox(
            row,
            textvariable=var,
            values=[text for text, _ in options],
            state="readonly",
        ).pack(side="left", fill="x", expand=True)
        self.fields[name] = var
        self.choices[name] = options
        if hint:
            ttk.Label(
                parent, text=hint, style="Hint.TLabel", wraplength=560, justify="left"
            ).pack(anchor="w", padx=(184, 0))

    def _choice_label(self, name: str, value: str) -> str:
        """The label for a stored value; the first option if it is unknown."""
        options = self.choices[name]
        for text, stored in options:
            if stored == value:
                return text
        return options[0][0]

    def _choice_value(self, name: str, fallback: str) -> str:
        """The stored value for whatever label is showing."""
        showing = self.fields[name].get()
        for text, stored in self.choices[name]:
            if text == showing:
                return stored
        return fallback

    def _check(self, parent, name: str, label: str) -> None:
        var = tk.BooleanVar()
        ttk.Checkbutton(parent, text=label, variable=var).pack(anchor="w", pady=2)
        self.fields[name] = var

    def _load_settings_into_form(self) -> None:
        s = self.settings
        values = {
            "phone_number_id": s.phone_number_id,
            "own_numbers": ", ".join(s.own_numbers),
            "default_template": s.default_template,
            "document_noun": s.document_noun,
            "dedupe_window_hours": str(s.dedupe_window_hours),
            "max_sends_per_minute": str(s.max_sends_per_minute),
            "branch_name": s.branch_name,
            "device_name": s.device_name,
            "update_url": s.update_url,
            "send_mode": self._choice_label("send_mode", s.send_mode),
            "dry_run": s.dry_run,
            "auto_open_chat": s.auto_open_chat,
            "confirm_before_send": s.confirm_before_send,
            "ocr_enabled": s.ocr_enabled,
            "ocr_silent_send": s.ocr_silent_send,
        }
        for name, value in values.items():
            self.fields[name].set(value)

    def apply_settings(self) -> None:
        s = self.settings
        try:
            s.dedupe_window_hours = int(self.fields["dedupe_window_hours"].get())
            s.max_sends_per_minute = int(self.fields["max_sends_per_minute"].get())
        except ValueError:
            messagebox.showerror(
                "WhatsApp Printer",
                "Reprint window and maximum per minute must be whole numbers.",
                parent=self.root,
            )
            return

        was_test = s.dry_run
        was_manual = s.send_mode == "link"
        s.send_mode = self._choice_value("send_mode", s.send_mode)
        s.phone_number_id = self.fields["phone_number_id"].get().strip()
        s.own_numbers = [
            n.strip() for n in self.fields["own_numbers"].get().split(",") if n.strip()
        ]
        s.default_template = (
            self.fields["default_template"].get().strip() or s.default_template
        )
        s.document_noun = (
            self.fields["document_noun"].get().strip() or s.document_noun
        )
        s.branch_name = self.fields["branch_name"].get().strip()
        s.device_name = self.fields["device_name"].get().strip()
        s.update_url = self.fields["update_url"].get().strip()
        s.dry_run = bool(self.fields["dry_run"].get())
        s.auto_open_chat = bool(self.fields["auto_open_chat"].get())
        s.confirm_before_send = bool(self.fields["confirm_before_send"].get())
        s.ocr_enabled = bool(self.fields["ocr_enabled"].get())
        s.ocr_silent_send = bool(self.fields["ocr_silent_send"].get())
        s.save()

        # One dialog, not two: changing both at once is the go-live moment,
        # and two stacked warnings get clicked through as a single reflex.
        changed = []
        if was_test and not s.dry_run:
            changed.append("Test mode is now OFF.")
        if was_manual and s.send_mode == "api":
            changed.append(
                "Receipts will now go out on their own, with no one pressing "
                "send."
            )
        if changed:
            messagebox.showwarning(
                "WhatsApp Printer",
                "\n\n".join(changed)
                + "\n\nPrinting will send real messages to customers.",
                parent=self.root,
            )
        self.refresh()
        # Saved values are normalised on the way in (numbers trimmed, own
        # numbers split), so show what was actually stored rather than what
        # was typed.
        self._load_settings_into_form()

    # -- actions -----------------------------------------------------------

    def test_send(self) -> None:
        from ..send.readiness import problems

        outstanding = problems(self.settings, self.pipeline.templates)
        if outstanding:
            messagebox.showerror(
                "WhatsApp Printer",
                "Cannot send yet:\n\n• " + "\n• ".join(outstanding),
                parent=self.root,
            )
        elif self.settings.dry_run:
            messagebox.showinfo(
                "WhatsApp Printer",
                "Everything is configured correctly, but test mode is on so "
                "nothing was sent.",
                parent=self.root,
            )
        else:
            messagebox.showinfo(
                "WhatsApp Printer",
                "Configuration looks complete. Print a document to send one.",
                parent=self.root,
            )

    def send_job(self, job_id: str, recipient: str) -> None:
        try:
            job = self.pipeline.release(job_id, recipient)
        except (KeyError, ValueError) as exc:
            messagebox.showerror("WhatsApp Printer", str(exc), parent=self.root)
            return
        if job.status is JobStatus.FAILED:
            messagebox.showerror(
                "WhatsApp Printer", job.error or "Send failed", parent=self.root
            )
        self.refresh()

    def discard_job(self, job_id: str) -> None:
        try:
            self.pipeline.discard(job_id)
        except KeyError as exc:
            messagebox.showerror("WhatsApp Printer", str(exc), parent=self.root)
        self.refresh()

    def open_pdf(self, job_id: str) -> None:
        job = self.pipeline.store.get(job_id)
        if job is None or not job.pdf_path.exists():
            messagebox.showerror(
                "WhatsApp Printer", "That PDF is no longer on disk.", parent=self.root
            )
            return
        webbrowser.open(job.pdf_path.as_uri())

    def open_whatsapp(self, job_id: str) -> bool:
        """Open the member's chat with the message already typed.

        Also puts the receipt on the clipboard, because a wa.me link carries
        text only — there is no way to attach a file to one. Ctrl+V in WhatsApp
        Desktop then attaches it, which is the shortest path we can offer until
        the API account is live.
        """
        from ..send.link import copy_file_to_clipboard, open_chat, reveal

        job = self.pipeline.store.get(job_id)
        if job is None or not job.chat_url:
            messagebox.showerror(
                "WhatsApp Printer",
                "This document has no WhatsApp link.",
                parent=self.root,
            )
            return False

        copied = copy_file_to_clipboard(job.pdf_path)
        try:
            open_chat(job.chat_url)
            self.pipeline.hand_off(job_id)
        except Exception as exc:
            log.exception("could not open the chat")
            messagebox.showerror("WhatsApp Printer", str(exc), parent=self.root)
            return False

        if not copied:
            # No clipboard help available, so show them where the file is.
            reveal(job.pdf_path)
        self.refresh()
        return True

    def check_updates(self) -> None:
        """Manual check, so a same-day fix does not wait for the daily one."""
        if self.on_check_updates is None:
            return
        self.update_button.state(["disabled"])
        self.update_status.configure(text="Checking…")

        def worker() -> None:
            try:
                message = self.on_check_updates()
            except Exception as exc:
                log.exception("update check failed")
                message = f"Could not check for updates: {exc}"
            # Back onto the main thread before touching a widget.
            self.root.after(0, lambda: self._update_checked(message))

        threading.Thread(target=worker, name="update-check", daemon=True).start()

    def _update_checked(self, message: str) -> None:
        self.update_status.configure(text=message)
        self.update_button.state(["!disabled"])

    # -- notifications -----------------------------------------------------

    def submit(self, job_id: str) -> None:
        """Thread-safe: called by the watcher when a job finishes."""
        self.incoming.put(job_id)

    def request_show(self) -> None:
        """Thread-safe: called by the instance guard from its own thread.

        Tk may only be touched by the thread running the loop, so this records
        the request and lets _pump act on it.
        """
        self._show_requested.set()

    def _pump(self) -> None:
        if self._show_requested.is_set():
            self._show_requested.clear()
            self.show()
        try:
            job_id = self.incoming.get_nowait()
        except queue.Empty:
            pass
        else:
            self._notify(job_id)
            self.refresh()
        self.root.after(POLL_MS, self._pump)

    def _notify(self, job_id: str) -> None:
        job = self.pipeline.store.get(job_id)
        if job is None:
            return

        # One member at a time at a counter, so go straight to the chat rather
        # than making them click. The notification that follows reports what
        # happened and reminds them to paste.
        if (
            job.status is JobStatus.READY
            and getattr(self.settings, "auto_open_chat", False)
            and job.chat_url
        ):
            if self.open_whatsapp(job_id):
                job = self.pipeline.store.get(job_id) or job
        # A batch print should not stack panels down the screen.
        for existing in self._notifications:
            existing.close()
        self._notifications = [
            Notification(
                self.root,
                job,
                on_open=self.show,
                on_whatsapp=lambda j=job.id: self.open_whatsapp(j),
            )
        ]

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> None:
        from ..send.readiness import problems

        outstanding = problems(self.settings, self.pipeline.templates)
        counters = vm.counters_for_today(self.pipeline.store, self.settings)
        waiting = len(self.pipeline.store.pending())

        state = vm.device_state(self.settings, waiting, outstanding)
        self.state_label.configure(
            text=state.text, foreground=TONE_COLOURS.get(state.tone, "")
        )

        for key, value in (
            ("sent", counters.sent),
            ("printed", counters.printed),
            ("waiting", counters.waiting),
            ("failed", counters.failed),
        ):
            self.counter_values[key].configure(text=str(value))
        self.counter_captions["sent"].configure(text=vm.sent_caption(self.settings))

        if outstanding:
            self.problems_label.configure(text="• " + "\n• ".join(outstanding))
            self.problems_box.pack(fill="x", pady=(0, 12), before=None)
        else:
            self.problems_box.pack_forget()

        self.tabs.tab(1, text=f"Needs attention ({waiting})" if waiting else "Needs attention")

        self._render_queue()
        self._render_recent()
        # The settings form is deliberately NOT reloaded here. refresh() runs
        # on a two-second timer, and reloading would overwrite whatever the
        # operator is halfway through typing. The form is filled once at
        # startup and again after Apply, which is the only time saved state
        # and the form can legitimately disagree.

    def _refresh(self) -> None:
        try:
            self.refresh()
        except Exception:
            log.exception("refresh failed")
        self.root.after(REFRESH_MS, self._refresh)

    # -- lifecycle ---------------------------------------------------------

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        """Closing the window must not stop prints being captured."""
        self.root.withdraw()

    def run(self, visible: bool = True) -> None:
        if not visible:
            self.root.withdraw()
        self.root.mainloop()

    def stop(self) -> None:
        try:
            self.root.quit()
        except tk.TclError:
            pass
