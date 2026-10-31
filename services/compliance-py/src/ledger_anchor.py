"""
Daily external anchor of the evidence ledger (red team RTM-07; AEGIS wave A F-5; ADR 0025 section 5).

The ledger's hash chain is unkeyed: whoever can write the ledger's volume can rewrite history consistently, or roll it
back with a matching ``.head``. The control is a copy of the head kept where the ledger's volume is not: this store,
on Compliance's own volume (``<COMPLIANCE_DATA_DIR>/ledger_anchors.jsonl``), written by Compliance's in-process
scheduler once a day (and at start).

Each run:

1. reads ``GET /ledger/anchor`` (``<entries>:<head_hash>``, with the ledger's own HMAC signature when it has a key);
2. if the store holds an earlier anchor at ``n`` entries, reads ``GET /ledger/anchor?at=n`` and requires the SAME hash
   at that position (today's ledger extends the stored one) and at least ``n`` entries. Otherwise: ledger history was
   rolled back or rewritten since -> ``ledger_anchor_mismatch`` on Compliance's /health (503) and an ALERT line,
   kept until Andre resolves it;
3. appends today's anchor. Every record carries ``prev`` (the previous record's MAC) and ``mac`` =
   HMAC-SHA256(COMPLIANCE_LEDGER_ANCHOR_KEY, record) -- a key the ledger never holds -- so the store itself is
   tamper-evident (``verify`` checks the chain at every run; a broken chain is a mismatch too).

So no anchor has to be copied by hand at a ledger redeploy: a rolled-back or rewritten ledger is caught by the next
run, which closes nothing by itself but turns Compliance's /health red (the heartbeat stops) the same day.
``LEDGER_ANCHORED_HEAD`` on the ledger stays available as an extra start-time floor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Optional

FILE = "ledger_anchors.jsonl"


def _canon(rec: dict) -> bytes:
    return json.dumps({k: v for k, v in rec.items() if k != "mac"}, sort_keys=True, separators=(",", ":")).encode()


class AnchorStore:
    def __init__(self, path: str, key: Optional[str]):
        self.path = path
        self.key = (key or "").encode()

    def _mac(self, rec: dict) -> str:
        if not self.key:
            return hashlib.sha256(_canon(rec)).hexdigest()       # no key: still a hash chain (documented weaker)
        return hmac.new(self.key, _canon(rec), hashlib.sha256).hexdigest()

    def records(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "rb") as fh:
            for ln in fh.read().split(b"\n"):
                if ln.strip():
                    out.append(json.loads(ln))
        return out

    def verify(self) -> Optional[str]:
        """None when every record's MAC and ``prev`` link verify."""
        prev = None
        for i, rec in enumerate(self.records()):
            if rec.get("prev") != prev or not hmac.compare_digest(str(rec.get("mac")), self._mac(rec)):
                return f"the anchor store does not verify at record {i + 1} (edited, truncated or another key)"
            prev = rec["mac"]
        return None

    def append(self, anchor: dict, at: str) -> dict:
        recs = self.records()
        rec = {"anchor": anchor["anchor"], "entries": int(anchor["entries"]), "head_hash": anchor["head_hash"],
               "ledger_at": anchor.get("at"), "ledger_signature": anchor.get("signature"), "stored_at": at,
               "prev": recs[-1]["mac"] if recs else None}
        rec["mac"] = self._mac(rec)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, json.dumps(rec, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        return rec


def check(store: AnchorStore, client, now_iso: str) -> tuple[Optional[str], Optional[dict]]:
    """One run: ``(problem, appended record)``. ``client`` has ``anchor(at=None) -> dict`` (raises when the ledger
    cannot be read: the caller retries later, that is not a mismatch)."""
    broken = store.verify()
    if broken:
        return broken, None
    cur = client.anchor()
    recs = store.records()
    if recs:
        last = recs[-1]
        n = int(last["entries"])
        if int(cur["entries"]) < n:
            return (f"the ledger holds {cur['entries']} entries but the anchor stored at {last['stored_at']} has {n}: "
                    "it was rolled back"), None
        then = client.anchor(at=n)
        if then.get("head_hash") != last["head_hash"]:
            return (f"the ledger's entry {n - 1} no longer has the hash anchored at {last['stored_at']}: history "
                    "before it was rewritten"), None
        if cur["anchor"] == last["anchor"]:
            return None, None                                    # nothing new since the last run
    return None, store.append(cur, now_iso)
