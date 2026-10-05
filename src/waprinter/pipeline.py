"""One captured print job, start to finish.

Deliberately synchronous and single-file: this is the path every invoice takes,
and it should be readable end to end by whoever debugs a misdirected send at
five o'clock on a Friday.
"""

from __future__ import annotations

import hashlib
import logging
import functools
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .archive import file_away
from .config import Settings, paths
from .extract import extract_fields
from .extract.pdf_text import read as read_pdf
from .extract.profile import DocumentProfile
from .extract.split import receipt_groups, segment_path, write_segment
from .models import JobStatus, PrintJob
from .rules import Decision, evaluate
from .rules.gate import dedupe_key, excluded_numbers
from .send.base import Sender
from .send.templates import RenderedMessage, TemplateStore, render
from .store import Store

log = logging.getLogger(__name__)


def _processing(method):
    """One document operation at a time, also excluding installation."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.operation_lock:
            return method(self, *args, **kwargs)
    return wrapped


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sender: Sender,
        templates: TemplateStore,
        profile: DocumentProfile | None = None,
    ):
        self.settings = settings
        self.store = store
        self.sender = sender
        self.templates = templates
        # What this client's paperwork calls things. Defaults cover every layout
        # seen so far; a profile.json overrides key by key.
        self.profile = profile or DocumentProfile()
        # How many sends are with the sender right now, on any thread. The
        # agent asks before installing an update: a queue send runs on a
        # worker thread the watcher's own busy flag knows nothing about.
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        # Shared by capture, queue sends, settings commits and the updater.
        # Reentrant because process_document() calls process() for each part.
        self.operation_lock = threading.RLock()
        self.update_lock = threading.Lock()
        # What the shared files looked like when we last read them.
        self._stamps: dict[str, bytes | None] = {
            "settings": self._stamp(paths().settings),
            "templates": self._stamp(self.templates.path),
            "profile": self._stamp(paths().profile),
        }

    @property
    def busy(self) -> bool:
        """Whether any send is in progress."""
        with self._in_flight_lock:
            return self._in_flight > 0

    def reload_if_changed(self) -> bool:
        # Refresh runs on the UI thread; do not freeze it behind a network send.
        if not self.operation_lock.acquire(blocking=False):
            return False
        try:
            return self._reload_if_changed()
        finally:
            self.operation_lock.release()

    def _reload_if_changed(self) -> bool:
        """Pick up settings and templates written by another process.

        The CLI and the window are two processes over the same two files.
        `waprinter go-live` and `waprinter templates --sync` both write, and
        the agent used to keep whatever it read at startup -- so the operator
        ran the command, watched it succeed, printed, and the agent went on
        refusing to send using a status it had cached hours earlier. Restarting
        was the only cure, and nothing said so.

        Settings are updated in place rather than replaced, because the window
        holds a reference to the same object.
        """
        settings_path = paths().settings
        templates_path = self.templates.path
        changed = False

        if self._stamp(settings_path) != self._stamps.get("settings"):
            self._stamps["settings"] = self._stamp(settings_path)
            try:
                fresh = Settings.load()
            except Exception:
                log.exception("could not re-read settings.json")
                return changed
            before = (
                self.settings.dry_run,
                self.settings.phone_number_id,
                self.settings.graph_api_version,
                self.settings.force_ipv4,
            )
            for name in fresh.__dataclass_fields__:
                setattr(self.settings, name, getattr(fresh, name))
            after = (
                self.settings.dry_run,
                self.settings.phone_number_id,
                self.settings.graph_api_version,
                self.settings.force_ipv4,
            )
            changed = True
            if before != after:
                try:
                    self.rebuild_sender()
                    log.info("settings changed on disk; sender re-wired")
                except Exception as exc:
                    from .send.unavailable import UnavailableSender

                    # Never keep a test sender after importing live settings.
                    self.sender = UnavailableSender(str(exc))
                    log.exception("could not re-wire the sender after a reload")

        if self._stamp(templates_path) != self._stamps.get("templates"):
            self._stamps["templates"] = self._stamp(templates_path)
            try:
                self.templates.load()
                log.info("templates changed on disk; reloaded")
                changed = True
            except Exception:
                log.exception("could not re-read templates.json")

        if self.reload_profile():
            changed = True

        return changed

    def reload_profile(self, force: bool = False) -> bool:
        """Pick up document types and fields taught since the last read.

        Replaced rather than updated in place: extraction reads
        `self.profile` once per document, so a print already under way keeps
        the profile it started with.
        """
        profile_path = paths().profile
        stamp = self._stamp(profile_path)
        if not force and stamp == self._stamps.get("profile"):
            return False
        self._stamps["profile"] = stamp
        try:
            self.profile = DocumentProfile.load(profile_path)
        except Exception:
            log.exception("could not re-read profile.json")
            return False
        log.info("document profile changed on disk; reloaded")
        return True

    @staticmethod
    def _stamp(path: Path) -> bytes | None:
        """What the file holds, not when it was last written.

        An mtime is not enough on Windows, where file timestamps advance on a
        ~15.6 ms tick: a write landing in the same tick as our last read is
        invisible, and the CLI's change then goes unnoticed until something
        else happens to touch the file. These are small JSON files read on a
        poll, so digesting the contents costs nothing worth saving.
        """
        try:
            return hashlib.blake2b(path.read_bytes(), digest_size=16).digest()
        except OSError:
            return None

    def rebuild_sender(self) -> None:
        """Re-choose the sender from the current settings.

        The sender used to be chosen once, at startup. Turning test mode off in
        the settings page then changed what a job was *recorded* as without
        changing what actually happened: the dry-run sender stayed wired in, so
        the job was written down as SENT while nothing left the machine and
        nothing reached Meta. Anything that saves settings must call this.
        """
        self.sender = build_sender(self.settings)

    def apply_settings(self, settings: Settings) -> None:
        """Commit validated settings and their sender together, while idle."""
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError("A document or update is in progress. Apply again when it finishes.")
        try:
            sender = build_sender(settings)
            settings.save()
            for name in settings.__dataclass_fields__:
                setattr(self.settings, name, getattr(settings, name))
            self.sender = sender
            self._stamps["settings"] = self._stamp(paths().settings)
        finally:
            self.operation_lock.release()

    def fields_for_kind(self, pdf_path, document_kind):
        from .extract import extract_fields
        selected = next((k for k in self.profile.all_kinds if k.name == document_kind), None)
        if selected is None:
            raise ValueError("Choose a known document type before sending.")
        fields = extract_fields(pdf_path, profile=self.profile.for_kind(selected),
                                ocr=self.settings.ocr(),
                                country_code=self.settings.default_country_code,
                                forced_kind=selected)
        if not fields.readable:
            raise ValueError(fields.ocr_error or "Document could not be read.")
        fields.document_kind_verified = True
        fields.classification_error = ""
        return fields

    def template_for(self, fields) -> tuple[str, object | None]:
        """The approved template this document goes out under, and its name.

        A removal notice is not a receipt and must not borrow its wording, so
        the kind read off the page picks the template. Recognised non-receipt
        kinds require a mapping; unrecognised documents keep default_template.
        """
        kind = fields.document_kind
        if fields.classification_error or (kind is None and self.settings.document_templates):
            return "Choose the document type before selecting a message", None
        name = self.settings.document_templates.get(kind or "")
        if not name and kind not in (None, "", "receipt"):
            # A recognised notice must never inherit receipt wording just
            # because an older installation has an empty mapping.
            return f"{kind.replace('_', ' ').capitalize()} — choose a message in Messages", None
        name = name or self.settings.default_template
        return name, self.templates.get(name)

    def noun_for(self, fields) -> str:
        """What to call the attached PDF for this kind of document."""
        return (
            self.settings.document_nouns.get(fields.document_kind or "")
            or self.settings.document_noun
        )

    def compose(self, job: PrintJob) -> tuple[str, RenderedMessage | None]:
        """The message this job goes out as, and the template name wanted.

        The one place the template is rendered. It used to be rendered in
        four places with slightly different arguments, and the copy behind
        the confirmation dialog left out the business name -- so the operator
        checked a preview reading "Thank you for your payment to -." and the
        member received a different message.

        Returns (name, None) when the template is not configured.
        """
        wanted, template = self.template_for(job.fields)
        if template is None:
            return wanted, None
        message = render(
            template,
            self.variable_map_for(template),
            job.fields,
            doc_title=job.doc_title,
            extra={"business_name": self.settings.business_name},
            document_noun=self.noun_for(job.fields),
        )
        job.template_name = template.name
        job.message_preview = message.preview
        return wanted, message

    def variable_map_for(self, template) -> dict[str, str]:
        """How this template's variables are filled.

        Its own mapping from the setup screen when it has one, otherwise the
        shared template_variables -- as far as they fit it. The shared
        numbered entries were written for one template's body and used to be
        applied to every numbered template, which sent invoice_document as
        "Your invoice <business name> for ₹<invoice number>".
        """
        from .send.templates import variable_map

        return variable_map(template, self.settings)

    def unfilled_reason(self, template) -> str:
        """Why this template cannot be filled on any print, or "".

        Checked before anything is sent or handed to WhatsApp. A variable
        with no source is empty on every print, so the choice is between
        holding the document and sending "-" -- or, before the shared map
        was narrowed, sending another field's value in its place.
        """
        from .extract.rules import known_fields
        from .send.templates import unfilled_reason, unfilled_variables

        unfilled = unfilled_variables(
            template, self.variable_map_for(template), known_fields(self.profile)
        )
        return unfilled_reason(template, unfilled) if unfilled else ""

    def missing_required(self, message: RenderedMessage) -> list[str]:
        """Mapped values this print did not supply, for templates set up on
        the mapping screen. Empty for everything else.

        A template someone mapped by hand says which values belong in the
        message. Sending "Member ID: -" to a member is the silent failure the
        mapping exists to prevent, so the document waits for a person
        instead. Templates with no mapping keep the old behaviour.
        """
        if message.template.name not in self.settings.template_mappings:
            return []
        from .extract.rules import display_name

        names = []
        for source in message.missing:
            if source.startswith("{{"):
                names.append(source)
            else:
                names.append(display_name(source).lower())
        return names

    @_processing
    def process_document(
        self,
        pdf_path: Path,
        doc_title: str | None = None,
        windows_user: str | None = None,
        now: datetime | None = None,
    ) -> list[PrintJob]:
        """Take one captured PDF all the way through, as one job per receipt.

        This is the entry point for anything that captures a print. A run of
        receipts printed as a single job becomes one job per subscriber, each
        carrying only its own pages, because the send path uploads the job's
        PDF whole and the alternative is posting one subscriber the rest of
        the counter's afternoon.

        A document holding a single receipt -- which is nearly all of them --
        takes the same path it always did, against the original file.
        """
        try:
            groups = self._receipt_groups(pdf_path)
        except Exception:
            # Splitting is an optimisation on top of a working single-job
            # path, and must never be the reason a print is lost.
            log.exception("could not split %s; processing it whole", pdf_path)
            groups = []

        parts = self._write_segments(pdf_path, groups) if len(groups) >= 2 else []
        if not parts:
            jobs = [self.process(pdf_path, doc_title, windows_user, now)]
        else:
            log.info("%s holds %d receipts; splitting", pdf_path, len(parts))
            jobs = [
                self.process(part, doc_title, windows_user, now) for part in parts
            ]

        # The office keeps its own copy of everything that was printed,
        # whatever the gate decided about sending it. Filed here rather than
        # inside process() so it happens once per receipt, on every outcome,
        # and cannot interfere with the decision itself.
        for job in jobs:
            filed = file_away(job, self.settings)
            if filed is not None:
                self.store.log(job.id, "filed", str(filed))

        return jobs

    @staticmethod
    def _write_segments(pdf_path: Path, groups: list[list[int]]) -> list[Path]:
        """Every receipt of a batch as its own file, or nothing at all.

        All or nothing on purpose. A segment that failed to write used to be
        skipped with a log line, and the subscriber on those pages never got
        a job -- their receipt was the one print in the run that vanished. If
        any part cannot be written, the whole document is processed as one
        job instead: it is held, because it carries several numbers, and a
        held batch is a nuisance where a dropped receipt is a loss.
        """
        written: list[Path] = []
        for index, pages in enumerate(groups, start=1):
            try:
                written.append(
                    write_segment(pdf_path, pages, segment_path(pdf_path, index))
                )
            except Exception:
                log.exception(
                    "could not write receipt %d of %s; processing it whole",
                    index,
                    pdf_path,
                )
                for part in written:
                    try:
                        part.unlink()
                    except OSError:
                        pass
                return []
        return written

    def _receipt_groups(self, pdf_path: Path) -> list[list[int]]:
        """Page groups, one per receipt. Empty when the question is moot.

        Read without OCR on purpose. A scanned batch would have to be rendered
        twice over -- once to find the boundaries and again to read the pages
        -- and a scan is held for a person anyway, so the cost buys nothing.
        """
        doc = read_pdf(pdf_path)
        if not doc.has_text_layer or doc.page_count < 2:
            return []
        return receipt_groups(doc, self.profile)

    @_processing
    def process(
        self,
        pdf_path: Path,
        doc_title: str | None = None,
        windows_user: str | None = None,
        now: datetime | None = None,
    ) -> PrintJob:
        """Take a captured PDF all the way to sent, held, or failed."""
        now = now or datetime.now()
        job = PrintJob(
            id=uuid.uuid4().hex[:16],
            created_at=now,
            pdf_path=pdf_path,
            doc_title=doc_title,
            windows_user=windows_user,
        )
        # Persist before doing anything that can fail, so a crash still leaves
        # a record that a job existed.
        self.store.upsert(job)
        self.store.log(job.id, "captured", str(pdf_path))

        # --- extract ------------------------------------------------------
        try:
            job.fields = extract_fields(
                pdf_path,
                excluded_numbers=excluded_numbers(self.settings),
                country_code=self.settings.default_country_code,
                ocr=self.settings.ocr(),
                profile=self.profile,
            )
        except Exception as exc:  # a malformed PDF must not stop the service
            log.exception("extraction failed for %s", pdf_path)
            return self._fail(job, f"Could not read the PDF: {exc}")

        self.store.log(
            job.id,
            "extracted",
            f"{len(job.fields.candidates)} candidate(s), "
            f"invoice={job.fields.invoice_number}",
        )

        # --- gate ---------------------------------------------------------
        outcome = evaluate(job, self.settings, self.store, now=now)
        job.recipient = outcome.recipient
        job.confidence = outcome.confidence
        job.dedupe_key = outcome.dedupe_key
        self.store.log(job.id, f"gate:{outcome.decision}", outcome.reason)

        if outcome.decision is Decision.CONFIRM:
            # The operator supplies the number. Rendering the message preview
            # here means the dialog can show the exact text without repeating
            # any of this work.
            _wanted, message = self.compose(job)
            unfilled = self.unfilled_reason(message.template) if message else ""
            if unfilled:
                # Asking for a number would be asking for nothing: the
                # message cannot go out until setup fills it in.
                return self._hold(job, unfilled)
            job.status = JobStatus.AWAITING
            job.hold_reason = outcome.reason
            self.store.upsert(job)
            return job

        if outcome.decision is Decision.HOLD:
            return self._hold(job, outcome.reason)
        if outcome.decision is Decision.DUPLICATE:
            job.status = JobStatus.DUPLICATE
            job.hold_reason = outcome.reason
            self.store.upsert(job)
            return job

        # --- compose ------------------------------------------------------
        wanted, message = self.compose(job)
        if message is None:
            return self._hold(job, f"Template '{wanted}' is not configured.")
        unfilled = self.unfilled_reason(message.template)
        if unfilled:
            return self._hold(job, unfilled)
        required = self.missing_required(message)
        if required:
            return self._hold(
                job,
                f"Could not read {', '.join(required)} from this print, and the "
                f"'{message.template.name}' message needs it. Check the PDF; "
                f"sending it from here fills the gap with \"-\".",
            )

        # --- link mode ----------------------------------------------------
        if self.settings.send_mode == "link":
            return self._prepare_link(job, message)

        if message.missing:
            # Not fatal — the placeholder still sends — but worth recording,
            # because a run of these means the extractor needs tuning.
            self.store.log(
                job.id, "template:missing", ", ".join(message.missing)
            )

        # --- send ---------------------------------------------------------
        return self._send(job, message, now)

    def _send(self, job: PrintJob, message: RenderedMessage, now: datetime) -> PrintJob:
        """Hand one composed job to the sender and record what came back.

        The sender is wrapped, because an exception out of it used to leave
        the job at QUEUED for ever: that status is shown nowhere -- not in the
        queue, not counted as waiting -- so a receipt whose upload raised on
        an unreadable file or an unexpected response simply disappeared.
        """
        job.status = JobStatus.QUEUED
        self.store.upsert(job)

        with self._in_flight_lock:
            self._in_flight += 1
        try:
            result = self.sender.send(job.recipient, job.pdf_path, message)
        except Exception as exc:
            log.exception("sender raised for job %s", job.id)
            return self._fail(
                job,
                f"Sending failed unexpectedly ({type(exc).__name__}: {exc}). "
                f"Check WhatsApp before sending it again.",
                retryable=False,
            )
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

        if result.ok:
            job.status = JobStatus.DRY_RUN if self.settings.dry_run else JobStatus.SENT
            job.wamid = result.wamid
            job.sent_at = now
            job.error = None
            self.store.upsert(job)
            self.store.log(job.id, "sent", f"{job.recipient} {result.wamid}")
            return job

        return self._fail(job, result.error or "Send failed", retryable=result.retryable)

    def _prepare_link(self, job: PrintJob, message) -> PrintJob:
        """Compose a click-to-chat link and wait for the operator to open it.

        Nothing is sent here and nothing can be: a wa.me link opens WhatsApp
        with the text prefilled and a person presses send. The job sits at READY
        until they do, and is only ever recorded as handed over.
        """
        from .send.link import chat_url

        try:
            job.chat_url = chat_url(job.recipient, message.preview)
        except ValueError as exc:
            return self._hold(job, str(exc))
        job.status = JobStatus.READY
        job.hold_reason = "Ready to send. Open WhatsApp to review and send it."
        self.store.upsert(job)
        self.store.log(job.id, "link:ready", job.recipient)
        return job

    def hand_off(self, job_id: str, now: datetime | None = None) -> PrintJob:
        """Record that the operator opened the chat. Link mode only.

        Deliberately not SENT: the app cannot observe whether they pressed send
        in WhatsApp, and claiming otherwise would make the history a lie.
        """
        now = now or datetime.now()
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(f"No such job: {job_id}")
        if not job.chat_url:
            raise ValueError("This document has no WhatsApp link.")
        job.status = JobStatus.HANDED_OFF
        job.hold_reason = None
        job.sent_at = now
        self.store.upsert(job)
        self.store.log(job.id, "link:opened", job.recipient)
        return job

    @_processing
    def release(
        self,
        job_id: str,
        recipient: str,
        customer_name: str | None = None,
        now: datetime | None = None,
        document_kind: str | None = None,
    ) -> PrintJob:
        """Send a job to a recipient the operator supplied.

        This is the normal path, not an override: these documents carry no phone
        number, so the operator types it. Confidence is irrelevant here — a human
        read the page — but dedupe and the audit trail still apply.

        `customer_name` overrides whatever was extracted, so the message that
        goes out matches the preview the operator was looking at.

        A reprint is caught here as well as at the gate. The gate only sees
        the recipient it found itself, and in confirmation mode it finds none,
        so the same receipt released twice to the same number used to go out
        twice. It comes back as DUPLICATE, exactly as an automatic send would.
        """
        from .extract.phone import parse_typed_number

        now = now or datetime.now()
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(f"No such job: {job_id}")
        if job.status == JobStatus.SENT or (job.status == JobStatus.DRY_RUN and self.settings.dry_run):
            raise ValueError(f"Job {job_id} was already sent to {job.recipient}")

        # Passed through with separators intact so a landline is rejected here
        # too, not just in the dialog.
        e164 = parse_typed_number(recipient, self.settings.default_country_code)
        if e164 is None:
            raise ValueError(f"'{recipient}' is not a valid mobile number")

        if document_kind is not None:
            job.fields = self.fields_for_kind(job.pdf_path, document_kind)
        if job.fields.classification_error or (
            job.fields.document_kind is None and self.settings.document_templates
        ):
            raise ValueError("Choose the document type before sending this document.")

        # Refused before anything about the job changes. Typing a number
        # cannot fill a variable the message has no source for, and the
        # queue shows this reason where the operator can read it.
        _wanted, template = self.template_for(job.fields)
        if template is not None:
            unfilled = self.unfilled_reason(template)
            if unfilled:
                raise ValueError(unfilled)

        if customer_name:
            job.fields.customer_name = customer_name

        job.recipient = e164
        job.hold_reason = None
        job.dedupe_key = dedupe_key(job.fields, e164)
        self.store.log(job.id, "released", f"operator chose {e164}; document type {job.fields.document_kind or 'default'}")

        prior = self.store.find_duplicate(
            job.dedupe_key, self.settings.dedupe_window_hours, now=now,
            dry_run=self.settings.dry_run, send_mode=self.settings.send_mode,
        )
        if prior is not None and prior.id != job.id:
            job.status = JobStatus.DUPLICATE
            job.hold_reason = (
                f"Already sent to {e164} at {prior.sent_at:%d %b %H:%M}. "
                f"Reprint suppressed."
            )
            self.store.upsert(job)
            self.store.log(job.id, "gate:duplicate", job.hold_reason)
            return job

        wanted, message = self.compose(job)
        if message is None:
            return self._hold(job, f"Template '{wanted}' is not configured.")

        if self.settings.send_mode == "link":
            return self._prepare_link(job, message)

        return self._send(job, message, now)

    def recover_interrupted(self) -> list[PrintJob]:
        """Put back on the queue whatever was mid-send when we last stopped.

        Called once, by the process that owns the spool folder, at startup.
        Not from the CLI: a CLI run beside a live agent would see the agent's
        current send as interrupted and pull it out from under it.

        Held rather than failed. Whether the message reached Meta is
        unknown, and that is a question for a person with WhatsApp open, not
        a retry loop.
        """
        recovered = []
        for job in self.store.interrupted():
            job.status = JobStatus.HELD
            job.hold_reason = (
                "The app stopped while this was being sent, so it may or may "
                "not have arrived. Check WhatsApp before sending it again."
            )
            self.store.upsert(job)
            self.store.log(job.id, "recovered", "interrupted send")
            recovered.append(job)
        if recovered:
            log.warning("%d send(s) were interrupted; held for review", len(recovered))
        return recovered

    def defer(self, job_id: str) -> PrintJob:
        """Operator closed the dialog without sending. Keep it in the queue."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(f"No such job: {job_id}")
        job.status = JobStatus.HELD
        job.hold_reason = (
            "Skipped at the print dialog. Send it from here when ready."
        )
        self.store.upsert(job)
        self.store.log(job.id, "deferred")
        return job

    def discard(self, job_id: str) -> PrintJob:
        """Throw a held job away without sending it."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(f"No such job: {job_id}")
        job.status = JobStatus.DISCARDED
        self.store.upsert(job)
        self.store.log(job.id, "discarded")
        return job

    # -- terminal states ---------------------------------------------------

    def _hold(self, job: PrintJob, reason: str) -> PrintJob:
        job.status = JobStatus.HELD
        job.hold_reason = reason
        self.store.upsert(job)
        return job

    def _fail(self, job: PrintJob, error: str, retryable: bool = False) -> PrintJob:
        job.status = JobStatus.FAILED
        job.hold_reason = None
        job.error = error
        self.store.upsert(job)
        self.store.log(job.id, "failed", f"{error} (retryable={retryable})")
        return job


def build_sender(settings: Settings) -> Sender:
    """Choose the sender these settings ask for.

    Split out of build_default so it can be called again when the settings
    change. Which sender is wired in decides whether a message actually leaves
    the machine, and that decision must not outlive the setting it was made
    from — see Pipeline.rebuild_sender.
    """
    if settings.dry_run:
        from .send.dryrun import DryRunSender

        return DryRunSender(paths().logs / "dry_run.jsonl")

    from .secrets import load_token
    from .send.whatsapp import WhatsAppCloudSender

    token = load_token()
    if not token:
        raise RuntimeError(
            "No WhatsApp access token stored. Connect WhatsApp in Setup, or turn "
            "dry-run back on."
        )
    return WhatsAppCloudSender(
        phone_number_id=settings.phone_number_id,
        access_token=token,
        api_version=settings.graph_api_version,
        force_ipv4=settings.force_ipv4,
    )


def build_default(settings: Settings | None = None) -> Pipeline:
    """Wire a pipeline from the on-disk configuration."""
    settings = settings or Settings.load()
    p = paths()
    p.ensure()
    store = Store(p.db)
    templates = TemplateStore(
        p.templates, settings.business_name, settings.template_language
    )
    profile = DocumentProfile.load(p.profile)

    try:
        sender = build_sender(settings)
    except Exception as exc:
        # Test mode is off and the real sender cannot be built -- no token,
        # usually. Raising here stopped the agent starting at all, with a
        # crash box at every logon and no prints captured. Start anyway:
        # every send fails with this reason, the status line reports it, and
        # rebuild_sender() puts the real one in once it is fixed.
        from .send.unavailable import UnavailableSender

        log.error("cannot send until this is fixed: %s", exc)
        sender = UnavailableSender(str(exc))

    return Pipeline(settings, store, sender, templates, profile)
