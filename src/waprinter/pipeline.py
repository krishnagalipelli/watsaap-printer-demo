"""One captured print job, start to finish.

Deliberately synchronous and single-file: this is the path every invoice takes,
and it should be readable end to end by whoever debugs a misdirected send at
five o'clock on a Friday.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from .config import Settings, paths
from .extract import extract_fields
from .extract.profile import DocumentProfile
from .models import JobStatus, PrintJob
from .rules import Decision, evaluate
from .rules.gate import _excluded_numbers, dedupe_key
from .send.base import Sender
from .send.templates import TemplateStore, render
from .store import Store

log = logging.getLogger(__name__)


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
                excluded_numbers=_excluded_numbers(self.settings),
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
            template = self.templates.get(self.settings.default_template)
            if template is not None:
                message = render(
                    template,
                    self.settings.template_variables,
                    job.fields,
                    doc_title=job.doc_title,
                )
                job.template_name = template.name
                job.message_preview = message.preview
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
        template = self.templates.get(self.settings.default_template)
        if template is None:
            return self._hold(
                job,
                f"Template '{self.settings.default_template}' is not configured.",
            )

        message = render(
            template,
            self.settings.template_variables,
            job.fields,
            doc_title=job.doc_title,
        )
        job.template_name = template.name
        job.message_preview = message.preview

        if message.missing:
            # Not fatal — the placeholder still sends — but worth recording,
            # because a run of these means the extractor needs tuning.
            self.store.log(
                job.id, "template:missing", ", ".join(message.missing)
            )

        # --- send ---------------------------------------------------------
        job.status = JobStatus.QUEUED
        self.store.upsert(job)

        result = self.sender.send(job.recipient, pdf_path, message)
        if result.ok:
            job.status = (
                JobStatus.DRY_RUN if self.settings.dry_run else JobStatus.SENT
            )
            job.wamid = result.wamid
            job.sent_at = now
            self.store.upsert(job)
            self.store.log(job.id, "sent", f"{job.recipient} {result.wamid}")
            return job

        return self._fail(job, result.error or "Send failed", retryable=result.retryable)

    def release(
        self,
        job_id: str,
        recipient: str,
        customer_name: str | None = None,
        now: datetime | None = None,
    ) -> PrintJob:
        """Send a job to a recipient the operator supplied.

        This is the normal path, not an override: these documents carry no phone
        number, so the operator types it. Confidence is irrelevant here — a human
        read the page — but dedupe and the audit trail still apply.

        `customer_name` overrides whatever was extracted, so the message that
        goes out matches the preview the operator was looking at.
        """
        from .extract.phone import parse_typed_number

        now = now or datetime.now()
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(f"No such job: {job_id}")
        if job.status in (JobStatus.SENT, JobStatus.DRY_RUN):
            raise ValueError(f"Job {job_id} was already sent to {job.recipient}")

        # Passed through with separators intact so a landline is rejected here
        # too, not just in the dialog.
        e164 = parse_typed_number(recipient, self.settings.default_country_code)
        if e164 is None:
            raise ValueError(f"'{recipient}' is not a valid mobile number")

        if customer_name:
            job.fields.customer_name = customer_name

        job.recipient = e164
        job.hold_reason = None
        job.dedupe_key = dedupe_key(job.fields, e164)
        self.store.log(job.id, "released", f"operator chose {e164}")

        template = self.templates.get(self.settings.default_template)
        if template is None:
            return self._fail(
                job, f"Template '{self.settings.default_template}' is not configured."
            )

        message = render(
            template,
            self.settings.template_variables,
            job.fields,
            doc_title=job.doc_title,
        )
        job.template_name = template.name
        job.message_preview = message.preview
        job.status = JobStatus.QUEUED
        self.store.upsert(job)

        result = self.sender.send(e164, job.pdf_path, message)
        if result.ok:
            job.status = JobStatus.DRY_RUN if self.settings.dry_run else JobStatus.SENT
            job.wamid = result.wamid
            job.sent_at = now
            job.error = None
            self.store.upsert(job)
            self.store.log(job.id, "sent", f"{e164} {result.wamid}")
            return job

        return self._fail(job, result.error or "Send failed", retryable=result.retryable)

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
        job.error = error
        self.store.upsert(job)
        self.store.log(job.id, "failed", f"{error} (retryable={retryable})")
        return job


def build_default(settings: Settings | None = None) -> Pipeline:
    """Wire a pipeline from the on-disk configuration."""
    settings = settings or Settings.load()
    p = paths()
    p.ensure()
    store = Store(p.db)
    templates = TemplateStore(p.templates)
    profile = DocumentProfile.load(p.profile)

    sender: Sender
    if settings.dry_run:
        from .send.dryrun import DryRunSender

        sender = DryRunSender(p.logs / "dry_run.jsonl")
    else:
        from .secrets import load_token
        from .send.whatsapp import WhatsAppCloudSender

        token = load_token()
        if not token:
            raise RuntimeError(
                "No WhatsApp access token stored. Add one in Settings, or turn "
                "dry-run back on."
            )
        sender = WhatsAppCloudSender(
            phone_number_id=settings.phone_number_id,
            access_token=token,
            api_version=settings.graph_api_version,
        )

    return Pipeline(settings, store, sender, templates, profile)
