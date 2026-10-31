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

Fail loud (AEGIS round 2 N-2, N-6). The job may not fail silently:

* a torn last line (a crash or ENOSPC mid-append) is cut back to the last complete record before the run (an ALERT
  line says how many bytes went); a last record that is complete and MAC-valid and only lacks its newline is kept
  (the newline is added; AEGIS round 3 N3-7); a garbage *complete* line is not repaired: the run fails;
* a store that is missing, empty or shorter than the state file says it was is ``anchor_store_missing`` (503), and no
  new chain is started over it (round 3 N3-7);
* any failure of a run other than a ledger read is ``anchor_check_failing`` on /health (503) until a run succeeds;
* with no anchor stored or confirmed for more than ``STALE_AFTER`` (26 h; the ledger unreadable, the job not running)
  /health is 503 ``ledger_anchor_stale``, counted from the last good run kept in the state file, so a restart does
  not reset it (round 3 N3-6);
* a mismatch is kept in ``ledger_anchor_state.json`` beside the store, so a restart does not hide it;
* a store without ``COMPLIANCE_LEDGER_ANCHOR_KEY`` is a /health warning (``ledger_anchor_unkeyed``).

Resolving a mismatch is Andre's decision: docs/deploy/railway.md, "Ledger anchor mismatch".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from typing import Optional

FILE = "ledger_anchors.jsonl"
STATE_FILE = "ledger_anchor_state.json"
STALE_AFTER = timedelta(hours=26)


class StoreMissing(Exception):
    """The anchor store is missing, empty or shorter than the state file says it was (AEGIS round 3 N3-7)."""


class StateInvalid(Exception):
    """The state file is missing beside a store with history, unsigned, or does not verify (AEGIS round 4 N4-5)."""


def _write_all(fd: int, data: bytes) -> None:
    """AEGIS round 4 N4-4: os.write may write fewer bytes (ENOSPC mid-record); loop, and raise when nothing moves."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("short write")
        view = view[n:]


def _canon(rec: dict) -> bytes:
    return json.dumps({k: v for k, v in rec.items() if k != "mac"}, sort_keys=True, separators=(",", ":")).encode()


class AnchorStore:
    def __init__(self, path: str, key: Optional[str]):
        self.path = path
        self.key = (key or "").encode()
        self.state_path = os.path.join(os.path.dirname(path) or ".", STATE_FILE)
        st, self.state_invalid = self._load_state()
        self.mismatch: Optional[str] = st.get("mismatch")      # sticky until Andre resolves it (survives restarts)
        self.failing: Optional[str] = st.get("failing")        # a run failed (not a ledger read); clears on success
        self.last_ok: Optional[str] = st.get("last_ok")        # ISO time of the last run that stored or confirmed
        self.first_seen: Optional[str] = st.get("first_seen")  # ISO time this store was first used (no good run yet)
        self.size: int = int(st.get("size") or 0)              # the store's size after the last append

    @property
    def keyed(self) -> bool:
        return bool(self.key)

    # --- state beside the store (AEGIS round 4 N4-5: signed with the anchor key) ----------------------------------

    def _store_has_history(self) -> bool:
        try:
            return os.stat(self.path).st_size > 0
        except FileNotFoundError:
            return False

    def _load_state(self) -> tuple[dict, bool]:
        """(state, invalid). Invalid: missing beside a store with history, unreadable, unsigned or a bad MAC."""
        try:
            with open(self.state_path, "rb") as fh:
                st = json.loads(fh.read())
        except FileNotFoundError:
            return {}, self._store_has_history()
        except (OSError, ValueError):
            return {}, True
        if not isinstance(st, dict) or not isinstance(st.get("mac"), str):
            return {}, True
        body = {k: v for k, v in st.items() if k != "mac"}
        if not hmac.compare_digest(st["mac"], self._mac(body)):
            return {}, True
        return body, False

    def save_state(self) -> None:
        """Signed, written to a temp file, fsynced and renamed (atomic)."""
        if self.state_invalid:
            raise StateInvalid("the anchor state file is invalid; run `ledger_anchor.py resync` after review")
        body = {"mismatch": self.mismatch, "failing": self.failing, "last_ok": self.last_ok,
                "first_seen": self.first_seen, "size": self.size}
        data = json.dumps({**body, "mac": self._mac(body)}, sort_keys=True).encode()
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.state_path)

    def missing(self) -> bool:
        """The state file says records were stored, but the store is gone, empty or shorter than it was."""
        if self.size <= 0:
            return False
        try:
            return os.stat(self.path).st_size < self.size
        except FileNotFoundError:
            return True

    def reasons(self, now: datetime) -> list[str]:
        """/health codes (empty: nothing wrong)."""
        if self.state_invalid:
            return ["anchor_state_invalid"]
        if self.first_seen is None and self.last_ok is None:
            self.first_seen = now.isoformat()
            try:
                self.save_state()
            except OSError:
                pass                                         # the next run saves it; staleness still counts in memory
        out = []
        if self.mismatch:
            out.append("ledger_anchor_mismatch")
        if self.failing:
            out.append("anchor_check_failing")
        if self.missing():
            out.append("anchor_store_missing")
        try:
            since = datetime.fromisoformat((self.last_ok or self.first_seen).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            since = now
        if now - since > STALE_AFTER:
            out.append("ledger_anchor_stale")
        return out

    # --- the store ------------------------------------------------------------------------------------------------

    def repair(self) -> int:
        """A tail without its newline: kept (newline added) when it is a complete record whose MAC and ``prev`` link
        verify (round 3 N3-7); otherwise cut back to the last complete record. Returns the bytes removed."""
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return 0
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                return 0
            os.lseek(fd, 0, os.SEEK_SET)
            data = b""
            while len(data) < size:
                chunk = os.read(fd, size - len(data))
                if not chunk:
                    break
                data += chunk
            if data.endswith(b"\n"):
                return 0
            keep = data.rfind(b"\n") + 1
            if self._valid_tail(data[:keep], data[keep:]):
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, b"\n")
                os.fsync(fd)
                return 0
            os.ftruncate(fd, keep)
            os.fsync(fd)
            return size - keep
        finally:
            os.close(fd)

    def _valid_tail(self, head: bytes, tail: bytes) -> bool:
        try:
            rec = json.loads(tail)
            lines = [ln for ln in head.split(b"\n") if ln.strip()]
            prev = json.loads(lines[-1])["mac"] if lines else None
        except (ValueError, KeyError, TypeError):
            return False
        return (isinstance(rec, dict) and rec.get("prev") == prev
                and hmac.compare_digest(str(rec.get("mac")), self._mac(rec)))

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
        """Appends one record. AEGIS round 4 N4-4: every byte is written and fsynced, or the file is cut back to its
        size before the append and the run raises (never reported as stored)."""
        if self.state_invalid:
            raise StateInvalid("the anchor state file is invalid; run `ledger_anchor.py resync` after review")
        if self.missing():
            raise StoreMissing("the anchor store is missing, empty or shorter than the state file says")
        self.repair()
        recs = self.records()
        rec = {"anchor": anchor["anchor"], "entries": int(anchor["entries"]), "head_hash": anchor["head_hash"],
               "ledger_at": anchor.get("at"), "ledger_signature": anchor.get("signature"), "stored_at": at,
               "prev": recs[-1]["mac"] if recs else None}
        rec["mac"] = self._mac(rec)
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            before = os.fstat(fd).st_size
            try:
                _write_all(fd, line)
                os.fsync(fd)
                after = os.fstat(fd).st_size
                if after != before + len(line):
                    raise OSError(f"anchor record length {after - before} != {len(line)}")
            except OSError:
                os.ftruncate(fd, before)
                os.fsync(fd)
                raise
            self.size = after
        finally:
            os.close(fd)
        return rec

def check(store: AnchorStore, client, now_iso: str) -> tuple[Optional[str], Optional[dict]]:
    """One run: ``(problem, appended record)``. ``client`` has ``anchor(at=None) -> dict`` (raises when the ledger
    cannot be read: the caller retries later, that is not a mismatch)."""
    if store.state_invalid:
        raise StateInvalid("the anchor state file is invalid; run `ledger_anchor.py resync` after review")
    if store.missing():
        raise StoreMissing("the anchor store is missing, empty or shorter than the state file says")
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


def resync(data_dir: str, key: Optional[str]) -> dict:
    """AEGIS round 4 N4-5: the explicit, one-time rebuild of a signed state file from the store's own history (the
    migration of a pre-round state, or the repair after a restore). Never discards a record: a torn tail is cut back
    to the last valid record, every remaining record must verify, and the state is re-signed with ``size`` = the
    store's size. A mismatch recorded in an old state is kept. Run by an operator only:
    ``COMPLIANCE_LEDGER_ANCHOR_KEY=… python3 src/ledger_anchor.py resync <COMPLIANCE_DATA_DIR>``."""
    store = AnchorStore(os.path.join(data_dir, FILE), key)
    old: dict = {}
    try:
        with open(store.state_path, "rb") as fh:
            old = json.loads(fh.read()) or {}
    except (OSError, ValueError):
        old = {}
    store.repair()
    broken = store.verify()
    if broken:
        raise StateInvalid(f"cannot resync: {broken}")
    recs = store.records()
    store.state_invalid = False
    store.mismatch = old.get("mismatch") if isinstance(old, dict) else None
    store.failing = None
    store.last_ok = (old.get("last_ok") if isinstance(old, dict) else None) or (recs[-1]["stored_at"] if recs else None)
    store.first_seen = old.get("first_seen") if isinstance(old, dict) else None
    store.size = os.stat(store.path).st_size if recs else 0
    store.save_state()
    return {"records": len(recs), "size": store.size, "mismatch": bool(store.mismatch)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != "resync":
        sys.exit("usage: COMPLIANCE_LEDGER_ANCHOR_KEY=... python3 ledger_anchor.py resync <COMPLIANCE_DATA_DIR>")
    print(json.dumps(resync(sys.argv[2], os.environ.get("COMPLIANCE_LEDGER_ANCHOR_KEY"))))
