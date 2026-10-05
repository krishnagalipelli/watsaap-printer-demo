"""The application window.

A side bar on the left and one page at a time beside it. The counter clerk
needs three pages -- Status, Needs attention, Recent documents -- and those
are all the side bar opens without a PIN. Setup and Message templates, which
decide what members receive, swap the setup screens (ui/setup.py) into the same
place, optionally behind a PIN, rather than opening a second window.

The look -- ink on paper, one mint accent -- is ui/theme.py. Everything the
operator reads comes from ui/viewmodel.py, which is where the wording and the
status labels live and where they are tested. This file is widgets and wiring
only.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import __version__
from ..archive import folder_for
from ..models import JobStatus
from . import icons, theme
from . import viewmodel as vm
from .notification import Notification

log = logging.getLogger(__name__)

REFRESH_MS = 2000
POLL_MS = 400
SIDEBAR_WIDTH = 224
PAGE_PADDING = (32, 26, 32, 28)

# Settings that are a fixed choice rather than free text: the plain-English
# label the operator picks, paired with the value stored in settings.json.
SEND_MODES = [
    ("Send automatically", "api"),
    ("Open WhatsApp for me to send", "link"),
]

# The side bar: (key, label, icon). The counter's own pages, then the two
# ways into setup.
COUNTER_PAGES = [
    ("status", "Status", "activity"),
    ("attention", "Needs attention", "alert"),
    ("recent", "Recent documents", "file"),
]
SETUP_LINKS = [
    ("setup", "Setup", "settings"),
    ("templates", "Message templates", "clipboard"),
]

HISTORY_COLUMNS = [
    ("time", "Time", 100),
    ("status", "Status", 120),
    ("sent_to", "Sent to", 130),
    ("document", "Document", 120),
    ("detail", "Detail", 200),
]


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
        self._queue_rows: dict[str, ttk.Entry] = {}
        self._queue_shown: tuple | None = None
        self._queue_limit = 200
        self._setup_shown = False
        self._view = "status"
        self._recent_shown: tuple | None = None
        self._needs_shown: tuple | None = None
        # Jobs whose send is running on a worker thread right now.
        self._sending: set[str] = set()
        self.root = tk.Tk()
        self.root.title("WhatsApp Printer")
        self._size_window()
        theme.apply(self.root)

        self._build_sidebar()
        ttk.Separator(self.root, orient="vertical").pack(side="left", fill="y")
        self.main = ttk.Frame(self.root)
        self.main.pack(side="left", fill="both", expand=True)
        self._build_header()
        self._build_pages()

        # Closing the window leaves the app running: prints still have to be
        # captured. Only Exit actually quits.
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self.root.after(POLL_MS, self._pump)
        self.root.after(REFRESH_MS, self._refresh)
        self.show_view("status")
        self._load_settings_into_form()

    # -- chrome ------------------------------------------------------------

    def _size_window(self) -> None:
        """As roomy as the design wants, but never past the edge of a small
        counter screen: some of them are still 1024 x 768."""
        width = min(1120, self.root.winfo_screenwidth() - 40)
        height = min(740, self.root.winfo_screenheight() - 90)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(960, width), min(600, height))

    def _icon(self, name: str, colour: str, size: int = 16, **gap) -> object:
        return icons.icon(self.root, name, colour, size, **gap)

    def _build_sidebar(self) -> None:
        side = ttk.Frame(self.root, width=theme.px(self.root, SIDEBAR_WIDTH))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        # The mode card first, at the bottom, so a short window squeezes the
        # gap above it rather than the card.
        foot = ttk.Frame(side, padding=16)
        foot.pack(side="bottom", fill="x")
        ttk.Separator(side).pack(side="bottom", fill="x")
        mode = ttk.Frame(foot, style="Mode.TFrame", padding=14)
        mode.pack(fill="x")
        ttk.Label(mode, text="CURRENT MODE", style="ModeEyebrow.TLabel").pack(anchor="w")
        self.mode_title = ttk.Label(mode, style="Mode.TLabel", compound="left")
        self.mode_title.pack(anchor="w", pady=(8, 0))
        self.mode_detail = ttk.Label(mode, style="ModeHint.TLabel", justify="left")
        self.mode_detail.pack(anchor="w", fill="x", pady=(4, 0))
        theme.autowrap(self.mode_detail)

        brand = ttk.Frame(side, padding=(18, 24, 12, 26))
        brand.pack(fill="x")
        row = ttk.Frame(brand)
        row.pack(fill="x")
        size = theme.px(self.root, 30)
        mark = tk.Canvas(row, width=size, height=size, highlightthickness=0,
                         borderwidth=0, background=theme.MINT)
        mark.create_image(size // 2, size // 2, image=self._icon("printer", theme.CARD))
        mark.pack(side="left")
        ttk.Label(row, text="WhatsApp Printer", style="Brand.TLabel").pack(
            side="left", padx=(10, 0))
        theme.eyebrow(brand, f"Operations / v{__version__}").pack(
            anchor="w", padx=(size + 10, 0), pady=(8, 0))

        nav = ttk.Frame(side, padding=(12, 0))
        nav.pack(fill="x")
        self.nav_buttons: dict[str, ttk.Button] = {}
        self._nav_icons: dict[str, str] = {}
        for key, label, name in COUNTER_PAGES:
            self._nav_button(nav, key, label, name)
        theme.eyebrow(nav, "Configuration").pack(anchor="w", padx=10, pady=(24, 6))
        for key, label, name in SETUP_LINKS:
            self._nav_button(nav, key, label, name)

    def _nav_button(self, parent, key: str, label: str, icon_name: str) -> None:
        button = ttk.Button(parent, text=label, style="Nav.TButton", compound="left",
                            command=lambda: self._go(key))
        button.pack(fill="x", pady=1)
        self.nav_buttons[key] = button
        self._nav_icons[key] = icon_name

    def _mark_nav(self) -> None:
        """Light up the side bar entry for whatever is showing."""
        if self._setup_shown:
            active = "templates" if self.setup_view.current == "templates" else "setup"
        else:
            active = self._view
        for key, button in self.nav_buttons.items():
            on = key == active
            button.configure(
                style="NavOn.TButton" if on else "Nav.TButton",
                image=self._icon(self._nav_icons[key], theme.INK if on else theme.SUBTLE,
                                 after=10),
            )

    def _build_header(self) -> None:
        header = ttk.Frame(self.main, padding=(PAGE_PADDING[0], 12, 24, 12))
        header.pack(fill="x")
        # Packed before the text, so a long line is what gets clipped.
        ttk.Button(
            header, text="Check configuration", style="Mint.TButton", compound="left",
            image=self._icon("refresh", theme.CARD, 14, after=7), command=self.test_send,
        ).pack(side="right")
        self.mode_chip = ttk.Label(header, style="Chip.TLabel")
        self.mode_chip.pack(side="left")
        self.state_label = ttk.Label(header, style="Hint.TLabel")
        self.state_label.pack(side="left", padx=(12, 12))
        ttk.Separator(self.main).pack(fill="x")

    def _build_pages(self) -> None:
        from .setup import SetupView

        self.pages_area = ttk.Frame(self.main)
        self.pages_area.pack(fill="both", expand=True)

        self.views: dict[str, ttk.Frame] = {}
        for key in ("status", "attention"):
            outer = ttk.Frame(self.pages_area)
            page = ttk.Frame(theme.scrollable(outer), padding=PAGE_PADDING)
            page.pack(fill="both", expand=True)
            self.views[key] = outer
            setattr(self, f"{key}_page", page)
        # Recent is one long table with its own scroll bar, so the page itself
        # does not scroll: two scroll bars side by side is one too many.
        self.views["recent"] = ttk.Frame(self.pages_area, padding=PAGE_PADDING)
        self.recent_page = self.views["recent"]

        # Built now but not shown: the setup view replaces the counter's pages
        # when Setup is chosen. The counter settings form is one of its
        # pages, and keeps the name it had when it was a tab.
        self.setup_view = SetupView(self.pages_area, self)
        self.settings_tab = self.setup_view.pages["preferences"].frame

        self._build_status()
        self._build_queue()
        self._build_recent()
        self._build_settings()

    # -- navigation ----------------------------------------------------------

    def _go(self, key: str) -> None:
        """What a side bar entry does."""
        if key in {k for k, _label, _icon in COUNTER_PAGES}:
            self.show_view(key)
            return
        current = self.setup_view.current
        page = "templates" if key == "templates" else (
            current if current and current != "templates" else "connect")
        if self._setup_shown:
            self.setup_view.show_page(page)   # past the PIN already
        else:
            self.open_setup(page)

    def show_view(self, key: str) -> None:
        """Show one of the counter's own pages, leaving setup if it is open."""
        if self._setup_shown:
            self.setup_view.pack_forget()
            self._setup_shown = False
        for name, frame in self.views.items():
            if name != key:
                frame.pack_forget()
        self.views[key].pack(fill="both", expand=True)
        self._view = key
        self._mark_nav()
        self.refresh()

    # -- status page ---------------------------------------------------------

    def _build_status(self) -> None:
        page = self.status_page
        theme.page_title(page, "Overview / 01", "Status",
                         "What this printer has handled today.")

        # Four counts in a row, divided by hairlines: the rules are the wash
        # showing through one-pixel gaps between the tiles.
        self.tiles = tk.Frame(page, background=theme.WASH)
        self.tiles.pack(fill="x")
        self.counter_values: dict[str, ttk.Label] = {}
        self.counter_captions: dict[str, ttk.Label] = {}
        keys = (
            ("sent", "sent today"),
            ("printed", "documents printed"),
            ("waiting", "need attention"),
            ("failed", "failed"),
        )
        for column, (key, caption) in enumerate(keys):
            tile = ttk.Frame(self.tiles, padding=(18, 14, 18, 16))
            tile.grid(row=0, column=column, sticky="nsew", pady=1,
                      padx=(1, 1 if column == len(keys) - 1 else 0))
            self.tiles.columnconfigure(column, weight=1, uniform="tile")
            label = theme.eyebrow(tile, caption)
            label.pack(anchor="w")
            value = ttk.Label(tile, text="00", style="Big.TLabel")
            value.pack(anchor="w", pady=(12, 0))
            self.counter_values[key] = value
            self.counter_captions[key] = label

        self.total_sent_label = ttk.Label(page, text="Total messages sent: 0",
                                         style="Hint.TLabel")
        self.total_sent_label.pack(anchor="w", pady=(8, 0))

        # Shown only while something stops sending: one card per setup step
        # that has something left to do.
        self.problems_box = ttk.Frame(page)
        head = theme.section_head(self.problems_box, "Before this can send")
        ttk.Label(head, text="SETUP REQUIRED", style="WarnTag.TLabel").pack(side="right")
        self.needs_grid = ttk.Frame(self.problems_box)
        self.needs_grid.pack(fill="x")
        self.needs_grid.columnconfigure((0, 1), weight=1, uniform="need")

        import sys
        if sys.platform == "win32" and sys.getwindowsversion().major < 10:
            from ..capture.spooler import PRINTER_NAME, printer_installed
            from ..config import paths
            # Windows 7 gets a real queue built on the inbox XPS driver. Only
            # when that driver has been turned off does the machine fall back
            # to reading the spool folder, and only then is the operator asked
            # to do anything different. None means we could not tell, so say
            # both rather than send them to the folder for nothing.
            has_printer = printer_installed()
            if has_printer is not False:
                capture = theme.card(page, "How to send", padding=14)
                capture.pack(fill="x", pady=(16, 0))
                text = f'Print to "{PRINTER_NAME}" from your billing software.'
                if has_printer is None:
                    text += ("\n\nIf it is not in the print dialog, export a PDF into "
                             f"{paths().spool} instead.")
                theme.autowrap(ttk.Label(capture, text=text, justify="left")).pack(fill="x")
            else:
                capture = theme.card(page, "PDF folder capture", padding=14)
                capture.pack(fill="x", pady=(16, 0))
                theme.autowrap(ttk.Label(
                    capture,
                    text=(f'"{PRINTER_NAME}" is not installed, so export each document '
                          f"as a PDF into {paths().spool} instead.\nUse a new filename "
                          "each time. The app reads and moves it automatically."),
                    justify="left")).pack(fill="x")
                ttk.Button(capture, text="Open PDF folder",
                           command=lambda: webbrowser.open(paths().spool.as_uri())).pack(
                    anchor="w", pady=(6, 0))

        self.activity_box = ttk.Frame(page)
        self.activity_box.pack(fill="x", pady=(30, 0))
        head = theme.section_head(self.activity_box, "Recent activity")
        ttk.Button(
            head, text="View all", style="Link.TButton", compound="right",
            image=self._icon("arrow", theme.INK, 14, before=6),
            command=lambda: self.show_view("recent"),
        ).pack(side="right")
        self.activity = self._history_table(self.activity_box, height=5)
        # Before the first print the table would be empty, which says
        # nothing; say how to use the printer instead.
        self.activity_empty = theme.card(self.activity_box, "Nothing printed yet", padding=18)
        theme.autowrap(ttk.Label(self.activity_empty, text=vm.HOW_TO_USE,
                                 style="Hint.TLabel", justify="left")).pack(fill="x")

        ttk.Separator(page).pack(fill="x", pady=(36, 14))
        foot = ttk.Frame(page)
        foot.pack(fill="x")
        self.version_label = ttk.Label(
            foot, text=f"WhatsApp Printer · Version {__version__}", style="Small.TLabel")
        self.version_label.pack(side="left")
        self.update_button = ttk.Button(
            foot, text="Check for updates", style="Link.TButton", command=self.check_updates
        )
        self.update_button.pack(side="right")
        self.update_status = ttk.Label(foot, text="", style="Small.TLabel")
        self.update_status.pack(side="right", padx=(0, 12))

    def _history_table(self, parent, height: int) -> ttk.Treeview:
        table = ttk.Treeview(parent, columns=[c for c, _h, _w in HISTORY_COLUMNS],
                             show="headings", height=height, selectmode="browse")
        theme.headings(table, HISTORY_COLUMNS)
        # Only the rows that mean something different from the rest are
        # coloured: failures, and what was ignored.
        table.tag_configure("bad", foreground=theme.DANGER)
        table.tag_configure("muted", foreground=theme.SUBTLE)
        return table

    def _render_needs(self, problems: list[str]) -> None:
        needs = tuple(vm.setup_needs(problems))
        if needs == self._needs_shown:
            return
        self._needs_shown = needs
        for child in self.needs_grid.winfo_children():
            child.destroy()
        if not needs:
            self.problems_box.pack_forget()
            return
        for index, need in enumerate(needs):
            box = theme.card(self.needs_grid, padding=18)
            box.grid(row=index // 2, column=index % 2, sticky="nsew", pady=(0, 12),
                     padx=(0, 12) if index % 2 == 0 else 0)
            top = ttk.Frame(box)
            top.pack(fill="x", pady=(0, 12))
            ttk.Label(top, image=self._icon("dot", theme.WARM, 12)).pack(side="left")
            ttk.Label(top, text=need.tag.upper(),
                      style="OkTag.TLabel" if need.tag == "Required" else "MutedTag.TLabel",
                      ).pack(side="right")
            ttk.Label(box, text=need.title, style="Head.TLabel").pack(anchor="w")
            for line in need.lines:
                theme.autowrap(ttk.Label(box, text=line, style="Hint.TLabel", justify="left")
                               ).pack(fill="x", anchor="w", pady=(4, 0))
            ttk.Button(
                box, text=need.action, style="Link.TButton", compound="right",
                image=self._icon("arrow", theme.INK, 14, before=6),
                command=lambda page=need.page: self.open_setup(page),
            ).pack(anchor="w", pady=(14, 0))
        self.problems_box.pack(fill="x", pady=(30, 0), before=self.activity_box)

    # -- needs attention page ------------------------------------------------

    def _build_queue(self) -> None:
        page = self.attention_page
        _title, self.attention_detail = theme.page_title(
            page, "Operations / 02", "Needs attention", "")
        self.queue_body = ttk.Frame(page)
        self.queue_body.pack(fill="x")
        # The same problems as the Status page, as a numbered list: the
        # operator who comes here because nothing is sending should find why.
        self.checklist_box = ttk.Frame(page)
        self._checklist_shown: tuple | None = None

    @staticmethod
    def _queue_signature(jobs) -> tuple:
        """Everything about the queue the operator can actually see.

        If this has not changed there is nothing to redraw, and redrawing
        anyway would destroy the number they are halfway through typing.
        """
        return tuple(
            (j.id, str(j.status), j.recipient, j.hold_reason, j.error, j.chat_url)
            for j in jobs
        )

    def _render_queue(self) -> None:
        jobs = self.pipeline.store.pending(limit=self._queue_limit)
        total = self.pipeline.store.pending_count()
        signature = (self._queue_signature(jobs), tuple(sorted(self._sending)), total)

        # refresh() runs on a two-second timer. Rebuilding these rows destroys
        # the Entry the operator is typing a number into, which is the same
        # hazard the settings form is spared below -- except here it silently
        # ate the input. Redraw only when the visible state actually differs.
        if signature == self._queue_shown:
            return
        self._queue_shown = signature

        # A job arriving or leaving does force a rebuild, and the operator may
        # be mid-number in one of the rows that survives it. Carry the typed
        # text, the focus and the caret across.
        drafts = {
            job_id: entry.get()
            for job_id, entry in self._queue_rows.items()
            if entry.winfo_exists()
        }
        focused = self.root.focus_get()
        focused_job = next(
            (jid for jid, e in self._queue_rows.items() if e is focused), None
        )
        caret = focused.index("insert") if focused_job else None

        for child in self.queue_body.winfo_children():
            child.destroy()
        self._queue_rows.clear()

        self.attention_detail.configure(
            text=f"{total} document(s) could not go out on their own. Check the "
                 f"number, then send or discard each one."
            if total else "Failed sends and documents waiting for review appear here."
        )
        if not jobs:
            self._all_clear()
            return

        for job in jobs:
            self._queue_row(job, drafts.get(job.id))
        if total > len(jobs):
            ttk.Button(self.queue_body, text=f"Show more ({total - len(jobs)} remaining)",
                       command=self._more_pending).pack(pady=8)

        if focused_job in self._queue_rows:
            entry = self._queue_rows[focused_job]
            entry.focus_set()
            if caret is not None:
                entry.icursor(caret)

    def _all_clear(self) -> None:
        ttk.Separator(self.queue_body).pack(fill="x")
        row = ttk.Frame(self.queue_body, padding=(0, 16))
        row.pack(fill="x")
        size = theme.px(self.root, 30)
        badge = tk.Canvas(row, width=size, height=size, highlightthickness=0,
                          borderwidth=0, background=theme.WASH)
        badge.create_image(size // 2, size // 2, image=self._icon("check", theme.MINT))
        badge.pack(side="left")
        ttk.Label(row, text="Nothing needs attention. No failed documents or "
                            "delivery problems.").pack(side="left", padx=(12, 0))
        ttk.Separator(self.queue_body).pack(fill="x")

    def _more_pending(self) -> None:
        self._queue_limit += 200
        self._queue_shown = None
        self._render_queue()

    def _queue_row(self, job, draft: str | None = None) -> None:
        box = theme.card(self.queue_body, padding=18)
        box.pack(fill="x", pady=(0, 12))

        top = ttk.Frame(box)
        top.pack(fill="x")
        ttk.Label(top, text=vm.document_of(job), style="Head.TLabel").pack(side="left")
        label, tone = vm.label_of(job)
        ttk.Label(top, text=label.upper(), style=f"{tone.title()}Tag.TLabel").pack(
            side="right")
        ttk.Label(box, text=vm.queue_caption(job), style="MonoHint.TLabel").pack(
            anchor="w", pady=(2, 0))
        reason = job.error or job.hold_reason or ""
        if reason:
            theme.autowrap(ttk.Label(box, text=reason, style="Warn.TLabel", justify="left")
                           ).pack(fill="x", anchor="w", pady=(10, 0))

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(14, 0))
        entry = ttk.Entry(row, width=22)
        entry.insert(0, draft if draft is not None else (job.recipient or ""))
        entry.pack(side="left")
        self._queue_rows[job.id] = entry
        sending = job.id in self._sending
        send = ttk.Button(
            row,
            text="Sending…" if sending else ("Retry…" if job.status == JobStatus.FAILED else "Send"),
            style="Mint.TButton",
            command=lambda j=job.id, e=entry: self.send_job(j, e.get()),
        )
        send.pack(side="left", padx=(8, 6))
        if sending:
            send.state(["disabled"])
            entry.state(["disabled"])
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
            row, text="Discard", style="Link.TButton",
            command=lambda j=job.id: self.discard_job(j),
        ).pack(side="right")

    def _render_checklist(self, problems: list[str]) -> None:
        shown = tuple(problems)
        if shown == self._checklist_shown:
            return
        self._checklist_shown = shown
        for child in self.checklist_box.winfo_children():
            child.destroy()
        if not problems:
            self.checklist_box.pack_forget()
            return
        theme.section_head(self.checklist_box,
                           f"Setup checklist / {len(problems):02d} items")
        box = theme.card(self.checklist_box, padding=0)
        box.pack(fill="x")
        for number, problem in enumerate(problems, 1):
            if number > 1:
                ttk.Separator(box).pack(fill="x")
            row = ttk.Frame(box, padding=(18, 12))
            row.pack(fill="x")
            ttk.Label(row, text=f"{number:02d}", style="MonoHint.TLabel").pack(
                side="left", anchor="n", pady=(1, 0))
            page = vm.fixed_by(problem)
            ttk.Button(
                row, text=vm.SETUP_ACTIONS[page], style="Link.TButton", compound="right",
                image=self._icon("arrow", theme.INK, 14, before=6),
                command=lambda p=page: self.open_setup(p),
            ).pack(side="right", anchor="n")
            theme.autowrap(ttk.Label(row, text=problem, justify="left")).pack(
                side="left", fill="x", expand=True, padx=14)
        self.checklist_box.pack(fill="x", pady=(30, 0))

    # -- recent documents page -----------------------------------------------

    def _build_recent(self) -> None:
        page = self.recent_page
        theme.page_title(page, "Operations / 03", "Recent documents",
                         "The last 80 documents this printer handled, newest first.")
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 10))
        self.recent_count = theme.eyebrow(bar, "")
        self.recent_count.pack(side="left")
        self.recent_date = ttk.Label(bar, style="Small.TLabel")
        self.recent_date.pack(side="right")

        holder = ttk.Frame(page)
        holder.pack(fill="both", expand=True)
        self.recent = self._history_table(holder, height=12)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.recent.yview)
        self.recent.configure(yscrollcommand=scroll.set)
        self.recent.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _render_recent(self) -> None:
        rows = tuple(
            (vm.history_row(job), vm.label_of(job)[1])
            for job in self.pipeline.store.recent(80)
        )
        # Redraw only when something changed. This runs every two seconds,
        # and rebuilding the list threw the scroll position back to the top
        # while the operator was reading further down it.
        if rows == self._recent_shown:
            return
        self._recent_shown = rows
        for table, shown in ((self.recent, rows), (self.activity, rows[:5])):
            table.delete(*table.get_children())
            for values, tone in shown:
                table.insert("", "end", values=values, tags=(tone,))
        self.recent_count.configure(
            text=f"{len(rows)} record{'' if len(rows) == 1 else 's'}".upper())
        self.recent_date.configure(text=datetime.now().strftime("%d %b"))
        if rows:
            self.activity_empty.pack_forget()
            self.activity.configure(height=min(5, len(rows)))
            self.activity.pack(fill="x")
        else:
            self.activity.pack_forget()
            self.activity_empty.pack(fill="x")

    # -- counter settings (a setup page) ---------------------------------------

    def _build_settings(self) -> None:
        self.fields: dict[str, tk.Variable] = {}
        # For fields whose stored value is not what the operator reads.
        self.choices: dict[str, list[tuple[str, str]]] = {}

        # Apply belongs to the page, not to the scrolling content. The groups
        # below run past the bottom edge on a laptop screen, and a button that
        # has scrolled out of sight reads as a button that is not there — an
        # operator typed a whole page of settings, pressed Test send, and was
        # told the fields were empty.
        buttons = ttk.Frame(self.settings_tab)
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Apply", style="Mint.TButton",
                   command=self.apply_settings).pack(side="right")

        body = theme.scrollable(self.settings_tab)

        business = self._group(body, "Your business")
        self._entry(business, "business_name", "Business name",
                    "Used where a message names the business.")
        self._entry(business, "own_numbers", "Our own numbers",
                    "Comma separated. Never treated as a customer, so the number "
                    "on your own letterhead cannot be sent its own receipt.")

        sending = self._group(body, "Sending")
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

        scanned = self._group(body, "Scanned documents")
        self._check(scanned, "ocr_enabled", "Read documents printed as an image (OCR)")
        self._check(scanned, "ocr_silent_send",
                    "Send to numbers read by OCR without asking")

        receipts = self._group(body, "Printed receipts")
        self._check(
            receipts,
            "keep_printed_pdfs",
            "Keep a copy of every receipt that is printed",
        )
        self._folder(
            receipts, "pdf_folder", "Keep them in",
            "Filed by the date they were printed and named after the receipt: "
            "2026-09-07\\CHQ6511-26 SHAHNAVAZDANISH MOHAMMAD.pdf. Leave blank "
            "to keep them inside the program's own folder.",
        )

        install = self._group(body, "This computer")
        self._entry(install, "branch_name", "Branch", "")
        self._entry(install, "device_name", "Computer", "")
        self._entry(install, "update_url", "Update location",
                    "A link to the version file. Leave blank to disable updates.")

    @staticmethod
    def _group(parent, title: str) -> ttk.Frame:
        # The right-hand gap keeps the cards' edge off the scroll bar.
        group = theme.card(parent, title, padding=(18, 16, 18, 18))
        group.pack(fill="x", pady=(0, 12), padx=(0, 12))
        return group

    @staticmethod
    def _label(parent, label: str) -> None:
        ttk.Label(parent, text=label, style="Field.TLabel").pack(anchor="w", pady=(8, 4))

    @staticmethod
    def _hint(parent, hint: str) -> None:
        if hint:
            theme.autowrap(ttk.Label(parent, text=hint, style="Hint.TLabel", justify="left")
                           ).pack(fill="x", anchor="w", pady=(4, 0))

    def _entry(self, parent, name: str, label: str, hint: str) -> None:
        self._label(parent, label)
        var = tk.StringVar()
        ttk.Entry(parent, textvariable=var).pack(fill="x")
        self.fields[name] = var
        self._hint(parent, hint)

    def _folder(self, parent, name: str, label: str, hint: str) -> None:
        """A path setting, with the two buttons that make it a folder setting.

        Typing a path by hand is how you end up with receipts filed to a drive
        letter that is not mapped any more, so Browse is the way in and Open is
        how the operator checks it went where they meant.
        """
        self._label(parent, label)
        row = ttk.Frame(parent)
        row.pack(fill="x")
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        ttk.Button(
            row, text="Browse...", command=lambda v=var: self._choose_folder(v)
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            row, text="Open", command=lambda v=var: self._open_folder(v)
        ).pack(side="left", padx=(6, 0))
        self.fields[name] = var
        self._hint(parent, hint)

    def _resolved_folder(self, var: "tk.StringVar") -> Path:
        typed = (var.get() or "").strip()
        return Path(typed).expanduser() if typed else folder_for(self.settings)

    def _choose_folder(self, var: "tk.StringVar") -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Where should printed receipts be kept?",
            initialdir=str(self._resolved_folder(var)),
            mustexist=False,
        )
        if chosen:
            # askdirectory hands back forward slashes even on Windows.
            var.set(str(Path(chosen)))

    def _open_folder(self, var: "tk.StringVar") -> None:
        folder = self._resolved_folder(var)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            webbrowser.open(folder.as_uri())
        except OSError as exc:
            messagebox.showerror(
                "WhatsApp Printer",
                f"Could not open that folder.\n\n{folder}\n\n{exc}",
                parent=self.root,
            )

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
        self._label(parent, label)
        var = tk.StringVar()
        ttk.Combobox(
            parent,
            textvariable=var,
            values=[text for text, _ in options],
            state="readonly",
        ).pack(fill="x")
        self.fields[name] = var
        self.choices[name] = options
        self._hint(parent, hint)

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
        ttk.Checkbutton(parent, text=label, variable=var).pack(anchor="w", pady=(8, 0))
        self.fields[name] = var

    def _load_settings_into_form(self) -> None:
        s = self.settings
        values = {
            "business_name": s.business_name,
            "own_numbers": ", ".join(s.own_numbers),
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
            "keep_printed_pdfs": s.keep_printed_pdfs,
            "pdf_folder": s.pdf_folder,
        }
        for name, value in values.items():
            self.fields[name].set(value)

    def apply_settings(self) -> None:
        s = deepcopy(self.settings)
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
        s.business_name = self.fields["business_name"].get().strip() or s.business_name
        s.own_numbers = [
            n.strip() for n in self.fields["own_numbers"].get().split(",") if n.strip()
        ]

        # Switching to automatic sending is when an unapproved message stops
        # being harmless: link mode never asks Meta, the API always does. So
        # the check runs here, over every message this counter actually uses,
        # and asks rather than refuses -- approval may be minutes away.
        if s.send_mode == "api":
            problems = []
            for ref in sorted({s.default_template, *s.document_templates.values()}):
                template = self.pipeline.templates.get(ref)
                if template is None:
                    problems.append(f"{ref} — not on this computer")
                elif not template.usable:
                    problems.append(f"{ref} — {template.status}, not approved by Meta")
            if problems and not messagebox.askyesno(
                "WhatsApp Printer",
                "These messages cannot be sent automatically as things stand:\n\n• "
                + "\n• ".join(problems)
                + "\n\nRefresh them in Setup → Message templates.\n\nSave anyway?",
                parent=self.root,
            ):
                return
        s.branch_name = self.fields["branch_name"].get().strip()
        s.device_name = self.fields["device_name"].get().strip()
        s.update_url = self.fields["update_url"].get().strip()
        s.dry_run = bool(self.fields["dry_run"].get())
        s.auto_open_chat = bool(self.fields["auto_open_chat"].get())
        s.confirm_before_send = bool(self.fields["confirm_before_send"].get())
        s.ocr_enabled = bool(self.fields["ocr_enabled"].get())
        s.ocr_silent_send = bool(self.fields["ocr_silent_send"].get())

        keep = bool(self.fields["keep_printed_pdfs"].get())
        folder = self.fields["pdf_folder"].get().strip()
        # Refuse a folder that cannot be written to now, rather than accept it
        # and quietly file nothing. This is the office's own copy of its
        # paperwork; discovering next March that it stopped in September is
        # not a recoverable position.
        if keep and folder:
            try:
                Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "WhatsApp Printer",
                    f"Printed receipts cannot be kept in that folder.\n\n"
                    f"{folder}\n\n{exc}",
                    parent=self.root,
                )
                return
        s.keep_printed_pdfs = keep
        s.pdf_folder = folder
        try:
            self.pipeline.apply_settings(s)
        except Exception as exc:
            messagebox.showerror(
                "WhatsApp Printer",
                f"Settings could not be applied. Your previous settings are still active.\n\n{exc}",
                parent=self.root,
            )
            return
        self.pipeline.templates.business_name = s.business_name
        self.pipeline.templates.load()

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

    def _show_setup_text(self, title: str, text: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("640x480")
        dialog.configure(background=theme.PAPER)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        view = theme.style_text(tk.Text(body, wrap="word", padx=10, pady=10))
        scroll = ttk.Scrollbar(body, command=view.yview)
        view.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        view.pack(fill="both", expand=True)
        view.insert("1.0", text)
        view.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(0, 12))

    def export_setup(self) -> None:
        from ..setup_profile import export_setup

        selected = filedialog.asksaveasfilename(parent=self.root, title="Export setup (without the token)",
                                               initialfile="provision.json", defaultextension=".json",
                                               filetypes=[("Setup file", "*.json")])
        if not selected:
            return
        try:
            export_setup(Path(selected), self.settings, self.pipeline.templates,
                         self.pipeline.profile)
        except Exception as exc:
            messagebox.showerror("Export setup", str(exc), parent=self.root)
            return
        messagebox.showinfo("Export setup", "Setup exported in test mode. Put provision.json next to the installer, "
                            "or import it on another counter. Connect WhatsApp there and check a sample PDF "
                            "of each kind before going live.",
                            parent=self.root)

    def import_setup(self) -> None:
        from ..provision import apply

        selected = filedialog.askopenfilename(parent=self.root, title="Import counter setup",
                                              filetypes=[("Setup file", "*.json")])
        if not selected:
            return
        try:
            # Keep the source: it may be the engineer's reusable USB copy.
            result = apply(Path(selected), remove=False)
            self.pipeline.reload_if_changed()
            self.pipeline.templates.load()
            self.pipeline.reload_profile(force=True)
            # Token-only imports do not change the settings timestamp.
            try:
                self.pipeline.rebuild_sender()
            except Exception as exc:
                from ..send.unavailable import UnavailableSender

                self.pipeline.sender = UnavailableSender(str(exc))
                result.warnings.append(str(exc))
            self._load_settings_into_form()
            self.refresh()
            if self.in_setup:
                self.setup_view.show_page(self.setup_view.current or "share")
        except Exception as exc:
            messagebox.showerror("Import setup", str(exc), parent=self.root)
            return
        self._show_setup_text("Import setup", result.summary() + "\n\n" + "\n".join(result.warnings))

    # -- setup mode ----------------------------------------------------------

    @property
    def in_setup(self) -> bool:
        return self._setup_shown

    def open_setup(self, page: str = "") -> bool:
        """Swap the counter's pages for the setup screens, after the PIN."""
        from .setupmodel import check_pin

        if self._setup_shown:
            self.setup_view.show_page(page or self.setup_view.current or "connect")
            return True
        stored = self.settings.setup_pin
        if stored:
            pin = simpledialog.askstring("WhatsApp Printer setup", "Setup PIN:",
                                         show="•", parent=self.root)
            if pin is None:
                return False
            if not check_pin(pin, stored):
                messagebox.showerror("WhatsApp Printer setup", "That PIN is not right.",
                                     parent=self.root)
                return False
        self.show_setup(page)
        return True

    def show_setup(self, page: str = "") -> None:
        """Show the setup screens. No PIN check: callers have done it."""
        for frame in self.views.values():
            frame.pack_forget()
        self.setup_view.pack(fill="both", expand=True)
        self._setup_shown = True
        self._load_settings_into_form()
        self.setup_view.show_page(page or self.setup_view.current or "connect")
        self._mark_nav()

    def show_counter(self) -> None:
        """Back to whichever of the counter's pages was showing last."""
        self.show_view(self._view)

    def test_send(self) -> None:
        from ..send.readiness import problems

        outstanding = problems(self.settings, self.pipeline.templates, self.pipeline.profile)
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

    def send_job(self, job_id: str, recipient: str, document_kind: str | None = None) -> None:
        """Send a waiting job to the number in its row.

        The send itself runs on a worker thread. It used to run here, on the
        thread that owns every widget, and a Meta upload with its retries and
        timeouts could hold the window frozen for over a minute with no way
        to tell a stuck app from a slow one.
        """
        if job_id in self._sending:
            return
        job = self.pipeline.store.get(job_id)
        if job is not None and job.status == JobStatus.FAILED:
            if not messagebox.askyesno(
                "Retry document",
                f"{job.error or 'The previous send failed.'}\n\n"
                "Check WhatsApp first if the previous result was uncertain. "
                f"Retry this document to {recipient}?",
                parent=self.root,
            ):
                return
        if job is not None and document_kind is None and (
            job.fields.classification_error or
            (job.fields.document_kind is None and self.settings.document_templates)
        ):
            self._choose_job_kind(job, recipient)
            return
        self._sending.add(job_id)
        self._queue_shown = None
        self._render_queue()

        def worker() -> None:
            try:
                outcome: object = self.pipeline.release(job_id, recipient, document_kind=document_kind)
            except Exception as exc:  # anything: the job must not vanish
                log.exception("send from the queue failed for %s", job_id)
                outcome = exc
            self.root.after(0, lambda: self._sent(job_id, outcome))

        threading.Thread(target=worker, name=f"send-{job_id}", daemon=True).start()

    def _choose_job_kind(self, job, recipient):
        from .setup import Tasks
        dialog = tk.Toplevel(self.root)
        dialog.title("Review document type and message")
        dialog.geometry("620x440")
        dialog.transient(self.root)
        ttk.Label(dialog, text="Open the PDF, choose its type, then review the message.",
                  padding=12).pack(anchor="w")
        ttk.Button(dialog, text="Open PDF", command=lambda: webbrowser.open(job.pdf_path.resolve().as_uri())).pack()
        kinds = list(dict.fromkeys(k.name for k in self.pipeline.profile.all_kinds))
        selected = tk.StringVar()
        box = ttk.Combobox(dialog, values=kinds, textvariable=selected, state="readonly")
        box.pack(fill="x", padx=12, pady=8)
        preview = tk.Text(dialog, height=12, wrap="word", state="disabled")
        preview.pack(fill="both", expand=True, padx=12)
        tasks = Tasks(dialog)
        send = ttk.Button(dialog, text="Confirm type and send", state="disabled")
        send.pack(pady=12)
        def refresh(_event=None):
            chosen = selected.get()
            send.configure(state="disabled")
            def work():
                copy = deepcopy(job)
                copy.fields = self.pipeline.fields_for_kind(job.pdf_path, chosen)
                name, message = self.pipeline.compose(copy)
                if message is None:
                    raise ValueError(name)
                if message.missing:
                    raise ValueError("Message values missing: " + ", ".join(message.missing))
                return message.preview
            def done(result):
                if selected.get() != chosen:
                    refresh()
                    return
                preview.configure(state="normal")
                preview.delete("1.0", "end")
                preview.insert("1.0", str(result))
                preview.configure(state="disabled")
                if not isinstance(result, Exception):
                    send.configure(state="normal", command=lambda: (dialog.destroy(), self.send_job(job.id, recipient, chosen)))
            tasks.run("preview", work, done)
        box.bind("<<ComboboxSelected>>", refresh)

    def _sent(self, job_id: str, outcome: object) -> None:
        """Back on the main thread: report what the worker found."""
        self._sending.discard(job_id)
        self._queue_shown = None
        if isinstance(outcome, Exception):
            messagebox.showerror("WhatsApp Printer", str(outcome), parent=self.root)
        else:
            job = outcome
            if job.status is JobStatus.FAILED:
                messagebox.showerror(
                    "WhatsApp Printer", job.error or "Send failed", parent=self.root
                )
            elif job.status is JobStatus.DUPLICATE:
                messagebox.showinfo(
                    "WhatsApp Printer",
                    job.hold_reason or "This document was already sent.",
                    parent=self.root,
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

    def submit(self, job_id: str, auto_open: bool = True) -> None:
        """Thread-safe: called by the watcher when a job finishes.

        `auto_open` is False for every receipt of a batch print. One receipt at
        a counter should go straight to its chat; ten receipts should not throw
        ten chat windows at whoever is standing there.
        """
        self.incoming.put((job_id, auto_open))

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
            job_id, auto_open = self.incoming.get_nowait()
        except queue.Empty:
            pass
        else:
            self._notify(job_id, auto_open=auto_open)
            self.refresh()
        self.root.after(POLL_MS, self._pump)

    def _notify(self, job_id: str, auto_open: bool = True) -> None:
        job = self.pipeline.store.get(job_id)
        if job is None:
            return

        # One member at a time at a counter, so go straight to the chat rather
        # than making them click. The notification that follows reports what
        # happened and reminds them to paste.
        if (
            auto_open
            and job.status is JobStatus.READY
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

        # The CLI writes the same files this window reads. Notice that before
        # deciding what the status line says, or it reports a staleness the
        # operator has already fixed.
        try:
            self.pipeline.reload_if_changed()
        except Exception:
            log.exception("could not reload settings or templates")

        outstanding = problems(self.settings, self.pipeline.templates, self.pipeline.profile)
        counters = vm.counters_for_today(self.pipeline.store, self.settings)
        waiting = self.pipeline.store.pending_count()

        state = vm.device_state(self.settings, waiting, outstanding)
        self.mode_chip.configure(text=state.label.upper(),
                                 style=f"{state.tone.title()}Chip.TLabel")
        self.state_label.configure(text=vm.header_line(state))
        self.mode_title.configure(
            text=state.label, image=self._icon("dot", theme.DOTS[state.tone], 12, after=6))
        self.mode_detail.configure(text=state.summary)

        for key, value in (
            ("sent", counters.sent),
            ("printed", counters.printed),
            ("waiting", counters.waiting),
            ("failed", counters.failed),
        ):
            self.counter_values[key].configure(
                text=f"{value:02d}", foreground=theme.INK if value else theme.FAINT)
        self.counter_captions["sent"].configure(text=vm.sent_caption(self.settings).upper())
        self.total_sent_label.configure(text=f"Total messages sent: {counters.total_sent:,}")

        self.nav_buttons["attention"].configure(
            text=f"Needs attention ({waiting})" if waiting else "Needs attention")

        self._render_needs(outstanding)
        self._render_queue()
        self._render_checklist(outstanding)
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
