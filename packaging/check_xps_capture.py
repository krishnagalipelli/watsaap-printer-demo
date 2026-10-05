"""End-to-end check of the Windows 7 capture route, on a Windows runner.

Windows 7 has no inbox PDF driver, so its queue is built on the XPS Document
Writer and the agent converts each job as it lands. That is three moving parts
-- a Local Port named as a file path, a queue on an inbox driver, and the
conversion in waprinter.capture -- and a break in any of them is invisible
until someone prints at a counter.

So this builds the queue the way installer/provision.ps1 does, prints to it,
and then runs the real SpoolWatcher over the spool folder, asserting a readable
PDF comes out the other side. Windows only, and it needs administrator rights
to touch the spooler, which is what a CI runner has.

It does not run on Windows 7 itself -- no such runner exists -- so it proves
the APIs and the conversion, not the 3.8 build on that OS. The remaining risk
is the v3 driver there versus the v4 driver here; both write packages MuPDF
reads, and the pinned pymupdf is exercised by the Windows 7 build job.
"""

import ctypes
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

PRINTER = "WAPrinter XPS check"
SERVER_ACCESS_ADMINISTER = 0x01
# A fixed short path directly off the root, as the installed product uses
# (C:\ProgramData\WAPrinter\spool). The Local Port monitor rejects a path
# under a user profile with ERROR_BAD_PATHNAME, so a temp directory will not
# do -- and a port is machine-wide anyway, so there is nothing to isolate.
ROOT = Path(r"C:\waprinter-xps-check")
# Statuses worth reading rather than looking up.
STATUS_NAMES = {5: "ERROR_ACCESS_DENIED", 87: "ERROR_INVALID_PARAMETER",
                161: "ERROR_BAD_PATHNAME", 183: "ERROR_ALREADY_EXISTS"}


class PrinterDefaults(ctypes.Structure):
    _fields_ = [("pDatatype", wintypes.LPWSTR),
                ("pDevMode", ctypes.c_void_p),
                ("DesiredAccess", wintypes.DWORD)]


def _winspool():
    dll = ctypes.WinDLL("winspool.drv")
    dll.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE),
                                 ctypes.POINTER(PrinterDefaults)]
    dll.OpenPrinterW.restype = wintypes.BOOL
    dll.XcvDataW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(wintypes.DWORD)]
    dll.XcvDataW.restype = wintypes.BOOL
    dll.ClosePrinter.argtypes = [wintypes.HANDLE]
    return dll


def local_port(action, port):
    """Add or delete a Local Port, as provision.ps1 does through XcvDataW."""
    dll = _winspool()
    handle = wintypes.HANDLE()
    defaults = PrinterDefaults(None, None, SERVER_ACCESS_ADMINISTER)
    if not dll.OpenPrinterW(",XcvMonitor Local Port", ctypes.byref(handle),
                            ctypes.byref(defaults)):
        raise OSError("cannot open the Local Port monitor")
    try:
        buf = ctypes.create_unicode_buffer(str(port))
        needed, status = wintypes.DWORD(), wintypes.DWORD()
        ok = dll.XcvDataW(handle, action, ctypes.byref(buf), ctypes.sizeof(buf),
                          None, 0, ctypes.byref(needed), ctypes.byref(status))
        # 183 is ERROR_ALREADY_EXISTS, and a missing port on delete is fine too.
        if not ok or status.value not in (0, 183):
            raise OSError("{} failed with status {} ({})".format(
                action, status.value,
                STATUS_NAMES.get(status.value, "see winerror.h")))
    finally:
        dll.ClosePrinter(handle)


def xps_driver():
    import win32print

    names = [d["Name"] for d in win32print.EnumPrinterDrivers(None, None, 2)]
    for name in names:
        if name.startswith("Microsoft XPS Document Writer"):
            return name
    raise SystemExit("No inbox XPS driver here; drivers were: {}".format(names))


def make_printer(driver, port):
    import win32com.client

    wmi = win32com.client.GetObject(r"winmgmts:\\.\root\cimv2")
    spec = wmi.Get("Win32_Printer").SpawnInstance_()
    spec.DeviceID = PRINTER
    spec.DriverName = driver
    spec.PortName = str(port)
    spec.Put_()


def remove_printer():
    import win32print
    try:
        handle = win32print.OpenPrinter(PRINTER)
        win32print.DeletePrinter(handle)
        win32print.ClosePrinter(handle)
    except Exception as exc:
        print("  (could not remove the printer: {})".format(exc))


def main():
    if sys.platform != "win32":
        raise SystemExit("Windows only.")

    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    from waprinter.capture.watcher import SpoolWatcher

    root = ROOT
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    spool, inbox = root / "spool", root / "inbox"
    spool.mkdir(parents=True)
    inbox.mkdir(parents=True)
    port = spool / "job1.xps"

    driver = xps_driver()
    print("driver        :", driver)
    local_port("AddPort", port)
    print("port          :", port)
    try:
        make_printer(driver, port)
        print("printer       :", PRINTER)

        sample = root / "invoice.txt"
        sample.write_text("Invoice INV-2291\nMobile: 9876543210\nTotal: 18,450.00\n")
        printed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Content '{}' | Out-Printer -Name '{}'".format(sample, PRINTER)],
            capture_output=True, text=True, timeout=240)
        if printed.returncode != 0:
            raise SystemExit("printing failed: {}".format(
                (printed.stderr or printed.stdout).strip()))

        # The spooler writes asynchronously, and the watcher wants the file to
        # settle, so poll it the way the agent does.
        captured = []
        watcher = SpoolWatcher(spool, inbox, captured.append, settle_seconds=0.5)
        for _ in range(60):
            if watcher.drain_once():
                break
            time.sleep(1.0)

        if not captured:
            raise SystemExit("nothing was captured; spool held: {}".format(
                [p.name for p in spool.glob('*')]))

        pdf = captured[0]
        print("captured      :", pdf.name, pdf.stat().st_size, "bytes")
        if pdf.suffix != ".pdf":
            raise SystemExit("the watcher handed on a {} rather than a PDF".format(pdf.suffix))
        if list(inbox.glob("*.xps")):
            raise SystemExit("the XPS was left behind in the inbox")

        import pymupdf

        doc = pymupdf.open(str(pdf))
        try:
            text = " ".join(page.get_text() for page in doc)
        finally:
            doc.close()
        flat = " ".join(text.replace("\xa0", " ").split())
        print("text read back:", flat[:160])
        for expected in ("INV-2291", "9876543210"):
            if expected not in flat:
                raise SystemExit("{!r} did not survive the round trip".format(expected))
        print("\nOK: a print became a readable PDF through the shipped watcher.")
    finally:
        remove_printer()
        try:
            local_port("DeletePort", port)
        except Exception as exc:
            print("  (could not remove the port: {})".format(exc))
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
