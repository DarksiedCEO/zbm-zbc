"""AEGIS r6 R6-1: money-out ledger events that a restore can lose carry their full payload in their summary, so a
restore derives the correction (accounts, amounts, counterparty, idempotency key) from the lost event itself and
checks it against the payload SHA-256 the ledger holds. Never typed by hand.

The ledger keeps a payload's SHA-256 and a summary of at most 280 characters, never the payload. Each summary below
names every payload field and the local log line (``seq``), so ``payload_sha256({**payload, rk, seq})`` can be
recomputed and compared with the ledger's (svc_restore ``_verified``). A summary that does not parse, or does not hash
to the ledger's value, derives nothing (the item stays with Andre: fail closed).
"""

from __future__ import annotations

import re
from typing import Optional

ITEM_SUMMARY = re.compile(r"Payout item submitted to ([a-z]{1,20}): (\d+\.\d{2}) payee (\S{1,64}) batch (\S{1,64}) "
                          r"key (\S{1,96}) line (\d+)")
XFER_SUMMARY = re.compile(r"([a-z_]{1,20}) posted \(attempt (\d+)\) (F\w{1,4}) (\d+\.\d{2}) dr (\d{4}) (\S{1,80}) "
                          r"cr (\d{4}) (\S{1,80}) ref (\S{1,64}) line (\d+)")
REFUND_PAID_SUMMARY = re.compile(r"Refund paid: (\d+\.\d{2}) campaign (\S{1,64}) client (\S{1,64}) line (\d+)")


def item_payload(item_id: str, net: str, key: str, payee_id: str, rail: str, batch_id: str) -> dict:
    return {"item_id": item_id, "net": net, "idempotency_key": key, "payee_id": payee_id, "rail": rail,
            "batch_id": batch_id}


def item_summary(net: str, key: str, payee_id: str, rail: str, batch_id: str, seq: int) -> str:
    return f"Payout item submitted to {rail}: {net} payee {payee_id} batch {batch_id} key {key} line {seq}"


def xfer_payload(op_id: str, attempt: int, kind: str, flow: str, amount: str, dr: tuple, cr: tuple,
                 ref_id: Optional[str]) -> dict:
    return {"op_id": op_id, "attempt": attempt, "kind": kind, "flow": flow, "amount": amount,
            "dr": [dr[0], dr[1] or "-"], "cr": [cr[0], cr[1] or "-"], "ref_id": ref_id or "-"}


def xfer_summary(kind: str, attempt: int, flow: str, amount: str, dr: tuple, cr: tuple, ref_id: Optional[str],
                 seq: int) -> str:
    return (f"{kind} posted (attempt {attempt}) {flow} {amount} dr {dr[0]} {dr[1] or '-'} cr {cr[0]} {cr[1] or '-'} "
            f"ref {ref_id or '-'} line {seq}")


def refund_paid_payload(refund_id: str, amount: str, campaign_id: str, client_id: str) -> dict:
    return {"refund_id": refund_id, "amount": amount, "campaign_id": campaign_id, "client_id": client_id}


def refund_paid_summary(amount: str, campaign_id: str, client_id: str, seq: int) -> str:
    return f"Refund paid: {amount} campaign {campaign_id} client {client_id} line {seq}"
