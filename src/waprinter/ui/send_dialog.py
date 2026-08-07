"""The window that opens when someone prints.

This is the whole product from the operator's point of view: they print a
receipt from their chit fund software, this appears, they type the member's
number, press Send. Everything else in the codebase exists to make this window
correct and fast.

Design constraints worth knowing before changing it:

* **Tk owns the main thread.** The spool watcher runs on a background thread and
  cannot touch widgets, so captures arrive through a queue that the main thread
  polls. See `DialogHost`.
* **The number is typed, not detected.** These documents do not print the
  customer's phone number, so there is nothing to parse. Validation as they type
  is the only guard against a typo, which is why the normalised number is echoed
  back before Send will work at all.
* **The message body is fixed by the provider.** WhatsApp requires an approved
  template for business-initiated messages, so the wording is not editable here.
  The customer name is, because it feeds a template variable, and the preview
  updates live to show exactly what will arrive.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

import pymupdf

from ..extract.phone import parse_typed_number
from ..models import PrintJob

log = logging.getLogger(__name__)

PREVIEW_WIDTH = 300      # px; page 1 thumbnail
POLL_MS = 400            # how often the main thread checks for new captures

BG = "#f4f4f5"
ACCENT = "#128c7e"       # WhatsApp green
DANGER = "#b91c1c"
MUTED = "#6b7280"


class SendDialog:
    """One window for one captured document."""

    def __init__(
        self,
        master: tk.Tk,
        job: PrintJob,
        country_code: str,
        dry_run: bool,
        on_send: Callable[[str, str], None],
        on_skip: Callable[[], None],
        fetch_status: Callable[[], dict | None] = lambda: None,
    ):
        self.job = job
        self.country_code = country_code
        self.on_send = on_send
        self.on_skip = on_skip
        self.fetch_status = fetch_status
        self._sent = False
        self._thumbnail: tk.PhotoImage | None = None  # must outlive the call
        self._qr_image: tk.PhotoImage | None = None

        self.win = tk.Toplevel(master)
        self.win.title("Send on WhatsApp")
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        # A print is an explicit user action, so taking focus is expected here
        # rather than rude.
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self._skip)

        self._build(dry_run)
        self._centre()
        self.entry.focus_force()

    # -- layout ------------------------------------------------------------

    def _build(self, dry_run: bool) -> None:
        outer = tk.Frame(self.win, bg=BG, padx=16, pady=14)
        outer.pack(fill="both", expand=True)

        if dry_run:
            banner = tk.Label(
                outer,
                text="DRY RUN — nothing will actually be sent",
                bg="#fef3c7",
                fg="#92400e",
                padx=10,
                pady=5,
            )
            banner.pack(fill="x", pady=(0, 12))

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True)

        self._build_preview(body)
        self._build_form(body)
        self._build_buttons(outer)

        # After every widget exists: _validate() gates the Send button, so it
        # cannot run while the form is still being constructed.
        self._refresh_preview()
        self._validate()
        self._poll_status()

    def _build_preview(self, parent: tk.Frame) -> None:
        left = tk.Frame(parent, bg=BG)
        left.pack(side="left", padx=(0, 16), anchor="n")

        holder = tk.Frame(left, bg="#d4d4d8", bd=0)
        holder.pack()

        image = self._render_first_page()
        if image is not None:
            self._thumbnail = image
            tk.Label(holder, image=image, bg="#d4d4d8", bd=0).pack()
        else:
            tk.Label(
                holder,
                text="(no preview)",
                bg="#d4d4d8",
                fg=MUTED,
                width=30,
                height=18,
            ).pack()

        pages = self.job.fields.page_count
        tk.Label(
            left,
            text=f"{pages} page{'s' if pages != 1 else ''}",
            bg=BG,
            fg=MUTED,
        ).pack(pady=(6, 0))

    def _render_first_page(self) -> tk.PhotoImage | None:
        """Page 1 as a thumbnail, or None if the PDF will not open."""
        try:
            with pymupdf.open(self.job.pdf_path) as doc:
                page = doc[0]
                scale = PREVIEW_WIDTH / page.rect.width
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
                # Tk 8.6+ reads PNG from a data= argument.
                return tk.PhotoImage(data=pix.tobytes("png"))
        except Exception:
            log.exception("could not render preview for %s", self.job.pdf_path)
            return None

    def _build_form(self, parent: tk.Frame) -> None:
        self.right_frame = tk.Frame(parent, bg=BG)
        self.right_frame.pack(side="left", fill="both", expand=True)

        self.form_frame = tk.Frame(self.right_frame, bg=BG)
        self.form_frame.pack(fill="both", expand=True)
        
        # We also prepare the QR labels but don't pack them yet
        self.qr_msg = tk.Label(self.right_frame, text="Scan to link WhatsApp", bg=BG, fg=DANGER, font=("Segoe UI", 11, "bold"))
        self.qr_label = tk.Label(self.right_frame, bg=BG)

        heading = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        title = (
            self.job.fields.invoice_number
            or self.job.doc_title
            or "Printed document"
        )
        tk.Label(self.form_frame, text=title, bg=BG, font=heading, anchor="w").pack(
            fill="x"
        )

        detail_bits = [
            b
            for b in (
                self.job.fields.invoice_date,
                f"₹{self.job.fields.total_amount}"
                if self.job.fields.total_amount
                else None,
            )
            if b
        ]
        if detail_bits:
            tk.Label(
                self.form_frame, text="  ·  ".join(detail_bits), bg=BG, fg=MUTED, anchor="w"
            ).pack(fill="x", pady=(0, 10))

        # --- customer name (feeds a template variable) --------------------
        tk.Label(self.form_frame, text="Customer name", bg=BG, anchor="w").pack(fill="x")
        self.name_var = tk.StringVar(value=self.job.fields.customer_name or "")
        self.name_var.trace_add("write", lambda *_: self._refresh_preview())
        ttk.Entry(self.form_frame, textvariable=self.name_var, width=34).pack(
            fill="x", pady=(2, 10)
        )

        # --- the number ---------------------------------------------------
        tk.Label(
            self.form_frame, text="WhatsApp number", bg=BG, anchor="w"
        ).pack(fill="x")
        self.number_var = tk.StringVar(value=self._initial_number())
        self.number_var.trace_add("write", lambda *_: self._validate())
        self.entry = ttk.Entry(self.form_frame, textvariable=self.number_var, width=34)
        self.entry.pack(fill="x", pady=(2, 2))
        self.entry.bind("<Return>", lambda _e: self._send())

        self.status = tk.Label(self.form_frame, text="", bg=BG, fg=MUTED, anchor="w")
        self.status.pack(fill="x", pady=(0, 10))

        # --- message ------------------------------------------------------
        tk.Label(self.form_frame, text="Message", bg=BG, anchor="w").pack(fill="x")
        self.preview = tk.Text(
            self.form_frame, height=7, width=34, wrap="word", bg="white", relief="solid",
            borderwidth=1, padx=8, pady=6,
        )
        self.preview.pack(fill="both", expand=True, pady=(2, 2))
        tk.Label(
            self.form_frame,
            text="Wording is fixed by the approved template — change it in Settings.",
            bg=BG,
            fg=MUTED,
            wraplength=250,
            justify="left",
            anchor="w",
        ).pack(fill="x")

    def _build_buttons(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(14, 0))

        self.send_button = tk.Button(
            row,
            text="Send",
            command=self._send,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT,
            activeforeground="white",
            relief="flat",
            padx=22,
            pady=6,
        )
        self.send_button.pack(side="right")

        tk.Button(
            row,
            text="Skip",
            command=self._skip,
            bg=BG,
            fg=MUTED,
            relief="flat",
            padx=14,
            pady=6,
        ).pack(side="right", padx=(0, 8))

    def _centre(self) -> None:
        self.win.update_idletasks()
        width = self.win.winfo_width()
        height = self.win.winfo_height()
        x = (self.win.winfo_screenwidth() - width) // 2
        y = (self.win.winfo_screenheight() - height) // 3
        self.win.geometry(f"+{x}+{y}")

    def _poll_status(self) -> None:
        if not self.win.winfo_exists():
            return
            
        status = self.fetch_status()
        if status and status.get("state") != "open":
            self.form_frame.pack_forget()
            self.qr_msg.configure(text="⚠ WhatsApp Not Connected\n\nPlease open the Dashboard from your Start Menu\nto scan the QR code and link your device.")
            self.qr_msg.pack(pady=(20, 20))
            self.send_button.configure(state="disabled")
        else:
            self.qr_msg.pack_forget()
            self.form_frame.pack(fill="both", expand=True)
            self._validate()
            
        self.win.after(2000, self._poll_status)

    # -- behaviour ---------------------------------------------------------

    def _initial_number(self) -> str:
        """Prefill only when the page happened to carry a number."""
        return (self.job.recipient or "").replace(f"+{self.country_code}", "")

    def _validate(self) -> str | None:
        """Echo the normalised number, and gate the Send button on it."""
        raw = self.number_var.get()
        if not any(ch.isdigit() for ch in raw):
            self.status.configure(text="Enter a 10-digit mobile number.", fg=MUTED)
            self.send_button.configure(state="disabled")
            return None

        # Separators are kept, not stripped: they are what distinguishes a
        # landline like "080-25551234" from a mobile. See parse_typed_number.
        e164 = parse_typed_number(raw, self.country_code)
        if e164 is None:
            self.status.configure(
                text="Not a mobile number WhatsApp can reach.", fg=DANGER
            )
            self.send_button.configure(state="disabled")
            return None

        self.status.configure(text=f"Will send to {e164}", fg=ACCENT)
        self.send_button.configure(state="normal")
        return e164

    def _refresh_preview(self) -> None:
        """Show the message with the current name substituted in."""
        text = self.job.message_preview or ""
        original = self.job.fields.customer_name
        typed = self.name_var.get().strip()
        if original and typed and original != typed:
            text = text.replace(original, typed)
        elif not original and typed:
            # The template rendered a placeholder because no name was found.
            text = text.replace("Hello -", f"Hello {typed}")

        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _send(self) -> None:
        e164 = self._validate()
        if e164 is None:
            return
        self._sent = True
        self.win.destroy()
        self.on_send(e164, self.name_var.get().strip())

    def _skip(self) -> None:
        if self._sent:
            return
        self.win.destroy()
        self.on_skip()


class DialogHost:
    """Owns the Tk main loop and shows a dialog per captured document.

    The watcher thread calls `submit()`; this polls for it on the main thread,
    because Tk widgets may only be touched from the thread that created them.
    """

    def __init__(self, pipeline, on_error: Callable[[str], None] | None = None):
        self.pipeline = pipeline
        self.on_error = on_error or (lambda message: log.error(message))
        self.incoming: queue.Queue[str] = queue.Queue()
        self.root: tk.Tk | None = None
        self._showing = False

    def submit(self, job_id: str) -> None:
        """Thread-safe: queue a job for the operator."""
        self.incoming.put(job_id)

    def _pump(self) -> None:
        # One dialog at a time. A batch print queues up and is worked through
        # in order rather than burying the screen in windows.
        if not self._showing:
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
        self._showing = True

        def finish() -> None:
            self._showing = False

        def send(e164: str, name: str) -> None:
            try:
                self.pipeline.release(job_id, e164, customer_name=name or None)
            except Exception as exc:
                self.on_error(f"Could not send: {exc}")
            finally:
                finish()

        def skip() -> None:
            try:
                self.pipeline.defer(job_id)
            except Exception as exc:
                self.on_error(f"Could not update the job: {exc}")
            finally:
                finish()

        def fetch_status() -> dict | None:
            if hasattr(self.pipeline.sender, "get_status"):
                return self.pipeline.sender.get_status()
            return None

        SendDialog(
            self.root,
            job,
            country_code=self.pipeline.settings.default_country_code,
            dry_run=self.pipeline.settings.dry_run,
            on_send=send,
            on_skip=skip,
            fetch_status=fetch_status,
        )

    def run(self) -> None:
        """Block on the Tk main loop. Must be called from the main thread."""
        self.root = tk.Tk()
        self.root.withdraw()  # no main window; dialogs are all the UI there is
        self.root.after(POLL_MS, self._pump)
        self.root.mainloop()

    def stop(self) -> None:
        if self.root:
            self.root.quit()
