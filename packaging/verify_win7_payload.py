"""Reject known Win7 packaging regressions without executing Windows binaries.

Requires pefile (build-time only). This is not a replacement for the Win7 VM
startup/OCR test: the import denylist covers known newer APIs, not every API.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pefile


NEWER_APIS = {
    "GetSystemTimePreciseAsFileTime", "CreateFile2", "CopyFile2",
    "GetTempPath2W", "GetTempPath2A", "WaitOnAddress",
    "WakeByAddressSingle", "WakeByAddressAll",
}
REQUIRED = {"python3.dll", "python38.dll", "msvcp140.dll", "vcruntime140.dll",
            "ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll"}


def inspect(path):
    """Read headers/imports/exports only; never load or execute the file."""
    pe = pefile.PE(str(path))
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
    by_location = {(p.parent, p.name.lower()): data for p, data in binaries.items()}
    for path, (machine, version, imports, exports) in binaries.items():
        name = path.name.lower()
        if machine != 0x14C:
            errors.append("{}: expected x86, got PE machine {:#x}".format(path, machine))
        if name.startswith("python") and name.endswith(".dll"):
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
            target = by_location.get((path.parent, dll), by_location.get((folder, dll)))
            if target is None and dll.startswith(("python", "msvcp140", "vcruntime140", "api-ms-win-crt-")):
                errors.append("{}: imported runtime {} is not bundled".format(path, dll))
            if target is not None and symbol not in target[3]:
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
