"""The desktop agent: one process, everything the operator needs.

There is no Windows service, for a concrete reason: a service runs in session 0
with no desktop and cannot show a window. The flow ends in a popup, so the code
that watches the spool folder has to live in the signed-in user's session.

Three parts, and the thread each runs on matters:

    main thread    Tk main loop — owns every window (Tk requires this)
    watcher        polls the spool folder, runs the pipeline
    web            the control panel on 127.0.0.1

The watcher never touches a widget. It hands a job id to the popup host's queue
and the main thread picks it up.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

from .capture.spooler import latest_job
from .capture.watcher import SpoolWatcher
from .config import Settings, paths
from .models import JobStatus
from .pipeline import Pipeline, build_default
from .runner import configure_logging
from .ui.result_popup import PopupHost

log = logging.getLogger(__name__)

# Statuses that mean a person still has to do something.
NEEDS_ATTENTION = {JobStatus.AWAITING, JobStatus.HELD, JobStatus.FAILED}


class Agent:
    def __init__(
        self, pipeline: Pipeline | None = None, settings: Settings | None = None
    ):
        self.settings = settings or Settings.load()
        self.pipeline = pipeline or build_default(self.settings)
        self.paths = paths()
        self.paths.ensure()

        self.host = PopupHost(self.pipeline, open_queue=self.open_control_panel)
        self.watcher = SpoolWatcher(
            spool=self.paths.spool,
            inbox=self.paths.inbox,
            on_job=self._handle,
        )
        self._threads: list[threading.Thread] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.settings.ui_port}/"

    def open_control_panel(self) -> None:
        webbrowser.open(self.url)

    # -- callbacks ---------------------------------------------------------

    def _handle(self, pdf_path: Path) -> None:
        """Runs on the watcher thread. Must not touch Tk.

        Print, read the page, send — then hand the outcome to the popup,
        whatever it was. Even a failure produces a popup, because the operator
        walking back to their desk needs to know a receipt did not go out.
        """
        info = latest_job()
        job = self.pipeline.process(
            pdf_path,
            doc_title=info.document,
            windows_user=info.user,
        )
        log.info(
            "job %s -> %s (%s)",
            job.id,
            job.status,
            job.recipient or job.hold_reason or job.error or "",
        )
        self.host.submit(job.id)

    # -- lifecycle ---------------------------------------------------------

    def _serve_web(self) -> None:
        import uvicorn

        from .ui.app import create_app

        uvicorn.run(
            create_app(self.pipeline),
            host="127.0.0.1",
            port=self.settings.ui_port,
            log_level="warning",
        )

    def run(self) -> None:
        for target, name in ((self.watcher.run, "watcher"), (self._serve_web, "web")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        mode = "TEST MODE — nothing will be sent" if self.settings.dry_run else "LIVE"
        log.info("WhatsApp Printer started (%s); control panel at %s", mode, self.url)

        self.host.run()  # blocks until the Tk loop exits
        self.watcher.stop()
        log.info("WhatsApp Printer stopped")


def _write_crash_report(exc: BaseException) -> Path | None:
    """Record a startup failure somewhere a person can find it.

    Frozen with --windowed there is no console and no stderr, so an exception
    here is otherwise completely invisible — the exe just appears not to run.
    Uses plain file IO rather than logging, because logging may be what failed.
    """
    try:
        target = paths().logs
        target.mkdir(parents=True, exist_ok=True)
        report = target / "crash.txt"
        with report.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 60}\n{datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"frozen={getattr(sys, 'frozen', False)} exe={sys.executable}\n\n")
            traceback.print_exception(exc, file=fh)
        return report
    except Exception:
        return None


def _show_crash(exc: BaseException, report: Path | None) -> None:
    where = f"\n\nDetails were written to:\n{report}" if report else ""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "WhatsApp Printer could not start",
            f"{type(exc).__name__}: {exc}{where}",
        )
        root.destroy()
    except Exception:
        pass  # no display; the crash file is the fallback


def selftest() -> int:
    """Build everything and exit, without opening a window.

    The build runs this against the frozen executable. It catches the failure
    mode that shipped a broken installer once already: an import that only
    breaks when packaged, which --windowed then hides completely.
    """
    agent = Agent()
    checks = {
        "settings": agent.settings is not None,
        "pipeline": agent.pipeline is not None,
        "message": agent.pipeline.templates.get(
            agent.settings.default_template
        ) is not None,
        "spool dir": agent.paths.spool.is_dir(),
        "popup host": agent.host is not None,
    }
    for name, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        print(f"selftest FAILED: {', '.join(failed)}")
        return 1
    print("selftest passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        configure_logging(paths().logs)
        if "--selftest" in argv:
            return selftest()
        Agent().run()
        return 0
    except Exception as exc:
        log.exception("agent crashed")
        report = _write_crash_report(exc)
        if "--selftest" in argv:
            traceback.print_exception(exc)
            return 1
        _show_crash(exc, report)
        return 1


if __name__ == "__main__":
    sys.exit(main())
