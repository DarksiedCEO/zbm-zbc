"""AEGIS r6 R6-1 / r7 R7-2: money-out ledger events that a restore can lose carry their full payload in their summary,
so a restore derives the correction (accounts, amounts, counterparty, idempotency key) from the lost event itself and
checks it against the payload SHA-256 the ledger holds. Never typed by hand.

The ledger keeps a payload's SHA-256 and a summary of at most 280 characters, never the payload. Each summary below
names every payload field and the local log line (``seq``), so ``payload_sha256({**payload, rk, seq})`` can be
recomputed and compared with the ledger's (svc_restore ``_verified``). The formats are compact so that the LONGEST
ids the types allow still fit (payee / campaign / batch ids up to 128 characters, client ids up to 100; AEGIS r7
R7-2: tests/test_finance_ops_r7.py builds every event with maximum-length ids). A summary that does not parse, or
does not hash to the ledger's value, derives nothing; Andre then books it with the manual restore booking.
"""

from __future__ import annotations

import re
from typing import Optional

SUMMARY_MAX = 280
_ID = r"[A-Za-z0-9._:-]{1,128}"
_CLIENT = r"[A-Za-z0-9._-]{1,100}"
_SUB = r"(?:-|[A-Za-z0-9._:-]{1,140})"
_OWN = r"fin-[a-z][a-z0-9]{0,7}-[0-9A-Z]{26}"     # an id Finance minted (service.rid): 34-38 characters
ITEM_SUMMARY = re.compile(rf"Payout sent ([a-z]{{1,20}}) (\d{{1,12}}\.\d{{2}}) p ({_ID}) b ({_OWN}) w (\d{{4}}-W\d{{2}}) "
                          rf"l (\d{{1,9}})")
XFER_SUMMARY = re.compile(rf"([a-z_]{{1,20}}) posted a(\d{{1,6}}) (F\w{{1,4}}) (\d{{1,12}}\.\d{{2}}) dr (\d{{4}}) ({_SUB}) "
                          rf"cr (\d{{4}}) ({_SUB}) r (-|{_ID}) l (\d{{1,9}})")
REFUND_PAID_SUMMARY = re.compile(rf"Refund paid (\d{{1,12}}\.\d{{2}}) c ({_ID}) k ({_CLIENT}) l (\d{{1,9}})")


def item_payload(item_id: str, net: str, key: str, payee_id: str, rail: str, batch_id: str) -> dict:
    return {"item_id": item_id, "net": net, "idempotency_key": key, "payee_id": payee_id, "rail": rail,
            "batch_id": batch_id}


def item_summary(net: str, period: str, payee_id: str, rail: str, batch_id: str, seq: int) -> str:
    """The idempotency key is not written: it is SHA-256(zbc|payout|batch|payee|period) (i04), so the summary names
    the batch's ISO week and the restore recomputes the key (64 characters saved; a 128-character payee id fits)."""
    return f"Payout sent {rail} {net} p {payee_id} b {batch_id} w {period} l {seq}"


def xfer_payload(op_id: str, attempt: int, kind: str, flow: str, amount: str, dr: tuple, cr: tuple,
                 ref_id: Optional[str]) -> dict:
    return {"op_id": op_id, "attempt": attempt, "kind": kind, "flow": flow, "amount": amount,
            "dr": [dr[0], dr[1] or "-"], "cr": [cr[0], cr[1] or "-"], "ref_id": ref_id or "-"}


def xfer_summary(kind: str, attempt: int, flow: str, amount: str, dr: tuple, cr: tuple, ref_id: Optional[str],
                 seq: int) -> str:
    return (f"{kind} posted a{attempt} {flow} {amount} dr {dr[0]} {dr[1] or '-'} cr {cr[0]} {cr[1] or '-'} "
            f"r {ref_id or '-'} l {seq}")


def refund_paid_payload(refund_id: str, amount: str, campaign_id: str, client_id: str) -> dict:
    return {"refund_id": refund_id, "amount": amount, "campaign_id": campaign_id, "client_id": client_id}


def refund_paid_summary(amount: str, campaign_id: str, client_id: str, seq: int) -> str:
    return f"Refund paid {amount} c {campaign_id} k {client_id} l {seq}"


# AEGIS r7 R7-2b: the accounts a manual restore booking posts are FIXED by the lost event's type (never chosen)
def manual_lines(event_type: str, kind: Optional[str], amount, counterparty: str, rail_account: str):
    """-> (debit account, debit sub-ledger, credit account, credit sub-ledger) or None."""
    if event_type in ("item_submitted", "payout_item") or event_type.startswith("rail_"):
        return "2020", f"payee:{counterparty}", rail_account, None
    if event_type == "refund_paid":
        return "2010", f"campaign:{counterparty}", "1020", None
    if event_type in ("treasury_posting_committed", "treasury_op") and kind == "funding":
        return rail_account, None, "1020", None
    return None
