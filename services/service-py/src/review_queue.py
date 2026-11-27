"""
RR-F5 review queue (front desk). One job: list the customer replies the consent keeper is holding for a person to
read, and turn a named staff reviewer's ruling into the ``staff_review`` event the keeper understands. Deterministic;
fixture-only; nothing is sent, and nothing here decides a send: the consent keeper's ``replay`` stays the only judge.

Rules:
- The queue is read from the keeper itself, never from its own rules: for every (client, number) that ever sent an
  inbound text, a probe send is replayed after the last event, and only a record the keeper blocks for
  ``unrecognised_reply`` (a free-text reply nobody has read) is queued. A record blocked for anything stronger (an
  opt-out, a possible opt-out, a carrier block, a reassignment) is never queued: no review can clear it.
- An item shows every inbound reply for that record since the last event that cleared it (an earlier review, new
  written consent), oldest first, so the reviewer reads all of what is unread, not only the latest line.
- ``review_event`` turns a ruling into a ``staff_review`` event only when the item is still exactly what the queue
  shows now: the same record, still held for ``unrecognised_reply``, with the same unread replies. A reply that
  arrived after the reviewer opened the item makes the ruling stale (``StaleItem``), so a ruling can never clear a
  reply its reviewer did not read. The review is timed after the newest event in the history, the reviewer must have
  a readable name (the keeper's own rule), and the ruling is ``not_an_opt_out`` (clears the hold) or ``opt_out``
  (records a permanent opt-out).
- Every ruling is kept as a plain audit row (who, when, which replies, which ruling); the history plus the review
  events is the record. Malformed histories refuse as a whole (ValueError, from the keeper).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

import consent_keeper as ck

RULINGS = ("not_an_opt_out", "opt_out")


class StaleItem(ValueError):
    """The item changed after it was listed (a new reply, a clear, a stronger block): list the queue again."""


@dataclass(frozen=True)
class QueueItem:
    client_id: str
    number: str                          # +1NXXNXXXXXX, as the keeper reads it
    held_since: int                      # t of the oldest unread reply
    replies: tuple[tuple[int, str], ...]  # (t, text) of every unread reply, oldest first
    item_id: str                         # a digest of the above; a ruling names it


@dataclass(frozen=True)
class AuditRow:
    item_id: str
    client_id: str
    number: str
    reviewer: str
    ruling: str
    t: int
    replies: tuple[tuple[int, str], ...]


def _digest(client: str, number: str, replies: tuple[tuple[int, str], ...]) -> str:
    raw = json.dumps([client, number, [list(r) for r in replies]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _validated(events: object) -> list[dict]:
    if not isinstance(events, (list, tuple)):
        raise ValueError("events must be a list of event objects")
    rows = list(events)
    ck.ConsentKeeper().replay(rows)          # the keeper's own validation: a malformed history refuses as a whole
    return [dict(e) for e in rows]


def _clear_points(rows: list[dict], client: str, number: str) -> list[int]:
    """t of every event that can clear an unread-reply hold for this record (a review, new written consent)."""
    out = []
    for e in rows:
        if e["kind"] not in ("staff_review", "opt_in") or e["kind"] == "send_attempt":
            continue
        if ck.normalise_client(e["client_id"]) != client or ck.normalise_number(e.get("number")) != number:
            continue
        if e["kind"] == "staff_review" or (e.get("consent") == "express_written" and
                                           e.get("source") in ck.CONSENT_SOURCES):
            out.append(e["t"])
    return out


def pending(events: object) -> list[QueueItem]:
    """Every record the keeper holds for ``unrecognised_reply``, with its unread replies, oldest hold first."""
    rows = _validated(events)
    last_t = max((e["t"] for e in rows), default=0)
    records = []
    for e in rows:
        if e["kind"] == "inbound_sms":
            key = (ck.normalise_client(e["client_id"]), ck.normalise_number(e["number"]))
            if key not in records:
                records.append(key)
    if not records:
        return []
    probes = [{"kind": "send_attempt", "t": last_t + 1, "client_id": c, "number": n,
               "send_id": f"review-queue-probe-{j}", "message_class": "informational"}
              for j, (c, n) in enumerate(records)]
    taken = {e.get("send_id") for e in rows if e["kind"] == "send_attempt"}
    if taken & {p["send_id"] for p in probes}:
        raise ValueError("a send_id in the history collides with the queue's probe ids")
    decisions = {d.send_id: d for d in ck.ConsentKeeper().replay(rows + probes)}
    items = []
    for p, (client, number) in zip(probes, records):
        if decisions[p["send_id"]].reason != "unrecognised_reply":
            continue
        cleared = max(_clear_points(rows, client, number), default=None)
        replies = tuple(sorted(
            (e["t"], e["text"]) for e in rows
            if e["kind"] == "inbound_sms" and ck.normalise_client(e["client_id"]) == client
            and ck.normalise_number(e["number"]) == number and (cleared is None or e["t"] >= cleared)))
        items.append(QueueItem(client, number, replies[0][0] if replies else last_t, replies,
                               _digest(client, number, replies)))
    return sorted(items, key=lambda it: (it.held_since, it.client_id, it.number))


def review_event(events: object, item_id: str, reviewer: object, ruling: str) -> tuple[dict, AuditRow]:
    """The ``staff_review`` event for a ruling on the item ``item_id``, and its audit row. Raises StaleItem when the
    item is no longer exactly what the queue shows, ValueError for a bad reviewer or ruling."""
    if ruling not in RULINGS:
        raise ValueError(f"the ruling must be one of {RULINGS}")
    if not ck._is_named(reviewer):
        raise ValueError("a review needs a reviewer with a readable name")
    rows = _validated(events)
    current = {it.item_id: it for it in pending(rows)}
    item = current.get(item_id) if isinstance(item_id, str) else None
    if item is None:
        raise StaleItem("that item is not in the queue as listed: list the queue again and re-read it")
    t = max(e["t"] for e in rows) + 1        # after everything the reviewer could have seen
    event = {"kind": "staff_review", "t": t, "client_id": item.client_id, "number": item.number,
             "reviewer": reviewer, "ruling": ruling}
    ck.ConsentKeeper().replay(rows + [event])    # the keeper accepts it, or this raises
    return event, AuditRow(item.item_id, item.client_id, item.number, reviewer, ruling, t, item.replies)
