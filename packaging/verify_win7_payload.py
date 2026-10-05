"""Reject known Win7 packaging regressions without executing Windows binaries.

Requires pefile (build-time only). This is not a replacement for the Win7 VM
startup/OCR test: the import denylist covers known newer APIs, not every API.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pefile


NEWER_APIS = {
    "GetSystemTimePreciseAsFileTime", "CreateFile2", "CopyFile2",
    "GetTempPath2W", "GetTempPath2A", "WaitOnAddress",
    "WakeByAddressSingle", "WakeByAddressAll",
}
REQUIRED = {"python3.dll", "python38.dll", "msvcp140.dll", "vcruntime140.dll",
            "ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll"}
# python3.dll, python38.dll, python312.dll -- the interpreter itself. Matching
# "python*.dll" instead would also catch pywin32's pythoncom38.dll, which is a
# COM library, carries no version resource, and is not a second interpreter.
INTERPRETER = re.compile(r"python\d+\.dll$")
# pefile stops parsing an export directory once it looks corrupt, and its
# default ceiling is below what MFC exports: mfc140u.dll alone has over 8192.
# win32ui.pyd imports it by ordinal, so a truncated table makes every ordinal
# past the cut appear missing. Read the whole table, and keep a bound.
MAX_EXPORTS = 0x40000


def inspect(path):
    """Read headers/imports/exports only; never load or execute the file.

    Returns exports as None when the directory could not be read in full, so
    callers can tell "no such symbol" apart from "we did not see the symbols".
    """
    pe = pefile.PE(str(path), max_symbol_exports=MAX_EXPORTS)
    try:
        version = None
        info = getattr(pe, "VS_FIXEDFILEINFO", [])
        if info:
            ms, ls = info[0].FileVersionMS, info[0].FileVersionLS
            version = (ms >> 16, ms & 65535, ls >> 16, ls & 65535)
        imports = []
        for table in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, table, []):
                for symbol in entry.imports:
                    name = symbol.name.decode("ascii") if symbol.name else symbol.ordinal
                    imports.append((entry.dll.decode("ascii").lower(), name))
        exports = {}
        for symbol in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", []):
            forward = symbol.forwarder.decode("ascii") if symbol.forwarder else None
            exports[symbol.ordinal] = forward
            if symbol.name:
                exports[symbol.name.decode("ascii")] = forward
        if any("Assuming corrupt" in w for w in pe.get_warnings()):
            exports = None
        return pe.FILE_HEADER.Machine, version, imports, exports
    finally:
        pe.close()


def verify(folder):
    folder = Path(folder)
    errors = []
    binaries = {}
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in {".exe", ".dll", ".pyd"}:
            continue
        try:
            binaries[path] = inspect(path)
        except (pefile.PEFormatError, OSError, ValueError) as exc:
            errors.append("{}: invalid PE: {}".format(path, exc))
    root_names = {p.name.lower() for p in binaries if p.parent == folder}
    for name in sorted(REQUIRED - root_names):
        errors.append("{}: required runtime missing".format(folder / name))
    by_location = {}
    by_name = {}
    for path, data in binaries.items():
        by_location[(path.parent, path.name.lower())] = data
        by_name.setdefault(path.name.lower(), []).append(data)

    def resolve(importer, dll):
        """Where the loader would find dll, as the frozen app is laid out.

        Its own directory first, then the payload root. Failing both, anywhere
        in the payload: PyInstaller stages pywin32's DLLs in a pywin32_system32
        subdirectory and puts that on the search path with a runtime hook, so a
        .pyd importing one of them is satisfied from there, not from the root.
        """
        for key in ((importer.parent, dll), (folder, dll)):
            if key in by_location:
                return [by_location[key]]
        return by_name.get(dll, [])

    for path, (machine, version, imports, exports) in binaries.items():
        name = path.name.lower()
        if machine != 0x14C:
            errors.append("{}: expected x86, got PE machine {:#x}".format(path, machine))
        if INTERPRETER.match(name):
            if name not in {"python3.dll", "python38.dll"} or not version or version[:2] != (3, 8):
                errors.append("{}: expected Python 3.8 runtime, got {}".format(path, version))
        if name.startswith(("msvcp140", "vcruntime140")) and name.endswith(".dll"):
            if not version or version[:2] != (14, 29):
                errors.append("{}: expected VC142 14.29 runtime, got {}".format(path, version))
        if name == "ucrtbase.dll" and (not version or version[:3] != (10, 0, 19041)):
            errors.append("{}: expected SDK 10.0.19041 UCRT, got {}".format(path, version))
        if name == "python3.dll":
            if not exports or any(not f or not f.lower().startswith("python38.") for f in exports.values()):
                errors.append("{}: stable ABI must forward only to python38.dll".format(path))
        for dll, symbol in imports:
            if dll in {"kernel32.dll", "kernelbase.dll"} and symbol in NEWER_APIS:
                errors.append("{}: {}!{} requires newer Windows".format(path, dll, symbol))
            targets = resolve(path, dll)
            if not targets and dll.startswith(("python", "msvcp140", "vcruntime140", "api-ms-win-crt-")):
                errors.append("{}: imported runtime {} is not bundled".format(path, dll))
            readable = [t[3] for t in targets if t[3] is not None]
            if len(readable) == len(targets) and readable and not any(symbol in e for e in readable):
                errors.append("{}: {} lacks imported symbol {}".format(path, dll, symbol))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+", type=Path)
    args = parser.parse_args()
    errors = [error for folder in args.folders for error in verify(folder)]
    if errors:
        parser.exit(1, "Windows 7 payload rejected:\n" + "\n".join(errors) + "\n")
    print("Windows 7 payload architecture, runtime and import checks passed.")


if __name__ == "__main__":
    main()
