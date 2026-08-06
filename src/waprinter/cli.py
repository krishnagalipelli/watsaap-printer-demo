"""Command line entry points.

    waprinter run                 watch the spool folder and process jobs
    waprinter process FILE.pdf    push one PDF through the pipeline
    waprinter corpus DIR          score extraction over a folder of invoices
    waprinter queue               show jobs waiting on an operator
    waprinter history             recent jobs and where they went
    waprinter set-token           store the WhatsApp access token
    waprinter go-live             turn dry-run off, with the checks that implies
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from .config import Settings, paths
from .extract import extract_fields
from .models import Confidence, JobStatus
from .rules.gate import _excluded_numbers
from .store import Store

# The precision bar from the project plan: silent sending stays off until
# extraction over the client's real invoices clears this.
REQUIRED_PRECISION = 0.95


def cmd_run(args: argparse.Namespace) -> int:
    from .runner import Runner, configure_logging

    configure_logging(paths().logs, logging.DEBUG if args.verbose else logging.INFO)
    Runner().run_forever()
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    from .pipeline import build_default

    pipeline = build_default()
    job = pipeline.process(Path(args.pdf), doc_title=Path(args.pdf).stem)

    print(f"status     : {job.status}")
    print(f"recipient  : {job.recipient or '-'}")
    print(f"confidence : {job.confidence or '-'}")
    if job.hold_reason:
        print(f"reason     : {job.hold_reason}")
    if job.error:
        print(f"error      : {job.error}")
    print(f"invoice    : {job.fields.invoice_number or '-'}")
    print(f"customer   : {job.fields.customer_name or '-'}")
    print(f"total      : {job.fields.total_amount or '-'}")
    if job.message_preview:
        print("--- message ---")
        print(job.message_preview)
    return 0


def cmd_corpus(args: argparse.Namespace) -> int:
    """Score extraction over a folder of real invoices.

    Writes a CSV with one row per invoice. Fill in the `correct_number` column
    by hand, re-run with --score, and it reports the precision that decides
    whether silent sending can be switched on.
    """
    settings = Settings.load()
    excluded = _excluded_numbers(settings)
    directory = Path(args.directory)
    pdfs = sorted(directory.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {directory}")
        return 1

    out_path = Path(args.out or directory / "corpus_results.csv")
    truth = _load_truth(out_path) if args.score else {}

    rows = []
    would_send = 0
    correct = 0
    scored = 0
    ocr_count = 0

    for pdf in pdfs:
        fields = extract_fields(
            pdf,
            excluded_numbers=excluded,
            country_code=settings.default_country_code,
            ocr=settings.ocr(),
        )
        if fields.used_ocr:
            ocr_count += 1
        highs = [c for c in fields.candidates if c.confidence is Confidence.HIGH]
        # Mirrors the gate: exactly one high-confidence candidate sends, and an
        # OCR-derived number only when OCR is explicitly trusted.
        picked = highs[0].e164 if len(highs) == 1 else ""
        if picked and highs[0].from_ocr and not settings.ocr_silent_send:
            picked = ""
        decision = "SEND" if picked else "HOLD"
        if picked:
            would_send += 1

        expected = truth.get(pdf.name, "")
        verdict = ""
        if args.score and picked and expected:
            scored += 1
            if picked == expected:
                correct += 1
                verdict = "ok"
            else:
                verdict = "WRONG"

        rows.append(
            {
                "file": pdf.name,
                "decision": decision,
                "picked_number": picked,
                "correct_number": expected,
                "verdict": verdict,
                "candidates": " ".join(
                    f"{c.e164}({c.score})" for c in fields.candidates
                ),
                "invoice_number": fields.invoice_number or "",
                "customer_name": fields.customer_name or "",
                "source": (
                    "text" if fields.has_text_layer
                    else "ocr" if fields.used_ocr
                    else "UNREADABLE"
                ),
            }
        )

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    total = len(pdfs)
    print(f"invoices          : {total}")
    print(f"would send        : {would_send} ({would_send / total:.0%})")
    print(f"would hold        : {total - would_send} ({(total - would_send) / total:.0%})")
    if ocr_count:
        trusted = "trusted" if settings.ocr_silent_send else "always held"
        print(f"read by OCR       : {ocr_count} ({trusted})")
    print(f"results           : {out_path}")

    if not args.score:
        print()
        print("Next: fill in the 'correct_number' column, then re-run with --score.")
        return 0

    if not scored:
        print("\nNo rows to score — fill in 'correct_number' first.")
        return 1

    precision = correct / scored
    print(f"precision         : {precision:.1%} ({correct}/{scored})")
    print(f"required to go live: {REQUIRED_PRECISION:.0%}")
    if precision >= REQUIRED_PRECISION:
        print("\nPASS — silent sending can be enabled.")
        return 0
    print("\nFAIL — keep dry-run on and tune the extractor.")
    for row in rows:
        if row["verdict"] == "WRONG":
            print(f"  {row['file']}: picked {row['picked_number']}, "
                  f"expected {row['correct_number']}")
    return 1


def _load_truth(csv_path: Path) -> dict[str, str]:
    if not csv_path.exists():
        return {}
    with csv_path.open(encoding="utf-8") as fh:
        return {
            row["file"]: (row.get("correct_number") or "").strip()
            for row in csv.DictReader(fh)
        }


def cmd_queue(_args: argparse.Namespace) -> int:
    store = Store(paths().db)
    held = store.by_status(JobStatus.HELD)
    if not held:
        print("Nothing waiting.")
        return 0
    for job in held:
        print(f"{job.created_at:%d %b %H:%M}  {job.id}  {job.fields.invoice_number or '-'}")
        print(f"    {job.hold_reason}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store = Store(paths().db)
    for job in store.recent(args.limit):
        target = job.recipient or "-"
        print(
            f"{job.created_at:%d %b %H:%M}  {str(job.status):9s}  {target:15s}  "
            f"{job.fields.invoice_number or '-'}"
        )
    return 0


def cmd_set_token(_args: argparse.Namespace) -> int:
    import getpass

    from .secrets import save_token

    token = getpass.getpass("WhatsApp access token: ").strip()
    if not token:
        print("Nothing entered; token unchanged.")
        return 1
    save_token(token)
    print(f"Stored in {paths().root}")
    if sys.platform != "win32":
        print("WARNING: no DPAPI on this platform — the token is only base64 "
              "encoded. Do not use a production token here.")
    return 0


def cmd_go_live(_args: argparse.Namespace) -> int:
    """Turn off dry-run, refusing if the prerequisites are not in place."""
    from .secrets import load_token
    from .send.templates import TemplateStore

    settings = Settings.load()
    problems = []

    if not settings.phone_number_id:
        problems.append("phone_number_id is not set in settings.json")
    if not load_token():
        problems.append("no access token stored (run: waprinter set-token)")
    if not settings.own_numbers:
        problems.append(
            "own_numbers is empty — your own numbers must be blocklisted so an "
            "invoice footer is never treated as a customer"
        )

    templates = TemplateStore(paths().root / "templates.json")
    template = templates.get(settings.default_template)
    if template is None:
        problems.append(f"template '{settings.default_template}' is not configured")
    elif not template.usable:
        problems.append(
            f"template '{template.name}' is {template.status}, not approved"
        )

    if problems:
        print("Cannot go live yet:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    settings.dry_run = False
    settings.save()
    print("Dry-run is OFF. Printing to 'WhatsApp Printer' will now send real "
          "messages.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waprinter", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="watch the spool folder").set_defaults(func=cmd_run)

    p = sub.add_parser("process", help="push one PDF through the pipeline")
    p.add_argument("pdf")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("corpus", help="score extraction over a folder of invoices")
    p.add_argument("directory")
    p.add_argument("--out", help="CSV path (default: <directory>/corpus_results.csv)")
    p.add_argument(
        "--score",
        action="store_true",
        help="compare against the correct_number column and report precision",
    )
    p.set_defaults(func=cmd_corpus)

    sub.add_parser("queue", help="jobs waiting on an operator").set_defaults(
        func=cmd_queue
    )

    p = sub.add_parser("history", help="recent jobs")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_history)

    sub.add_parser("set-token", help="store the WhatsApp access token").set_defaults(
        func=cmd_set_token
    )
    sub.add_parser("go-live", help="turn dry-run off").set_defaults(func=cmd_go_live)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
