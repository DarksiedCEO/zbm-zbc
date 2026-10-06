"""
Cybersecurity (22) — the core (ADR 0012).

One lock guards all state. Every state change is one ``_commit``: the exact log line is prepared, fsynced aside
(pending line), anchored on the evidence ledger, appended to the local log, and only then applied to memory —
the same ``_apply`` that rebuilds state from the log at start, so live state and replayed state cannot diverge.
Every release of a secret value and every service credential issued is recorded on the ledger BEFORE it is
returned. Nothing is ever logged, recorded or returned in an error that carries a secret value.

Andre is the only human approver and approves only with a passkey (webauthn.py): an approval is a WebAuthn
assertion over a single-use challenge bound to the SHA-256 of exactly the action, target and body it approves.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Optional

import crypto
import tokens
import webauthn
from clock import Clock, SystemClock, iso, parse_iso
from config import KNOWN_CALLERS, Settings
from errors import ApprovalRefused, Conflict, Forbidden, Invalid, NotFound, Throttled, Unavailable
from ledger import DEPARTMENT, LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, derived_id, payload_sha256
from ports import AlertMessage, Ports
from reasons import R
from store import RecordLog, SealedStore, StoreCorrupt, StoreWriteError, verify_lines

INTERNAL = "cybersecurity"
DETECTOR = "sec22_detector"
CHALLENGE_TTL_S = 300
MAX_CHALLENGES = 64
INTEGRITY_RETRY_S = 15
FORCED_MIN_S = 10
EMERGENCY_ACTIONS = ("FREEZE", "LIFT_FREEZE")
SIGNING_KEY_ROTATE_DAYS = 30
SIGNING_KEY_OVERLAP_S = 24 * 3600
SLA_DAYS = {"critical": 7, "high": 30, "unknown": 30, "medium": 90, "low": 180}
SCAN_MAX_AGE_DAYS = 8
MAX_ACCEPT_DAYS = 90
CHANNELS_BY_SEVERITY = {"sev1": ("sms", "email", "push"), "sev2": ("sms", "push"), "sev3": ("email",), "sev4": ()}
# detection thresholds (ADR 0012 decision 27)
AUTH_FAIL_LIMIT, AUTH_FAIL_WINDOW_S = 20, 300
DENIED_LIMIT, DENIED_WINDOW_S = 3, 600
APPROVAL_FAIL_LIMIT, APPROVAL_FAIL_WINDOW_S = 3, 600
RECENT_ACCESS = 1000
ALL = "all"
JOBS = ("rotate-signing-key", "compliance-report", "rotation-due", "findings-due", "alerts-retry", "integrity")


def _maybe(exc: Unavailable) -> Unavailable:
    """Mark an outcome as unknown: the line is pending and may still take effect (round 2 N2)."""
    exc.maybe = True
    return exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_sha(body: dict) -> str:
    return payload_sha256({k: v for k, v in body.items() if k != "approval"})


def ref_of(owner: str, name: str) -> str:
    return f"vault:{owner}.{name}"


def parse_ref(raw: str) -> tuple[str, str]:
    import re
    s = raw[6:] if raw.startswith("vault:") else raw
    m = re.fullmatch(r"([a-z0-9_]{1,40})\.([A-Za-z0-9_][A-Za-z0-9._-]{0,79})", s or "")
    if not m:
        raise Invalid(R("INVALID"), field="ref")
    return m.group(1), m.group(2)


class Window:
    """Timestamps in a sliding window (in memory; a restart forgets them, which only delays a detection)."""

    def __init__(self, seconds: int, cap: int = 4096):
        self.seconds = seconds
        self.q: deque = deque(maxlen=cap)

    def hit(self, now: float) -> int:
        self.q.append(now)
        while self.q and self.q[0] <= now - self.seconds:
            self.q.popleft()
        return len(self.q)


class SecurityService:
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, sealed: SealedStore,
                 kms: crypto.KeyService, ports: Optional[Ports] = None, clock: Optional[Clock] = None):
        self.settings = settings
        self.rec = recorder
        self.log = log
        self.sealed = sealed
        self.kms = kms
        self.ports = ports or Ports.default()
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self.rp = settings.relying
        self.recovery_token_sha: Optional[str] = None
        # state rebuilt from the log
        self.secrets: dict[str, dict] = {}
        self.by_ref: dict[str, str] = {}
        self.generations: dict[tuple, int] = {}
        self.passkeys: dict[str, dict] = {}
        self.consumed_tokens: set[str] = set()
        self.freezes: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}
        self.incidents: dict[str, dict] = {}
        self.findings: dict[str, dict] = {}
        self.scans: dict[str, dict] = {}
        self.signing_keys: dict[str, dict] = {}
        self.reports: list[dict] = []
        self.requests: dict[tuple, tuple] = {}
        # memory only
        self.challenges: dict[str, dict] = {}
        self.alert_status: dict[str, dict] = {}
        self.recent: deque = deque(maxlen=RECENT_ACCESS)
        self.windows: dict[tuple, Window] = {}
        self.release_counts: dict[str, Window] = {}
        self._signing_cache: dict[str, bytes] = {}
        self.integrity = {"ok": False, "checked_at": None, "problem": "not yet verified against the ledger"}
        self._last_integrity_try = 0.0
        self._outbox: list[AlertMessage] = []
        self._unrecorded_alerted: set = set()
        self._em_key = os.urandom(32)
        self._em_used: dict[str, int] = {}
        self._deferred_incidents: list[tuple] = []
        self._own_pending: Optional[bytes] = None
        for r in self.log.iter_records():
            self._apply(r["kind"], r["data"], r["at"])
        if self.log.read_pending() is None and self.log.read_discarded() is None:
            self._remove_orphans()      # never while a pending line may still refer to a sealed file (round 2 N2)
        self.verify_integrity(force=True)

    # ================================================================================================ plumbing

    def now(self) -> datetime:
        return self.clock.now()

    def _window(self, key: tuple, seconds: int) -> Window:
        w = self.windows.get(key)
        if w is None:
            w = self.windows[key] = Window(seconds)
        return w

    def _anchor_ids(self, epoch: str, seq: int, line_sha: str) -> tuple[str, dict]:
        payload = {"epoch": epoch, "seq": seq, "line_sha256": line_sha}
        return derived_id("anc", epoch, seq, line_sha), payload

    def _commit(self, kind: str, data: dict, actor: str) -> dict:
        """Ledger anchor first, then the local log, then memory (fail closed at every step)."""
        with self.lock:
            if not self.integrity["ok"]:
                raise Unavailable(R("INTEGRITY_UNVERIFIED"))
            at = iso(self.now())
            data = {**data, "actor": data.get("actor", actor)}   # the anchor's actor is read back from the line
            actor = data["actor"]
            rec, line = self.log.prepare(kind, at, data)
            line_sha = sha256_hex(line)
            epoch = self.log.epoch or line_sha[:16]
            eid, payload = self._anchor_ids(epoch, rec["seq"], line_sha)
            self._own_pending = line                  # R4-1: only a line THIS process wrote is anchored by it
            try:
                self.log.write_pending(line)
            except StoreWriteError:
                # R3-1: a "certain" failure only when the pending file is certainly gone
                if not self._drop_pending():
                    raise _maybe(Unavailable(R("STORE_UNAVAILABLE"))) from None
                raise Unavailable(R("STORE_UNAVAILABLE")) from None
            try:
                self._anchor(eid, actor, epoch, payload, kind, rec["seq"])
            except LedgerRecordError as exc:
                if exc.took_effect is False and self._drop_pending():
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
                # AEGIS H1 / round 2 N1: the ledger may hold this anchor, or record it late. The pending line is
                # kept and writes stop; the next integrity check ROLLS IT FORWARD (re-records the identical anchor,
                # then appends). The outcome is "maybe": callers keep what the line refers to (round 2 N2).
                self.integrity = {"ok": False, "checked_at": at, "problem": "a ledger answer was lost; the "
                                  "pending line is rolled forward at the next integrity check"}
                raise _maybe(Unavailable(R("LEDGER_UNAVAILABLE"))) from None
            try:
                self.log.append_prepared(rec, line)
            except StoreWriteError:
                # anchored but not written: the pending line completes it at the next start; stop writing now
                self.integrity = {"ok": False, "checked_at": at, "problem": "a log line was anchored but not written; "
                                  "it is rolled forward at the next integrity check"}
                raise _maybe(Unavailable(R("STORE_UNAVAILABLE"))) from None
            self._drop_pending()    # if it fails, the stale copy is discarded at the next check (it is not the next line)
            self._apply(kind, data, at)
            return rec

    def _anchor(self, eid: str, actor: str, epoch: str, payload: dict, kind: str, seq: int) -> None:
        """Record the anchor; one retry of the SAME event when the answer was lost (the ledger is idempotent on
        identical content, so the retry either records it or confirms it is there)."""
        try:
            self.rec.record(eid, "log_anchor", actor, f"log:{epoch}", payload, f"{kind} #{seq}")
        except LedgerRecordError as exc:
            if exc.took_effect is False or isinstance(exc, LedgerConflict):
                raise
            self.rec.record(eid, "log_anchor", actor, f"log:{epoch}", payload, f"{kind} #{seq}")

    def _drop_pending(self) -> bool:
        """True when the pending line is certainly gone. Otherwise writes stop until the next integrity check."""
        try:
            self.log.clear_pending()
            self._own_pending = None
            return True
        except StoreWriteError:
            self.integrity = {"ok": False, "checked_at": iso(self.now()),
                              "problem": "the pending line could not be removed"}
            return False

    def _idem(self, actor: str, request_id: str, body: dict) -> Optional[tuple]:
        prev = self.requests.get((actor, request_id))
        if prev is None:
            return None
        if prev[0] != request_sha(body):
            raise Conflict(R("REQUEST_ID_REUSED"))
        return prev

    def _record_request(self, data: dict) -> None:
        if data.get("request_id") and data.get("actor"):
            self.requests[(data["actor"], data["request_id"])] = (data.get("request_sha"), data.get("_obj"))

    # ================================================================================================ replay

    def _apply(self, kind: str, d: dict, at: str) -> None:
        handler = getattr(self, f"_a_{kind}", None)
        if handler is None:
            raise StoreCorrupt(f"log record of unknown kind {kind!r}")
        handler(d, at)
        appr = d.get("approval")
        if appr and appr.get("credential_id") in self.passkeys:
            pk = self.passkeys[appr["credential_id"]]
            pk["sign_count"] = max(pk["sign_count"], appr["sign_count"])
            pk["last_used_at"] = at
        self._record_request(d)

    def _a_secret_stored(self, d, at):
        s = {k: d[k] for k in ("secret_id", "ref", "owner", "name", "generation", "kind", "encoding", "client_id",
                               "subject_refs", "readers", "purposes", "rotate_by")}
        s.update(status="active", created_at=at, updated_at=at,
                 versions={str(d["version"]): {"envelope_sha256": d["envelope_sha256"], "key_id": d["key_id"],
                                               "at": at}},
                 version=d["version"])
        self.secrets[d["secret_id"]] = s
        self.by_ref[d["ref"]] = d["secret_id"]
        self.generations[(d["owner"], d["name"])] = d["generation"]

    def _a_secret_rotated(self, d, at):
        s = self.secrets[d["secret_id"]]
        s["versions"] = {str(d["version"]): {"envelope_sha256": d["envelope_sha256"], "key_id": d["key_id"], "at": at}}
        s["version"] = d["version"]
        s["rotate_by"] = d.get("rotate_by")
        s["updated_at"] = at

    def _a_secret_destroyed(self, d, at):
        s = self.secrets[d["secret_id"]]
        s["status"] = "destroyed"
        s["versions"] = {}
        s["updated_at"] = at
        if self.by_ref.get(s["ref"]) == d["secret_id"]:
            del self.by_ref[s["ref"]]

    def _a_access_set(self, d, at):
        s = self.secrets[d["secret_id"]]
        s["readers"], s["purposes"], s["updated_at"] = d["readers"], d["purposes"], at

    def _a_passkey_enrolled(self, d, at):
        c = d["credential"]
        self.passkeys[c["credential_id"]] = {**c, "label": d["label"], "status": "active", "enrolled_at": at,
                                             "last_used_at": None}
        if d.get("token_sha256"):
            self.consumed_tokens.add(d["token_sha256"])

    def _a_passkey_revoked(self, d, at):
        self.passkeys[d["credential_id"]]["status"] = "revoked"

    def _a_passkey_suspended(self, d, at):
        self.passkeys[d["credential_id"]]["status"] = "suspended"

    def _a_passkeys_reset(self, d, at):
        for cid in d["revoked"]:
            self.passkeys[cid]["status"] = "revoked"
        self.recovery_token_sha = d["token_sha256"]

    def _a_freeze_applied(self, d, at):
        self.freezes[d["freeze_id"]] = {k: d[k] for k in ("freeze_id", "target_kind", "target_id", "reason_code",
                                                          "actor")}
        self.freezes[d["freeze_id"]].update(status="active", applied_at=at, lifted_at=None)

    def _a_freeze_lifted(self, d, at):
        f = self.freezes[d["freeze_id"]]
        f["status"], f["lifted_at"] = "lifted", at

    def _a_hold_recorded(self, d, at):
        self.holds[d["hold_id"]] = {"hold_id": d["hold_id"], "systems": d["systems"], "subject_refs": d["subject_refs"],
                                    "outcome": d["outcome"], "status": "active", "recorded_at": at,
                                    "released_at": None}

    def _a_hold_released(self, d, at):
        h = self.holds[d["hold_id"]]
        h["status"], h["released_at"] = "released", at

    def _a_incident_opened(self, d, at):
        self.incidents[d["incident_id"]] = {
            "incident_id": d["incident_id"], "severity": d["severity"], "code": d["code"], "subject": d["subject"],
            "status": "open", "opened_at": at, "opened_by": d["actor"], "closed_at": None, "root_cause_code": None,
            "timeline": [{"at": at, "event": "opened", "by": d["actor"]}]}

    def _a_incident_event(self, d, at):
        inc = self.incidents[d["incident_id"]]
        entry = {"at": at, "event": d["event"], "by": d["actor"]}
        if d.get("note"):
            entry["note"] = d["note"]
        inc["timeline"].append(entry)
        inc["timeline"] = inc["timeline"][-200:]

    def _a_incident_closed(self, d, at):
        inc = self.incidents[d["incident_id"]]
        inc.update(status="closed", closed_at=at, root_cause_code=d["root_cause_code"])
        inc["timeline"].append({"at": at, "event": "closed", "by": d["actor"], "note": d["note"]})

    def _a_scan_ingested(self, d, at):
        self.scans[d["source"]] = {"source": d["source"], "tool": d["tool"], "scanned_at": d["scanned_at"],
                                   "ingested_at": at, "scan_id": d["scan_id"], "open": len(d["seen"]) + len(d["opened"])}
        for f in d["opened"]:
            self.findings[f["finding_id"]] = {**f, "status": "open", "fixed_at": None, "accepted_until": None,
                                              "accept_reason": None}
        for fid in d["fixed"]:
            self.findings[fid].update(status="fixed", fixed_at=at)

    def _a_risk_accepted(self, d, at):
        f = self.findings[d["finding_id"]]
        f.update(status="accepted", accepted_until=d["until"], accept_reason=d["reason_code"])

    def _a_signing_key_created(self, d, at):
        for k in self.signing_keys.values():
            if k["status"] == "active":
                k.update(status="retiring", retiring_at=at)
        self.signing_keys[d["kid"]] = {"kid": d["kid"], "secret_id": d["secret_id"], "jwk": d["jwk"],
                                       "status": "active", "created_at": at, "retiring_at": None}

    def _a_signing_key_retired(self, d, at):
        self.signing_keys[d["kid"]]["status"] = "retired"
        self._signing_cache.pop(d["kid"], None)

    def _a_compliance_reported(self, d, at):
        self.reports.append({"at": at, "result": d["result"], "delivery": d["delivery"], "tested_at": d["tested_at"]})
        self.reports = self.reports[-50:]

    def _a_clean_exit_planned(self, d, at):
        pass                    # the plan lives in self.requests (its _obj), rebuilt by _record_request

    def _a_job_ran(self, d, at):
        pass

    def _a_approval_recorded(self, d, at):
        pass

    def _remove_orphans(self) -> None:
        """Remove sealed files no record refers to. A secret whose current version file is missing keeps every
        other version's file (manual recovery after tampering or disk loss)."""
        present = self.sealed.names()
        live = set()
        for s in self.secrets.values():
            names = {SealedStore.name(s["secret_id"], int(v)) for v in s["versions"]}
            live |= names
            if s["status"] == "active" and not names <= present:
                live |= {n for n in present if n.startswith(s["secret_id"] + ".v")}
        for name in present - live:
            sid, v = name.rsplit(".v", 1)
            self.sealed.delete(sid, int(v))

    # ================================================================================================ integrity

    def verify_integrity(self, force: bool = False, always: bool = False) -> dict:
        """Complete or discard the pending line, then check every local line's anchor on the ledger, and that
        the ledger holds no anchor this log lacks (a truncated, rolled back, deleted or replaced log)."""
        with self.lock:
            mono = time.monotonic()
            if not force and (self.integrity["ok"] or mono - self._last_integrity_try < INTEGRITY_RETRY_S):
                return dict(self.integrity)
            if force and not always and mono - self._last_integrity_try < FORCED_MIN_S and self._last_integrity_try:
                return dict(self.integrity)     # AEGIS L7: a full ledger read at most every FORCED_MIN_S
            self._last_integrity_try = mono
            at = iso(self.now())
            try:
                entries = self.rec.client.entries()
            except LedgerQueryFailed:
                self.integrity = {"ok": False, "checked_at": at, "problem": "the ledger cannot be read"}
                return self.integrity
            mine = [e for e in entries if e.get("department") == DEPARTMENT and e.get("event_type") == "log_anchor"]
            problem, rolled = self._settle_pending({e.get("event_id"): e for e in mine})
            if rolled and self.log.read_pending() is None and self.log.read_discarded() is None:
                self._remove_orphans()                      # e.g. the old version a rolled-forward rotation replaced
            if problem is None and rolled:
                try:
                    entries = self.rec.client.entries()     # the roll-forward may have just recorded an anchor
                except LedgerQueryFailed:
                    problem = "the ledger cannot be read"
            if problem is None:
                mine = [e for e in entries if e.get("department") == DEPARTMENT and e.get("event_type") == "log_anchor"]
                problem = self._anchor_problem(mine, {e.get("event_id"): e for e in mine})
            self.integrity = {"ok": problem is None, "checked_at": at, "problem": problem}
            if problem is None:
                self._unrecorded_alerted.clear()
                self._after_integrity()     # under the lock (no double reset or key); alerts are only QUEUED here
            return dict(self.integrity)

    def _settle_pending(self, by_id: dict) -> tuple[Optional[str], bool]:
        """Settle a line that was prepared but may not have reached the log. Returns (problem, appended).

        - A line THIS process wrote (``_own_pending``, kept in memory) is rolled forward: its identical anchor is
          re-recorded (the ledger answers 200 whether or not it already held it), then it is appended. The file
          copy is never what is trusted (AEGIS R4-1).
        - A line found on disk at start (this process did not write it) is appended only if the ledger ALREADY holds
          its anchor: the ledger vouches for it. Otherwise it is kept aside (``pending.discarded``), never anchored by
          us: a forged line stays inert. If its anchor appears later (it was in flight when the process stopped),
          the next check appends it.
        """
        own = self._own_pending
        if own is not None:
            problem, appended = self._roll_forward(own, by_id, rerecord=True)
            if problem is not None:
                return problem, False
            self._own_pending = None
            try:
                self.log.clear_pending()
            except StoreWriteError:
                return "the pending line could not be removed", appended
            return None, appended
        appended_any = False
        raw = self.log.read_pending()
        if raw is not None:
            problem, appended = self._roll_forward(raw, by_id, rerecord=False)
            if problem is not None and not problem.startswith("not vouched"):
                return problem, False
            try:
                if not appended:
                    self.log.write_discarded(raw)
                self.log.clear_pending()
            except StoreWriteError:
                return "the pending line could not be set aside", appended
            appended_any |= appended
        aside = self.log.read_discarded()
        if aside is not None:
            problem, appended = self._roll_forward(aside, by_id, rerecord=False)
            if appended or problem == "not vouched: stale":
                try:
                    self.log.clear_discarded()
                except StoreWriteError:
                    pass
            appended_any |= appended
        return None, appended_any

    def _roll_forward(self, raw: bytes, by_id: dict, rerecord: bool) -> tuple[Optional[str], bool]:
        try:
            rec = json.loads(raw)
            seq, kind, data = rec["seq"], rec["kind"], rec["data"]
            verify_lines(self._raw_lines() + [raw])        # the exact next line, chained to this log
            if not isinstance(data.get("actor"), str) or not re.fullmatch(r"[a-z0-9_]{1,64}", data["actor"]):
                raise ValueError("no ledger-valid actor")
        except (ValueError, KeyError, TypeError, AttributeError, StoreCorrupt):
            return "not vouched: stale", False
        line_sha = sha256_hex(raw)
        epoch = self.log.epoch or line_sha[:16]
        eid, payload = self._anchor_ids(epoch, seq, line_sha)
        if rerecord:
            try:
                self._anchor(eid, data["actor"], epoch, payload, kind, seq)
            except LedgerRecordError:
                return "the pending line could not be anchored yet (ledger unavailable); kept for the next check", \
                    False
        else:
            e = by_id.get(eid)
            if e is None or e.get("payload_sha256") != payload_sha256(payload) \
                    or e.get("subject_id") != f"log:{epoch}":
                return "not vouched: no anchor on the ledger", False
        try:
            self.log.append_prepared(rec, raw)
        except StoreWriteError:
            return "the pending line is anchored on the ledger but cannot be written here", False
        self._apply(kind, data, rec["at"])
        if not self._sealed_present(kind, data):
            # anchored lines are always rolled forward (discarding one would leave the ledger ahead of the log);
            # a sealed file that is missing here was removed from outside: tampering or disk loss (sev1). The
            # previous version's file is kept by _remove_orphans for a manual recovery.
            self._deferred_incidents.append(("sev1", "SEALED_SECRET_TAMPERED",
                                             self.secrets[data["secret_id"]]["ref"]))   # opened once verified
        return None, True

    def _sealed_present(self, kind: str, data: dict) -> bool:
        """A secret_stored / secret_rotated line is rolled forward only if its sealed file is here, intact."""
        if kind not in ("secret_stored", "secret_rotated"):
            return True
        raw = self.sealed.get(data["secret_id"], data["version"])
        return raw is not None and sha256_hex(raw) == data["envelope_sha256"]

    def _raw_lines(self) -> list[bytes]:
        return list(self.log._lines)

    def _anchor_problem(self, mine: list, by_id: dict) -> Optional[str]:
        shas = self.log.line_shas()
        epoch = self.log.epoch
        epochs = {e.get("subject_id") for e in mine}
        if epochs - ({f"log:{epoch}"} if epoch else set()):
            return "the ledger holds anchors of another security log: this log was deleted or replaced"
        for seq, line_sha in enumerate(shas, start=1):
            eid, payload = self._anchor_ids(epoch, seq, line_sha)
            e = by_id.get(eid)
            if e is None or e.get("payload_sha256") != payload_sha256(payload) or e.get("subject_id") != f"log:{epoch}":
                return f"local log line {seq} has no matching anchor on the ledger"
        if len(mine) > len(shas):
            return "the ledger holds anchors beyond the local log: the log was truncated or rolled back"
        return None

    def _gate(self) -> None:
        if not self.verify_integrity()["ok"]:
            raise Unavailable(R("INTEGRITY_UNVERIFIED"))

    def _after_integrity(self) -> None:
        """Duties that need a verified log: incidents found while settling, the passkey recovery reset, the first
        signing key."""
        pending, self._deferred_incidents = self._deferred_incidents, []
        for sev, code, subject in pending:
            self._open_incident(sev, code, subject, DETECTOR)
        try:
            if self.settings.passkey_recovery and self.settings.enroll_token is not None:
                tsha = sha256_hex(self.settings.enroll_token.reveal().encode("ascii"))
                if tsha not in self.consumed_tokens and self.recovery_token_sha != tsha:
                    active = sorted(c for c, p in self.passkeys.items() if p["status"] == "active")
                    self._commit("passkeys_reset", {"token_sha256": tsha, "revoked": active, "actor": "operator"},
                                 "operator")
                    self._open_incident("sev1", "PASSKEY_RECOVERY_STARTED", "passkeys", "operator")
            self._ensure_signing_key()
        except (Unavailable, crypto.KeyServiceUnavailable):
            pass

    # ================================================================================================ health

    def health(self) -> dict:
        with self.lock:
            active_pk = sum(1 for p in self.passkeys.values() if p["status"] == "active")
            return {
                "status": "ok" if self.integrity["ok"] else "degraded",
                "integrity": dict(self.integrity),
                "in_memory": self.log.in_memory,
                "non_production": self.settings.non_production,
                "key_service": self.kms.name,
                "vault_available": self.kms.healthy() and self.integrity["ok"],
                "passkeys_configured": self.rp.configured,
                "passkeys_active": active_pk,
                "passkey_warning": None if active_pk >= 2 else
                ("no passkey enrolled: nothing can be approved" if active_pk == 0 else
                 "only one passkey enrolled: enrol a second (a lost key means the recovery procedure)"),
                "lockdown": self._lockdown(),
                "alert_channels": {c: type(ch).__name__ != "NotWiredChannel" for c, ch in self.ports.channels.items()},
                "compliance_wired": type(self.ports.compliance).__name__ != "NotWiredCompliance",
                "preservation_systems_connected": sorted(self.ports.preservation),
                "open_incidents": sum(1 for i in self.incidents.values() if i["status"] == "open"),
                "signing_key": next((k["kid"] for k in self.signing_keys.values() if k["status"] == "active"), None),
            }

    # ================================================================================================ freezes

    def _lockdown(self) -> bool:
        return any(f["status"] == "active" and f["target_kind"] == "all" for f in self.freezes.values())

    def _frozen(self, kind: str, target: str) -> bool:
        return any(f["status"] == "active" and f["target_kind"] == kind and f["target_id"] == target
                   for f in self.freezes.values())

    def _check_caller(self, caller: str) -> None:
        if self._lockdown():
            raise Forbidden(R("LOCKDOWN"))
        if self._frozen("caller", caller):
            raise Forbidden(R("CALLER_FROZEN"))

    def _auto_freeze(self, kind: str, target: str, reason: str) -> None:
        if self._frozen(kind, target):
            return
        fid = derived_id("frz", kind, target, reason, iso(self.now()))
        try:
            self._commit("freeze_applied", {"freeze_id": fid, "target_kind": kind, "target_id": target,
                                            "reason_code": reason, "actor": DETECTOR}, DETECTOR)
        except Unavailable:
            pass

    def freeze(self, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"freeze|{body['request_id']}"
            prev = self._idem("andre", rk, body)
            if prev:
                return self.freezes[prev[1]]
            kind, target = body["target_kind"], body["target_id"]
            if kind == "caller" and target not in KNOWN_CALLERS:
                raise Invalid(R("FREEZE_TARGET"))
            if kind == "secret":
                owner, name = parse_ref(target)
                target = ref_of(owner, name)
                if target not in self.by_ref:
                    raise NotFound(R("SECRET_NOT_FOUND"))
            if kind == "all" and target != ALL:
                raise Invalid(R("FREEZE_TARGET"))
            appr = self._approve("FREEZE", f"{kind}:{target}", body)
            fid = derived_id("frz", "andre", body["request_id"])
            self._commit("freeze_applied", {"freeze_id": fid, "target_kind": kind, "target_id": target,
                                            "reason_code": body["reason_code"], "actor": "andre",
                                            "request_id": rk, "request_sha": request_sha(body),
                                            "_obj": fid, "approval": appr}, "andre")
            self._open_incident("sev2" if kind != "all" else "sev1", "FREEZE_APPLIED", f"{kind}:{target}"[:140],
                                "andre")
            out = dict(self.freezes[fid])
        self.flush_alerts()
        return out

    def lift(self, freeze_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"lift|{freeze_id}|{body['request_id']}"
            prev = self._idem("andre", rk, body)
            if prev:
                return self.freezes[prev[1]]
            f = self.freezes.get(freeze_id)
            if f is None:
                raise NotFound(R("FREEZE_NOT_FOUND"))
            if f["status"] != "active":
                raise Conflict(R("FREEZE_LIFTED"))
            appr = self._approve("LIFT_FREEZE", freeze_id, body)
            self._commit("freeze_lifted", {"freeze_id": freeze_id, "actor": "andre", "request_id": rk,
                                           "request_sha": request_sha(body), "_obj": freeze_id, "approval": appr},
                         "andre")
            return dict(self.freezes[freeze_id])

    def freezes_view(self) -> list[dict]:
        with self.lock:
            return [dict(f) for f in self.freezes.values()]

    def denylist(self) -> dict:
        with self.lock:
            return {"lockdown": self._lockdown(),
                    "callers": sorted({f["target_id"] for f in self.freezes.values()
                                       if f["status"] == "active" and f["target_kind"] == "caller"})}

    # ================================================================================================ approvals

    def _challenge(self, kind: str, action_sha: str, extra: Optional[dict] = None) -> tuple[str, bytes]:
        now = time.monotonic()
        for cid in [c for c, v in self.challenges.items() if v["expires"] <= now or v["used"]]:
            del self.challenges[cid]
        if len(self.challenges) >= MAX_CHALLENGES:
            self._open_incident("sev2", "APPROVAL_CHALLENGES_EXHAUSTED", "pool:general", DETECTOR)
            raise Throttled(R("APPROVAL_CHALLENGES_EXHAUSTED"))
        raw = os.urandom(32)
        cid = "ch-" + base64.urlsafe_b64encode(os.urandom(18)).decode("ascii")
        self.challenges[cid] = {"challenge": raw, "action_sha": action_sha, "kind": kind,
                                "expires": now + CHALLENGE_TTL_S, "used": False, **(extra or {})}
        return cid, raw

    # --- the freeze switch's challenges hold no server state (AEGIS round 3, R3-2): nothing to fill, nothing to
    #     evict. challenge = nonce(16) | expires(8) | HMAC(per-process key, action_sha | nonce | expires); single use
    #     is enforced by remembering only the challenges that APPROVED something, until they expire.

    def _em_mac(self, action_sha: str, nonce: bytes, expires: int) -> bytes:
        return hmac.new(self._em_key, b"sec22-em1|" + action_sha.encode("ascii") + nonce
                        + expires.to_bytes(8, "big"), hashlib.sha256).digest()

    def _em_challenge(self, action_sha: str) -> tuple[str, bytes]:
        nonce, expires = os.urandom(16), int(time.time()) + CHALLENGE_TTL_S
        raw = nonce + expires.to_bytes(8, "big") + self._em_mac(action_sha, nonce, expires)
        return "em-" + webauthn.b64url_encode(raw), raw

    def _em_check(self, cid: str, action_sha: str) -> tuple[Optional[str], Optional[bytes]]:
        try:
            raw = webauthn.b64url_decode(cid[3:], 56, "CHALLENGE")
        except webauthn.WebAuthnError:
            return "APPROVAL_CHALLENGE_UNKNOWN", None
        if len(raw) != 56:
            return "APPROVAL_CHALLENGE_UNKNOWN", None
        nonce, expires, mac = raw[:16], int.from_bytes(raw[16:24], "big"), raw[24:]
        if not hmac.compare_digest(mac, self._em_mac(action_sha, nonce, expires)):
            return "APPROVAL_ACTION_MISMATCH", None      # forged, or issued for another action/body
        now = int(time.time())
        for k in [k for k, exp in self._em_used.items() if exp <= now]:
            del self._em_used[k]
        if expires <= now:
            return "APPROVAL_CHALLENGE_EXPIRED", None
        if cid in self._em_used:
            return "APPROVAL_CHALLENGE_USED", None
        return None, raw

    @staticmethod
    def action_sha(action: str, target: str, body: dict) -> str:
        return payload_sha256({"action": action, "target": target,
                               "body": {k: v for k, v in body.items() if k != "approval"}})

    def approval_challenge(self, action: str, target: str, body: dict) -> dict:
        with self.lock:
            if not self.rp.configured:
                raise Unavailable(R("PASSKEYS_NOT_CONFIGURED"))
            sha = self.action_sha(action, target, body)
            if action in EMERGENCY_ACTIONS:
                cid, raw = self._em_challenge(sha)
            else:
                cid, raw = self._challenge("get", sha)
            creds = [c for c, p in self.passkeys.items() if p["status"] == "active"]
            return {"challenge_id": cid, "challenge": webauthn.b64url_encode(raw), "rp_id": self.rp.rp_id,
                    "allow_credentials": creds, "user_verification": "required",
                    "timeout_ms": CHALLENGE_TTL_S * 1000}

    def _approval_failed(self, code: str) -> None:
        self.rec.try_record(derived_id("apf", code, iso(self.now()), os.urandom(8).hex()), "approval_refused",
                            "andre", "approvals", {"code": code}, f"approval refused: {code}")
        if self._window(("approval_fail",), APPROVAL_FAIL_WINDOW_S).hit(time.monotonic()) >= APPROVAL_FAIL_LIMIT:
            self._open_incident("sev2", "APPROVAL_FAILURES", "approvals", DETECTOR)

    def _approve(self, action: str, target: str, body: dict) -> dict:
        """Verify Andre's passkey over exactly this action. Returns the approval record for the commit."""
        appr = body.get("approval")
        if not appr:
            raise ApprovalRefused(R("APPROVAL_REQUIRED"))
        if not self.rp.configured:
            raise ApprovalRefused(R("PASSKEYS_NOT_CONFIGURED"))
        if appr["challenge_id"].startswith("em-"):
            if action not in EMERGENCY_ACTIONS:
                code, raw = "APPROVAL_CHALLENGE_UNKNOWN", None
            else:
                code, raw = self._em_check(appr["challenge_id"], self.action_sha(action, target, body))
            if code is None:     # R4-3: a MAC-valid attempt uses the challenge up, whatever happens next
                self._em_used[appr["challenge_id"]] = int(time.time()) + CHALLENGE_TTL_S + 60
            ch = None if code else {"challenge": raw, "kind": "get", "used": False, "expires": float("inf"),
                                    "action_sha": self.action_sha(action, target, body), "em": appr["challenge_id"]}
            if code:
                self._approval_failed(code)
                raise ApprovalRefused(R(code))
        else:
            ch = self.challenges.get(appr["challenge_id"])
        code = None
        was_used = bool(ch and ch["used"])
        if ch is not None and ch["kind"] == "get":
            ch["used"] = True       # single use, whatever happens next (AEGIS L3: a mismatch burns it too)
        if ch is None or ch["kind"] != "get":
            code = "APPROVAL_CHALLENGE_UNKNOWN"
        elif was_used:
            code = "APPROVAL_CHALLENGE_USED"
        elif ch["expires"] <= time.monotonic():
            code = "APPROVAL_CHALLENGE_EXPIRED"
        elif ch["action_sha"] != self.action_sha(action, target, body):
            code = "APPROVAL_ACTION_MISMATCH"
        if code is None:
            pk = self.passkeys.get(appr["credential_id"])
            if pk is None or pk["status"] == "revoked":
                code = "APPROVAL_CREDENTIAL_UNKNOWN"
            elif pk["status"] == "suspended":
                code = "PASSKEY_SUSPENDED"
            else:
                cred = webauthn.Credential(pk["credential_id"], pk["alg"], pk["public_key_spki"], pk["sign_count"],
                                           pk["aaguid"], pk["backup_eligible"])
                try:
                    count = webauthn.verify_assertion(cred, appr["client_data_json"], appr["authenticator_data"],
                                                      appr["signature"], ch["challenge"], self.rp)
                    pk["sign_count"] = max(pk["sign_count"], count)   # AEGIS L9: even if the route fails later
                    return {"credential_id": pk["credential_id"], "sign_count": count,
                            "challenge_id": appr["challenge_id"]}
                except webauthn.CounterRegression:
                    code = "PASSKEY_COUNTER_REGRESSION"
                    try:
                        self._commit("passkey_suspended", {"credential_id": pk["credential_id"],
                                                           "reason": code, "actor": DETECTOR}, DETECTOR)
                    except Unavailable:
                        pass
                    self._open_incident("sev1", "PASSKEY_CLONE_SUSPECTED", "passkey:" + pk["credential_id"][:60],
                                        DETECTOR)
                except webauthn.WebAuthnError as exc:
                    code = exc.code
        self._approval_failed(code)
        raise ApprovalRefused(R(code))

    # ================================================================================================ passkeys

    def _active_passkeys(self) -> list[str]:
        return [c for c, p in self.passkeys.items() if p["status"] == "active"]

    def enroll_options(self, body: dict) -> dict:
        with self.lock:
            self._gate()
            if not self.rp.configured:
                raise Unavailable(R("PASSKEYS_NOT_CONFIGURED"))
            via, token_sha = "approval", None
            if not self._active_passkeys():
                tok = body.get("enroll_token")
                configured = self.settings.enroll_token
                ok = False
                if tok and configured is not None:
                    import hmac
                    try:
                        ok = hmac.compare_digest(tok, configured.reveal())
                    except TypeError:
                        ok = False
                    token_sha = sha256_hex(configured.reveal().encode("ascii"))
                if not ok or token_sha in self.consumed_tokens:
                    self._approval_failed("ENROLL_TOKEN_REFUSED")
                    raise ApprovalRefused(R("ENROLL_TOKEN_REFUSED"))
                via = "enroll_token"
            else:
                if body.get("enroll_token"):
                    raise ApprovalRefused(R("ENROLL_NEEDS_APPROVAL"))
                appr = self._approve("PASSKEY_ENROLL", "", body)
                self._commit("approval_recorded", {"action": "PASSKEY_ENROLL", "actor": "andre", "approval": appr},
                             "andre")
            cid, raw = self._challenge("create", "", {"via": via, "token_sha": token_sha})
            user_id = webauthn.b64url_encode(hashlib.sha256(b"zbm-sec22-andre").digest()[:16])
            return {"challenge_id": cid, "challenge": webauthn.b64url_encode(raw),
                    "rp": {"id": self.rp.rp_id, "name": "Z Best Media security"},
                    "user": {"id": user_id, "name": "andre", "displayName": "Andre"},
                    "pub_key_cred_params": [{"type": "public-key", "alg": a} for a in webauthn.ALGS],
                    "authenticator_selection": {"user_verification": "required", "resident_key": "preferred"},
                    "attestation": "none", "exclude_credentials": self._active_passkeys(),
                    "timeout_ms": CHALLENGE_TTL_S * 1000}

    def enroll(self, body: dict) -> dict:
        with self.lock:
            self._gate()
            ch = self.challenges.get(body["challenge_id"])
            if ch is None or ch["kind"] != "create":
                raise ApprovalRefused(R("APPROVAL_CHALLENGE_UNKNOWN"))
            if ch["used"]:
                raise ApprovalRefused(R("APPROVAL_CHALLENGE_USED"))
            if ch["expires"] <= time.monotonic():
                raise ApprovalRefused(R("APPROVAL_CHALLENGE_EXPIRED"))
            ch["used"] = True
            if ch["via"] == "enroll_token" and (self._active_passkeys() or ch["token_sha"] in self.consumed_tokens):
                raise ApprovalRefused(R("ENROLL_TOKEN_REFUSED"))
            try:
                cred = webauthn.verify_registration(body["attestation_object"], body["client_data_json"],
                                                    ch["challenge"], self.rp)
            except webauthn.WebAuthnError as exc:
                self._approval_failed(exc.code)
                raise ApprovalRefused(R(exc.code)) from None
            if cred.credential_id in self.passkeys:
                raise Conflict(R("PASSKEY_ALREADY_ENROLLED"))
            c = {"credential_id": cred.credential_id, "alg": cred.alg, "public_key_spki": cred.public_key_spki,
                 "sign_count": cred.sign_count, "aaguid": cred.aaguid, "backup_eligible": cred.backup_eligible}
            self._commit("passkey_enrolled", {"credential": c, "label": body["label"], "via": ch["via"],
                                              "token_sha256": ch["token_sha"], "actor": "andre"}, "andre")
            self._open_incident("sev3", "PASSKEY_ENROLLED", "passkey:" + cred.credential_id[:60], "andre")
            out = self._passkey_view(self.passkeys[cred.credential_id])
        self.flush_alerts()
        return out

    @staticmethod
    def _passkey_view(p: dict) -> dict:
        return {k: p[k] for k in ("credential_id", "label", "status", "alg", "aaguid", "backup_eligible", "enrolled_at",
                                  "last_used_at")}

    def passkeys_view(self) -> list[dict]:
        with self.lock:
            return [self._passkey_view(p) for p in self.passkeys.values()]

    def revoke_passkey(self, credential_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"revoke|{credential_id}|{body['request_id']}"
            prev = self._idem("andre", rk, body)
            if prev:
                return self._passkey_view(self.passkeys[prev[1]])
            pk = self.passkeys.get(credential_id)
            if pk is None or pk["status"] == "revoked":
                raise NotFound(R("PASSKEY_NOT_FOUND"))
            if self._active_passkeys() == [credential_id]:
                raise Conflict(R("LAST_PASSKEY"))
            appr = self._approve("PASSKEY_REVOKE", credential_id, body)
            self._commit("passkey_revoked", {"credential_id": credential_id, "actor": "andre",
                                             "request_id": rk, "request_sha": request_sha(body),
                                             "_obj": credential_id, "approval": appr}, "andre")
            self._open_incident("sev3", "PASSKEY_REVOKED", "passkey:" + credential_id[:60], "andre")
            out = self._passkey_view(self.passkeys[credential_id])
        self.flush_alerts()
        return out

    # ================================================================================================ vault

    def _value_bytes(self, value: str, encoding: str) -> bytes:
        if encoding == "utf8":
            return value.encode("utf-8")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise Invalid(R("VALUE_ENCODING")) from None
        if not raw:
            raise Invalid(R("VALUE_ENCODING"))
        return raw

    def _seal(self, secret_id: str, version: int, owner: str, kind: str, value: bytes) -> dict:
        ctx = {"secret_id": secret_id, "version": str(version), "owner": owner, "kind": kind}
        try:
            env = crypto.seal(self.kms, value, ctx)
            raw = env.to_bytes()
            self.sealed.put(secret_id, version, raw)
        except crypto.KeyServiceUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        except StoreWriteError:
            raise Unavailable(R("STORE_UNAVAILABLE")) from None
        return {"envelope_sha256": sha256_hex(raw), "key_id": env.key_id}

    def _open(self, s: dict) -> bytes:
        v = s["version"]
        raw = self.sealed.get(s["secret_id"], v)
        meta = s["versions"].get(str(v))
        if raw is None or meta is None or sha256_hex(raw) != meta["envelope_sha256"]:
            self._open_incident("sev1", "SEALED_SECRET_TAMPERED", s["ref"], DETECTOR)
            raise Unavailable(R("SEAL_BROKEN"))
        ctx = {"secret_id": s["secret_id"], "version": str(v), "owner": s["owner"], "kind": s["kind"]}
        try:
            return crypto.open_sealed(self.kms, crypto.Envelope.from_bytes(raw), ctx)
        except crypto.KeyServiceUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        except crypto.SealBroken:
            self._open_incident("sev1", "SEALED_SECRET_TAMPERED", s["ref"], DETECTOR)
            raise Unavailable(R("SEAL_BROKEN")) from None

    def _view(self, s: dict) -> dict:
        return {"ref": s["ref"], "owner": s["owner"], "name": s["name"], "kind": s["kind"], "status": s["status"],
                "version": s["version"], "readers": list(s["readers"]), "purposes": list(s["purposes"]),
                "client_id": s["client_id"], "subject_refs": list(s["subject_refs"]), "rotate_by": s["rotate_by"],
                "frozen": self._frozen("secret", s["ref"]), "created_at": s["created_at"],
                "updated_at": s["updated_at"]}

    def _new_secret(self, actor: str, owner: str, body: dict, value: bytes, encoding: str, readers: list,
                    purposes: list, approval: Optional[dict], rk: Optional[str] = None) -> dict:
        name = body["name"]
        ref = ref_of(owner, name)
        if ref in self.by_ref:
            raise Conflict(R("SECRET_EXISTS"))
        generation = self.generations.get((owner, name), 0) + 1
        sid = derived_id("sec", owner, name, generation)
        sealed = self._seal(sid, 1, owner, body["kind"], value)
        data = {"secret_id": sid, "ref": ref, "owner": owner, "name": name, "generation": generation,
                "kind": body["kind"], "encoding": encoding, "client_id": body.get("client_id"),
                "subject_refs": sorted(set(body.get("subject_refs") or [])), "readers": sorted(set(readers)),
                "purposes": sorted(set(purposes)), "rotate_by": body.get("rotate_by"), "version": 1, **sealed,
                "actor": actor, "request_id": rk or body["request_id"], "request_sha": request_sha(body), "_obj": sid}
        if approval:
            data["approval"] = approval
        try:
            self._commit("secret_stored", data, actor)
        except Unavailable as exc:
            if not getattr(exc, "maybe", False):      # certainly not recorded: the sealed file is an orphan now
                self.sealed.delete(sid, 1)
            raise
        return self._view(self.secrets[sid])

    def store(self, caller: str, body: dict) -> dict:
        """A department stores a secret it owns. It may make itself the only reader, or no one (write-only, the
        onboarding pattern); only Andre grants another department."""
        with self.lock:
            self._gate()
            self._check_caller(caller)
            rk = f"store|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self._view(self.secrets[prev[1]])
            readers = body.get("readers") or []
            if any(r != caller for r in readers):
                raise Forbidden(R("READERS_NOT_ALLOWED"))
            if readers and not body.get("purposes"):
                raise Invalid(R("PURPOSE_NOT_ALLOWED"))
            value = self._value_bytes(body["value"], body.get("encoding", "utf8"))
            return self._new_secret(caller, caller, body, value, body.get("encoding", "utf8"), readers,
                                    body.get("purposes") or [], None, rk)

    def andre_store(self, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"andre_store|{body['request_id']}"
            prev = self._idem("andre", rk, body)
            if prev:
                return self._view(self.secrets[prev[1]])
            kind = body["kind"]
            generate = body.get("generate") or kind in ("hmac_key", "canary")
            if generate and body.get("value") is not None:
                raise Invalid(R("VALUE_NOT_ALLOWED"))
            if not generate and body.get("value") is None:
                raise Invalid(R("VALUE_REQUIRED"))
            readers = [] if kind == "canary" else list(body.get("readers") or [])
            if body["owner"] == INTERNAL and readers:
                raise Invalid(R("READERS_NOT_ALLOWED"))
            if readers and not body.get("purposes"):
                raise Invalid(R("PURPOSE_NOT_ALLOWED"))
            target = ref_of(body["owner"], body["name"])
            appr = self._approve("SECRET_STORE", target, body)
            if generate:
                value, encoding = os.urandom(32), "base64"
            else:
                encoding = body.get("encoding", "utf8")
                value = self._value_bytes(body["value"], encoding)
            out = self._new_secret("andre", body["owner"], body, value, encoding, readers,
                                   body.get("purposes") or [], appr, rk)
        self.flush_alerts()
        return out

    def _find(self, raw_ref: str) -> dict:
        owner, name = parse_ref(raw_ref)
        sid = self.by_ref.get(ref_of(owner, name))
        if sid is None:
            raise NotFound(R("SECRET_NOT_FOUND"))
        return self.secrets[sid]

    def _visible(self, caller: str, s: dict) -> bool:
        return caller in ("dashboard",) or caller == s["owner"] or caller in s["readers"]

    def status(self, caller: str, raw_ref: str) -> dict:
        try:
            with self.lock:
                if caller != "dashboard":
                    self._check_caller(caller)
                s = self._find(raw_ref)
                if not self._visible(caller, s):
                    raise NotFound(R("SECRET_NOT_FOUND"))     # never confirm a secret exists to a stranger
                if caller != "dashboard" and s["kind"] == "canary":
                    self._canary_touched(caller)          # AEGIS M2: a department looking at a canary is D3
                return self._view(s)
        finally:
            self.flush_alerts()

    def _canary_touched(self, caller: str) -> None:
        self._auto_freeze("caller", caller, "CANARY_TOUCHED")
        self._open_incident("sev1", "CANARY_TOUCHED", f"caller:{caller}", DETECTOR)
        raise NotFound(R("SECRET_NOT_FOUND"))

    def list_secrets(self) -> list[dict]:
        with self.lock:
            return [self._view(s) for s in self.secrets.values() if s["owner"] != INTERNAL]

    def _deny(self, caller: str, s: Optional[dict], code: str) -> None:
        """A recognised caller asked for something it may not have: counted; repeated -> frozen (D2)."""
        self.rec.try_record(derived_id("dny", caller, code, iso(self.now()), os.urandom(8).hex()), "access_denied",
                            caller, (s or {}).get("secret_id", "vault")[:128], {"code": code},
                            f"{caller} refused: {code}")
        if self._window(("denied", caller), DENIED_WINDOW_S).hit(time.monotonic()) >= DENIED_LIMIT:
            self._auto_freeze("caller", caller, "ACCESS_DENIED_REPEATED")
            self._open_incident("sev2", "ACCESS_DENIED_REPEATED", f"caller:{caller}", DETECTOR)

    def use(self, caller: str, raw_ref: str, purpose: str) -> dict:
        """Release a secret value to a reader, for a declared purpose. Recorded on the ledger first."""
        try:
            with self.lock:
                self._gate()
                self._check_caller(caller)
                try:
                    s = self._find(raw_ref)
                except NotFound:
                    self._deny(caller, None, "SECRET_NOT_FOUND")
                    raise
                if s["kind"] == "canary":
                    self._canary_touched(caller)
                if s["owner"] == INTERNAL or caller not in s["readers"]:
                    self._deny(caller, s, "NOT_A_READER")
                    raise NotFound(R("SECRET_NOT_FOUND"))
                if purpose not in s["purposes"]:
                    self._deny(caller, s, "PURPOSE_NOT_ALLOWED")
                    raise Forbidden(R("PURPOSE_NOT_ALLOWED"))
                if self._frozen("secret", s["ref"]):
                    raise Forbidden(R("SECRET_FROZEN"))
                w = self.release_counts.setdefault(caller, Window(60))
                if w.hit(time.monotonic()) > self.settings.release_rate_per_min:
                    self._open_incident("sev3", "RELEASE_RATE_EXCEEDED", f"caller:{caller}", DETECTOR)
                    raise Throttled(R("RELEASE_RATE_EXCEEDED"))
                value = self._open(s)
                at = iso(self.now())
                eid = derived_id("rel", caller, s["secret_id"], s["version"], at, os.urandom(8).hex())
                try:
                    self.rec.record(eid, "secret_released", caller, s["secret_id"],
                                    {"ref": s["ref"], "version": s["version"], "purpose": purpose, "at": at},
                                    f"{caller} used {s['ref']} v{s['version']} for {purpose}")
                except LedgerRecordError:
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
                self.recent.append({"at": at, "caller": caller, "ref": s["ref"], "version": s["version"],
                                    "purpose": purpose, "ledger_event_id": eid})
                out = {"ref": s["ref"], "version": s["version"], "kind": s["kind"], "ledger_event_id": eid}
                if s["encoding"] == "utf8":
                    out["value"] = value.decode("utf-8")
                else:
                    out["value_b64"] = base64.b64encode(value).decode("ascii")
                return out
        finally:
            self.flush_alerts()

    def rotate(self, actor: str, raw_ref: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            if actor != "andre":
                self._check_caller(actor)
            rk = f"rotate|{ref_of(*parse_ref(raw_ref))}|{body['request_id']}"
            prev = self._idem(actor, rk, body)
            if prev:
                return self._view(self.secrets[prev[1]])
            s = self._find(raw_ref)
            if actor != "andre" and actor != s["owner"]:
                raise NotFound(R("SECRET_NOT_FOUND"))
            if s["kind"] == "canary":
                if actor != "andre":
                    self._canary_touched(actor)
                raise Invalid(R("INVALID"))
            if self._held(s):
                raise Conflict(R("PRESERVATION_HOLD"))    # AEGIS M6: rotating would destroy the preserved version
            generate = body.get("generate") or s["kind"] == "hmac_key"
            if generate and s["encoding"] != "base64":
                raise Invalid(R("VALUE_ENCODING"))
            if generate and body.get("value") is not None:
                raise Invalid(R("VALUE_NOT_ALLOWED"))
            if not generate and body.get("value") is None:
                raise Invalid(R("VALUE_REQUIRED"))
            appr = self._approve("SECRET_ROTATE", s["ref"], body) if actor == "andre" else None
            if generate:
                value = os.urandom(32)
            else:
                if body.get("encoding", "utf8") != s["encoding"]:
                    raise Invalid(R("VALUE_ENCODING"))
                value = self._value_bytes(body["value"], s["encoding"])
            old, new = s["version"], s["version"] + 1
            sealed = self._seal(s["secret_id"], new, s["owner"], s["kind"], value)
            data = {"secret_id": s["secret_id"], "version": new, "prev_version": old, **sealed,
                    "rotate_by": body.get("rotate_by"), "actor": actor, "request_id": rk,
                    "request_sha": request_sha(body), "_obj": s["secret_id"]}
            if appr:
                data["approval"] = appr
            try:
                self._commit("secret_rotated", data, actor)
            except Unavailable as exc:
                if not getattr(exc, "maybe", False):
                    self.sealed.delete(s["secret_id"], new)
                raise
            try:
                self.sealed.delete(s["secret_id"], old)
            except StoreWriteError:
                pass        # the rotation stands; the old file is an orphan removed at the next start
            return self._view(s)

    def _held(self, s: dict) -> bool:
        held = set()
        for h in self.holds.values():
            if h["status"] == "active":
                held.update(h["subject_refs"])
        keys = set(s["subject_refs"]) | ({f"client:{s['client_id']}"} if s["client_id"] else set())
        return bool(keys & held)

    def _destroy(self, actor: str, s: dict, body: dict, appr: Optional[dict], rk: Optional[str] = None) -> None:
        versions = sorted(int(v) for v in s["versions"])
        data = {"secret_id": s["secret_id"], "versions": versions, "actor": actor, "request_id": rk or body["request_id"],
                "request_sha": request_sha(body), "_obj": s["secret_id"]}
        if appr:
            data["approval"] = appr
        self._commit("secret_destroyed", data, actor)
        for v in versions:
            try:
                self.sealed.delete(s["secret_id"], v)
            except StoreWriteError:
                pass     # the record stands; the orphan is removed at the next start

    def destroy(self, actor: str, raw_ref: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            if actor != "andre":
                self._check_caller(actor)
            rk = f"destroy|{ref_of(*parse_ref(raw_ref))}|{body['request_id']}"
            prev = self._idem(actor, rk, body)
            if prev:
                return self._view(self.secrets[prev[1]])
            s = self._find(raw_ref)
            if actor != "andre" and actor != s["owner"]:
                raise NotFound(R("SECRET_NOT_FOUND"))
            if actor != "andre" and s["kind"] == "canary":
                self._canary_touched(actor)
            if self._held(s):
                raise Conflict(R("PRESERVATION_HOLD"))
            appr = self._approve("SECRET_DESTROY", s["ref"], body) if actor == "andre" else None
            self._destroy(actor, s, body, appr, rk)
            out = self._view(s)
        self.flush_alerts()
        return out

    def destroy_client(self, caller: str, client_id: str, body: dict) -> dict:
        """Onboarding's clean exit: every secret of this client that the caller owns."""
        with self.lock:
            self._gate()
            self._check_caller(caller)
            # round 2 N5: the first execution records WHICH secrets this request covers; a replay of the same
            # request finishes those and never reaches a secret stored afterwards
            rk = f"clean_exit|{client_id}|{body['request_id']}"
            if (caller, rk) not in self.requests:
                ids = sorted(s["secret_id"] for s in self.secrets.values()
                             if s["owner"] == caller and s["client_id"] == client_id and s["status"] == "active"
                             and s["kind"] != "canary")
                self._commit("clean_exit_planned", {"client_id": client_id, "secret_ids": ids, "actor": caller,
                                                    "request_id": rk, "request_sha": request_sha(body),
                                                    "_obj": ids}, caller)
            elif self.requests[(caller, rk)][0] != request_sha(body):
                raise Conflict(R("REQUEST_ID_REUSED"))
            planned = self.requests[(caller, rk)][1]
            targets = [self.secrets[i] for i in planned if self.secrets[i]["status"] == "active"]
            held = 0
            for s in sorted(targets, key=lambda x: x["ref"]):
                if s["kind"] == "canary":
                    continue
                if self._held(s):
                    held += 1
                    continue
                # keyed by the secret, not its position: a retry after a partial failure finishes the rest (AEGIS M1)
                sub = {"request_id": body["request_id"], "client_id": client_id, "secret_id": s["secret_id"]}
                self._destroy(caller, s, sub, None, f"destroy_client|{client_id}|{body['request_id']}|{s['secret_id']}")
            destroyed = sum(1 for i in planned if self.secrets[i]["status"] == "destroyed")
            return {"client_id": client_id, "destroyed": destroyed, "held": held}

    def set_access(self, raw_ref: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"access|{ref_of(*parse_ref(raw_ref))}|{body['request_id']}"
            prev = self._idem("andre", rk, body)
            if prev:
                return self._view(self.secrets[prev[1]])
            s = self._find(raw_ref)
            if s["owner"] == INTERNAL or s["kind"] == "canary":
                raise Invalid(R("READERS_NOT_ALLOWED"))
            if body["readers"] and not body["purposes"]:
                raise Invalid(R("PURPOSE_NOT_ALLOWED"))
            appr = self._approve("SECRET_ACCESS", s["ref"], body)
            self._commit("access_set", {"secret_id": s["secret_id"], "readers": sorted(set(body["readers"])),
                                        "purposes": sorted(set(body["purposes"])), "actor": "andre",
                                        "request_id": rk, "request_sha": request_sha(body),
                                        "_obj": s["secret_id"], "approval": appr}, "andre")
            return self._view(s)

    def recent_access(self) -> list[dict]:
        with self.lock:
            return list(self.recent)

    # ================================================================================================ identity

    def _ensure_signing_key(self) -> None:
        if any(k["status"] == "active" for k in self.signing_keys.values()) or not self.kms.healthy():
            return
        self._new_signing_key()

    def _new_signing_key(self) -> str:
        priv = tokens.new_private_key()
        kid = tokens.kid_for(priv)
        name = f"signing-key-{kid}"
        body = {"request_id": f"signing-key:{kid}", "name": name, "kind": "signing_key"}
        self._new_secret(INTERNAL, INTERNAL, body, priv, "base64", [], [], None)
        sid = self.by_ref[ref_of(INTERNAL, name)]
        self._commit("signing_key_created", {"kid": kid, "secret_id": sid, "jwk": tokens.public_jwk(kid, priv),
                                             "actor": INTERNAL}, INTERNAL)
        self._signing_cache[kid] = priv
        return kid

    def jwks(self) -> dict:
        with self.lock:
            return {"keys": [k["jwk"] for k in self.signing_keys.values() if k["status"] in ("active", "retiring")]}

    def mint(self, caller: str, audience: str, scope: list) -> dict:
        with self.lock:
            self._gate()
            self._check_caller(caller)
            if audience == caller:
                raise Invalid(R("AUDIENCE_REFUSED"))
            if scope:
                raise Invalid(R("SCOPE_NOT_ALLOWED"))    # AEGIS L6: no scope registry yet; a caller names none
            key = next((k for k in self.signing_keys.values() if k["status"] == "active"), None)
            if key is None:
                raise Unavailable(R("VAULT_UNAVAILABLE"))
            priv = self._signing_cache.get(key["kid"])
            if priv is None:
                priv = self._open(self.secrets[key["secret_id"]])
                self._signing_cache[key["kid"]] = priv
            iat = int(self.now().timestamp())
            jti = webauthn.b64url_encode(os.urandom(18))
            tok = tokens.mint(key["kid"], priv, caller, audience, tuple(sorted(set(scope))), iat,
                              self.settings.token_ttl_s, jti)
            try:
                self.rec.record(derived_id("tok", caller, jti), "credential_issued", caller, f"jti:{jti}",
                                {"aud": audience, "kid": key["kid"], "exp": iat + self.settings.token_ttl_s},
                                f"{caller} credential for {audience}")
            except LedgerRecordError:
                raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
            return {"token": tok, "kid": key["kid"], "expires_at": iat + self.settings.token_ttl_s,
                    "audience": audience}

    def _rotate_signing_key(self) -> dict:
        now = self.now()
        active = next((k for k in self.signing_keys.values() if k["status"] == "active"), None)
        done = {"created": None, "retired": []}
        if active is None or now - parse_iso(active["created_at"]) >= timedelta(days=SIGNING_KEY_ROTATE_DAYS):
            if self.kms.healthy():
                done["created"] = self._new_signing_key()
        for k in list(self.signing_keys.values()):
            if k["status"] == "retiring" and now - parse_iso(k["retiring_at"]) >= timedelta(seconds=SIGNING_KEY_OVERLAP_S):
                self._commit("signing_key_retired", {"kid": k["kid"], "actor": INTERNAL}, INTERNAL)
                s = self.secrets[k["secret_id"]]
                if s["status"] == "active":
                    self._destroy(INTERNAL, s, {"request_id": f"retire:{k['kid']}"}, None)
                done["retired"].append(k["kid"])
        return done

    # ================================================================================================ legal holds

    def preserve(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            self._check_caller(caller)
            rk = f"preserve|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self._hold_answer(self.holds[prev[1]])
            if body["hold_id"] in self.holds and self.holds[body["hold_id"]]["status"] == "active":
                raise Conflict(R("REQUEST_ID_REUSED"))
            systems = sorted(set(body["systems"]))
            outcome = {}
            for sysname in systems:
                adapter = self.ports.preservation.get(sysname)
                outcome[sysname] = "preserved" if adapter is not None and adapter.preserve(
                    body["hold_id"], body["subject_refs"]) else "not_connected"
            self._commit("hold_recorded", {"hold_id": body["hold_id"], "systems": systems,
                                           "subject_refs": sorted(set(body["subject_refs"])), "outcome": outcome,
                                           "actor": caller, "request_id": rk,
                                           "request_sha": request_sha(body), "_obj": body["hold_id"]}, caller)
            if any(v != "preserved" for v in outcome.values()):
                self._open_incident("sev3", "PRESERVATION_NOT_CONNECTED", f"hold:{body['hold_id']}"[:140], DETECTOR)
            out = self._hold_answer(self.holds[body["hold_id"]])
        self.flush_alerts()
        return out

    @staticmethod
    def _hold_answer(h: dict) -> dict:
        missing = sorted(s for s, v in h["outcome"].items() if v != "preserved")
        return {"hold_id": h["hold_id"], "delivered": not missing,
                "reason": "" if not missing else "not connected to Cybersecurity (22) yet: " + ", ".join(missing),
                "reference": derived_id("hld", h["hold_id"]), "systems": dict(h["outcome"]), "status": h["status"]}

    def release_hold(self, caller: str, hold_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            self._check_caller(caller)
            rk = f"release_hold|{hold_id}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self._hold_answer(self.holds[prev[1]])
            h = self.holds.get(hold_id)
            if h is None:
                raise NotFound(R("HOLD_NOT_FOUND"))
            if h["status"] != "active":
                raise Conflict(R("HOLD_RELEASED"))
            for sysname, adapter in self.ports.preservation.items():
                if h["outcome"].get(sysname) == "preserved":
                    adapter.release(hold_id)
            self._commit("hold_released", {"hold_id": hold_id, "actor": caller, "request_id": rk,
                                           "request_sha": request_sha(body), "_obj": hold_id}, caller)
            return self._hold_answer(h)

    # ================================================================================================ incidents

    def _open_incident(self, severity: str, code: str, subject: str, actor: str) -> Optional[str]:
        """Open (or, if one is open for the same code and subject, add to) an incident and queue its alerts.
        Never raises: a detection must not turn a refusal into a different error."""
        for inc in self.incidents.values():
            if inc["status"] == "open" and inc["code"] == code and inc["subject"] == subject:
                try:
                    if len(inc["timeline"]) < 200:
                        self._commit("incident_event", {"incident_id": inc["incident_id"], "event": "repeated",
                                                        "actor": actor}, actor)
                except Unavailable:
                    pass
                return inc["incident_id"]
        iid = derived_id("inc", code, subject, iso(self.now()), os.urandom(4).hex())
        try:
            self._commit("incident_opened", {"incident_id": iid, "severity": severity, "code": code,
                                             "subject": subject, "actor": actor}, actor)
        except Unavailable:
            # the log cannot take the record (e.g. its integrity is what failed): Andre is alerted anyway, and the
            # ledger gets a best-effort record; the incident itself is opened by hand once the log is healthy
            if (code, subject) in self._unrecorded_alerted:
                return None     # DELIVERED once already while the log is unhealthy (R3-4: a failed send retries)
            self.rec.try_record(derived_id("inu", iid), "incident_unrecorded", DETECTOR, iid,
                                {"severity": severity, "code": code}, f"unrecorded {severity} {code}")
            self._queue_alerts({"incident_id": iid, "severity": severity, "code": code, "subject": subject},
                               unrecorded=(code, subject))
            return None
        self._queue_alerts(self.incidents[iid])
        return iid

    def _queue_alerts(self, inc: dict, unrecorded: Optional[tuple] = None) -> None:
        channels = CHANNELS_BY_SEVERITY[inc["severity"]]
        if not channels:
            return
        aid = derived_id("alr", inc["incident_id"])
        self.alert_status[aid] = {"alert_id": aid, "incident_id": inc["incident_id"], "severity": inc["severity"],
                                  "code": inc["code"], "channels": {c: "queued" for c in channels},
                                  "unrecorded": unrecorded}
        self._outbox.append(AlertMessage(aid, inc["severity"], inc["code"], inc["incident_id"], inc["subject"]))

    def flush_alerts(self) -> None:
        """Send queued alerts OUTSIDE the lock (a slow provider must never hold up the vault)."""
        with self.lock:
            out, self._outbox = self._outbox, []
        for msg in out:
            for ch_name in list(self.alert_status.get(msg.alert_id, {}).get("channels", {})):
                ch = self.ports.channels.get(ch_name)
                try:
                    result = ch.send(msg) if ch is not None else "not_wired"
                except Exception:  # noqa: BLE001 - a provider failure is a failed send, never a crash
                    result = "failed"
                if result not in ("delivered", "failed", "not_wired"):
                    result = "failed"
                with self.lock:
                    entry = self.alert_status[msg.alert_id]
                    entry["channels"][ch_name] = result
                    if result == "delivered" and entry.get("unrecorded"):
                        self._unrecorded_alerted.add(entry["unrecorded"])
            self.rec.try_record(derived_id("alt", msg.alert_id, os.urandom(4).hex()), "alert_dispatched", DETECTOR,
                                msg.incident_id, dict(self.alert_status[msg.alert_id]["channels"]),
                                f"alert {msg.severity} {msg.code}")

    def open_incident(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"open_incident|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self.incident(prev[1])
            iid = derived_id("inc", caller, body["request_id"])
            self._commit("incident_opened", {"incident_id": iid, "severity": body["severity"], "code": body["code"],
                                             "subject": body["subject"], "actor": caller,
                                             "request_id": rk, "request_sha": request_sha(body),
                                             "_obj": iid}, caller)
            self._queue_alerts(self.incidents[iid])
            out = self.incident(iid)
        self.flush_alerts()
        return out

    def incident(self, iid: str) -> dict:
        with self.lock:
            inc = self.incidents.get(iid)
            if inc is None:
                raise NotFound(R("INCIDENT_NOT_FOUND"))
            alerts = [dict(a, channels=dict(a["channels"])) for a in self.alert_status.values()
                      if a["incident_id"] == iid]
            return {**inc, "timeline": [dict(t) for t in inc["timeline"]], "alerts": alerts}

    def incidents_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in i.items() if k != "timeline"} for i in self.incidents.values()
                    if status is None or i["status"] == status]

    def note_incident(self, caller: str, iid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"note|{iid}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self.incident(iid)
            inc = self.incidents.get(iid)
            if inc is None:
                raise NotFound(R("INCIDENT_NOT_FOUND"))
            if inc["status"] != "open":
                raise Conflict(R("INCIDENT_CLOSED"))
            self._commit("incident_event", {"incident_id": iid, "event": "note", "note": body["note"],
                                            "actor": caller, "request_id": rk,
                                            "request_sha": request_sha(body), "_obj": iid}, caller)
            return self.incident(iid)

    def close_incident(self, iid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"close|{iid}|{body['request_id']}"
            prev = self._idem("andre", rk, body)
            if prev:
                return self.incident(iid)
            inc = self.incidents.get(iid)
            if inc is None:
                raise NotFound(R("INCIDENT_NOT_FOUND"))
            if inc["status"] != "open":
                raise Conflict(R("INCIDENT_CLOSED"))
            appr = self._approve("INCIDENT_CLOSE", iid, body)
            self._commit("incident_closed", {"incident_id": iid, "root_cause_code": body["root_cause_code"],
                                             "note": body["note"], "actor": "andre", "request_id": rk,
                                             "request_sha": request_sha(body), "_obj": iid, "approval": appr}, "andre")
            return self.incident(iid)

    def auth_failed(self) -> None:
        """D1: a burst of requests with a wrong service token or an unknown caller token."""
        with self.lock:
            if self._window(("auth_fail",), AUTH_FAIL_WINDOW_S).hit(time.monotonic()) >= AUTH_FAIL_LIMIT:
                if self.integrity["ok"]:
                    self._open_incident("sev2", "AUTH_FAILURE_BURST", "api", DETECTOR)
        self.flush_alerts()

    # ================================================================================================ findings

    def ingest_scan(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            if caller != "dashboard":
                self._check_caller(caller)
            rk = f"scan|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self.scans[prev[1]]
            try:
                scanned = parse_iso(body["scanned_at"])
            except ValueError:
                raise Invalid(R("INVALID"), field="scanned_at") from None
            now = self.now()
            if scanned > now + timedelta(minutes=5):
                raise Invalid(R("INVALID"), field="scanned_at")
            source = body["source"]
            seen_now = {}
            for f in body["findings"]:
                fid = derived_id("fnd", source, f["advisory_id"], f["package"], f["installed_version"])
                seen_now[fid] = f
            open_before = {fid for fid, f in self.findings.items()
                           if f["source"] == source and f["status"] in ("open", "accepted")}
            opened = []
            for fid, f in sorted(seen_now.items()):
                if fid in self.findings and self.findings[fid]["status"] in ("open", "accepted"):
                    continue
                due = (scanned.date() + timedelta(days=SLA_DAYS[f["severity"]])).isoformat()
                opened.append({"finding_id": fid, "source": source, "advisory_id": f["advisory_id"],
                               "package": f["package"], "installed_version": f["installed_version"],
                               "fixed_versions": list(f["fixed_versions"]), "severity": f["severity"],
                               "first_seen": scanned.date().isoformat(), "due_by": due})
            fixed = sorted(open_before - set(seen_now))
            seen = sorted(open_before & set(seen_now))
            scan_id = derived_id("scn", caller, body["request_id"])
            self._commit("scan_ingested", {"scan_id": scan_id, "source": source, "tool": body["tool"],
                                           "scanned_at": iso(scanned), "opened": opened, "fixed": fixed, "seen": seen,
                                           "actor": caller, "request_id": rk,
                                           "request_sha": request_sha(body), "_obj": source}, caller)
            if any(f["severity"] == "critical" for f in opened):
                self._open_incident("sev2", "CRITICAL_VULNERABILITY", f"scan:{source}", DETECTOR)
            out = {**self.scans[source], "opened": len(opened), "fixed": len(fixed)}
        self.flush_alerts()
        return out

    def findings_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [dict(f) for f in self.findings.values() if status is None or f["status"] == status]

    def accept_risk(self, finding_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"accept|{finding_id}|{body['request_id']}"
            prev = self._idem("andre", rk, body)
            if prev:
                return dict(self.findings[prev[1]])
            f = self.findings.get(finding_id)
            if f is None:
                raise NotFound(R("FINDING_NOT_FOUND"))
            if f["status"] != "open":
                raise Conflict(R("FINDING_NOT_OPEN"))
            until = date.fromisoformat(body["until"])
            today = self.now().date()
            if until <= today or (until - today).days > MAX_ACCEPT_DAYS:
                raise Invalid(R("ACCEPTANCE_TOO_LONG"))
            appr = self._approve("RISK_ACCEPT", finding_id, body)
            self._commit("risk_accepted", {"finding_id": finding_id, "until": body["until"],
                                           "reason_code": body["reason_code"], "actor": "andre",
                                           "request_id": rk, "request_sha": request_sha(body),
                                           "_obj": finding_id, "approval": appr}, "andre")
            return dict(self.findings[finding_id])

    def _overdue(self, today: date) -> list[dict]:
        out = []
        for f in self.findings.values():
            if f["status"] == "open" and date.fromisoformat(f["due_by"]) < today:
                out.append(f)
            elif f["status"] == "accepted" and date.fromisoformat(f["accepted_until"]) < today:
                out.append(f)
        return out

    # ================================================================================================ jobs

    def run_job(self, name: str, body: dict) -> dict:
        if name not in JOBS:
            raise NotFound(R("JOB_UNKNOWN"))
        if name != "integrity":            # the integrity check stays available during a lockdown (read-only)
            with self.lock:
                self._check_caller("scheduler")
        if name == "integrity":
            res = self.verify_integrity(force=True, always=True)   # round 2 N4: the job always reads the ledger
            ledger_ok = self.rec.client.verify()
            if not res["ok"] or not ledger_ok:
                with self.lock:
                    self._open_incident("sev1", "INTEGRITY_FAILURE", "security_log", DETECTOR)
                self.flush_alerts()
            return {"job": name, "integrity": res, "ledger_valid": ledger_ok}
        if name == "compliance-report":
            return self._compliance_report(body)
        with self.lock:
            self._gate()
            prev = self._idem("scheduler", f"{name}:{body['request_id']}", body)
            if prev:
                return {"job": name, "already_ran": True}
            today = self.now().date()
            if name == "rotate-signing-key":
                result = self._rotate_signing_key()
            elif name == "rotation-due":
                due = [s["ref"] for s in self.secrets.values() if s["status"] == "active" and s["rotate_by"]
                       and date.fromisoformat(s["rotate_by"]) < today]
                for ref in due:
                    self._open_incident("sev3", "ROTATION_OVERDUE", ref, DETECTOR)
                result = {"overdue": due}
            elif name == "findings-due":
                over = self._overdue(today)
                for f in over:
                    sev = "sev2" if f["severity"] in ("critical", "high", "unknown") else "sev3"
                    self._open_incident(sev, "FINDING_OVERDUE", f["finding_id"], DETECTOR)
                result = {"overdue": [f["finding_id"] for f in over]}
            else:   # alerts-retry
                for a in self.alert_status.values():
                    if any(v != "delivered" for v in a["channels"].values()):
                        inc = self.incidents.get(a["incident_id"])
                        if inc and inc["status"] == "open":
                            self._outbox.append(AlertMessage(a["alert_id"], a["severity"], a["code"],
                                                             a["incident_id"], inc["subject"]))
                result = {"queued": len(self._outbox)}
            self._commit("job_ran", {"job": name, "actor": "scheduler", "request_id": f"{name}:{body['request_id']}",
                                     "request_sha": request_sha(body)}, "scheduler")
        self.flush_alerts()
        return {"job": name, **result}

    def _compliance_report(self, body: dict) -> dict:
        """C-14: pass only if every scan source is fresh, no finding is overdue, the log is verified, at least
        one passkey is enrolled and no sev1 incident is open."""
        with self.lock:
            self._gate()
            now = self.now()
            today = now.date()
            reasons = []
            if not self.scans:
                reasons.append("no scan ingested")
            for src, sc in self.scans.items():
                if now - parse_iso(sc["scanned_at"]) > timedelta(days=SCAN_MAX_AGE_DAYS):
                    reasons.append(f"scan stale: {src}")
            if self._overdue(today):
                reasons.append("findings past their deadline")
            if not self._active_passkeys():
                reasons.append("no passkey enrolled")
            if any(i["status"] == "open" and i["severity"] == "sev1" for i in self.incidents.values()):
                reasons.append("open sev1 incident")
            result = "fail" if reasons else "pass"
            tested_at = iso(now)
            evidence = [{"kind": "scan", "ref": f"urn:sec22:scan:{src}",
                         "sha256": payload_sha256(sc)} for src, sc in sorted(self.scans.items())][:48]
            evidence.append({"kind": "integrity", "ref": "urn:sec22:integrity",
                             "sha256": payload_sha256(self.integrity)})
            request_id = derived_id("c14", body["request_id"])
        push = self.ports.compliance.push_control_result("C-14", request_id, result, tested_at, evidence)
        with self.lock:
            try:
                self._commit("compliance_reported", {"result": result, "tested_at": tested_at,
                                                     "delivery": push.status, "reasons": reasons,
                                                     "actor": "scheduler"}, "scheduler")
            except Unavailable:
                pass
        return {"job": "compliance-report", "result": result, "reasons": reasons, "delivery": push.status}

    # ================================================================================================ audit

    def audit_events(self, since_seq: int, limit: int) -> dict:
        """The local log, metadata only (the log never holds a value), notes reduced to their SHA-256."""
        out = []
        for r in self.log.iter_records(max(1, since_seq)):
            d = {k: v for k, v in r["data"].items() if k not in ("approval", "token_sha256")}
            if "note" in d:
                d["note_sha256"] = sha256_hex(d.pop("note").encode("utf-8"))
            if "credential" in d:
                d["credential"] = {k: d["credential"][k] for k in ("credential_id", "alg", "aaguid")}
            out.append({"seq": r["seq"], "kind": r["kind"], "at": r["at"], "data": d,
                        "approved_with": (r["data"].get("approval") or {}).get("credential_id")})
            if len(out) >= limit:
                break
        return {"events": out, "log_length": len(self.log)}

