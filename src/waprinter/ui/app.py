"""The local control panel.

Bound to 127.0.0.1 only. No authentication, because there is no network
exposure — loopback is the access control and the installer opens no firewall
port.

Everything an operator sees goes through `label_of`: job statuses are internal
enum names (`dry_run`, `awaiting`) and must never reach the screen as-is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..config import Settings, paths
from ..models import JobStatus, PrintJob
from ..pipeline import Pipeline
from . import templates as tpl

env = Environment(autoescape=True)

# How each internal status is described to a person, and how it is coloured.
STATUS_LABELS: dict[JobStatus, tuple[str, str]] = {
    JobStatus.SENT: ("Sent", "ok"),
    JobStatus.DRY_RUN: ("Test only", "warn"),
    JobStatus.AWAITING: ("Needs a number", "warn"),
    JobStatus.HELD: ("Waiting", "warn"),
    JobStatus.DUPLICATE: ("Reprint ignored", ""),
    JobStatus.FAILED: ("Failed", "bad"),
    JobStatus.QUEUED: ("Sending", ""),
    JobStatus.CAPTURED: ("Reading", ""),
    JobStatus.DISCARDED: ("Discarded", ""),
}


def label_of(job: PrintJob) -> tuple[str, str]:
    return STATUS_LABELS.get(job.status, (str(job.status), ""))


@dataclass
class DeviceState:
    """The status line at the top, like a printer's own ready/offline state."""

    icon: str
    state: str
    tone: str


def device_state(settings: Settings, waiting: int, templates=None) -> DeviceState:
    if settings.dry_run:
        return DeviceState("🖨", "Test mode — documents are read but nothing is sent", "warn")
    from ..send.readiness import problems

    outstanding = problems(settings, templates)
    if outstanding:
        return DeviceState("🖨", f"Not ready — {outstanding[0]}", "bad")
    if waiting:
        return DeviceState("🖨", f"Ready — {waiting} document(s) need attention", "warn")
    return DeviceState("🖨", "Ready", "ok")


@dataclass
class TodayCounts:
    printed: int = 0
    sent: int = 0
    waiting: int = 0
    failed: int = 0


def _today(store) -> TodayCounts:
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = store.status_counts(midnight)
    return TodayCounts(
        printed=sum(counts.values()),
        # A test send counts here so the panel is meaningful before go-live;
        # the label changes rather than the number.
        sent=counts.get(JobStatus.SENT, 0) + counts.get(JobStatus.DRY_RUN, 0),
        waiting=counts.get(JobStatus.AWAITING, 0) + counts.get(JobStatus.HELD, 0),
        failed=counts.get(JobStatus.FAILED, 0),
    )


def _back(path: str, message: str, error: bool = False) -> RedirectResponse:
    """Redirect with a flash message.

    Always encoded: messages carry exception text and phone numbers, and an
    unescaped "&" would truncate the query string.
    """
    params = {"msg": message}
    if error:
        params["kind"] = "err"
    return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)


def create_app(pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="WhatsApp Printer", docs_url=None, redoc_url=None)
    store = pipeline.store

    def waiting_count() -> int:
        return len(store.pending())

    def render(page: str, body: str, request: Request | None = None, **context):
        waiting = waiting_count()
        inner = env.from_string(body).render(settings=pipeline.settings, **context)
        html = env.from_string(tpl.BASE).render(
            title=page.capitalize(),
            page=page,
            body=inner,
            attention=waiting,
            device=device_state(pipeline.settings, waiting, pipeline.templates),
            flash=request.query_params.get("msg") if request else None,
            flash_kind=request.query_params.get("kind", "") if request else "",
        )
        return HTMLResponse(html)

    @app.get("/", response_class=HTMLResponse)
    def status(request: Request):
        from ..send.readiness import problems

        return render(
            "status",
            tpl.STATUS,
            request,
            today=_today(store),
            problems=problems(pipeline.settings, pipeline.templates),
        )

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request):
        return render("queue", tpl.QUEUE, request, jobs=store.pending())

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        jobs = []
        for job in store.recent(60):
            job.label, job.tone = label_of(job)
            jobs.append(job)
        return render("history", tpl.HISTORY, request, jobs=jobs)

    @app.get("/jobs/{job_id}/pdf")
    def job_pdf(job_id: str):
        job = store.get(job_id)
        if job is None or not job.pdf_path.exists():
            return _back("/queue", "That PDF is no longer on disk.", error=True)
        return FileResponse(job.pdf_path, media_type="application/pdf")

    @app.post("/jobs/{job_id}/send")
    def send(job_id: str, recipient: str = Form(...)):
        try:
            job = pipeline.release(job_id, recipient)
        except (KeyError, ValueError) as exc:
            return _back("/queue", str(exc), error=True)
        if job.status is JobStatus.FAILED:
            return _back("/queue", job.error or "Send failed", error=True)
        return _back("/queue", f"Sent to {job.recipient}")

    @app.post("/jobs/{job_id}/discard")
    def discard(job_id: str):
        try:
            pipeline.discard(job_id)
        except KeyError as exc:
            return _back("/queue", str(exc), error=True)
        return _back("/queue", "Discarded")

    @app.post("/test-send")
    def test_send():
        """The equivalent of a printer's "Print Test Page".

        Reports what is missing rather than attempting a send that cannot work,
        because an unconfigured account fails in ways Meta's error codes do not
        explain well.
        """
        from ..send.readiness import problems

        outstanding = problems(pipeline.settings, pipeline.templates)
        if outstanding:
            return _back("/", "Cannot send yet: " + outstanding[0], error=True)
        if pipeline.settings.dry_run:
            return _back(
                "/", "Test mode is on, so nothing was sent. Everything else is "
                     "configured correctly."
            )
        return _back(
            "/", "Configuration looks complete. Print a document to send one."
        )

    @app.get("/note/{job_id}", response_class=HTMLResponse)
    def note(job_id: str):
        """The after-print notification, rendered into its own small window."""
        from .result import describe, needs_action

        job = store.get(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
        tone, headline, detail = describe(job)
        accent, glyph = {
            "ok": ("#0f7b43", "\u2713"),
            "bad": ("#b3261e", "!"),
            "wait": ("#a35a00", "i"),
        }[tone]
        return HTMLResponse(
            env.from_string(tpl.NOTE).render(
                accent=accent,
                glyph=glyph,
                headline=headline,
                detail=detail,
                actionable=needs_action(job),
            )
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        from ..extract.ocr import available as ocr_available

        return render(
            "settings",
            tpl.SETTINGS,
            request,
            template=pipeline.templates.get(pipeline.settings.default_template),
            ocr_available=ocr_available(pipeline.settings.ocr()),
        )

    @app.post("/settings")
    def save_settings(
        own_numbers: str = Form(""),
        phone_number_id: str = Form(""),
        default_template: str = Form(""),
        dedupe_window_hours: str = Form("24"),
        max_sends_per_minute: str = Form("10"),
        dry_run: str | None = Form(None),
        confirm_before_send: str | None = Form(None),
        ocr_enabled: str | None = Form(None),
        ocr_silent_send: str | None = Form(None),
    ):
        s = pipeline.settings
        s.own_numbers = [n.strip() for n in own_numbers.split(",") if n.strip()]
        s.phone_number_id = phone_number_id.strip()
        s.default_template = default_template.strip() or s.default_template

        for name, raw in (
            ("dedupe_window_hours", dedupe_window_hours),
            ("max_sends_per_minute", max_sends_per_minute),
        ):
            try:
                setattr(s, name, int(raw))
            except ValueError:
                return _back(
                    "/settings", f"{name.replace('_', ' ')} must be a whole number.",
                    error=True,
                )

        was_dry = s.dry_run
        s.dry_run = dry_run is not None
        s.confirm_before_send = confirm_before_send is not None
        s.ocr_enabled = ocr_enabled is not None
        s.ocr_silent_send = ocr_silent_send is not None
        s.save()

        if was_dry and not s.dry_run:
            note = "Test mode is OFF — printing now sends real messages."
        elif not was_dry and s.dry_run:
            note = "Test mode is ON — nothing will be sent."
        else:
            note = "Saved."
        return _back("/settings", note)

    return app


def serve(pipeline: Pipeline | None = None, port: int | None = None) -> None:
    """Run the control panel in the foreground."""
    import uvicorn

    from ..pipeline import build_default

    pipeline = pipeline or build_default()
    port = port or pipeline.settings.ui_port
    paths().ensure()
    uvicorn.run(create_app(pipeline), host="127.0.0.1", port=port, log_level="warning")
