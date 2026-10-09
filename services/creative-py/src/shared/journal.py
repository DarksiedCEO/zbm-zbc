"""
Evidence journal: the local anchored log that says which ledger evidence was COMMITTED (bug sweep D, Oct 9 2026).

Before this, the ledger was this department's only store. Every decision is recorded on the ledger FIRST (record
first, then act), so an operation that recorded its ruling and then stopped (a later record failed, a gate refused, the
process died) left ledger evidence for an action that never happened, and nothing told the two apart.

The R6 pattern (bizdev-py ``_commit`` / ``audit_evidence``; compliance-py bug sweep C):

* every ledger event an operation writes is collected while it runs (``event_id``, ``event_type``, ``subject_id`` and
  the exact ``payload_sha256`` the ledger holds);
* when the operation's state change is applied, ONE local log line names them all (``data.evidence``, with the
  operation's request key ``rk``). The exact line is anchored on the ledger FIRST (``log_anchor``: epoch, seq, the
  line's SHA-256, kind), then appended and fsynced (``store.RecordLog``);
* ``audit(...)`` marks a ledger event ``committed`` only when an ANCHORED local line names it with the same type and
  payload hash; everything else is ``attempted`` (recorded first, never committed). Unanchored evidence = attempted,
  not done. An anchor whose line never reached the log (append failed after the anchor) names nothing.

Event ids carry no timestamp: they are derived from the operation, the subject's sequence and the payload hash, so a
retry of the same operation records the same event (the ledger answers 200) and its committed line names it.

A line whose anchor or append failed AFTER the operation's state was applied is ``owed``: the next operation writes it
first (the identical line, so the identical anchor id) and is refused (nothing happens) while it cannot be written.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional

from shared.store import RecordLog

ANCHOR_TYPE = "log_anchor"
ANCHOR_ACTOR = "evidence_journal"
RULE = "unanchored evidence = attempted, not done"


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def anchor_id(epoch: str, seq: int, line_sha: str, prefix: str = "anc-") -> str:
    """Deterministic: the same line always has the same anchor id (a retry is answered 200 by the ledger)."""
    return prefix + sha256_hex(f"{epoch}|{seq}|{line_sha}".encode())[:40]


def anchor_payload(epoch: str, seq: int, line_sha: str, kind: str) -> dict:
    return {"epoch": epoch, "seq": seq, "line_sha256": line_sha, "kind": kind}


class EvidenceJournal:
    def __init__(self, log: RecordLog, department: str, record: Callable[..., None],
                 entries: Optional[Callable[[], list]] = None, payload_hash: Optional[Callable[[dict], str]] = None,
                 id_prefix: str = "anc-"):
        """``record(event_id, event_type, actor, subject_id, payload, summary)`` writes one ledger event (raises on
        failure); ``entries()`` reads the whole ledger; ``payload_hash`` is the ledger client's own payload hash (the
        anchor's ``payload_sha256`` on the ledger)."""
        self.log = log
        self.department = department
        self.record = record
        self.entries = entries
        self.payload_hash = payload_hash
        self.id_prefix = id_prefix
        self.owed: list[tuple[str, str, dict]] = []   # (kind, at, data): applied, line not yet written

    def commit(self, kind: str, at: str, rk: str, evidence: list[dict], extra: Optional[dict] = None,
               owe_on_failure: bool = False) -> dict:
        """Anchor, then append, one line naming ``evidence``. Raises the ledger's error or ``StoreWriteError``;
        with ``owe_on_failure`` the line is kept and written first by the next ``flush``."""
        data = {**(extra or {}), "rk": rk, "evidence": [dict(e) for e in evidence]}
        if self.owed:
            if owe_on_failure:
                self.owed.append((kind, at, data))
                self.flush()
                return {}
            self.flush()
        try:
            return self._write(kind, at, data)
        except Exception:
            if owe_on_failure:
                self.owed.append((kind, at, data))
            raise

    def flush(self) -> None:
        """Write every owed line, oldest first (raises on the first failure: nothing else may be written)."""
        while self.owed:
            kind, at, data = self.owed[0]
            self._write(kind, at, data)
            self.owed.pop(0)

    def _write(self, kind: str, at: str, data: dict) -> dict:
        rec, line = self.log.prepare(kind, at, data)
        line_sha = sha256_hex(line)
        epoch = self.log.epoch or line_sha[:16]
        self.record(anchor_id(epoch, rec["seq"], line_sha, self.id_prefix), ANCHOR_TYPE, ANCHOR_ACTOR, f"log:{epoch}",
                    anchor_payload(epoch, rec["seq"], line_sha, kind), f"Local log line {rec['seq']} ({kind}) anchored")
        return self.log.append_prepared(rec, line)

    # ---------------------------------------------------------------------------------------------- audit view

    def audit(self, raw_lines: list[bytes], entries: list, limit: int, offset: int,
              event_type: Optional[str] = None) -> dict:
        """Classify this department's ledger events (anchors excluded). Pure: call it OUTSIDE the service lock with
        the raw lines copied under it (``RecordLog.raw_lines``) and the ledger's entries."""
        epoch = sha256_hex(raw_lines[0])[:16] if raw_lines else None
        mine = [e for e in entries if isinstance(e, dict) and e.get("department") == self.department]
        anchors = {e.get("event_id"): e for e in mine if e.get("event_type") == ANCHOR_TYPE}
        named: dict[str, tuple] = {}
        anchored_lines = 0
        for ln in raw_lines:
            r = json.loads(ln)
            line_sha = sha256_hex(ln)
            a = anchors.get(anchor_id(epoch, r["seq"], line_sha, self.id_prefix))
            want = anchor_payload(epoch, r["seq"], line_sha, r["kind"])
            if a is None or a.get("subject_id") != f"log:{epoch}" or (
                    self.payload_hash is not None and a.get("payload_sha256") != self.payload_hash(want)):
                continue
            anchored_lines += 1
            for n in r["data"].get("evidence") or []:
                named[n.get("event_id")] = (r["seq"], r["data"].get("rk"), n)
        rows, counts = [], {"committed": 0, "attempted": 0}
        for e in mine:
            et = e.get("event_type")
            if et == ANCHOR_TYPE or (event_type is not None and et != event_type):
                continue
            row = {"event_id": e.get("event_id"), "event_type": et, "subject_id": e.get("subject_id"),
                   "payload_sha256": e.get("payload_sha256"), "status": "attempted", "seq": None, "rk": None}
            hit = named.get(e.get("event_id"))
            if hit is not None:
                seq, rk, n = hit
                if n.get("event_type") == et and n.get("payload_sha256") == e.get("payload_sha256") \
                        and n.get("subject_id") == e.get("subject_id"):
                    row.update(status="committed", seq=seq, rk=rk)
            counts[row["status"]] += 1
            rows.append(row)
        return {"rule": RULE, "consistency": "eventual; re-read to settle", "total": len(rows), "counts": counts,
                "limit": limit, "offset": offset, "events": rows[offset:offset + limit], "log_lines": len(raw_lines),
                "anchored_lines": anchored_lines, "epoch": epoch, "lines_owed": len(self.owed)}
