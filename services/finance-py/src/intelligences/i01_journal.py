"""
Intelligence 1 — Journal Keeper (Finance spec §B.2, §C.1). Pure judgment: builds and validates journal entries,
computes balances and the trial balance. It never edits or deletes an entry and never posts across entities; the
service appends only what ``validate`` accepts (record-first, then the local log, then memory).

Invariants checked on every append (violation -> 422, nothing written):
  sum(debits) == sum(credits) > 0; two or more lines; one entity; every account in that entity's chart with the
  sub-ledger kind the chart names; each line exactly one of debit/credit > 0.00, both canonical money strings; no
  posting into a locked period; the restricted-cash rule (FIN-01); nothing posts to 4010/5010 except the
  certification flows (F2/F3/F5/F5a); nothing posts to ZBM's media accounts (1150/2120/4120/5110) except the
  media flows (F12, F12c, F12v, F12r, F12x); a manual correction either exactly reverses an existing entry or touches none
  of the restricted, liability-sub-ledger, receivable-sub-ledger or verified-view revenue/cost accounts.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Iterable, Optional

import chart as C
import money as M
import reasons as R

NUMBER, NAME, ACTOR = 1, "Journal Keeper", "intel_01_journal"
FLOWS = ("F1", "F1a", "F1r", "F2", "F3", "F4a", "F4b", "F4c", "F4d", "F4e", "F4f", "F4g", "F5", "F5a", "F5b", "F5c",
         "F6", "F6p", "F7", "F7a", "F7l", "F8", "F9", "F10", "F11", "F11a", "F12", "F12c", "F12v", "F12r", "F12x",
         "F13", "F13f", "F13x", "F13p", "F13q", "correction")
# Media flows (ADR 0009 amendment, Oct 5 2026): F12 prepayment invoice issued, F12c issued invoice cancelled before
# payment, F12v vendor paid from collected money, F12r media delivered (revenue and cost together), F12x the bank
# returned the client's prepayment.
MEDIA_FLOWS = ("F12", "F12c", "F12v", "F12r", "F12x")
# Stripe incoming (ADR 0009 amendment, Oct 5 2026), ZBM only: F13 a client payment landed in the Stripe balance, F13f
# Stripe's processing fee on it, F13x the payment failed after it had succeeded, F13p a Stripe payout reached the
# operating account, F13q a payout failed after it was paid; F7 dispute funds (and fee) withdrawn, F7a reinstated,
# F7l a lost dispute (the client owes again). 1060 posts only from these flows or a manual correction (a Stripe fee
# that belongs to no payment, e.g. a monthly fee, shows as an L3 break and is booked by Andre).
STRIPE_FLOWS = ("F13", "F13f", "F13x", "F13p", "F13q", "F7", "F7a", "F7l")
STRIPE_ACCOUNT = "1060"
# a credit to restricted cash is only ever one of these flows (FIN-01): rail funding, rail paid, refund paid, chargeback,
# sweep of earned margin, Form 945 deposit, rail fees, a client deposit the bank returned (F1r, AEGIS N17-9), and an
# exact reversal
RESTRICTED_CREDIT_FLOWS = ("F1r", "F4a", "F4e", "F6p", "F7", "F8", "F9", "F10")
CERT_ONLY = ("4010", "5010")
CERT_FLOWS = ("F2", "F3", "F5", "F5a", "F5b")
MANUAL_FORBIDDEN = set(C.RESTRICTED_POOL) | {"1200", "1210", "2010", "2020", "2030", "2040", "2050", "2070", "4010",
                                              "5010", "5040"} | set(C.MEDIA_ACCOUNTS)
_SUB_RE = re.compile(r"(campaign|payee|item|client|buy):[A-Za-z0-9._:-]{1,120}")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def entry_sha256(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_sha256"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def line(account: str, subledger: Optional[str], debit: Decimal = M.ZERO, credit: Decimal = M.ZERO) -> dict:
    return {"account": account, "subledger": subledger, "debit": M.fmt(debit), "credit": M.fmt(credit)}


def dr(account: str, amount: Decimal, sub: Optional[str] = None) -> dict:
    return line(account, sub, debit=amount)


def cr(account: str, amount: Decimal, sub: Optional[str] = None) -> dict:
    return line(account, sub, credit=amount)


def period_of(effective_date: str) -> str:
    return effective_date[:7]


def build(entry_id: str, entity: str, effective_date: str, posted_at: str, lines: list[dict], memo_code: str,
          source: dict, idempotency_key: str, prev_sha: Optional[str], ledger_event_id: str,
          reverses_entry_id: Optional[str] = None, approval_ref: Optional[str] = None) -> dict:
    e = {"entry_id": entry_id, "entity": entity, "period": period_of(effective_date), "effective_date": effective_date,
         "posted_at": posted_at, "lines": [l for l in lines if l["debit"] != "0.00" or l["credit"] != "0.00"],
         "memo_code": memo_code, "source": source, "idempotency_key": idempotency_key,
         "reverses_entry_id": reverses_entry_id, "approval_ref": approval_ref, "prev_entry_sha256": prev_sha,
         "ledger_event_id": ledger_event_id}
    e["entry_sha256"] = entry_sha256(e)
    return e


def reversal_lines(original: dict) -> list[dict]:
    return [{"account": l["account"], "subledger": l["subledger"], "debit": l["credit"], "credit": l["debit"]}
            for l in original["lines"]]


def totals(entry: dict) -> tuple[Decimal, Decimal]:
    return M.total(l["debit"] for l in entry["lines"]), M.total(l["credit"] for l in entry["lines"])


def validate(entry: dict, locked: set, entries_by_id: dict) -> list[dict]:
    """Reasons the entry cannot be appended (empty list = valid)."""
    out: list[dict] = []
    ent = entry.get("entity")
    chart = C.CHARTS.get(ent)
    if chart is None:
        return [R.item("JOURNAL_INVALID", "unknown entity")]
    lines = entry.get("lines") or []
    if len(lines) < 2:
        out.append(R.item("JOURNAL_INVALID", "an entry needs two or more lines"))
    if entry.get("memo_code") not in FLOWS:
        out.append(R.item("JOURNAL_INVALID", "unknown memo code"))
    for i, ln in enumerate(lines):
        if ln.get("entity", ent) != ent:
            out.append(R.item("ENTITY_MIX", f"line {i + 1} belongs to another entity: one entry, one entity"))
            continue
        acct = chart.get(ln.get("account"))
        if acct is None:
            other = any(e != ent and ln.get("account") in C.CHARTS[e] for e in C.ENTITIES)
            out.append(R.item("ENTITY_MIX" if other else "JOURNAL_INVALID",
                              f"line {i + 1}: account {str(ln.get('account'))[:8]} is not in the {ent} chart"))
            continue
        sub = ln.get("subledger")
        want = acct[2]
        if want is None and sub is not None:
            out.append(R.item("JOURNAL_INVALID", f"line {i + 1}: account {ln['account']} carries no sub-ledger"))
        if want is not None and (not isinstance(sub, str) or not _SUB_RE.fullmatch(sub) or not sub.startswith(want + ":")):
            out.append(R.item("JOURNAL_INVALID", f"line {i + 1}: account {ln['account']} needs a {want}: sub-ledger"))
        try:
            d, c = M.parse(ln.get("debit")), M.parse(ln.get("credit"))
        except M.MoneyError:
            out.append(R.item("MONEY_FORMAT", f"line {i + 1}: amounts must be canonical two-decimal strings"))
            continue
        if (d > 0) == (c > 0):
            out.append(R.item("JOURNAL_INVALID", f"line {i + 1}: exactly one of debit/credit must be above 0.00"))
    if out:
        return out
    d_tot, c_tot = totals(entry)
    if d_tot != c_tot or d_tot <= 0:
        out.append(R.item("UNBALANCED", f"debits {M.fmt(d_tot)} != credits {M.fmt(c_tot)}"))
    if (ent, entry.get("period")) in locked:
        out.append(R.item("PERIOD_LOCKED", f"period {entry.get('period')} of {ent} is locked; post a correction in "
                                           "the open period with reverses_entry_id"))
    memo = entry["memo_code"]
    accounts = {ln["account"] for ln in lines}
    rev = entry.get("reverses_entry_id")
    exact_reversal = False
    if rev is not None:
        orig = entries_by_id.get(rev)
        if orig is None or orig["entity"] != ent:
            out.append(R.item("JOURNAL_INVALID", "reverses_entry_id names no entry of this entity"))
        else:
            exact_reversal = sorted(canonical(x) for x in reversal_lines(orig)) == sorted(canonical(x) for x in lines)
    restricted_credit = any(ln["account"] in C.RESTRICTED_POOL and ln["credit"] != "0.00" for ln in lines)
    if ent == "zbc" and restricted_credit and memo not in RESTRICTED_CREDIT_FLOWS and not exact_reversal:
        out.append(R.item("RESTRICTED_CASH_MISUSE", "restricted cash (1020/1040/1041) never pays operating expenses, "
                                                    "ZBM or anything but the listed creator/client flows"))
    if memo == "F8" and not all(ln["account"] in ("1010", "1020") for ln in lines):
        out.append(R.item("RESTRICTED_CASH_MISUSE", "a margin sweep moves 1020 to 1010 only"))
    if accounts & set(CERT_ONLY) and memo not in CERT_FLOWS and not exact_reversal:
        out.append(R.item("NOT_CERTIFIED", "4010/5010 post only from a V&I certification flow", rule="FIN-04"))
    if ent == "zbm" and accounts & set(C.MEDIA_ACCOUNTS) and memo not in MEDIA_FLOWS and not exact_reversal:
        out.append(R.item("JOURNAL_INVALID", "the media accounts (1150/2120/4120/5110) post only from a media flow"))
    if memo in MEDIA_FLOWS and ent != "zbm":
        out.append(R.item("ENTITY_MIX", "media flows are ZBM's; ZBC never buys media"))
    if ent == "zbm" and STRIPE_ACCOUNT in accounts and memo not in STRIPE_FLOWS + ("correction",) \
            and not exact_reversal:
        out.append(R.item("JOURNAL_INVALID", "the Stripe balance (1060) posts only from a Stripe flow or a correction"))
    if memo.startswith("F13") and ent != "zbm":
        out.append(R.item("ENTITY_MIX", "Stripe incoming is ZBM's Stripe account; ZBC has none yet (FIN-CQ-02)"))
    if memo == "correction" and not exact_reversal and accounts & MANUAL_FORBIDDEN:
        out.append(R.item("RESTRICTED_CASH_MISUSE" if accounts & set(C.RESTRICTED_POOL) else "JOURNAL_INVALID",
                          "a manual correction that is not an exact reversal may not touch restricted cash, creator/"
                          "client liabilities, clawback receivables, verified-view revenue/cost or the media accounts"))
    if memo == "correction" and rev is not None and not exact_reversal and entries_by_id.get(rev) is not None:
        out.append(R.item("JOURNAL_INVALID", "a correction naming reverses_entry_id must mirror that entry exactly"))
    return out


def apply_balances(balances: dict, entry: dict) -> None:
    """``balances[(entity, account, subledger)]`` = debits - credits (signed, debit-positive)."""
    ent = entry["entity"]
    for ln in entry["lines"]:
        k = (ent, ln["account"], ln["subledger"])
        balances[k] = M.q(balances.get(k, M.ZERO) + M.D(ln["debit"]) - M.D(ln["credit"]))


def account_balance(balances: dict, entity: str, account: str, sub: Optional[str] = None) -> Decimal:
    """Natural-side balance of an account (or one sub-ledger of it)."""
    s = M.ZERO
    for (e, a, sl), v in balances.items():
        if e == entity and a == account and (sub is None or sl == sub):
            s += v
    s = M.q(s)
    return s if C.normal_side(entity, account) == "debit" else M.q(-s)


def subledgers(balances: dict, entity: str, account: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for (e, a, sl), v in balances.items():
        if e == entity and a == account and sl is not None:
            out[sl] = M.q(out.get(sl, M.ZERO) + v)
    side = C.normal_side(entity, account)
    return {k: (v if side == "debit" else M.q(-v)) for k, v in out.items()}


def trial_balance(balances: dict, entity: str) -> dict:
    per: dict[str, Decimal] = {}
    for (e, a, _), v in balances.items():
        if e == entity:
            per[a] = M.q(per.get(a, M.ZERO) + v)
    debit = M.total(v for v in per.values() if v > 0)
    credit = M.total(-v for v in per.values() if v < 0)
    return {"entity": entity, "total_debits": M.fmt(debit), "total_credits": M.fmt(credit),
            "difference": M.sfmt(debit - credit),
            "accounts": [{"account": a, "title": C.CHARTS[entity][a][0],
                          "balance": M.sfmt(v if C.normal_side(entity, a) == "debit" else -v)}
                         for a, v in sorted(per.items())]}


def verify_chain(entries: Iterable[dict]) -> Optional[str]:
    """Per-entity entry chain check (prev_entry_sha256 / entry_sha256). Returns a problem or None."""
    heads: dict[str, Optional[str]] = {}
    for e in entries:
        if e.get("entry_sha256") != entry_sha256(e):
            return f"journal entry {e.get('entry_id')} does not match its own hash"
        if e.get("prev_entry_sha256") != heads.get(e["entity"]):
            return f"journal chain broken at {e.get('entry_id')}"
        heads[e["entity"]] = e["entry_sha256"]
    return None
