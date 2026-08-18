"""The desktop application: one process, one window, no server.

There is no Windows service, for a concrete reason: a service runs in session 0
with no desktop and cannot show a window. The flow ends in a notification, so the
code that watches the spool folder has to live in the signed-in user's session.

Two threads, and which one owns what matters:

    main thread    the Tk loop — owns every window, and blocks until quit
    watcher        polls the spool folder and runs the pipeline

The watcher never touches a widget. It hands a job id to the window's queue and
the main thread turns it into a notification.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from . import update
from .capture.spooler import latest_job
from .capture.watcher import SpoolWatcher
from .config import Settings, paths
from .pipeline import Pipeline, build_default
from .runner import configure_logging
from .ui.desktop import DesktopWindow

log = logging.getLogger(__name__)

DAILY_CHECK_SECONDS = 60 * 60  # how often to wonder whether a day has passed


class Agent:
    def __init__(
        self, pipeline: Pipeline | None = None, settings: Settings | None = None
    ):
        self.settings = settings or Settings.load()
        self.pipeline = pipeline or build_default(self.settings)
        self.paths = paths()
        self.paths.ensure()

        # Set while a document is being processed, so an update never lands
        # halfway through a send.
        self._busy = threading.Event()

        self.window = DesktopWindow(self.pipeline, on_check_updates=self.check_updates)
        self.watcher = SpoolWatcher(
            spool=self.paths.spool,
            inbox=self.paths.inbox,
            on_job=self._handle,
        )

    # -- capture -----------------------------------------------------------

    def _handle(self, pdf_path: Path) -> None:
        """Runs on the watcher thread. Must not create widgets."""
        self._busy.set()
        try:
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
            self.window.submit(job.id)
        finally:
            self._busy.clear()

    # -- updates -----------------------------------------------------------

    def check_updates(self, install: bool = True) -> str:
        """Look for a newer build. Returns a line for the window to show.

        Called both by the daily timer and by the Check for updates button. The
        button matters: a fix released at eleven in the morning should not wait
        for a timer, which is the whole reason a manual check exists.

        Never raises — an unreachable update server must not affect printing.
        """
        result = update.check(self.settings.update_url)
        self.settings.last_update_check = datetime.now().isoformat()
        try:
            self.settings.save()
        except Exception:
            log.exception("could not record the update check")

        if result.failed or not result.available or not install:
            return result.message

        if self._busy.is_set():
            return (
                f"Version {result.release.version} is ready, but a document is "
                f"being sent. It will install shortly."
            )

        try:
            installer = update.download(result.release)
        except Exception as exc:
            log.exception("update download failed")
            return f"Could not download the update: {exc}"

        try:
            update.install(installer)
        except Exception as exc:
            log.exception("update install failed")
            return f"Could not start the installer: {exc}"

        return f"Installing version {result.release.version}. The app will restart."

    def _update_loop(self) -> None:
        while True:
            try:
                if (
                    self.settings.update_check_enabled
                    and self.settings.update_url
                    and update.due(self.settings.last_update_check)
                ):
                    log.info("daily update check: %s", self.check_updates())
            except Exception:
                log.exception("the daily update check failed")
            if self._stop.wait(DAILY_CHECK_SECONDS):
                return

    # -- lifecycle ---------------------------------------------------------

    def run(self, visible: bool = True) -> None:
        self._stop = threading.Event()
        threading.Thread(target=self.watcher.run, name="watcher", daemon=True).start()
        threading.Thread(target=self._update_loop, name="updates", daemon=True).start()

        mode = "TEST MODE — nothing will be sent" if self.settings.dry_run else "LIVE"
        log.info("WhatsApp Printer started (%s)", mode)

        self.window.run(visible=visible)  # blocks on the Tk loop

        self._stop.set()
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
    """Report a startup failure without needing the GUI that just failed."""
    where = f"\n\nDetails were written to:\n{report}" if report else ""
    text = f"{type(exc).__name__}: {exc}{where}"
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, text, "WhatsApp Printer could not start", 0x10
            )
            return
        except Exception:
            pass
    if sys.stderr:
        print(text, file=sys.stderr)


def selftest() -> int:
    """Build everything and exit, without opening a window.

    The build runs this against the frozen executable. It catches the failure
    mode that shipped a broken installer once already: an import that only
    breaks when packaged, which --windowed then hides completely.
    """
    import tkinter  # noqa: F401  — proves the GUI toolkit survived freezing

    settings = Settings.load()
    pipeline = build_default(settings)
    p = paths()
    p.ensure()
    checks = {
        "settings": settings is not None,
        "pipeline": pipeline is not None,
        "message": pipeline.templates.get(settings.default_template) is not None,
        "spool dir": p.spool.is_dir(),
        "gui toolkit": tkinter.TkVersion > 0,
        "updater": update.parse_version("1.2.10") > update.parse_version("1.2.9"),
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
        # --hidden: started at logon, so do not steal focus with the window.
        Agent().run(visible="--hidden" not in argv)
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
