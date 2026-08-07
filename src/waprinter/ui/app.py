"""The local operator UI.

Bound to 127.0.0.1 only. It handles held jobs, shows what was sent to whom, and
edits the settings that are safe to change without a Meta round trip. There is
no authentication because there is no network exposure — binding to loopback is
the access control, and the installer does not open a firewall port.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment

from ..config import Settings, paths
from ..models import JobStatus
from ..pipeline import Pipeline
from . import templates as tpl

env = Environment(autoescape=True)


def _render(
    page: str,
    body_template: str,
    settings: Settings,
    queue_count: int,
    flash: str | None = None,
    flash_kind: str = "",
    **context,
) -> HTMLResponse:
    body = env.from_string(body_template).render(settings=settings, **context)
    html = env.from_string(tpl.BASE).render(
        title=page.capitalize(),
        page=page,
        body=body,
        dry_run=settings.dry_run,
        queue_badge=f" ({queue_count})" if queue_count else "",
        flash=flash,
        flash_kind=flash_kind,
    )
    return HTMLResponse(html)


def _back(path: str, message: str, error: bool = False) -> RedirectResponse:
    """Redirect with a flash message.

    Messages carry exception text and phone numbers, so the query string is
    always encoded — an unescaped "&" would otherwise truncate it.
    """
    params = {"msg": message}
    if error:
        params["kind"] = "err"
    return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)


@dataclass
class TodayCounts:
    printed: int = 0
    sent: int = 0
    waiting: int = 0
    failed: int = 0
    duplicate: int = 0


def _today(store) -> TodayCounts:
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = store.status_counts(midnight)
    return TodayCounts(
        printed=sum(counts.values()),
        # DRY_RUN counts as sent so the dashboard is meaningful before go-live;
        # the label changes instead of the number.
        sent=counts.get(JobStatus.SENT, 0) + counts.get(JobStatus.DRY_RUN, 0),
        waiting=counts.get(JobStatus.AWAITING, 0) + counts.get(JobStatus.HELD, 0),
        failed=counts.get(JobStatus.FAILED, 0),
        duplicate=counts.get(JobStatus.DUPLICATE, 0),
    )


def create_app(pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="WhatsApp Printer", docs_url=None, redoc_url=None)
    store = pipeline.store

    def held_count() -> int:
        return len(store.pending())

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return _render(
            "dashboard",
            tpl.DASHBOARD,
            pipeline.settings,
            held_count(),
            today=_today(store),
            recent=[j for j in store.recent(15) if j.created_at >= midnight],
            # Flipped on once the webhook relay lands.
            delivery_available=False,
        )

    @app.get("/wa-status")
    def wa_status():
        from fastapi.responses import JSONResponse
        try:
            if hasattr(pipeline, "sender") and hasattr(pipeline.sender, "get_status"):
                return JSONResponse(pipeline.sender.get_status())
            return JSONResponse({"state": "unknown", "error": "no baileys sender"})
        except Exception as exc:
            return JSONResponse({"state": "error", "error": str(exc)})

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request):
        jobs = store.pending()
        return _render(
            "queue",
            tpl.QUEUE,
            pipeline.settings,
            len(jobs),
            flash=request.query_params.get("msg"),
            flash_kind=request.query_params.get("kind", ""),
            jobs=jobs,
        )

    @app.get("/history", response_class=HTMLResponse)
    def history():
        return _render(
            "history",
            tpl.HISTORY,
            pipeline.settings,
            held_count(),
            jobs=store.recent(100),
        )

    @app.get("/jobs/{job_id}/pdf")
    def job_pdf(job_id: str):
        job = store.get(job_id)
        if job is None or not job.pdf_path.exists():
            return _back("/queue","That PDF is no longer on disk.", error=True)
        return FileResponse(job.pdf_path, media_type="application/pdf")

    @app.post("/jobs/{job_id}/send")
    def send(job_id: str, recipient: str = Form(...)):
        try:
            job = pipeline.release(job_id, recipient)
        except (KeyError, ValueError) as exc:
            return _back("/queue",str(exc), error=True)

        if job.status is JobStatus.FAILED:
            return _back("/queue",job.error or "Send failed", error=True)
        return _back("/queue",f"Sent to {job.recipient}")

    @app.post("/jobs/{job_id}/discard")
    def discard(job_id: str):
        try:
            pipeline.discard(job_id)
        except KeyError as exc:
            return _back("/queue",str(exc), error=True)
        return _back("/queue","Discarded")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        from ..extract.ocr import available as ocr_available

        return _render(
            "settings",
            tpl.SETTINGS,
            pipeline.settings,
            held_count(),
            flash=request.query_params.get("msg"),
            flash_kind=request.query_params.get("kind", ""),
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
                    "/settings", f"{name} must be a whole number.", error=True
                )

        was_dry = s.dry_run
        s.dry_run = dry_run is not None
        s.ocr_enabled = ocr_enabled is not None
        s.ocr_silent_send = ocr_silent_send is not None
        s.save()

        if was_dry and not s.dry_run:
            note = "Dry run is OFF — printing now sends real messages."
        elif not was_dry and s.dry_run:
            note = "Dry run is ON — nothing will be sent."
        else:
            note = "Saved."
        return _back("/settings", note)

    return app


def serve(pipeline: Pipeline | None = None, port: int | None = None) -> None:
    """Run the UI in the foreground."""
    import uvicorn

    from ..pipeline import build_default

    pipeline = pipeline or build_default()
    port = port or pipeline.settings.ui_port
    paths().ensure()
    uvicorn.run(create_app(pipeline), host="127.0.0.1", port=port, log_level="warning")
