"""Issue WhatsApp Printer licences. Runs on the issuing machine only.

    python tools/issue_licence.py keygen
    python tools/issue_licence.py issue --machine HYGR-W2VX-IY7L-LRBH-GINC \\
        --licensee "Sri Chits, Kukatpally" [--updates-until 2027-10-30] [-o FILE]

`keygen` makes the signing key once and prints the public half, which goes into
src/waprinter/licence.py as PUBLIC_KEY. Keep the key file, and a backup of it
somewhere safe: every licence in the field is checked against it, and a lost
key means no counter can be issued or renewed until a release carrying a new
public key reaches all of them.

`issue` writes the licence file and records it in issued.csv beside the key,
which is the list to work from when AMC renewals come round. A renewal is
another `issue` for the same machine code with a later --updates-until.
"""

from __future__ import annotations

import argparse
import csv
import os
import secrets
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from waprinter import licence as lic  # noqa: E402

DEFAULT_KEY = Path.home() / ".waprinter-licensing" / "signing-key.txt"


def _read_key(path: Path) -> bytes:
    try:
        secret = bytes.fromhex(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        raise SystemExit(f"No signing key at {path}. Run `keygen` first, or pass --key.")
    if len(secret) != 32:
        raise SystemExit(f"{path} does not hold a signing key.")
    return secret


def cmd_keygen(args: argparse.Namespace) -> int:
    path = Path(args.key)
    if path.exists():
        print(f"{path} already exists. Refusing to replace it: every licence "
              f"issued so far is checked against it.")
        return 1
    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret.hex() + "\n")
    print(f"Signing key written to {path}. Back it up; never commit it.")
    print(f'PUBLIC_KEY = "{lic.public_key_of(secret).hex()}"')
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    key_path = Path(args.key)
    secret = _read_key(key_path)
    if lic.public_key_of(secret).hex() != lic.PUBLIC_KEY:
        print("This key's public half is not the PUBLIC_KEY in licence.py, so the "
              "app would reject what it signs.")
        return 1
    machine = lic.format_code(args.machine)
    if len(machine.replace("-", "")) != 20:
        print(f"'{args.machine}' is not a machine code: it should be 20 letters "
              f"and digits, as shown in Setup -> Licence.")
        return 1
    issued = date.fromisoformat(args.issued) if args.issued else date.today()
    # Year one's AMC is included in the sale.
    until = (date.fromisoformat(args.updates_until) if args.updates_until
             else issued + timedelta(days=365))
    licence = lic.Licence(
        licensee=args.licensee.strip(),
        machine=machine,
        issued=issued,
        updates_until=until,
        serial=uuid.uuid4().hex[:12].upper(),
    )
    token = lic.issue(licence, secret)
    lic.parse(token)  # the app's own check, before anyone is sent the file

    out = Path(args.out or f"licence-{machine}.txt")
    out.write_text(lic.describe(licence, token), encoding="utf-8")

    ledger = key_path.parent / "issued.csv"
    fresh = not ledger.exists()
    with ledger.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if fresh:
            writer.writerow(["serial", "licensee", "machine", "issued", "updates_until"])
        writer.writerow([licence.serial, licence.licensee, machine,
                         issued.isoformat(), until.isoformat()])
    print(f"Wrote {out}: {licence.licensee}, {machine}, updates until {until:%d %b %Y}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", default=str(DEFAULT_KEY), help=f"signing key (default {DEFAULT_KEY})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen", help="make the signing key, once").set_defaults(func=cmd_keygen)
    p = sub.add_parser("issue", help="write a licence for one computer")
    p.add_argument("--machine", required=True, help="the code shown in Setup -> Licence")
    p.add_argument("--licensee", required=True, help="client and branch, as it should read")
    p.add_argument("--updates-until", help="YYYY-MM-DD (default: a year from issue)")
    p.add_argument("--issued", help="YYYY-MM-DD (default: today)")
    p.add_argument("-o", "--out", help="where to write it (default: licence-<machine>.txt)")
    p.set_defaults(func=cmd_issue)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
