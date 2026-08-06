"""The gate between "we parsed a number" and "we sent someone their invoice".

Sending is silent, so this is the only thing standing between a parsing mistake
and a customer's billing details arriving at a stranger's phone. The rule is
uniform: anything short of one unambiguous, high-confidence recipient is held
for the operator rather than guessed at.

A held job is a ten-second interruption. A wrong send cannot be taken back.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import Settings
from ..models import Confidence, ExtractedFields, PrintJob
from ..store import Store


class Decision(enum.StrEnum):
    SEND = "send"          # confident enough to go without anyone looking
    CONFIRM = "confirm"    # raise the dialog; the operator supplies the number
    HOLD = "hold"          # queued for later; nothing to act on right now
    DUPLICATE = "duplicate"


@dataclass
class GateOutcome:
    decision: Decision
    recipient: str | None = None
    confidence: Confidence | None = None
    reason: str = ""
    dedupe_key: str | None = None


def dedupe_key(fields: ExtractedFields, recipient: str) -> str:
    """Identify "this invoice, to this person".

    Falls back to a hash of the page content when there is no invoice number,
    so reprints of unnumbered documents are still caught.
    """
    basis = fields.invoice_number or "|".join(
        filter(None, [fields.customer_name, fields.invoice_date, fields.total_amount])
    )
    raw = f"{basis}->{recipient}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _excluded_numbers(settings: Settings) -> set[str]:
    """Numbers that may never be treated as a recipient, normalised to E.164."""
    from ..extract.phone import normalize

    out: set[str] = set()
    for raw in [*settings.own_numbers, *settings.blocklist]:
        digits = "".join(ch for ch in raw if ch.isdigit())
        e164 = normalize(digits, settings.default_country_code)
        if e164:
            out.add(e164)
        elif raw.startswith("+"):
            out.add(raw)
    return out


def evaluate(
    job: PrintJob,
    settings: Settings,
    store: Store,
    now: datetime | None = None,
) -> GateOutcome:
    """Decide what happens to a freshly parsed job."""
    now = now or datetime.now()
    fields = job.fields

    # --- nothing to read -------------------------------------------------
    if not fields.readable:
        detail = fields.ocr_error or "OCR could not read it."
        return GateOutcome(
            Decision.HOLD,
            reason=f"Page has no text layer (printed as an image). {detail}",
        )

    excluded = _excluded_numbers(settings)
    candidates = [c for c in fields.candidates if c.e164 not in excluded]

    # --- operator confirms every send ------------------------------------
    # Checked before the candidate logic so the outcome does not depend on
    # whether a number happened to be found. Any candidate we did find is still
    # passed through as a suggestion to prefill the dialog.
    if settings.confirm_before_send:
        # Prefill only an unambiguous, high-confidence number. A weak guess is
        # worse than an empty box: a footer or letterhead number looks plausible
        # in the field and invites the operator to press Send without reading it.
        strong = [c for c in candidates if c.confidence is Confidence.HIGH]
        suggestion = strong[0] if len(strong) == 1 else None
        return GateOutcome(
            Decision.CONFIRM,
            recipient=suggestion.e164 if suggestion else None,
            confidence=suggestion.confidence if suggestion else None,
            reason=(
                f"Found {suggestion.e164} on the page — check it before sending."
                if suggestion
                else "Enter the customer's WhatsApp number."
            ),
        )

    if not candidates:
        suppressed = len(fields.candidates) - len(candidates)
        if suppressed:
            return GateOutcome(
                Decision.HOLD,
                reason=f"Only found {suppressed} number(s) on the blocklist "
                f"(your own or excluded numbers).",
            )
        return GateOutcome(Decision.HOLD, reason="No phone number found on the page.")

    high = [c for c in candidates if c.confidence is Confidence.HIGH]

    # --- ambiguity is a hold, never a coin flip --------------------------
    if len(high) > 1:
        listed = ", ".join(c.e164 for c in high[:4])
        return GateOutcome(
            Decision.HOLD,
            reason=f"{len(high)} equally likely numbers found ({listed}). "
            f"Pick the right one.",
        )

    if not high:
        best = candidates[0]
        return GateOutcome(
            Decision.HOLD,
            recipient=best.e164,
            confidence=best.confidence,
            reason=f"Best guess {best.e164} is only {best.confidence} confidence "
            f"({'; '.join(best.reasons)}). Confirm before sending.",
        )

    winner = high[0]

    # --- OCR results are not trusted for a silent send by default ---------
    if winner.from_ocr and not settings.ocr_silent_send:
        return GateOutcome(
            Decision.HOLD,
            recipient=winner.e164,
            confidence=winner.confidence,
            reason=f"{winner.e164} was read by OCR from a scanned page. "
            f"Check it against the invoice before sending.",
        )

    key = dedupe_key(fields, winner.e164)

    # --- reprints ---------------------------------------------------------
    prior = store.find_duplicate(key, settings.dedupe_window_hours, now=now)
    if prior:
        return GateOutcome(
            Decision.DUPLICATE,
            recipient=winner.e164,
            confidence=winner.confidence,
            dedupe_key=key,
            reason=f"Already sent to {winner.e164} at "
            f"{prior.sent_at:%d %b %H:%M}. Reprint suppressed.",
        )

    # --- volume rails -----------------------------------------------------
    per_minute = store.count_sent_since(now - timedelta(minutes=1))
    if per_minute >= settings.max_sends_per_minute:
        return GateOutcome(
            Decision.HOLD,
            recipient=winner.e164,
            confidence=winner.confidence,
            dedupe_key=key,
            reason=f"Rate limit reached ({per_minute} sent in the last minute). "
            f"Held so a runaway batch print cannot fan out.",
        )

    per_day = store.count_sent_since(now - timedelta(days=1))
    if per_day >= settings.max_sends_per_day:
        return GateOutcome(
            Decision.HOLD,
            recipient=winner.e164,
            confidence=winner.confidence,
            dedupe_key=key,
            reason=f"Daily cap reached ({per_day} sent in 24h).",
        )

    return GateOutcome(
        Decision.SEND,
        recipient=winner.e164,
        confidence=winner.confidence,
        dedupe_key=key,
        reason="; ".join(winner.reasons),
    )
