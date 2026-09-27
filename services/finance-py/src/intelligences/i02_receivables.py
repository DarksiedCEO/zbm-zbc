"""
Intelligence 2 — Receivables & Billing (Finance spec §B.9, §C.2; FIN-21, FIN-22, FIN-23, FIN-29). Pure judgment:
invoice validation, the no-surcharge rule, the recurring-billing mechanics, the banned custody words, receipt
matching. It never issues an invoice without Andre, never adds a surcharge and never refunds during a dispute.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import money as M
import reasons as R
from textguard import banned_words_in, fee_words_in, iter_strings

NUMBER, NAME, ACTOR = 2, "Receivables & Billing", "intel_02_receivables"
# The line-code enum has no surcharge, card_fee or convenience_fee value (FIN-21).
LINE_CODES = {"zbc": ("campaign_deposit",),
              "zbm": ("creative_services", "strategy_services", "production_services", "retainer_fee",
                      "subscription_fee")}
KINDS = ("campaign_deposit", "service", "retainer", "subscription")
RECURRING_KINDS = ("retainer", "subscription")
RECURRING_FIELDS = ("consent_artifact_ref", "cancel_medium", "annual_reminder_due", "price_change_notice_days",
                    "trial_days", "trial_reminder_days")
PAYMENT_METHODS = ("ach", "wire")


def text_problems(obj) -> list[dict]:
    """FIN-21 fee words and R5 banned custody words anywhere in caller text (lines, notes, template variables)."""
    out = []
    for s in iter_strings(obj):
        fw = fee_words_in(s)
        if fw:
            out.append(R.item("SURCHARGE_REFUSED", f"fee wording refused: {', '.join(fw)[:60]}"))
        bw = banned_words_in(s)
        if bw:
            out.append(R.item("BANNED_WORD", f"custody wording refused: {', '.join(bw)[:60]}"))
    return R.dedupe(out)


def line_amount(line: dict) -> Decimal:
    with M.money_context():
        return M.q(M.D(line["unit_price"]) * Decimal(int(line["quantity"])))


def lines_total(lines: list[dict]) -> Decimal:
    return M.total(line_amount(l) for l in lines)


def recurring_problems(inv: dict) -> list[dict]:
    rec = inv.get("recurring")
    if inv["kind"] not in RECURRING_KINDS:
        return [] if rec is None else [R.item("RECURRING_INCOMPLETE", "recurring terms on a one-off invoice")]
    if not isinstance(rec, dict):
        return [R.item("RECURRING_INCOMPLETE", "subscription/retainer invoice without recurring terms (ARL/ROSCA)")]
    out = []
    for f in RECURRING_FIELDS:
        if rec.get(f) in (None, ""):
            out.append(R.item("RECURRING_INCOMPLETE", f"recurring field missing: {f}"))
    if rec.get("cancel_medium") not in (None, "same_medium_and_online"):
        out.append(R.item("RECURRING_INCOMPLETE", "cancellation must be in the same medium and online"))
    n = rec.get("price_change_notice_days")
    if isinstance(n, int) and not 7 <= n <= 30:
        out.append(R.item("RECURRING_INCOMPLETE", "price-change notice must be 7-30 days before"))
    t, tr = rec.get("trial_days"), rec.get("trial_reminder_days")
    if isinstance(t, int) and t > 31 and not (isinstance(tr, int) and 3 <= tr <= 21):
        out.append(R.item("RECURRING_INCOMPLETE", "trials over 31 days need a reminder 3-21 days before expiry"))
    return out


def match_receipt(amount: Decimal, token: Optional[str], open_invoices: dict, entity: str) -> Optional[dict]:
    """An issued, unpaid invoice of this entity whose id is the reference token and whose total equals the amount."""
    if not token:
        return None
    inv = open_invoices.get(token)
    if inv is None or inv["entity"] != entity or inv["status"] != "issued":
        return None
    return inv if M.D(inv["total"]) == amount else None
