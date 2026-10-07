"""
Customer Service (30) + Client Success (29) — the core (ADR 0014).

One lock guards all state. Every state change is one ``_commit``: the typed ledger events the change carries
(answer, escalation, consent change, approval, alert, ...) are recorded first, then the exact log line is prepared,
fsynced aside (pending line), anchored on the evidence ledger, appended to the local log, and only then applied to
memory — by the same ``_apply`` that rebuilds state from the log at start, so live state and replayed state cannot
diverge. The plumbing (``_commit``, ``_anchor``, ``_drop_pending``, ``verify_integrity``, ``_settle_pending``,
``_roll_forward``, ``_anchor_problem``) is security-py's as fixed in AEGIS rounds 1-5 (ADR 0012 amendments).

A line holds a list of EFFECTS (``{"op": ..., ...}``), applied in order by ``_e_<op>``; one API operation is one
line, so an inbound message, its ticket, its triage, its answer or escalation and its alerts take effect together
or not at all. Message bodies are never in a line (BodyStore, by SHA-256) and never on the ledger.

Nothing is sent with the lock held: handoffs, alerts and outbound messages go out after the line that decided them
is committed, and an outbound message is recorded on the ledger (``message_sent``) BEFORE its provider is called.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

import channels
import health as health_mod
import kb
import triage as triage_mod
from clock import Clock, SystemClock, iso, parse_iso
from config import BRAND_NAMES, PRIORITIES, Settings
from errors import Conflict, FounderRefused, Invalid, NotFound, Unavailable
from ledger import DEPARTMENT, LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, derived_id, payload_sha256
from ports import NOT_WIRED, Alert, HandoffRequest, Outbound, Ports
from reasons import R
from store import BodyStore, DataDirBusy, RecordLog, StoreCorrupt, StoreWriteError, verify_lines

INTERNAL = "service_desk"
INTEGRITY_RETRY_S = 15
FORCED_MIN_S = 10
JOBS = ("sla-sweep", "health-recompute", "save-plan-tick", "outbound-tick", "handoff-retries", "integrity")
TICKET_STATUSES = ("open", "pending_customer", "escalated", "resolved", "closed")
TRANSITIONS = {
    "open": ("pending_customer", "escalated", "resolved"),
    "pending_customer": ("open", "escalated", "resolved"),
    "escalated": ("open", "pending_customer", "resolved"),
    "resolved": ("open", "closed"),
    "closed": (),
}
ALERT_CATEGORIES = {"money": "ESCALATION_MONEY", "contract": "ESCALATION_CONTRACT", "complaint": "ESCALATION_COMPLAINT",
                    "security": "ESCALATION_SECURITY", "privacy": "ESCALATION_PRIVACY"}
PLACEHOLDERS = ("first_name", "brand_name", "offer_title", "offer_terms", "offer_price", "survey_id")
TEMPLATE_PURPOSES = ("check_in", "nps_survey", "offer")
PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
SMS_FOOTER = "\nReply STOP to opt out."
OPT_OUT_ONLY_WORDS = 4          # an opt-out of at most this many words is only that: no ticket
MAX_SEND_ATTEMPTS = 5
KEY_FINGERPRINT_LABEL = b"service-py SVC_HMAC_KEY fingerprint v1"
# V3-H1: an email subject must be one of these (or the article's title or one of its questions) for the bot to answer
NEUTRAL_SUBJECTS = frozenset({"question", "quick question", "hello", "hi", "hey", "inquiry", "enquiry", "help"})
SENSITIVE = frozenset({"money", "contract", "complaint", "security", "privacy"})
CATALOGS = ("kb", "template", "offer")
PERSONAL_KEYS = ("email", "phone", "display_name", "contact_ref", "timezone", "to", "from_number", "from_address",
                 "address", "bound_to")
# typed ledger events: effect op -> event type (recorded BEFORE the line that carries the effect)
EVENTS = {"consent_granted": "consent_changed", "consent_revoked": "consent_changed",
          "handoff_new": "escalation_opened", "ticket_escalated": "escalation_opened",
          "catalog_approved": "approval_recorded", "catalog_retired": "approval_recorded",
          "alert_new": "alert_raised", "sla_breach": "sla_breached", "plan_new": "save_plan_started",
          "plan_offer": "offer_selected", "routing_set": "approval_recorded", "message_sending": "message_sending",
          "sms_paused": "sms_paused", "sms_pause_cleared": "sms_pause_cleared"}


def _maybe(exc: Unavailable) -> Unavailable:
    """Mark an outcome as unknown: the line is pending and may still take effect (security-py round 2 N2)."""
    exc.maybe = True
    return exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def body_sha(body: dict) -> str:
    return payload_sha256(body)


def money_str(v: str) -> str:
    """A price string, Decimal-checked, rendered with a dollar sign. Never a float."""
    d = Decimal(v)
    return f"${d:,.2f}"


class SupportService:
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, bodies: BodyStore,
                 ports: Optional[Ports] = None, clock: Optional[Clock] = None, lock_token: Optional[str] = None):
        self.settings = settings
        self.rec = recorder
        self.log = log
        self.bodies = bodies
        self.ports = ports or Ports.default()
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self._tick_lock = threading.Lock()
        # state rebuilt from the log
        self.contacts: dict[str, dict] = {}
        self.contact_index: dict[tuple, str] = {}
        self.consents: dict[tuple, dict] = {}
        # V3-C2: revocations belong to the ADDRESS: (brand, channel, HMAC of the address) -> when last revoked
        self.addr_revocations: dict[tuple, str] = {}
        # V2-H3 / V4-L1: pauses belong to the NUMBER: (brand, HMAC of the phone) -> when proactive SMS was paused
        self.sms_paused: dict[tuple, str] = {}
        self.address_changed_at: dict[tuple, str] = {}   # (contact, "phone" | "email") -> when it last changed
        self.tickets: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.handoffs: dict[str, dict] = {}
        self.alerts: dict[str, dict] = {}
        self.catalog: dict[str, dict] = {c: {} for c in CATALOGS}
        self.accounts: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.surveys: dict[str, dict] = {}
        self.calls: dict[str, dict] = {}
        self.routing: dict[str, dict] = {}
        self.requests: dict[tuple, tuple] = {}
        # memory only
        self.integrity = {"ok": False, "checked_at": None, "problem": "not yet verified against the ledger"}
        self._last_integrity_try = 0
        self._own_pending: Optional[bytes] = None
        self._deferred_alerts: list[tuple] = []
        self._in_flight: set = set()
        self._unconfirmed: dict[str, dict] = {}     # sent by the provider, result not yet committed: never resent
        # /audit/evidence cache (bizdev-py AEGIS round 7): lines parsed so far, keyed by log length; never touched
        # under self.lock
        self._evidence_lock = threading.Lock()
        self._evidence_cache: dict = {"n": 0, "epoch": None, "lines": []}
        self.key_fingerprint: Optional[str] = None
        # V5-L1: one service instance per data directory, also within one process (config.load caches the flock)
        # V5r-L1: api.build claims BEFORE the log and the body store are built and passes the claim's token; the
        # service adopts that claim only if the token IS the current claim (round 5c item 1), and gives it back on
        # close or a failed start
        self._closed = False
        self._dir_lock = getattr(settings, "data_dir_lock", None)
        self._lock_token: Optional[str] = None
        if self._dir_lock is not None:
            if lock_token is None:
                lock_token = self._dir_lock.claim()
            adopted = self._dir_lock.adopt(lock_token)    # single use (a5dd261 L4); a token of our own (cc27b69 Info)
            if adopted is not None:
                self._lock_token = adopted
            else:
                self._dir_lock = None
                raise DataDirBusy("the data-directory claim handed to this service instance is not held (released, "
                                  "stale, another instance's, or already adopted); refusing to start")
        try:
            self.bodies.sweep_tmp()                       # only after the claim is verified (AEGIS a5dd261 I5)
            self._start()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Give the data directory back (a restart in the same process closes the old instance first). A closed
        instance never writes again: its log and body store refuse, and every commit is refused (V5r-Info). Taken
        under the service lock, so it never interleaves with a commit (round 5c item 2)."""
        with self.lock:
            self._closed = True
            with self.log.lock:
                self.log.closed = True
            with self.bodies.lock:
                self.bodies.closed = True
            if getattr(self, "_dir_lock", None) is not None:
                self._dir_lock.release_claim(self._lock_token)
                self._dir_lock = None
                self._lock_token = None

    def _start(self) -> None:
        for r in self.log.iter_records():
            self._apply(r["kind"], r["data"], r["at"])
        if self.key_fingerprint is not None and not hmac.compare_digest(self.key_fingerprint, self._key_fp()):
            # V2-M1: another key would make every stored body unreadable (and new ones unjoinable): refuse
            raise StoreCorrupt(f"{self.settings.key_source} is not the key this log was written with (fingerprint "
                               "mismatch); refusing to start: restore the key the log was written with")
        if self.key_fingerprint is None and len(self.log):
            # V3-L2: a log from before the fingerprint existed: adopt this key only if every stored body the log cites
            # verifies with it (a wrong key would make them all unreadable); otherwise refuse
            cited: set = set()
            for r in self.log.iter_records():
                _digests(r["data"], cited)
            present = cited & self.bodies.names()
            if any(self.bodies.get(d) is None for d in present):
                raise StoreCorrupt("this log predates the key fingerprint and its stored bodies do not verify with the "
                                   "configured key; refusing to start (restore the key the log was written with)")
        if self.log.read_pending() is None and self.log.read_discarded() is None:
            self._remove_orphans()
        self.verify_integrity(force=True)

    # ============================================================================================ plumbing

    def now(self) -> datetime:
        return self.clock.now()

    def _anchor_ids(self, epoch: str, seq: int, line_sha: str) -> tuple[str, dict]:
        payload = {"epoch": epoch, "seq": seq, "line_sha256": line_sha}
        return derived_id("anc", epoch, seq, line_sha), payload

    def _rk(self, kind: str, data: dict) -> str:
        """The request key named in a typed evidence payload (sweep A R6-M1). A request key can name a contact ref or
        an address, so the ledger gets its keyed HMAC (never enumerable without the service's key); a line with no
        request is ``service_desk|<kind>``."""
        req = (data.get("request") or {}).get("key")
        if not req:
            return f"{INTERNAL}|{kind}"
        return "rk-" + hmac.new(self.settings.hmac_key, b"evidence rk\x00" + json.dumps(req, sort_keys=True).encode(
            "utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:40]

    def _events_for(self, kind: str, data: dict, seq: int) -> list[tuple]:
        """(event_id, event_type, subject_id, payload, summary) for each effect that is recorded as its own typed
        event. The payload holds ids, codes and hashes only: no body, no address, no name.

        Sweep A R6-M1 (bizdev-py's round-6 pattern): every payload also carries ``rk`` (the line's request key, keyed
        HMAC) and ``seq`` (the log seq it is meant for), and the id includes that payload's hash. The line names its
        events (``ledger_evidence``), so /audit/evidence tells the one ``committed`` event of a logical action from
        the ``attempted`` ones a refused or failed commit left behind (record-first: a retry after the state changed
        records another event; it is never a lasting 409, and never counted twice)."""
        rk = self._rk(kind, data)
        out = []
        for e in data.get("effects", ()):
            et = EVENTS.get(e["op"])
            if e["op"] == "message_out" and e.get("origin") in ("kb", "human", "template", "offer", "opt_out"):
                et = "message_sent" if e["status"] == "sent" else "answer_queued"
            if e["op"] == "message_status" and e.get("status") == "sent":
                et = "message_sent"
            if et is None:
                continue
            core = {k: v for k, v in e.items() if k not in PERSONAL_KEYS and not k.endswith("_due")
                    and k not in ("content", "signals_detail")}
            core = {**core, "rk": rk, "seq": seq}
            subject = next((e[k] for k in ("ticket_id", "message_id", "handoff_id", "alert_id", "plan_id",
                                           "contact_id", "item_id", "brand") if e.get(k)), INTERNAL)
            eid = derived_id("evt", kind, (data.get("request") or {}).get("key"), e["op"], payload_sha256(core))
            out.append((eid, et, str(subject)[:128], core, f"{kind}: {e['op']}"))
        return out

    def _commit(self, kind: str, data: dict, actor: str) -> dict:
        """Typed events, then ledger anchor, then the local log, then memory (fail closed at every step)."""
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            if not self.integrity["ok"]:
                raise Unavailable(R("INTEGRITY_UNVERIFIED"))
            at = iso(self.now())
            data = {**data, "actor": data.get("actor", actor)}
            actor = data["actor"]
            seq = len(self.log) + 1
            named = []
            for eid, et, subject, payload, summary in self._events_for(kind, data, seq):
                try:
                    self._record_twice(eid, et, actor, subject, payload, summary)
                except LedgerRecordError:
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None   # the line was not written: no effect
                if all(n["event_id"] != eid for n in named):
                    named.append({"event_id": eid, "event_type": et, "subject_id": subject, "payload": payload})
            if named:
                data["ledger_evidence"] = named
            rec, line = self.log.prepare(kind, at, data)
            if rec["seq"] != seq:                           # the log moved under us: never anchor a mislabelled line
                raise Unavailable(R("STORE_UNAVAILABLE"))
            line_sha = sha256_hex(line)
            epoch = self.log.epoch or line_sha[:16]
            eid, payload = self._anchor_ids(epoch, rec["seq"], line_sha)
            self._own_pending = line                  # R4-1: only a line THIS process wrote is anchored by it
            try:
                self.log.write_pending(line)
            except StoreWriteError:
                if not self._drop_pending():
                    raise _maybe(Unavailable(R("STORE_UNAVAILABLE"))) from None
                raise Unavailable(R("STORE_UNAVAILABLE")) from None
            try:
                self._anchor(eid, actor, epoch, payload, kind, rec["seq"])
            except LedgerRecordError as exc:
                if exc.took_effect is False and self._drop_pending():
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
                self.integrity = {"ok": False, "checked_at": at, "problem": "a ledger answer was lost; the "
                                  "pending line is rolled forward at the next integrity check"}
                raise _maybe(Unavailable(R("LEDGER_UNAVAILABLE"))) from None
            try:
                self.log.append_prepared(rec, line)
            except StoreWriteError:
                self.integrity = {"ok": False, "checked_at": at, "problem": "a log line was anchored but not written; "
                                  "it is rolled forward at the next integrity check"}
                raise _maybe(Unavailable(R("STORE_UNAVAILABLE"))) from None
            self._own_pending = None   # R5-1
            self._drop_pending()
            self._apply(kind, data, at)
            return rec

    def _record_twice(self, eid, event_type, actor, subject, payload, summary) -> None:
        """One retry of the SAME event when the answer was lost (the ledger is idempotent on identical content)."""
        try:
            self.rec.record(eid, event_type, actor, subject, payload, summary)
        except LedgerRecordError as exc:
            if exc.took_effect is False or isinstance(exc, LedgerConflict):
                raise
            self.rec.record(eid, event_type, actor, subject, payload, summary)

    def _anchor(self, eid: str, actor: str, epoch: str, payload: dict, kind: str, seq: int) -> None:
        self._record_twice(eid, "log_anchor", actor, f"log:{epoch}", payload, f"{kind} #{seq}")

    def _drop_pending(self) -> bool:
        try:
            self.log.clear_pending()
            self._own_pending = None
            return True
        except StoreWriteError:
            self.integrity = {"ok": False, "checked_at": iso(self.now()),
                              "problem": "the pending line could not be removed"}
            return False

    # --- idempotency: (actor, operation, target, request_id) with the body's hash -----------------------------------

    def _idem(self, actor: str, op: str, target: str, body: dict) -> Optional[dict]:
        key = (actor, op, target, body["request_id"])
        prev = self.requests.get(key)
        if prev is None:
            return None
        if prev[0] != body_sha(body):
            raise Conflict(R("REQUEST_ID_REUSED"))
        return prev[1]

    @staticmethod
    def _req(actor: str, op: str, target: str, body: dict) -> dict:
        return {"key": [actor, op, target, body["request_id"]], "sha": body_sha(body)}

    def _did(self, abbr: str, actor: str, op: str, target: str, request_id: str, *extra) -> str:
        return derived_id(abbr, actor, op, target, request_id, *extra)

    # ============================================================================================ replay

    def _apply(self, kind: str, d: dict, at: str) -> None:
        for e in d.get("effects", ()):
            handler = getattr(self, f"_e_{e.get('op')}", None)
            if handler is None:
                raise StoreCorrupt(f"log record carries an unknown effect {str(e.get('op'))[:40]!r}")
            handler(e, at)
        req = d.get("request")
        if req:
            self.requests[tuple(req["key"])] = (req["sha"], d.get("response"))

    def _e_contact_new(self, e, at):
        c = {k: e.get(k) for k in ("contact_id", "brand", "contact_ref", "account_id", "email", "phone", "timezone",
                                   "display_name")}
        c.update(created_at=at, updated_at=at)
        self.contacts[e["contact_id"]] = c
        self._index_contact(c)

    def _e_contact_update(self, e, at):
        c = self.contacts[e["contact_id"]]
        for k, v in e["fields"].items():
            if k in ("email", "phone", "contact_ref") and c.get(k) and c[k] != v:
                self.contact_index.pop((c["brand"], k, c[k]), None)      # the old address no longer finds them
                if k != "contact_ref":
                    self.address_changed_at[(c["contact_id"], k)] = at
            c[k] = v
        c["updated_at"] = at
        self._index_contact(c)

    def _index_contact(self, c: dict) -> None:
        for kind in ("contact_ref", "email", "phone"):
            if c.get(kind):
                self.contact_index[(c["brand"], kind, c[kind])] = c["contact_id"]

    def _e_consent_granted(self, e, at):
        self.consents[(e["contact_id"], e["channel"])] = {
            "consent_id": e["consent_id"], "contact_id": e["contact_id"], "channel": e["channel"],
            "status": "active", "source": e["source"], "consent_text_sha256": e["text_sha256"],
            "captured_at": e["captured_at"], "recorded_at": at, "revoked_at": None, "revoked_via": None,
            "express": e["express"], "address": e.get("address")}

    def _e_consent_revoked(self, e, at):
        key = (e["contact_id"], e["channel"])
        c = self.consents.get(key) or {"consent_id": None, "contact_id": e["contact_id"], "channel": e["channel"],
                                       "source": None, "consent_text_sha256": None, "captured_at": None,
                                       "recorded_at": None, "express": False, "address": e.get("address")}
        if c.get("status") == "active" and e.get("address") and c.get("address") not in (None, e["address"]):
            pass            # a revocation for another address leaves this one's consent alone (it is not theirs)
        else:
            c.update(status="revoked", revoked_at=at, revoked_via=e["via"])
        self.consents[key] = c
        if e.get("address"):
            brand = self.contacts[e["contact_id"]]["brand"]
            self.addr_revocations[(brand, e["channel"], self._addr_key(e["channel"], e["address"]))] = at
        for m in self.messages.values():
            if m["dir"] == "out" and m["contact_id"] == e["contact_id"] and m["channel"] == e["channel"] \
                    and m["status"] == "queued" and m.get("origin") != "opt_out":
                m.update(status="cancelled", reason=R("CONSENT_REVOKED"), updated_at=at)

    def _e_sms_paused(self, e, at):
        brand = self.contacts[e["contact_id"]]["brand"]
        self.sms_paused.setdefault((brand, self._addr_key("sms", e["phone"])), at)

    def _e_sms_pause_cleared(self, e, at):
        brand = self.contacts[e["contact_id"]]["brand"]
        self.sms_paused.pop((brand, self._addr_key("sms", e["phone"])), None)

    def _e_ticket_new(self, e, at):
        self.tickets[e["ticket_id"]] = {
            "ticket_id": e["ticket_id"], "brand": e["brand"], "contact_id": e["contact_id"],
            "account_id": e.get("account_id"), "channel": e["channel"], "priority": e["priority"], "status": "open",
            "queue": "bot", "categories": [], "created_at": at, "updated_at": at, "first_response_at": None,
            "first_due": e["first_due"], "resolution_due": e["resolution_due"], "resolved_at": None,
            "closed_at": None, "reopened": 0, "breaches": [], "messages": [], "handoffs": [], "answered_by": None,
            "complaint_at": []}

    def _e_ticket_status(self, e, at):
        t = self.tickets[e["ticket_id"]]
        if e["status"] == "open" and t["status"] == "resolved":
            t["reopened"] += 1
            t["resolved_at"] = None
        t["status"] = e["status"]
        t["updated_at"] = at
        if e["status"] == "resolved":
            t["resolved_at"] = at
        if e["status"] == "closed":
            t["closed_at"] = at
        if e.get("queue"):
            t["queue"] = e["queue"]
        if e.get("answered_by"):
            t["answered_by"] = e["answered_by"]

    def _e_ticket_escalated(self, e, at):
        t = self.tickets[e["ticket_id"]]
        t.update(status="escalated", queue="andre", updated_at=at)
        for c in e["categories"]:
            if c not in t["categories"]:
                t["categories"].append(c)

    def _e_ticket_priority(self, e, at):
        t = self.tickets[e["ticket_id"]]
        t.update(priority=e["priority"], first_due=e["first_due"], resolution_due=e["resolution_due"], updated_at=at)

    def _e_message_in(self, e, at):
        m = {k: e.get(k) for k in ("message_id", "ticket_id", "contact_id", "brand", "channel", "body_sha256",
                                   "subject_sha256", "triage", "refs")}
        m.update(dir="in", at=at)
        self.messages[e["message_id"]] = m
        t = self.tickets[e["ticket_id"]]
        t["messages"].append(e["message_id"])
        if e.get("triage") and "complaint" in e["triage"]["categories"]:
            t["complaint_at"].append(at)

    def _e_message_out(self, e, at):
        m = {k: e.get(k) for k in ("message_id", "ticket_id", "contact_id", "brand", "channel", "origin", "ref",
                                   "body_sha256", "subject", "proactive", "status", "plan_id", "step_id",
                                   "survey_id", "bound_to")}
        m.update(dir="out", at=at, updated_at=at, attempts=0, reason=e.get("reason"), provider_accepted=False)
        self.messages[e["message_id"]] = m
        if e.get("ticket_id"):
            self.tickets[e["ticket_id"]]["messages"].append(e["message_id"])

    def _e_message_status(self, e, at):
        m = self.messages[e["message_id"]]
        if m["status"] == "cancelled" and e["status"] == "sent":
            m.update(provider_accepted=True, updated_at=at)        # V1-C2: a cancelled message never flips to sent
            return
        m.update(status=e["status"], reason=e.get("reason"), updated_at=at)

    def _e_message_sending(self, e, at):
        """The send intent, durable BEFORE the provider is called. A message still ``sending`` after a restart is
        held for Andre's review (``/outbound/{id}/resolve``), never re-sent automatically."""
        m = self.messages[e["message_id"]]
        m.update(status="sending", reason=None, updated_at=at)
        m["attempts"] += 1

    def _e_first_response(self, e, at):
        t = self.tickets[e["ticket_id"]]
        if t["first_response_at"] is None:
            t["first_response_at"] = at

    def _e_handoff_new(self, e, at):
        self.handoffs[e["handoff_id"]] = {
            "handoff_id": e["handoff_id"], "ticket_id": e["ticket_id"], "department": e["department"],
            "category": e["category"], "kind": e["kind"], "status": "pending", "reference": None, "attempts": 0,
            "created_at": at, "updated_at": at}
        self.tickets[e["ticket_id"]]["handoffs"].append(e["handoff_id"])

    def _e_handoff_result(self, e, at):
        h = self.handoffs[e["handoff_id"]]
        h.update(status=e["status"], reference=e.get("reference"), updated_at=at)
        h["attempts"] += 1

    def _e_alert_new(self, e, at):
        self.alerts[e["alert_id"]] = {"alert_id": e["alert_id"], "code": e["code"], "subject": e["subject"],
                                      "status": "recorded", "created_at": at, "delivered_at": None, "attempts": 0}

    def _e_alert_result(self, e, at):
        a = self.alerts[e["alert_id"]]
        a["attempts"] += 1
        a["status"] = e["status"]
        if e["status"] == "delivered":
            a["delivered_at"] = at

    def _e_sla_breach(self, e, at):
        t = self.tickets[e["ticket_id"]]
        if e["which"] not in t["breaches"]:
            t["breaches"].append(e["which"])

    def _e_catalog_saved(self, e, at):
        cat = self.catalog[e["catalog"]]
        prev = cat.get(e["item_id"])
        item = {"item_id": e["item_id"], "version": e["version"], **e["content"], "content_sha256": e["content_sha256"],
                "status": "active", "approved": None, "created_at": prev["created_at"] if prev else at,
                "updated_at": at}
        if e["catalog"] == "kb":
            item["article_id"] = e["item_id"]
        cat[e["item_id"]] = item

    def _e_catalog_approved(self, e, at):
        item = self.catalog[e["catalog"]][e["item_id"]]
        item["approved"] = {"version": e["version"], "content_sha256": e["content_sha256"], "at": at, "by": "andre"}

    def _e_catalog_retired(self, e, at):
        item = self.catalog[e["catalog"]][e["item_id"]]
        item.update(status="retired", updated_at=at)

    def _e_account_saved(self, e, at):
        a = self.accounts.get(e["account_id"])
        if a is None:
            a = self.accounts[e["account_id"]] = {
                "account_id": e["account_id"], "brand": e["brand"], "primary_contact_id": None, "contract_end": None,
                "contract_source": None, "last_login": None, "health": None, "at_risk": False, "plan_id": None,
                "renewal_flagged": [], "created_at": at}
        for k in ("primary_contact_id", "contract_end", "contract_source"):
            if e.get(k) is not None:
                a[k] = e[k]
        a["updated_at"] = at

    def _e_login_event(self, e, at):
        a = self.accounts[e["account_id"]]
        if a["last_login"] is None or e["occurred_at"] > a["last_login"]:
            a["last_login"] = e["occurred_at"]

    def _e_health_scored(self, e, at):
        a = self.accounts[e["account_id"]]
        a["health"] = {"score": e["score"], "signals": e["signals"], "threshold": e["threshold"], "at": at}
        a["at_risk"] = e["at_risk"]

    def _e_renewal_flagged(self, e, at):
        self.accounts[e["account_id"]]["renewal_flagged"].append(e["contract_end"])

    def _e_plan_new(self, e, at):
        self.plans[e["plan_id"]] = {"plan_id": e["plan_id"], "account_id": e["account_id"], "status": "active",
                                    "score": e["score"], "created_at": at, "closed_at": None, "outcome": None,
                                    "steps": [dict(s) for s in e["steps"]]}
        self.accounts[e["account_id"]]["plan_id"] = e["plan_id"]

    def _step(self, plan_id: str, step_id: str) -> dict:
        return next(s for s in self.plans[plan_id]["steps"] if s["step_id"] == step_id)

    def _e_plan_step(self, e, at):
        s = self._step(e["plan_id"], e["step_id"])
        s.update({k: e[k] for k in ("status", "reason", "message_id") if k in e}, updated_at=at)

    def _e_plan_offer(self, e, at):
        s = self._step(e["plan_id"], e["step_id"])
        s.update(status="selected", offer_id=e["offer_id"], offer_version=e["version"],
                 offer_sha256=e["content_sha256"], updated_at=at)

    def _e_plan_closed(self, e, at):
        p = self.plans[e["plan_id"]]
        p.update(status="closed", outcome=e["outcome"], closed_at=at)
        a = self.accounts[p["account_id"]]
        if a["plan_id"] == e["plan_id"]:
            a["plan_id"] = None

    def _e_survey_new(self, e, at):
        self.surveys[e["survey_id"]] = {"survey_id": e["survey_id"], "account_id": e["account_id"],
                                        "contact_id": e["contact_id"], "message_id": e["message_id"],
                                        "sent_at": at, "score": None, "answered_at": None, "comment_sha256": None}

    def _e_nps_response(self, e, at):
        s = self.surveys[e["survey_id"]]
        s.update(score=e["score"], answered_at=at, comment_sha256=e.get("comment_sha256"))

    def _e_call_new(self, e, at):
        self.calls[e["call_id"]] = {k: e.get(k) for k in ("call_id", "brand", "contact_id", "ticket_id", "direction",
                                                          "started_at", "duration_seconds", "outcome",
                                                          "voicemail_ref", "transcript_ref")}
        self.calls[e["call_id"]].update(recorded_at=at, handed_off=False)

    def _e_call_handoff(self, e, at):
        self.calls[e["call_id"]]["handed_off"] = True

    def _e_routing_set(self, e, at):
        self.routing[e["brand"]] = {**e["rules"], "updated_at": at}

    def _e_job_ran(self, e, at):
        pass

    def _e_key_fingerprint(self, e, at):
        if self.key_fingerprint is None:
            self.key_fingerprint = e["fingerprint"]

    def _key_fp(self) -> str:
        return hmac.new(self.settings.hmac_key, KEY_FINGERPRINT_LABEL, hashlib.sha256).hexdigest()

    def _remove_orphans(self) -> None:
        """Remove stored bodies no log line cites (a body is written before its line; a failed line leaves one).
        V1-M1: the keep-set is every digest cited ANYWHERE in the log, not what memory still points at (a replaced
        consent's text is evidence and is kept)."""
        live: set = set()
        for r in self.log.iter_records():
            _digests(r["data"], live)
        for name in self.bodies.names() - live:
            try:
                self.bodies.delete(name)
            except StoreWriteError:
                pass

    # ============================================================================================ integrity

    def verify_integrity(self, force: bool = False, always: bool = False) -> dict:
        """Complete or set aside the pending line, then check every local line's anchor on the ledger, and that
        the ledger holds no anchor this log lacks (a truncated, rolled back, deleted or replaced log)."""
        with self.lock:
            if self._closed:                              # AEGIS a5dd261 M1: no ledger I/O from a closed instance
                return self._closed_integrity()
            mono = time.monotonic()
            if not force and (self.integrity["ok"] or mono - self._last_integrity_try < INTEGRITY_RETRY_S):
                return dict(self.integrity)
            if force and not always and mono - self._last_integrity_try < FORCED_MIN_S and self._last_integrity_try:
                return dict(self.integrity)
            self._last_integrity_try = mono
            at = iso(self.now())
            try:
                entries = self.rec.client.entries()
            except LedgerQueryFailed:
                self.integrity = {"ok": False, "checked_at": at, "problem": "the ledger cannot be read"}
                return dict(self.integrity)
            mine = [e for e in entries if e.get("department") == DEPARTMENT and e.get("event_type") == "log_anchor"]
            problem, rolled = self._settle_pending({e.get("event_id"): e for e in mine})
            if rolled and self.log.read_pending() is None and self.log.read_discarded() is None:
                self._remove_orphans()
            if problem is None and rolled:
                try:
                    entries = self.rec.client.entries()
                except LedgerQueryFailed:
                    problem = "the ledger cannot be read"
            if problem is None:
                mine = [e for e in entries if e.get("department") == DEPARTMENT and e.get("event_type") == "log_anchor"]
                problem = self._anchor_problem(mine, {e.get("event_id"): e for e in mine})
            self.integrity = {"ok": problem is None, "checked_at": at, "problem": problem}
            if problem is None:
                self._after_integrity()
            return dict(self.integrity)

    def _closed_integrity(self) -> dict:
        return {"ok": False, "checked_at": self.integrity.get("checked_at"), "problem": "this service instance is closed"}

    def audit_integrity(self) -> dict:
        """``/svc/v1/audit/integrity``: a closed instance refuses 503 SERVICE_CLOSED (AEGIS a5dd261 M1 / L2)."""
        return self._integrity_and_ledger(always=False)

    def _integrity_and_ledger(self, always: bool) -> dict:
        """The integrity job and the audit route. The ledger's chain ``verify()`` (an HTTP call, up to the client
        timeout) runs OUTSIDE the service lock (AEGIS cc27b69 L1). It checks the ledger's OWN chain, independent of
        the local log, so its verdict is ALWAYS reported as returned: a False is never dropped, whatever commits
        land meanwhile (AEGIS db08ff1 M). After it, the lock is re-taken, closed is re-checked (closed meanwhile:
        503 SERVICE_CLOSED, nothing written), and the integrity result and log length are read fresh.
        Remaining stall (accepted): ``verify_integrity`` still reads ``entries()`` under the lock."""
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            self.verify_integrity(force=True, always=always)
        ledger_ok = self.rec.client.verify()
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            return {"integrity": dict(self.integrity), "ledger_valid": ledger_ok, "log_length": len(self.log)}

    def _settle_pending(self, by_id: dict) -> tuple[Optional[str], bool]:
        """security-py's (AEGIS R4-1 / R5-1): our own line (kept in memory) is rolled forward; a line found on disk
        is appended only if the ledger ALREADY holds its anchor, else set aside (``pending.discarded``), inert."""
        own = self._own_pending
        if own is not None:
            problem, appended = self._roll_forward(own, by_id, rerecord=True)
            if problem == "not vouched: stale":
                self._own_pending = None
                return self._settle_pending(by_id)
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
            verify_lines(self._raw_lines() + [raw])
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
        try:
            self._apply(kind, data, rec["at"])
        except (KeyError, TypeError, StoreCorrupt):
            return "an anchored pending line could not be applied; an operator must inspect the log", True
        for sha in self._cited_bodies(data):
            if self.bodies.get(sha) is None:
                self._deferred_alerts.append(("BODY_MISSING", sha[:16]))
        return None, True

    @staticmethod
    def _cited_bodies(data: dict) -> list[str]:
        out = []
        for e in data.get("effects", ()):
            for k in ("body_sha256", "subject_sha256", "text_sha256", "comment_sha256"):
                if e.get(k):
                    out.append(e[k])
        return out

    def _raw_lines(self) -> list[bytes]:
        return list(self.log._lines)

    def _anchor_problem(self, mine: list, by_id: dict) -> Optional[str]:
        shas = self.log.line_shas()
        epoch = self.log.epoch
        epochs = {e.get("subject_id") for e in mine}
        if epochs - ({f"log:{epoch}"} if epoch else set()):
            return "the ledger holds anchors of another service log: this log was deleted or replaced"
        for seq, line_sha in enumerate(shas, start=1):
            eid, payload = self._anchor_ids(epoch, seq, line_sha)
            e = by_id.get(eid)
            if e is None or e.get("payload_sha256") != payload_sha256(payload) or e.get("subject_id") != f"log:{epoch}":
                return f"local log line {seq} has no matching anchor on the ledger"
        if len(mine) > len(shas):
            return "the ledger holds anchors beyond the local log: the log was truncated or rolled back"
        return None

    def _gate(self) -> None:
        if self._closed:
            raise Unavailable(R("SERVICE_CLOSED"))
        if not self.verify_integrity()["ok"]:
            raise Unavailable(R("INTEGRITY_UNVERIFIED"))

    def _after_integrity(self) -> None:
        if self.key_fingerprint is None:                # V2-M1: written once, at the first verified start
            try:
                self._commit("key_fingerprint", {"effects": [{"op": "key_fingerprint",
                                                              "fingerprint": self._key_fp()}]}, INTERNAL)
            except Unavailable:
                pass
        pending, self._deferred_alerts = self._deferred_alerts, []
        if pending:
            effects = [self._alert_effect(code, subject, f"deferred:{code}:{subject}") for code, subject in pending]
            try:
                self._commit("alerts_raised", {"effects": effects}, INTERNAL)
            except Unavailable:
                self._deferred_alerts = pending

    # ============================================================================================ status

    def health(self) -> dict:
        with self.lock:
            closed = self._closed            # round 5c item 3: a closed instance is never reported ok
            return {
                "status": "closed" if closed else ("ok" if self.integrity["ok"] else "degraded"),
                "closed": closed,
                "integrity": self._closed_integrity() if closed else dict(self.integrity),
                "in_memory": self.log.in_memory,
                "non_production": self.settings.non_production,
                "andre_approvals_configured": None,          # filled by the API (the gate lives there)
                "support_email": {b: bool(self.settings.support_email.get(b)) for b in BRAND_NAMES},
                "sms_number": {b: bool(self.settings.sms_number.get(b)) for b in BRAND_NAMES},
                "wired": self.ports.wired(),
                "open_tickets": sum(1 for t in self.tickets.values() if t["status"] not in ("resolved", "closed")),
                "queued_outbound": sum(1 for m in self.messages.values() if m["dir"] == "out"
                                       and m["status"] == "queued"),
                "alerts_undelivered": sum(1 for a in self.alerts.values() if a["status"] != "delivered"),
                "at_risk_accounts": sum(1 for a in self.accounts.values() if a["at_risk"]),
                "sla": {"first_response_minutes": dict(self.settings.sla_first),
                        "resolution_minutes": dict(self.settings.sla_resolution)},
                "at_risk_threshold": self.settings.at_risk_threshold,
                "log_length": len(self.log),
            }

    # ============================================================================================ helpers

    def _alert_effect(self, code: str, subject: str, ident: str) -> dict:
        return {"op": "alert_new", "alert_id": derived_id("alr", code, subject, ident), "code": R(code),
                "subject": subject}

    def _addr_key(self, channel: str, address: str) -> str:
        return hmac.new(self.settings.hmac_key, f"{channel}:{address}".encode("utf-8"), hashlib.sha256).hexdigest()

    def _addr_revoked_at(self, brand: str, channel: str, address: Optional[str]) -> Optional[str]:
        return self.addr_revocations.get((brand, channel, self._addr_key(channel, address))) if address else None

    def _live_consent(self, contact: dict, channel: str) -> Optional[dict]:
        """The contact's consent for a channel, unless its address was revoked (on ANY contact of the brand) after it
        was captured (V3-C2)."""
        c = self.consents.get((contact["contact_id"], channel))
        if c and c.get("status") == "active":
            revoked = self._addr_revoked_at(contact["brand"], channel, c.get("address"))
            if revoked is not None and (not c.get("captured_at") or c["captured_at"] <= revoked):
                return None
        return c

    def _channel_check(self, channel: str, contact: dict, proactive: bool, opt_out: bool = False) -> Optional[str]:
        return channels.check(channel, contact, lambda ch: self._live_consent(contact, ch), proactive,
                              self.now(), opt_out, self.sms_paused_for(contact))

    def sms_paused_for(self, contact: dict) -> bool:
        """Proactive SMS to this contact's CURRENT number is paused, whichever contact the pause was recorded on."""
        phone = contact.get("phone")
        return bool(phone) and (contact["brand"], self._addr_key("sms", phone)) in self.sms_paused

    def _named_contacts(self, brand: str, text: str, exclude: str) -> list[dict]:
        """Contacts of this brand a message names by phone number or email address (V3-M2)."""
        found = []
        for raw in PHONE_RE.findall(text):
            digits = re.sub(r"[^0-9]", "", raw)
            for num in {"+" + digits, "+1" + digits}:
                cid = self.contact_index.get((brand, "phone", num))
                if cid and cid != exclude and cid not in [c["contact_id"] for c in found]:
                    found.append(self.contacts[cid])
        for addr in EMAIL_RE.findall(text.lower()):
            cid = self.contact_index.get((brand, "email", addr))
            if cid and cid != exclude and cid not in [c["contact_id"] for c in found]:
                found.append(self.contacts[cid])
        return found

    def _pause_effects(self, contacts: list, extra_phone: Optional[str] = None) -> list:
        """Pause proactive SMS to every phone of these contacts (V3-C1); the first may also be paused on the number
        the message came from."""
        out, seen = [], set()
        for i, c in enumerate(contacts):
            for phone in {c.get("phone"), extra_phone if i == 0 else None} - {None}:
                if (c["contact_id"], phone) not in seen:
                    seen.add((c["contact_id"], phone))
                    out.append({"op": "sms_paused", "contact_id": c["contact_id"], "phone": phone})
        return out

    def clear_sms_pause(self, contact_id: str, body: dict) -> dict:
        """Andre clears a pause on proactive SMS. It never restores a consent: a revoked consent stays revoked."""
        with self.lock:
            self._gate()
            prev = self._idem("andre", "sms_pause_clear", contact_id, body)
            if prev is not None:
                return prev
            c = self.contacts.get(contact_id)
            if c is None:
                raise NotFound(R("CONTACT_NOT_FOUND"))
            if not self.sms_paused_for(c):
                raise Conflict(R("NOT_PAUSED"))
            resp = {"contact_id": contact_id, "sms_paused": False,
                    "sms_consent": (self.consents.get((contact_id, "sms")) or {}).get("status")}
            self._commit("sms_pause_cleared", {"effects": [{"op": "sms_pause_cleared", "contact_id": contact_id,
                                                            "phone": c["phone"]}],
                                               "request": self._req("andre", "sms_pause_clear", contact_id, body),
                                               "response": resp}, "andre")
            return resp

    def _put_body(self, text: str) -> str:
        try:
            return self.bodies.put(text)
        except StoreWriteError:
            raise Unavailable(R("STORE_UNAVAILABLE")) from None

    def _due(self, created: datetime, priority: str) -> tuple[str, str]:
        return (iso(created + timedelta(minutes=self.settings.sla_first[priority])),
                iso(created + timedelta(minutes=self.settings.sla_resolution[priority])))

    def _usable(self, catalog: str, item_id: Optional[str], version=None, sha=None) -> Optional[dict]:
        """The item when it is active, its CURRENT version approved, and that approval bound to exactly its content
        (recomputed here: an edit is a new, unapproved version; a tampered record never matches) — and, when given,
        still the version and content the caller relied on."""
        item = self.catalog[catalog].get(item_id or "")
        if item is None or item["status"] != "active":
            return None
        appr = item["approved"]
        content = {k: item[k] for k in CATALOG_CONTENT[catalog]}
        if not appr or appr["version"] != item["version"] or appr["content_sha256"] != item["content_sha256"] \
                or catalog_sha(catalog, content) != item["content_sha256"]:
            return None
        if version is not None and (item["version"] != version or item["content_sha256"] != sha):
            return None
        return item

    def _template(self, purpose: str, brand: str, channel: str) -> Optional[dict]:
        cands = [t for t in self.catalog["template"].values() if t["purpose"] == purpose and t["brand"] == brand
                 and channel in t["channels"] and self._usable("template", t["item_id"])]
        return sorted(cands, key=lambda t: t["item_id"])[0] if cands else None

    def _render(self, template: dict, contact: dict, extra: dict) -> str:
        first = (contact.get("display_name") or "").split(" ")[0] or "there"
        values = {"first_name": first, "brand_name": BRAND_NAMES[template["brand"]], **extra}
        # single pass (round 2): a value containing "{offer_terms}" is inserted as text, never expanded again
        return PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "")), template["text"])

    def _out_effect(self, message_id: str, contact: dict, brand: str, channel: str, origin: str, text: str,
                    proactive: bool, status: str = "queued", ticket_id: Optional[str] = None,
                    ref: Optional[dict] = None, subject: Optional[str] = None, **extra) -> dict:
        if channel == "sms" and origin != "opt_out":
            text = text + SMS_FOOTER
        sha = self._put_body(text)
        return {"op": "message_out", "message_id": message_id, "ticket_id": ticket_id,
                "contact_id": contact["contact_id"], "brand": brand, "channel": channel, "origin": origin,
                "ref": ref, "body_sha256": sha, "subject": subject, "proactive": proactive, "status": status, **extra}

    def _pick_proactive_channel(self, contact: dict) -> str:
        for ch in ("email", "sms"):
            reason = self._channel_check(ch, contact, proactive=True)
            if reason in (None, "QUIET_HOURS"):
                return ch
        return "chat"

    # ============================================================================================ contacts, consent

    def save_contact(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            target = f"{body['brand']}:{body['contact_ref']}"
            prev = self._idem(actor, "contact", target, body)
            if prev is not None:
                return prev
            fields = {k: body.get(k) for k in ("account_id", "email", "phone", "timezone", "display_name")}
            if fields["timezone"] is not None and channels.zone(fields["timezone"]) is None:
                raise Invalid(R("TIMEZONE_UNKNOWN"))
            cid = self.contact_index.get((body["brand"], "contact_ref", body["contact_ref"]))
            for kind in ("email", "phone"):
                other = fields[kind] and self.contact_index.get((body["brand"], kind, fields[kind]))
                if other and other != cid:
                    raise Conflict(R("CONTACT_ADDRESS_TAKEN"))
            if cid is None:
                cid = self._did("con", actor, "contact", target, body["request_id"])
                effect = {"op": "contact_new", "contact_id": cid, "brand": body["brand"],
                          "contact_ref": body["contact_ref"], **fields}
            else:
                effect = {"op": "contact_update", "contact_id": cid,
                          "fields": {k: v for k, v in fields.items() if v is not None}}
            effects = [effect]
            old = self.contacts.get(cid) or {}
            for kind, ch in (("phone", "sms"), ("email", "email")):
                if fields[kind] and old.get(kind) and old[kind] != fields[kind]:
                    c = self.consents.get((cid, ch))
                    if c and c["status"] == "active":          # V1-C1: a new address ends the old address's consent
                        effects.insert(0, {"op": "consent_revoked", "contact_id": cid, "channel": ch,
                                           "via": "address_changed", "address": old[kind],
                                           "request_id": body["request_id"]})
            resp = {"contact_id": cid, "brand": body["brand"]}
            self._commit("contact_saved", {"effects": effects, "request": self._req(actor, "contact", target, body),
                                           "response": resp}, actor)
            return resp

    def contact_view(self, contact_id: str) -> dict:
        with self.lock:
            c = self.contacts.get(contact_id)
            if c is None:
                raise NotFound(R("CONTACT_NOT_FOUND"))
            return {**c, "consents": self._consents_of(contact_id),
                    "sms_paused": self.sms_paused_for(c)}

    def _consents_of(self, contact_id: str) -> list[dict]:
        return [dict(v) for (cid, _), v in sorted(self.consents.items()) if cid == contact_id]

    def consents_view(self, contact_id: str) -> list[dict]:
        with self.lock:
            if contact_id not in self.contacts:
                raise NotFound(R("CONTACT_NOT_FOUND"))
            return self._consents_of(contact_id)

    def record_consent(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            target = f"{body['contact_id']}:{body['channel']}"
            prev = self._idem(actor, "consent", target, body)
            if prev is not None:
                return prev
            contact = self.contacts.get(body["contact_id"])
            if contact is None:
                raise NotFound(R("CONTACT_NOT_FOUND"))
            if body["channel"] == "sms" and not contact.get("phone"):
                raise Invalid(R("NO_ADDRESS"))
            if body["channel"] == "email" and not contact.get("email"):
                raise Invalid(R("NO_ADDRESS"))
            captured = parse_iso(body["captured_at"])
            if captured > self.now() + timedelta(minutes=5):
                raise Invalid(R("CONSENT_IN_FUTURE"))
            address = contact["phone"] if body["channel"] == "sms" else contact["email"]
            if body["address"] != address:
                raise Conflict(R("CONSENT_ADDRESS_MISMATCH"))         # V2-H2: consent names the address it is for
            changed = self.address_changed_at.get((contact["contact_id"], "phone" if body["channel"] == "sms"
                                                   else "email"))
            if changed is not None and captured < parse_iso(changed):
                raise Conflict(R("CONSENT_PREDATES_ADDRESS"))         # V2-H2: captured before this address was theirs
            revoked = self._addr_revoked_at(contact["brand"], body["channel"], address)
            if revoked is not None and captured <= parse_iso(revoked):
                # V1-H4 / V3-C2: an old capture never undoes a STOP, on this contact or any other with this address
                raise Conflict(R("CONSENT_PREDATES_REVOCATION"))
            if captured < parse_iso(contact["created_at"]):
                raise Conflict(R("CONSENT_PREDATES_CONTACT"))         # V3-C2: captured before the contact existed
            text_sha = self._put_body(body["consent_text"])
            cid = self._did("cns", actor, "consent", target, body["request_id"])
            effect = {"op": "consent_granted", "consent_id": cid, "contact_id": contact["contact_id"],
                      "channel": body["channel"], "source": body["source"], "text_sha256": text_sha,
                      "captured_at": iso(captured), "express": True, "address": address}
            resp = {"consent_id": cid, "contact_id": contact["contact_id"], "channel": body["channel"],
                    "status": "active", "consent_text_sha256": text_sha}
            self._commit("consent_granted", {"effects": [effect], "request": self._req(actor, "consent", target, body),
                                             "response": resp}, actor)
            return resp

    def revoke_consent(self, actor: str, body: dict, andre: bool = False) -> dict:
        with self.lock:
            self._gate()
            target = f"{body['contact_id']}:{body['channel']}"
            prev = self._idem(actor, "revoke", target, body)
            if prev is not None:
                return prev
            if body["contact_id"] not in self.contacts:
                raise NotFound(R("CONTACT_NOT_FOUND"))
            # V1-L1: "andre" only with his verified token; the dashboard alone is "dashboard"
            via = ("andre" if andre else "dashboard") if actor == "dashboard" else "contact_request"
            c = self.contacts[body["contact_id"]]
            effect = {"op": "consent_revoked", "contact_id": body["contact_id"], "channel": body["channel"],
                      "via": via, "request_id": body["request_id"],
                      "address": c.get("phone") if body["channel"] == "sms" else c.get("email")}
            resp = {"contact_id": body["contact_id"], "channel": body["channel"], "status": "revoked"}
            self._commit("consent_revoked", {"effects": [effect], "request": self._req(actor, "revoke", target, body),
                                             "response": resp}, actor)
            return resp

    # ============================================================================================ inbound

    def _find_or_new_contact(self, actor: str, brand: str, kind: str, value: str, request_id: str,
                             effects: list, extra: Optional[dict] = None) -> dict:
        cid = self.contact_index.get((brand, kind, value))
        if cid is not None:
            return self.contacts[cid]
        cid = self._did("con", actor, "inbound-contact", f"{brand}:{kind}", request_id)
        c = {"contact_id": cid, "brand": brand, "contact_ref": None, "account_id": None, "email": None,
             "phone": None, "timezone": None, "display_name": None, **(extra or {})}
        c[kind] = value
        effects.append({"op": "contact_new", **c})
        return c

    def _ticket_for(self, actor: str, contact: dict, brand: str, channel: str, ticket_id: Optional[str],
                    request_id: str, effects: list, new_contact: bool) -> tuple[dict, bool]:
        """The ticket this message belongs to: the one named (it must be this contact's, same brand), else the
        contact's latest ticket in this brand that is not closed, else a new one. Returns (ticket, is_new)."""
        t = None
        if ticket_id is not None:
            t = self.tickets.get(ticket_id)
            if t is None or t["contact_id"] != contact["contact_id"] or t["brand"] != brand:
                raise NotFound(R("TICKET_NOT_FOUND"))
            if t["status"] == "closed":
                t = None
        elif not new_contact:
            open_t = [x for x in self.tickets.values() if x["contact_id"] == contact["contact_id"]
                      and x["brand"] == brand and x["status"] != "closed"]
            t = max(open_t, key=lambda x: (x["created_at"], x["ticket_id"])) if open_t else None
        if t is not None:
            return t, False
        tid = self._did("tkt", actor, "ticket", f"{brand}:{channel}", request_id)
        first_due, res_due = self._due(self.now(), "p3")
        t = {"ticket_id": tid, "brand": brand, "contact_id": contact["contact_id"],
             "account_id": contact.get("account_id"), "channel": channel, "priority": "p3", "status": "open",
             "queue": "bot", "reopened": 0, "first_response_at": None, "created_at": iso(self.now())}
        effects.append({"op": "ticket_new", "ticket_id": tid, "brand": brand, "contact_id": contact["contact_id"],
                        "account_id": contact.get("account_id"), "channel": channel, "priority": "p3",
                        "first_due": first_due, "resolution_due": res_due})
        return t, True

    def _recent_inbound(self, contact_id: str) -> int:
        since = iso(self.now() - timedelta(hours=24))
        return sum(1 for m in self.messages.values() if m["dir"] == "in" and m["contact_id"] == contact_id
                   and m["at"] >= since)

    def inbound(self, actor: str, channel: str, body: dict) -> dict:
        """One inbound chat / email / SMS message: contact, ticket, triage, then an approved answer or an
        escalation — one line. Handoffs and alerts are sent after the line, outside the lock."""
        with self.lock:
            self._gate()
            brand = body["brand"]
            target = f"{brand}:{channel}"
            try:
                prev = self._idem(actor, f"inbound_{channel}", target, body)
            except Conflict:
                if channel not in ("email", "sms"):
                    raise                     # our own hub (chat) reusing an id is a caller bug: 409 as before
                # AEGIS re-review N3: a gateway that reuses a request id for another message must not get that
                # message (an opt-out, say) refused: it is re-keyed with its own body hash, as sales-py does
                orig = body["request_id"]
                rid_h = hashlib.sha256(orig.encode("utf-8", "surrogatepass")).hexdigest()[:8]
                body = {**body, "request_id": f"{orig[:80]}.r{rid_h}.b{body_sha(body)[:16]}"}
                try:
                    prev = self._idem(actor, f"inbound_{channel}", target, body)
                except Conflict:              # AEGIS R7: a literal id of that shape with yet another body
                    body = {**body, "request_id": f"rk.{body_sha(body)}"}
                    prev = self._idem(actor, f"inbound_{channel}", target, body)
            if prev is not None:
                return prev
            effects: list = []
            rid = body["request_id"]
            if channel == "email":
                identity = self.settings.support_email.get(brand)
                if not identity:
                    raise Unavailable(R("BRAND_EMAIL_NOT_CONFIGURED"))
                if body["to_address"] != identity:
                    raise Invalid(R("WRONG_BRAND_IDENTITY"))
                contact = self._find_or_new_contact(actor, brand, "email", body["from_address"], rid, effects)
            elif channel == "sms":
                number = self.settings.sms_number.get(brand)
                if not number:
                    raise Unavailable(R("BRAND_SMS_NOT_CONFIGURED"))
                if body["to_number"] != number:
                    raise Invalid(R("WRONG_BRAND_IDENTITY"))
                contact = self._find_or_new_contact(actor, brand, "phone", body["from_number"], rid, effects)
            else:
                contact = self._find_or_new_contact(actor, brand, "contact_ref", body["contact_ref"], rid, effects)
            new_contact = bool(effects)
            text = body["text"]
            # Opt-outs (V1-H3, V2-H3, V3): checked on EVERY channel, body and subject. "exact" revokes SMS consent
            # anywhere; "suspected" (a typo, a negation near text/sms/phone) revokes on SMS and, on chat or email,
            # pauses proactive SMS and asks Andre. Any inbound the bot does not answer pauses proactive SMS to every
            # phone of the resolved contact and of any contact the message names (V3-C1, V3-M2).
            # sweep A (AEGIS H1): an email is classified on the person's own words — quoted lines and the quoted
            # thread below a reply header are someone else's (often our own footer)
            own = channels.strip_quoted(text) if channel == "email" else text
            levels = {channels.opt_out_level(own), channels.opt_out_level(body["subject"]) if body.get("subject")
                      else None}
            level = "exact" if "exact" in levels else ("suspected" if "suspected" in levels else None)
            # AEGIS re-review of 1e709a0 (N1): strong opt-out wording in the unmarked quoted tail (an Outlook
            # "Original Message" block, "On ... wrote:" with no ">" lines) may be a reply typed below the quote: it is
            # honoured (over-suppressing is the safe side) and Andre is told, since it may be quoted text
            tail = channels.quoted_tail_opt_out(text) if channel == "email" and level != "exact" else None
            tail_opt_out = tail == "revoke"
            if tail_opt_out:
                level = "exact"
            revoke = level == "exact" or (level == "suspected" and channel == "sms")
            sms_number = body["from_number"] if channel == "sms" else contact.get("phone")
            named = self._named_contacts(brand, text + " " + (body.get("subject") or ""), contact["contact_id"])
            pause = self._pause_effects([contact] + named, extra_phone=sms_number)
            resp = {"contact_id": contact["contact_id"]}
            # sweep A (AEGIS H1, M1): an opt-out received BY EMAIL revokes EMAIL consent unless every opt-out phrase in
            # the person's own words names the phone; a typo of unsubscribe / stop by email revokes it too
            decision = (channels.email_opt_out_decision(text, body.get("subject"))
                        if channel == "email" and contact.get("email") and level else None)
            email_revoke = decision == "revoke"
            if decision == "ask":        # AEGIS H-F: an unclear email scope is never guessed: SMS only, Andre decides
                effects.append(self._alert_effect("EMAIL_OPT_OUT_UNCLEAR", contact["contact_id"], rid))
            email_effect = {"op": "consent_revoked", "contact_id": contact["contact_id"], "channel": "email",
                            "via": "stop_by_email" if level == "exact" else "suspected_stop_by_email",
                            "request_id": rid, "address": contact.get("email")}
            if revoke:
                had = channel == "sms" and channels.consent_matches(
                    self.consents.get((contact["contact_id"], "sms")), sms_number)
                effects.append({"op": "consent_revoked", "contact_id": contact["contact_id"], "channel": "sms",
                                "via": "stop_keyword" if channel == "sms" else f"stop_by_{channel}",
                                "request_id": rid, "address": sms_number})
                if email_revoke:
                    effects.append(email_effect)
                    resp["email_opted_out"] = True
                resp["action"] = "opted_out"
                if tail_opt_out:
                    effects.append(self._alert_effect("OPT_OUT_IN_QUOTED_TEXT", contact["contact_id"], rid))
                if had:     # V3-C3: the one confirmation goes ONLY to the number that sent the STOP
                    conf = channels.OPT_OUT_CONFIRMATION.replace("{brand_name}", BRAND_NAMES[brand])
                    cmid = self._did("msg", actor, "opt_out_confirmation", target, rid)
                    effects.append(self._out_effect(cmid, contact, brand, "sms", "opt_out", conf, False,
                                                    bound_to=sms_number))
                    resp["confirmation_message_id"] = cmid
                if new_contact or named:
                    # V3-M2: an opt-out from an address no contact had (or naming another contact): Andre sees it
                    effects.append(self._alert_effect("OPT_OUT_UNKNOWN_SENDER", contact["contact_id"], rid))
                elif channel == "sms" and len(triage_mod.normalise(text).split()) <= OPT_OUT_ONLY_WORDS:
                    self._commit("sms_opt_out", {"effects": effects + pause,
                                                 "request": self._req(actor, f"inbound_{channel}", target, body),
                                                 "response": resp}, actor)
                    return resp
            elif tail == "alert":            # AEGIS R2/R4: opt-out wording in the quoted tail: Andre reads it
                effects.append(self._alert_effect("OPT_OUT_IN_QUOTED_TEXT", contact["contact_id"], rid))
            if not revoke and level == "suspected":
                if email_revoke:             # AEGIS M1: err toward honouring; consent_changed is its typed evidence
                    effects.append(email_effect)
                    resp["email_opted_out"] = True
                effects.append(self._alert_effect("SMS_OPT_OUT_SUSPECTED", contact["contact_id"], rid))
            ticket, is_new = self._ticket_for(actor, contact, brand, channel, body.get("ticket_id"), rid, effects,
                                              new_contact)
            if not is_new and ticket["status"] in ("pending_customer", "resolved"):
                effects.append({"op": "ticket_status", "ticket_id": ticket["ticket_id"], "status": "open"})
                if ticket["status"] == "resolved":
                    ticket = {**ticket, "reopened": ticket["reopened"] + 1}
            tri = triage_mod.classify(text, self._recent_inbound(contact["contact_id"]), ticket["reopened"],
                                      body.get("subject"))
            if level:
                tri = triage_mod.Triage(tri.primary if tri.categories else "other", tri.categories,
                                        tri.signals + (f"sms:opt_out_{level}",), False, tri.question_count)
            body_sha_ = self._put_body(text)
            subject_sha = self._put_body(body["subject"]) if body.get("subject") else None
            mid = self._did("msg", actor, f"inbound_{channel}", target, rid)
            effects.append({"op": "message_in", "message_id": mid, "ticket_id": ticket["ticket_id"],
                            "contact_id": contact["contact_id"], "brand": brand, "channel": channel,
                            "body_sha256": body_sha_, "subject_sha256": subject_sha,
                            "triage": {"primary": tri.primary, "categories": list(tri.categories),
                                       "signals": list(tri.signals)}})
            resp.update(ticket_id=ticket["ticket_id"], message_id=mid, **({"opted_out": True} if revoke else {}))
            resp.pop("action", None)
            answered = False
            why = None
            if tri.categories:
                why = "escalation"
            elif level:
                why = "opt_out"
            elif ticket["queue"] != "bot":
                why = "ticket_with_human"            # a follow-up on Andre's ticket is his, never the bot's
            else:
                # V3-H1: only a message that IS an approved example question (after exact_form) is answered
                article, why = kb.match(list(self.catalog["kb"].values()), text, brand, channel)
                if article is not None and body.get("subject") and kb.exact_form(body["subject"]) not in (
                        NEUTRAL_SUBJECTS | {kb.exact_form(q) for q in article["questions"]}
                        | {kb.exact_form(article["title"])}):
                    article, why = None, "subject_not_neutral"
                reason = self._channel_check(channel, contact, proactive=False) if article else None
                if article is not None and reason in (None, "QUIET_HOURS"):
                    answered = True
                    out_id = self._did("msg", actor, f"answer_{channel}", target, rid)
                    status = "sent" if channel == "chat" else "queued"     # chat: answered inline, right away
                    subj = f"Re: your message to {BRAND_NAMES[brand]}" if channel == "email" else None
                    effects.append(self._out_effect(out_id, contact, brand, channel, "kb", article["answer"], False,
                                                    status, ticket["ticket_id"],
                                                    {"catalog": "kb", "item_id": article["article_id"],
                                                     "version": article["version"],
                                                     "content_sha256": article["content_sha256"]}, subj))
                    effects.append({"op": "first_response", "ticket_id": ticket["ticket_id"]})
                    effects.append({"op": "ticket_status", "ticket_id": ticket["ticket_id"],
                                    "status": "pending_customer", "answered_by": "kb", "queue": "bot"})
                    resp.update(action="answered", answer_message_id=out_id)
                    if channel == "chat":
                        resp["answer"] = {"text": article["answer"], "article_id": article["article_id"]}
                elif article is not None:
                    why = reason
            if not answered:
                tri = triage_mod.Triage("no_answer" if not tri.categories else tri.primary, tri.categories,
                                        tri.signals + (f"kb:{why}",), False, tri.question_count)
                effects += pause
                effects += self._escalate_effects(actor, ticket, tri, target, rid)
                resp["action"] = "escalated" if tri.categories else "queued_for_human"
            priority = triage_mod.PRIORITY.get(tri.primary, "p3")
            if PRIORITIES.index(priority) < PRIORITIES.index(ticket["priority"]):
                created = parse_iso(ticket["created_at"])
                fd, rd = self._due(created, priority)
                effects.append({"op": "ticket_priority", "ticket_id": ticket["ticket_id"], "priority": priority,
                                "first_due": fd, "resolution_due": rd})
            self._commit(f"inbound_{channel}", {"effects": effects,
                                                "request": self._req(actor, f"inbound_{channel}", target, body),
                                                "response": resp}, actor)
        self.dispatch_side_effects()
        return resp

    def _escalate_effects(self, actor: str, ticket: dict, tri, target: str, rid: str) -> list:
        """Every category routes (triage.routes_for); Andre's queue always; one alert per category to Andre."""
        effects: list = []
        routes = triage_mod.routes_for(tri)
        if tri.categories:
            effects.append({"op": "ticket_escalated", "ticket_id": ticket["ticket_id"],
                            "categories": list(tri.categories)})
        elif ticket["status"] != "escalated":          # never demote an escalated ticket
            effects.append({"op": "ticket_status", "ticket_id": ticket["ticket_id"], "status": "open",
                            "queue": "andre"})
        for dept in routes:
            if dept == "andre":
                continue
            cat = next(c for c in tri.categories if dept in triage_mod.ROUTES[c])
            kind = triage_mod.legal_kind(tri) if dept == "legal_37" else cat
            effects.append({"op": "handoff_new", "handoff_id": self._did("hof", actor, "handoff", target, rid, dept),
                            "ticket_id": ticket["ticket_id"], "department": dept, "category": cat, "kind": kind})
        for cat in tri.categories:
            effects.append(self._alert_effect(ALERT_CATEGORIES[cat], ticket["ticket_id"], f"{target}:{rid}"))
        return effects

    def thread(self, ticket_id: str, contact_ref: str, brand: str) -> dict:
        """The chat thread as the hub shows it to the contact: their messages and ours (queued ones marked)."""
        with self.lock:
            t = self.tickets.get(ticket_id)
            c = self.contacts.get(t["contact_id"]) if t else None
            if t is None or c is None or c.get("contact_ref") != contact_ref or t["brand"] != brand:
                raise NotFound(R("TICKET_NOT_FOUND"))
            out = []
            for mid in t["messages"]:
                m = self.messages[mid]
                if m["dir"] == "out" and m["status"] == "cancelled":
                    continue
                out.append({"message_id": mid, "from": "contact" if m["dir"] == "in" else "team", "at": m["at"],
                            "channel": m["channel"], "status": m.get("status", "received"),
                            "text": self.bodies.get(m["body_sha256"]) if m.get("body_sha256") else None})
            return {"ticket_id": ticket_id, "status": t["status"], "messages": out}

    # ============================================================================================ tickets (Andre)

    def tickets_view(self, status: Optional[str], brand: Optional[str], queue: Optional[str]) -> list[dict]:
        with self.lock:
            out = [self._ticket_summary(t) for t in self.tickets.values()
                   if (status is None or t["status"] == status) and (brand is None or t["brand"] == brand)
                   and (queue is None or t["queue"] == queue)]
            return sorted(out, key=lambda t: (t["priority"], t["created_at"], t["ticket_id"]))

    def _ticket_summary(self, t: dict) -> dict:
        return {k: t[k] for k in ("ticket_id", "brand", "contact_id", "account_id", "channel", "priority", "status",
                                  "queue", "categories", "created_at", "updated_at", "first_response_at",
                                  "first_due", "resolution_due", "resolved_at", "breaches", "answered_by",
                                  "reopened")}

    def ticket_view(self, ticket_id: str) -> dict:
        with self.lock:
            t = self.tickets.get(ticket_id)
            if t is None:
                raise NotFound(R("TICKET_NOT_FOUND"))
            msgs = []
            for mid in t["messages"]:
                m = self.messages[mid]
                v = {k: m.get(k) for k in ("message_id", "dir", "channel", "at", "status", "origin", "ref", "reason",
                                           "triage", "proactive")}
                v["text"] = self.bodies.get(m["body_sha256"]) if m.get("body_sha256") else None
                msgs.append(v)
            return {**self._ticket_summary(t), "messages": msgs,
                    "handoffs": [self._handoff_view(self.handoffs[h]) for h in t["handoffs"]]}

    def _handoff_view(self, h: dict) -> dict:
        port = self.ports.handoffs.get(h["department"])
        status = h["status"]
        if status == "pending" and not getattr(port, "wired", False):
            status = NOT_WIRED
        return {**h, "status": status}

    def reply(self, ticket_id: str, body: dict) -> dict:
        """Andre's own reply (a human answer; the only free text that leaves this service)."""
        with self.lock:
            self._gate()
            prev = self._idem("andre", "reply", ticket_id, body)
            if prev is not None:
                return prev
            t = self.tickets.get(ticket_id)
            if t is None:
                raise NotFound(R("TICKET_NOT_FOUND"))
            if t["status"] == "closed":
                raise Conflict(R("TICKET_CLOSED"))
            channel = body.get("channel") or t["channel"]
            if channel == "phone":
                raise Invalid(R("CHANNEL_NOT_OUTBOUND"))
            contact = self.contacts[t["contact_id"]]
            reason = self._channel_check(channel, contact, proactive=False)
            if reason not in (None, "QUIET_HOURS"):
                raise Conflict(R(reason))
            mid = self._did("msg", "andre", "reply", ticket_id, body["request_id"])
            subj = f"Re: your message to {BRAND_NAMES[t['brand']]}" if channel == "email" else None
            effects = [self._out_effect(mid, contact, t["brand"], channel, "human", body["text"], False, "queued",
                                        ticket_id, None, subj),
                       {"op": "first_response", "ticket_id": ticket_id},
                       {"op": "ticket_status", "ticket_id": ticket_id, "status": "pending_customer",
                        "answered_by": "andre"}]
            resp = {"ticket_id": ticket_id, "message_id": mid, "status": "queued", "waiting_for":
                    "quiet_hours" if reason == "QUIET_HOURS" else None}
            self._commit("reply", {"effects": effects, "request": self._req("andre", "reply", ticket_id, body),
                                   "response": resp}, "andre")
        self.dispatch_side_effects()
        return resp

    def set_status(self, actor: str, ticket_id: str, body: dict, andre: bool = False) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem(actor, "status", ticket_id, body)
            if prev is not None:
                return prev
            t = self.tickets.get(ticket_id)
            if t is None:
                raise NotFound(R("TICKET_NOT_FOUND"))
            if body["status"] not in TRANSITIONS[t["status"]]:
                raise Conflict(R("TRANSITION_NOT_ALLOWED"))
            if body["status"] in ("resolved", "closed") and set(t["categories"]) & SENSITIVE and not andre:
                raise FounderRefused(R("ANDRE_APPROVAL_REQUIRED"))   # V1-L2: Andre closes money/legal/complaints
            effect = {"op": "ticket_status", "ticket_id": ticket_id, "status": body["status"]}
            if body["status"] == "escalated":
                effect = {"op": "ticket_escalated", "ticket_id": ticket_id, "categories": []}
            resp = {"ticket_id": ticket_id, "status": body["status"]}
            self._commit("ticket_status", {"effects": [effect], "request": self._req(actor, "status", ticket_id, body),
                                           "response": resp}, actor)
            return resp

    def set_priority(self, actor: str, ticket_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem(actor, "priority", ticket_id, body)
            if prev is not None:
                return prev
            t = self.tickets.get(ticket_id)
            if t is None:
                raise NotFound(R("TICKET_NOT_FOUND"))
            if t["status"] == "closed":
                raise Conflict(R("TICKET_CLOSED"))
            fd, rd = self._due(parse_iso(t["created_at"]), body["priority"])
            resp = {"ticket_id": ticket_id, "priority": body["priority"], "first_due": fd, "resolution_due": rd}
            self._commit("ticket_priority", {"effects": [{"op": "ticket_priority", "ticket_id": ticket_id,
                                                          "priority": body["priority"], "first_due": fd,
                                                          "resolution_due": rd}],
                                             "request": self._req(actor, "priority", ticket_id, body),
                                             "response": resp}, actor)
            return resp

    # ============================================================================================ catalog

    def save_item(self, catalog: str, body: dict) -> dict:
        """A new version of a KB article, template or offer. Always unapproved: editing un-approves."""
        with self.lock:
            self._gate()
            item_id = body["item_id"]
            prev = self._idem("dashboard", f"{catalog}_save", item_id, body)
            if prev is not None:
                return prev
            content = {k: body[k] for k in CATALOG_CONTENT[catalog]}
            if catalog == "template":
                bad = set(re.findall(r"\{([^{}]*)\}", content["text"])) - set(PLACEHOLDERS)
                if bad or content["text"].count("{") != content["text"].count("}"):
                    raise Invalid(R("TEMPLATE_PLACEHOLDER"))
                if content["purpose"] == "offer" and "{offer_terms}" not in content["text"]:
                    raise Invalid(R("TEMPLATE_PLACEHOLDER"))
            if catalog == "kb":
                if any(question_denied(q) for q in content["questions"]):
                    raise Invalid(R("QUESTION_DENIED"))     # V3-M1: an example question never carries a category
                if len({kb.exact_form(q) for q in content["questions"]}) != len(content["questions"]) \
                        or not all(kb.exact_form(q) for q in content["questions"]):
                    raise Invalid(R("INVALID"))
            if catalog == "offer":
                Decimal(content["price"])                      # the model already pins the format
            cur = self.catalog[catalog].get(item_id)
            if cur is not None and cur["status"] == "retired":
                raise Conflict(R("ITEM_RETIRED"))
            sha = catalog_sha(catalog, content)
            if cur is not None and cur["content_sha256"] == sha:
                raise Conflict(R("ITEM_UNCHANGED"))
            version = (cur["version"] + 1) if cur else 1
            resp = {"item_id": item_id, "version": version, "content_sha256": sha, "approved": False}
            if catalog == "kb":
                resp["warnings"] = self._question_warnings(content)
            self._commit(f"{catalog}_saved", {"effects": [{"op": "catalog_saved", "catalog": catalog,
                                                           "item_id": item_id, "version": version,
                                                           "content": content, "content_sha256": sha}],
                                              "request": self._req("dashboard", f"{catalog}_save", item_id, body),
                                              "response": resp}, "dashboard")
            return resp

    def approve_item(self, catalog: str, item_id: str, body: dict) -> dict:
        """Andre approves exactly one version, named by its content hash (what he read is what is approved)."""
        with self.lock:
            self._gate()
            prev = self._idem("andre", f"{catalog}_approve", item_id, body)
            if prev is not None:
                return prev
            item = self.catalog[catalog].get(item_id)
            if item is None:
                raise NotFound(R("ITEM_NOT_FOUND"))
            if item["status"] == "retired":
                raise Conflict(R("ITEM_RETIRED"))
            if body["version"] != item["version"] or body["content_sha256"] != item["content_sha256"]:
                raise Conflict(R("APPROVAL_STALE"))
            resp = {"item_id": item_id, "version": item["version"], "content_sha256": item["content_sha256"],
                    "approved": True}
            if catalog == "kb":
                resp["warnings"] = self._question_warnings(item)
            self._commit(f"{catalog}_approved", {"effects": [{"op": "catalog_approved", "catalog": catalog,
                                                              "item_id": item_id, "version": item["version"],
                                                              "content_sha256": item["content_sha256"]}],
                                                 "request": self._req("andre", f"{catalog}_approve", item_id, body),
                                                 "response": resp}, "andre")
            return resp

    def retire_item(self, catalog: str, item_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem("andre", f"{catalog}_retire", item_id, body)
            if prev is not None:
                return prev
            item = self.catalog[catalog].get(item_id)
            if item is None:
                raise NotFound(R("ITEM_NOT_FOUND"))
            if item["status"] == "retired":
                raise Conflict(R("ITEM_RETIRED"))
            resp = {"item_id": item_id, "status": "retired"}
            self._commit(f"{catalog}_retired", {"effects": [{"op": "catalog_retired", "catalog": catalog,
                                                             "item_id": item_id}],
                                                "request": self._req("andre", f"{catalog}_retire", item_id, body),
                                                "response": resp}, "andre")
            return resp

    @staticmethod
    def _question_warnings(item: dict) -> list[dict]:
        return [{"question": q, "terms": t} for q in item["questions"] for t in [question_warnings(q)] if t]

    def catalog_view(self, catalog: str) -> list[dict]:
        with self.lock:
            out = []
            for item in sorted(self.catalog[catalog].values(), key=lambda i: i["item_id"]):
                v = dict(item)
                v["usable"] = self._usable(catalog, item["item_id"]) is not None
                if catalog == "kb":
                    v["warnings"] = self._question_warnings(item)
                out.append(v)
            return out

    # ============================================================================================ accounts, health

    def save_account(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            aid = body["account_id"]
            prev = self._idem(actor, "account", aid, body)
            if prev is not None:
                return prev
            a = self.accounts.get(aid)
            if a is not None and a["brand"] != body["brand"]:
                raise Conflict(R("ACCOUNT_BRAND_MISMATCH"))
            pc = body.get("primary_contact_id")
            if pc is not None:
                c = self.contacts.get(pc)
                if c is None or c["brand"] != body["brand"]:
                    raise NotFound(R("CONTACT_NOT_FOUND"))
            effects = [{"op": "account_saved", "account_id": aid, "brand": body["brand"], "primary_contact_id": pc,
                        "contract_end": body.get("contract_end"),
                        "contract_source": "onboarding" if body.get("contract_end") else None}]
            if pc is not None and self.contacts[pc].get("account_id") != aid:
                effects.append({"op": "contact_update", "contact_id": pc, "fields": {"account_id": aid}})
            resp = {"account_id": aid, "brand": body["brand"]}
            self._commit("account_saved", {"effects": effects, "request": self._req(actor, "account", aid, body),
                                           "response": resp}, actor)
            return resp

    def account_event(self, actor: str, account_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem(actor, "account_event", account_id, body)
            if prev is not None:
                return prev
            if account_id not in self.accounts:
                raise NotFound(R("ACCOUNT_NOT_FOUND"))
            occurred = parse_iso(body["occurred_at"])
            if occurred > self.now() + timedelta(minutes=5):
                raise Invalid(R("EVENT_IN_FUTURE"))
            resp = {"account_id": account_id, "kind": body["kind"], "recorded": True}
            self._commit("account_event", {"effects": [{"op": "login_event", "account_id": account_id,
                                                        "occurred_at": iso(occurred)}],
                                           "request": self._req(actor, "account_event", account_id, body),
                                           "response": resp}, actor)
            return resp

    def _ticket_account(self, t: dict) -> Optional[str]:
        """A ticket counts for the account it was opened under, or (opened before the contact was linked to an
        account) for the contact's account now."""
        return t["account_id"] or (self.contacts.get(t["contact_id"]) or {}).get("account_id")

    def _health_aggregates(self) -> tuple[dict, dict, dict]:
        """Sweep A: the per-account inputs of a health run, pre-aggregated in ONE pass over the tickets and ONE over
        the surveys — (complaints in the last 30 days, open escalations, latest answered NPS score), each keyed by
        account id. The run used to rescan every ticket (twice) and every survey for EACH account, with the service
        lock held: O(accounts x tickets)."""
        since = iso(self.now() - timedelta(days=30))
        complaints: dict[str, int] = {}
        escalations: dict[str, int] = {}
        for t in self.tickets.values():
            aid = self._ticket_account(t)
            if aid is None:
                continue
            n = sum(1 for at in t["complaint_at"] if at >= since)
            if n:
                complaints[aid] = complaints.get(aid, 0) + n
            if t["status"] == "escalated":
                escalations[aid] = escalations.get(aid, 0) + 1
        latest: dict[str, tuple] = {}
        for sv in self.surveys.values():
            if sv["score"] is None:
                continue
            k = (sv["answered_at"], sv["survey_id"])
            cur = latest.get(sv["account_id"])
            if cur is None or k > cur[0]:
                latest[sv["account_id"]] = (k, sv["score"])
        return complaints, escalations, {aid: v[1] for aid, v in latest.items()}

    def account_health(self, account_id: str) -> dict:
        with self.lock:
            a = self.accounts.get(account_id)
            if a is None:
                raise NotFound(R("ACCOUNT_NOT_FOUND"))
            plan = self.plans.get(a["plan_id"]) if a["plan_id"] else None
            return {"account_id": account_id, "brand": a["brand"], "health": a["health"], "at_risk": a["at_risk"],
                    "contract_end": a["contract_end"], "contract_source": a["contract_source"],
                    "last_login": a["last_login"], "save_plan": plan}

    def accounts_view(self) -> list[dict]:
        with self.lock:
            return [{"account_id": a["account_id"], "brand": a["brand"],
                     "score": a["health"]["score"] if a["health"] else None, "at_risk": a["at_risk"],
                     "plan_id": a["plan_id"], "contract_end": a["contract_end"]}
                    for a in sorted(self.accounts.values(), key=lambda x: x["account_id"])]

    def renewals_view(self) -> list[dict]:
        with self.lock:
            today = self.now().date()
            out = []
            for a in self.accounts.values():
                if a["contract_end"]:
                    days = (date.fromisoformat(a["contract_end"]) - today).days
                    if days <= self.settings.renewal_window_days:
                        out.append({"account_id": a["account_id"], "brand": a["brand"],
                                    "contract_end": a["contract_end"], "days_left": days,
                                    "source": a["contract_source"], "at_risk": a["at_risk"]})
            return sorted(out, key=lambda r: (r["days_left"], r["account_id"]))

    def _read_signals(self) -> dict:
        """Port reads for every account, outside the lock; a raising port is unknown (None)."""
        with self.lock:
            snapshot = sorted(self.accounts)
        signals = {}
        for aid in snapshot:
            signals[aid] = (_safe(lambda: self.ports.results.trend(aid)),
                            _safe(lambda: self.ports.finance.payment_status(aid)),
                            [_safe(lambda p=p: p.contract_end(aid)) for _, p in sorted(self.ports.contracts.items())])
        return signals

    def _health_compute(self, run_key: str, signals: dict) -> dict:
        """Called with the lock held. Accounts added since the port reads are scored at the next run."""
        snapshot = sorted(signals)
        now = self.now()
        complaints, escalations, nps = self._health_aggregates()
        effects: list = []
        started, flagged = [], []
        for aid in snapshot:
            a = self.accounts[aid]
            trend, pay, ends = signals[aid]
            end = next((e for e in ends if e is not None and e.available and _is_date(e.end_date)), None)
            if end is not None and end.end_date != a["contract_end"]:
                effects.append({"op": "account_saved", "account_id": aid, "brand": a["brand"],
                                "contract_end": end.end_date, "contract_source": "port"})
            contract_end = end.end_date if end is not None else a["contract_end"]
            last = parse_iso(a["last_login"]) if a["last_login"] else None
            res = health_mod.compute(now, trend.direction if trend is not None and trend.available else None,
                                     pay.status if pay is not None and pay.available else None, last,
                                     complaints.get(aid, 0), escalations.get(aid, 0), nps.get(aid))
            at_risk = res["score"] < self.settings.at_risk_threshold
            if a["health"] is None or a["health"]["score"] != res["score"] or a["at_risk"] != at_risk \
                    or a["health"]["signals"] != res["signals"]:
                effects.append({"op": "health_scored", "account_id": aid, "score": res["score"],
                                "signals": res["signals"], "at_risk": at_risk,
                                "threshold": self.settings.at_risk_threshold})
            if at_risk and a["plan_id"] is None:
                pid = derived_id("pln", aid, run_key)
                steps = [{"step_id": derived_id("stp", pid, k), "kind": k, "status": "pending"}
                         for k in ("check_in", "results_review", "offer")]
                steps[2]["status"] = "awaiting_selection"
                effects.append({"op": "plan_new", "plan_id": pid, "account_id": aid, "score": res["score"],
                                "steps": steps})
                effects.append(self._alert_effect("ACCOUNT_AT_RISK", aid, run_key))
                started.append(aid)
            if contract_end and contract_end not in a["renewal_flagged"]:
                days = (date.fromisoformat(contract_end) - now.date()).days
                if 0 <= days <= self.settings.renewal_window_days:
                    effects.append({"op": "renewal_flagged", "account_id": aid, "contract_end": contract_end})
                    effects.append(self._alert_effect("RENEWAL_DUE", aid, contract_end))
                    flagged.append(aid)
        return {"effects": effects, "save_plans_started": started, "renewals_flagged": flagged,
                "scored": len(snapshot)}

    # ============================================================================================ save plans

    def plans_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [dict(p) for p in sorted(self.plans.values(), key=lambda p: p["plan_id"])
                    if status is None or p["status"] == status]

    def select_offer(self, plan_id: str, body: dict) -> dict:
        """Andre picks an offer for the plan's offer step: an approved catalogue offer by id, nothing else."""
        with self.lock:
            self._gate()
            prev = self._idem("andre", "plan_offer", plan_id, body)
            if prev is not None:
                return prev
            p = self.plans.get(plan_id)
            if p is None:
                raise NotFound(R("PLAN_NOT_FOUND"))
            if p["status"] != "active":
                raise Conflict(R("PLAN_CLOSED"))
            step = next(s for s in p["steps"] if s["kind"] == "offer")
            if step["status"] not in ("awaiting_selection",):
                raise Conflict(R("STEP_NOT_OPEN"))
            offer = self._usable("offer", body["offer_id"])
            brand = self.accounts[p["account_id"]]["brand"]
            if offer is None or offer["brand"] != brand:
                raise Conflict(R("OFFER_NOT_APPROVED"))
            resp = {"plan_id": plan_id, "step_id": step["step_id"], "offer_id": offer["item_id"],
                    "version": offer["version"], "status": "selected"}
            self._commit("plan_offer", {"effects": [{"op": "plan_offer", "plan_id": plan_id,
                                                     "step_id": step["step_id"], "offer_id": offer["item_id"],
                                                     "version": offer["version"],
                                                     "content_sha256": offer["content_sha256"]}],
                                        "request": self._req("andre", "plan_offer", plan_id, body), "response": resp},
                         "andre")
            return resp

    def complete_step(self, plan_id: str, step_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem("dashboard", "plan_step", f"{plan_id}:{step_id}", body)
            if prev is not None:
                return prev
            p = self.plans.get(plan_id)
            if p is None:
                raise NotFound(R("PLAN_NOT_FOUND"))
            step = next((s for s in p["steps"] if s["step_id"] == step_id), None)
            if step is None:
                raise NotFound(R("STEP_NOT_FOUND"))
            if p["status"] != "active" or step["kind"] != "results_review" or step["status"] != "open":
                raise Conflict(R("STEP_NOT_OPEN"))
            resp = {"plan_id": plan_id, "step_id": step_id, "status": "done"}
            self._commit("plan_step", {"effects": [{"op": "plan_step", "plan_id": plan_id, "step_id": step_id,
                                                    "status": "done"}],
                                       "request": self._req("dashboard", "plan_step", f"{plan_id}:{step_id}", body),
                                       "response": resp}, "dashboard")
            return resp

    def close_plan(self, plan_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem("andre", "plan_close", plan_id, body)
            if prev is not None:
                return prev
            p = self.plans.get(plan_id)
            if p is None:
                raise NotFound(R("PLAN_NOT_FOUND"))
            if p["status"] != "active":
                raise Conflict(R("PLAN_CLOSED"))
            resp = {"plan_id": plan_id, "status": "closed", "outcome": body["outcome"]}
            self._commit("plan_closed", {"effects": [{"op": "plan_closed", "plan_id": plan_id,
                                                      "outcome": body["outcome"]}],
                                         "request": self._req("andre", "plan_close", plan_id, body),
                                         "response": resp}, "andre")
            return resp

    def _save_plan_tick(self, run_key: str) -> dict:
        """Advance every active plan: queue the check-in (approved template), open the results-review task, queue
        the selected offer (re-checked: still approved, same version and content). Every step change recorded."""
        effects: list = []
        done = {"check_ins": 0, "tasks": 0, "offers": 0, "blocked": 0}
        for p in sorted(self.plans.values(), key=lambda x: x["plan_id"]):
            if p["status"] != "active":
                continue
            a = self.accounts[p["account_id"]]
            contact = self.contacts.get(a["primary_contact_id"] or "")
            for s in p["steps"]:
                if s["kind"] == "results_review" and s["status"] == "pending":
                    effects.append({"op": "plan_step", "plan_id": p["plan_id"], "step_id": s["step_id"],
                                    "status": "open"})
                    done["tasks"] += 1
                    continue
                if not ((s["kind"] == "check_in" and s["status"] in ("pending", "blocked"))
                        or (s["kind"] == "offer" and s["status"] == "selected")):
                    continue
                if contact is None:
                    if s["status"] != "blocked":
                        effects.append({"op": "plan_step", "plan_id": p["plan_id"], "step_id": s["step_id"],
                                        "status": "blocked", "reason": R("NO_PRIMARY_CONTACT")})
                    done["blocked"] += 1
                    continue
                channel = self._pick_proactive_channel(contact)
                purpose = "check_in" if s["kind"] == "check_in" else "offer"
                tpl = self._template(purpose, a["brand"], channel)
                extra: dict = {}
                ref: dict = {}
                if s["kind"] == "offer":
                    offer = self._usable("offer", s["offer_id"], s["offer_version"], s["offer_sha256"])
                    if offer is None:
                        effects.append({"op": "plan_step", "plan_id": p["plan_id"], "step_id": s["step_id"],
                                        "status": "awaiting_selection", "reason": R("OFFER_NOT_APPROVED")})
                        done["blocked"] += 1
                        continue
                    extra = {"offer_title": offer["title"], "offer_terms": offer["terms"],
                             "offer_price": money_str(offer["price"])}
                    ref["offer"] = {"item_id": offer["item_id"], "version": offer["version"],
                                    "content_sha256": offer["content_sha256"]}
                if tpl is None:
                    if s.get("reason") != "NO_APPROVED_TEMPLATE":
                        effects.append({"op": "plan_step", "plan_id": p["plan_id"], "step_id": s["step_id"],
                                        "status": "blocked" if s["kind"] == "check_in" else s["status"],
                                        "reason": R("NO_APPROVED_TEMPLATE")})
                    done["blocked"] += 1
                    continue
                ref["template"] = {"item_id": tpl["item_id"], "version": tpl["version"],
                                   "content_sha256": tpl["content_sha256"]}
                mid = derived_id("msg", p["plan_id"], s["step_id"], run_key)
                subj = f"{BRAND_NAMES[a['brand']]}: checking in" if channel == "email" else None
                effects.append(self._out_effect(mid, contact, a["brand"], channel,
                                                "offer" if s["kind"] == "offer" else "template",
                                                self._render(tpl, contact, extra), True, "queued", None, ref, subj,
                                                plan_id=p["plan_id"], step_id=s["step_id"]))
                effects.append({"op": "plan_step", "plan_id": p["plan_id"], "step_id": s["step_id"],
                                "status": "queued", "message_id": mid, "reason": None})
                done["check_ins" if s["kind"] == "check_in" else "offers"] += 1
        return {"effects": effects, **done}

    # ============================================================================================ NPS

    def send_survey(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            aid = body["account_id"]
            prev = self._idem(actor, "survey", aid, body)
            if prev is not None:
                return prev
            a = self.accounts.get(aid)
            if a is None:
                raise NotFound(R("ACCOUNT_NOT_FOUND"))
            contact = self.contacts.get(a["primary_contact_id"] or "")
            if contact is None:
                raise Conflict(R("NO_PRIMARY_CONTACT"))
            channel = body["channel"]
            reason = self._channel_check(channel, contact, proactive=True)
            if reason not in (None, "QUIET_HOURS"):
                raise Conflict(R(reason))
            tpl = self._template("nps_survey", a["brand"], channel)
            if tpl is None:
                raise Conflict(R("NO_APPROVED_TEMPLATE"))
            sid = self._did("nps", actor, "survey", aid, body["request_id"])
            mid = self._did("msg", actor, "survey", aid, body["request_id"])
            ref = {"template": {"item_id": tpl["item_id"], "version": tpl["version"],
                                "content_sha256": tpl["content_sha256"]}}
            subj = f"{BRAND_NAMES[a['brand']]}: a quick question" if channel == "email" else None
            effects = [self._out_effect(mid, contact, a["brand"], channel, "template",
                                        self._render(tpl, contact, {"survey_id": sid}), True, "queued", None, ref,
                                        subj, survey_id=sid),
                       {"op": "survey_new", "survey_id": sid, "account_id": aid, "contact_id": contact["contact_id"],
                        "message_id": mid}]
            resp = {"survey_id": sid, "message_id": mid, "channel": channel, "status": "queued",
                    "waiting_for": "quiet_hours" if reason == "QUIET_HOURS" else None}
            self._commit("survey_sent", {"effects": effects, "request": self._req(actor, "survey", aid, body),
                                         "response": resp}, actor)
            return resp

    def nps_response(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            sid = body["survey_id"]
            prev = self._idem(actor, "nps", sid, body)
            if prev is not None:
                return prev
            s = self.surveys.get(sid)
            if s is None:
                raise NotFound(R("SURVEY_NOT_FOUND"))
            if s["score"] is not None:
                raise Conflict(R("SURVEY_ANSWERED"))
            comment_sha = self._put_body(body["comment"]) if body.get("comment") else None
            effects = [{"op": "nps_response", "survey_id": sid, "score": body["score"], "comment_sha256": comment_sha}]
            band = health_mod.nps_band(body["score"])
            if band == "detractor":
                effects.append(self._alert_effect("NPS_DETRACTOR", s["account_id"], sid))
            resp = {"survey_id": sid, "score": body["score"], "band": band}
            self._commit("nps_response", {"effects": effects, "request": self._req(actor, "nps", sid, body),
                                          "response": resp}, actor)
        self.dispatch_side_effects()
        return resp

    # ============================================================================================ phone

    def record_call(self, actor: str, body: dict) -> dict:
        """A call record from the voice gateway (refs only: no audio, no transcript text). The call becomes a
        phone ticket in Andre's queue; a voicemail alerts him. No bot answers a call."""
        with self.lock:
            self._gate()
            brand = body["brand"]
            target = f"{brand}:phone"
            prev = self._idem(actor, "call", target, body)
            if prev is not None:
                return prev
            rid = body["request_id"]
            effects: list = []
            contact = self._find_or_new_contact(actor, brand, "phone", body["from_number"], rid, effects)
            ticket, _ = self._ticket_for(actor, contact, brand, "phone", None, rid, effects, bool(effects))
            cid = self._did("cal", actor, "call", target, rid)
            effects.append({"op": "call_new", "call_id": cid, "brand": brand, "contact_id": contact["contact_id"],
                            "ticket_id": ticket["ticket_id"], "direction": "inbound", "started_at": body["started_at"],
                            "duration_seconds": body["duration_seconds"], "outcome": body["outcome"],
                            "voicemail_ref": body.get("voicemail_ref"), "transcript_ref": body.get("transcript_ref")})
            if ticket["status"] != "escalated":
                effects.append({"op": "ticket_status", "ticket_id": ticket["ticket_id"], "status": "open",
                                "queue": "andre"})
            if body["outcome"] in ("voicemail", "missed"):
                effects.append(self._alert_effect("VOICEMAIL_RECEIVED" if body["outcome"] == "voicemail"
                                                  else "MISSED_CALL", ticket["ticket_id"], cid))
            resp = {"call_id": cid, "ticket_id": ticket["ticket_id"], "contact_id": contact["contact_id"]}
            self._commit("call_recorded", {"effects": effects, "request": self._req(actor, "call", target, body),
                                           "response": resp}, actor)
        self.dispatch_side_effects()
        return resp

    def call_handoff(self, actor: str, call_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem(actor, "call_handoff", call_id, body)
            if prev is not None:
                return prev
            c = self.calls.get(call_id)
            if c is None:
                raise NotFound(R("CALL_NOT_FOUND"))
            effects = [{"op": "call_handoff", "call_id": call_id},
                       {"op": "ticket_escalated", "ticket_id": c["ticket_id"], "categories": []},
                       self._alert_effect("CALL_HANDOFF", c["ticket_id"], f"{call_id}:{body['request_id']}")]
            resp = {"call_id": call_id, "ticket_id": c["ticket_id"], "handed_to": "andre"}
            self._commit("call_handoff", {"effects": effects, "request": self._req(actor, "call_handoff", call_id, body),
                                          "response": resp}, actor)
        self.dispatch_side_effects()
        return resp

    def route_call(self, brand: str) -> dict:
        """Where an incoming call goes now (advisory: no voice provider is wired). No rules: voicemail."""
        with self.lock:
            rules = self.routing.get(brand)
            now = self.now()
            decision, reason = "voicemail", "NO_ROUTING_RULES"
            if rules:
                local = now.astimezone(channels.zone(rules["timezone"]))
                open_ = local.strftime("%a").lower()[:3] in rules["days"] and \
                    rules["open_hour"] <= local.hour < rules["close_hour"]
                decision = rules["in_hours"] if open_ else "voicemail"
                reason = "IN_HOURS" if open_ else "AFTER_HOURS"
            return {"brand": brand, "action": decision, "reason": reason, "voice_provider_wired": False}

    def set_routing(self, brand: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            prev = self._idem("andre", "routing", brand, body)
            if prev is not None:
                return prev
            if channels.zone(body["timezone"]) is None:
                raise Invalid(R("TIMEZONE_UNKNOWN"))
            if body["open_hour"] >= body["close_hour"]:
                raise Invalid(R("INVALID"))
            rules = {k: body[k] for k in ("timezone", "days", "open_hour", "close_hour", "in_hours")}
            resp = {"brand": brand, **rules}
            self._commit("routing_set", {"effects": [{"op": "routing_set", "brand": brand, "rules": rules}],
                                         "request": self._req("andre", "routing", brand, body), "response": resp},
                         "andre")
            return resp

    # ============================================================================================ side effects

    def dispatch_side_effects(self) -> None:
        """Send what committed lines decided and the ports can carry: handoffs and alerts on wired ports. Called
        after a request (outside the lock) and by the handoff-retries job. A not-wired port is never called and
        nothing is recorded for it (the view shows ``not_wired``).

        Sweep A: a CLOSED instance dispatches nothing — ``_closed`` is checked at the snapshot and again, under the
        lock, right before each port call. Before, it called the ports and only the result commit was refused, so the
        instance that now owns the data directory sent the same alert or handoff again.

        Residual window (AEGIS L5, accepted and documented): the port is called outside the lock, so a ``close()``
        that lands between the last check and the call cannot stop that one call; its result commit is then refused
        and the next instance may send it once more. Holding the lock across a provider call would stall every
        request, so the window is kept to the call itself; the alert and handoff ids are the providers' idempotency
        keys."""
        with self.lock:
            if self._closed:
                return
            hofs = [dict(h) for h in self.handoffs.values() if h["status"] in ("pending", "unavailable", "failed")
                    and getattr(self.ports.handoffs.get(h["department"]), "wired", False)
                    and h["handoff_id"] not in self._in_flight]
            alerts = [dict(a) for a in self.alerts.values() if a["status"] in ("recorded", "failed")
                      and getattr(self.ports.alerts, "wired", False) and a["alert_id"] not in self._in_flight]
            if not self.integrity["ok"]:
                return
            self._in_flight |= {h["handoff_id"] for h in hofs} | {a["alert_id"] for a in alerts}
        try:
            for h in hofs:
                t = self.tickets[h["ticket_id"]]
                req = HandoffRequest(h["handoff_id"], h["ticket_id"], t["brand"], h["category"], h["kind"],
                                     t["account_id"])
                if not self._still_open():          # AEGIS L5: checked immediately before the port call
                    return
                res = _safe(lambda: self.ports.handoffs[h["department"]].handoff(req))
                status = res.status if res is not None and res.status in ("delivered", "refused", "unavailable") \
                    else "unavailable"
                try:
                    self._commit("handoff_result", {"effects": [{"op": "handoff_result", "handoff_id": h["handoff_id"],
                                                                 "status": status,
                                                                 "reference": res.reference if res else None}]},
                                 INTERNAL)
                except Unavailable:
                    break
            for a in alerts:
                msg = Alert(a["alert_id"], a["code"], a["subject"])
                if not self._still_open():          # AEGIS L5: checked immediately before the port call
                    return
                res = _safe(lambda: self.ports.alerts.send(msg))
                status = res if res in ("delivered", "failed") else "failed"
                try:
                    self._commit("alert_result", {"effects": [{"op": "alert_result", "alert_id": a["alert_id"],
                                                               "status": status}]}, INTERNAL)
                except Unavailable:
                    break
        finally:
            with self.lock:
                self._in_flight -= {h["handoff_id"] for h in hofs} | {a["alert_id"] for a in alerts}

    def _still_open(self) -> bool:
        with self.lock:
            return not self._closed

    def _send_block(self, m: dict) -> Optional[str]:
        """Why this message may not go NOW (None: it may). Run when the tick picks it AND again under the lock right
        before its send intent is recorded (V1-C2: a STOP, a revoked consent, an edited article, template or offer
        that lands mid-tick is honoured)."""
        if m["status"] != "queued":
            return "NOT_QUEUED"
        if self.bodies.get(m["body_sha256"]) is None:
            return "BODY_MISSING"
        contact = self.contacts[m["contact_id"]]
        if m.get("bound_to") and contact.get("phone") != m["bound_to"]:
            return "ADDRESS_CHANGED"         # V3-C3: a confirmation goes only to the number that sent the STOP
        reason = self._channel_check(m["channel"], contact, bool(m["proactive"]), m.get("origin") == "opt_out")
        if reason is not None:
            return reason
        ref = m.get("ref") or {}
        for key in ("template", "offer"):
            r = ref.get(key)
            if r and self._usable(key, r["item_id"], r["version"], r["content_sha256"]) is None:
                return "OFFER_NOT_APPROVED" if key == "offer" else "TEMPLATE_NOT_APPROVED"
        if ref.get("catalog") == "kb" and self._usable("kb", ref["item_id"], ref["version"],
                                                       ref["content_sha256"]) is None:
            return "ARTICLE_NOT_APPROVED"
        if m["attempts"] >= MAX_SEND_ATTEMPTS:
            return "PROVIDER_FAILED"
        return None

    def _cancel_effects(self, m: dict, reason: str) -> list:
        out = [{"op": "message_status", "message_id": m["message_id"], "status": "cancelled", "reason": R(reason)}]
        t = self.tickets.get(m.get("ticket_id") or "")
        if t is not None and t["status"] == "pending_customer":
            # the answer will not go: the ticket goes back to Andre's queue
            out.append({"op": "ticket_status", "ticket_id": t["ticket_id"], "status": "open", "queue": "andre"})
        return out

    def _outbound_tick(self) -> dict:
        """Send what may be sent now. Every queued message is checked (``_send_block``): one that may never go is
        cancelled with a reason, one that must wait (quiet hours) stays queued. Then, per message, under the lock:
        checked AGAIN, then the send intent committed (``message_sending``, its typed ledger event first: ledger down
        = nothing is sent and the tick stops); the provider called outside the lock with the message id as its
        idempotency key; the result committed. A result that could not be committed is committed at the next tick
        by this process; after a restart a message still ``sending`` is held for Andre, never re-sent."""
        result = {"sent": 0, "failed": 0, "cancelled": 0, "waiting": 0, "not_wired": 0, "held": 0}
        with self.lock:
            self._gate()
            for mid, effect in list(self._unconfirmed.items()):          # results this process could not record
                self._commit("outbound_result", {"effects": [effect]}, INTERNAL)
                del self._unconfirmed[mid]
                result["sent" if effect["status"] == "sent" else "failed"] += 1
            cancels, ready = [], []
            for m in sorted(self.messages.values(), key=lambda x: (x["at"], x["message_id"])):
                if m["dir"] != "out" or m["message_id"] in self._in_flight:
                    continue
                if m["status"] == "sending":
                    result["held"] += 1
                    continue
                if m["status"] != "queued":
                    continue
                reason = self._send_block(m)
                if reason == "QUIET_HOURS":
                    result["waiting"] += 1
                    continue
                if reason is not None:
                    cancels += self._cancel_effects(m, reason)
                    continue
                if not getattr(self.ports.senders.get(m["channel"]), "wired", False):
                    result["not_wired"] += 1
                    continue
                ready.append(m["message_id"])
            if cancels:
                self._commit("outbound_cancelled", {"effects": cancels}, INTERNAL)
                result["cancelled"] += sum(1 for c in cancels if c["op"] == "message_status")
            self._in_flight |= set(ready)
        try:
            for mid in ready:
                with self.lock:
                    m = self.messages[mid]
                    reason = self._send_block(m)                        # V1-C2: re-checked right before sending
                    if reason == "QUIET_HOURS":
                        result["waiting"] += 1
                        continue
                    if reason == "NOT_QUEUED":
                        continue
                    if reason is not None:
                        self._commit("outbound_cancelled", {"effects": self._cancel_effects(m, reason)}, INTERNAL)
                        result["cancelled"] += 1
                        continue
                    contact = self.contacts[m["contact_id"]]
                    attempt = m["attempts"] + 1
                    self._commit("outbound_sending", {"effects": [{
                        "op": "message_sending", "message_id": mid, "channel": m["channel"], "brand": m["brand"],
                        "body_sha256": m["body_sha256"], "attempt": attempt, "origin": m["origin"]}]}, INTERNAL)
                    to = m.get("bound_to") or (contact["email"] if m["channel"] == "email" else (
                        contact["phone"] if m["channel"] == "sms" else contact["contact_id"]))
                    sender_id = self.settings.support_email.get(m["brand"]) if m["channel"] == "email" else (
                        self.settings.sms_number.get(m["brand"]) if m["channel"] == "sms" else m["brand"])
                    msg = Outbound(mid, m["brand"], m["channel"], to, sender_id or "", m.get("subject"),
                                   self.bodies.get(m["body_sha256"]) or "")
                    channel = m["channel"]
                res = _safe(lambda: self.ports.senders[channel].send(msg))
                status = "sent" if res == "sent" else "failed"
                effect = {"op": "message_status", "message_id": mid, "status": "sent" if status == "sent" else "queued",
                          "reason": None if status == "sent" else R("PROVIDER_FAILED")}
                self._unconfirmed[mid] = effect
                self._commit("outbound_result", {"effects": [effect]}, INTERNAL)
                self._unconfirmed.pop(mid, None)
                result[status] += 1
        finally:
            with self.lock:
                self._in_flight -= set(ready)
        return result

    def resolve_held(self, message_id: str, body: dict) -> dict:
        """Andre decides a message whose send outcome is unknown (still ``sending`` after a restart): ``sent`` (the
        provider took it), ``requeue`` (it did not: send again; the provider sees the same message id) or
        ``cancel``."""
        with self.lock:
            self._gate()
            prev = self._idem("andre", "resolve_held", message_id, body)
            if prev is not None:
                return prev
            m = self.messages.get(message_id)
            if m is None or m["dir"] != "out":
                raise NotFound(R("NOT_FOUND"))
            if m["status"] != "sending" or message_id in self._in_flight or message_id in self._unconfirmed:
                raise Conflict(R("NOT_HELD"))
            status = {"sent": "sent", "requeue": "queued", "cancel": "cancelled"}[body["outcome"]]
            resp = {"message_id": message_id, "status": status}
            self._commit("outbound_resolved", {"effects": [{"op": "message_status", "message_id": message_id,
                                                            "status": status, "reason": None if status != "cancelled"
                                                            else R("CANCELLED_BY_ANDRE")}],
                                               "request": self._req("andre", "resolve_held", message_id, body),
                                               "response": resp}, "andre")
            return resp

    # ============================================================================================ jobs

    def run_job(self, name: str, body: dict) -> dict:
        if name not in JOBS:
            raise NotFound(R("JOB_UNKNOWN"))
        if name == "integrity":                          # refused when closed (AEGIS a5dd261 M1); verify() unlocked
            out = self._integrity_and_ledger(always=True)  # (AEGIS cc27b69 L1)
            return {"job": name, "integrity": out["integrity"], "ledger_valid": out["ledger_valid"]}
        if not self._tick_lock.acquire(blocking=False):
            raise Conflict(R("JOB_RUNNING"))
        try:
            with self.lock:
                self._gate()
                prev = self._idem("scheduler", "job", name, body)
                if prev is not None:
                    return {**prev, "already_ran": True}
            run_key = f"{name}:{body['request_id']}"
            result: dict = {}
            signals = None
            if name == "outbound-tick":
                result = self._outbound_tick()
            elif name == "handoff-retries":
                self.dispatch_side_effects()
            elif name == "health-recompute":
                signals = self._read_signals()
            with self.lock:
                self._gate()
                effects: list = []
                if name == "sla-sweep":
                    result = self._sla_sweep(run_key)
                elif name == "save-plan-tick":
                    result = self._save_plan_tick(run_key)
                elif name == "health-recompute":
                    result = self._health_compute(run_key, signals)
                elif name == "handoff-retries":
                    result = {"handoffs_open": sum(1 for h in self.handoffs.values()
                                                   if h["status"] not in ("delivered", "refused")),
                              "alerts_undelivered": sum(1 for a in self.alerts.values() if a["status"] != "delivered")}
                effects = result.pop("effects", [])
                resp = {"job": name, **result}
                self._commit("job_ran", {"effects": effects + [{"op": "job_ran", "job": name}],
                                         "request": self._req("scheduler", "job", name, body), "response": resp},
                             "scheduler")
        finally:
            self._tick_lock.release()
        self.dispatch_side_effects()
        return resp

    def _sla_sweep(self, run_key: str) -> dict:
        now = iso(self.now())
        effects: list = []
        breached = []
        for t in sorted(self.tickets.values(), key=lambda x: x["ticket_id"]):
            if t["status"] in ("resolved", "closed"):
                continue
            if t["first_response_at"] is None and t["first_due"] < now and "first_response" not in t["breaches"]:
                effects.append({"op": "sla_breach", "ticket_id": t["ticket_id"], "which": "first_response"})
                effects.append(self._alert_effect("SLA_FIRST_RESPONSE_BREACHED", t["ticket_id"], "first"))
                breached.append(t["ticket_id"])
            if t["resolution_due"] < now and "resolution" not in t["breaches"]:
                effects.append({"op": "sla_breach", "ticket_id": t["ticket_id"], "which": "resolution"})
                effects.append(self._alert_effect("SLA_RESOLUTION_BREACHED", t["ticket_id"], "resolution"))
                breached.append(t["ticket_id"])
        return {"effects": effects, "breaches": len(breached)}

    # ============================================================================================ views, audit

    def alerts_view(self) -> list[dict]:
        with self.lock:
            wired = getattr(self.ports.alerts, "wired", False)
            return [{**a, "delivery": a["status"] if wired or a["status"] == "delivered" else NOT_WIRED}
                    for a in sorted(self.alerts.values(), key=lambda x: (x["created_at"], x["alert_id"]))]

    def outbound_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [{k: m.get(k) for k in ("message_id", "ticket_id", "contact_id", "brand", "channel", "origin",
                                           "status", "reason", "proactive", "at", "attempts", "plan_id", "survey_id")}
                    for m in sorted(self.messages.values(), key=lambda x: (x["at"], x["message_id"]))
                    if m["dir"] == "out" and (status is None or m["status"] == status)]

    def _department_entries(self, event_type: Optional[str] = None) -> list[dict]:
        """This department's ledger entries (of one event type, if given): the ledger's filtered, paged read when the
        client has it (``entries_filtered``), else the whole ledger filtered here (an older client or a test fake)."""
        client = self.rec.client
        paged = getattr(client, "entries_filtered", None)
        if paged is not None:
            return paged(DEPARTMENT, event_type)
        return [e for e in client.entries() if e.get("department") == DEPARTMENT
                and (event_type is None or e.get("event_type") == event_type)]

    def audit_evidence(self, limit: int, offset: int, event_type: Optional[str] = None) -> dict:
        """``/svc/v1/audit/evidence`` (sweep A R6-M1, bizdev-py's round-6 view) — every typed evidence event this
        department holds on the ledger, each marked:

        * ``committed`` — a local log line with seq ``s`` names the event (``data.ledger_evidence``), the ledger holds
          that line's anchor (``log_anchor`` for epoch, ``s`` and the line's SHA-256), the event's payload carries
          ``rk`` = the line's request key and ``seq`` = ``s``, and the ledger's ``payload_sha256`` is that payload's;
        * ``attempted`` — anything else: recorded first (record-first commit), but its state change never reached the
          anchored log (a refused or failed commit, or a retry that was later committed under another seq).

        Unanchored evidence = attempted, not done. Events recorded before this change carry no rk/seq and are
        ``attempted`` by this rule (their lines name nothing). Under the service lock only the raw lines not yet seen
        are copied; they are parsed and hashed outside it and cached by log length. Eventually consistent."""
        with self._evidence_lock:
            with self.lock:
                if self._closed:
                    raise Unavailable(R("SERVICE_CLOSED"))
                cache = self._evidence_cache
                n = len(self.log)
                if n < cache["n"]:
                    cache = self._evidence_cache = {"n": 0, "epoch": None, "lines": []}
                new = self.log.raw_lines(cache["n"])
            for raw in new:                              # outside the service lock: parse and hash only new lines
                r = json.loads(raw)
                line_sha = sha256_hex(raw)
                if cache["epoch"] is None:
                    cache["epoch"] = line_sha[:16]
                evs = r["data"].get("ledger_evidence")
                if evs:
                    cache["lines"].append((r["seq"], line_sha, self._rk(r["kind"], r["data"]), r["kind"], evs))
                cache["n"] += 1
            epoch, lines, n_lines = cache["epoch"], list(cache["lines"]), cache["n"]
        try:                                             # outside the service lock (AEGIS M4: filtered, paged)
            if event_type is None:
                mine = self._department_entries()
                anchor_rows = [e for e in mine if e.get("event_type") == "log_anchor"]
            else:
                anchor_rows = self._department_entries("log_anchor")
                mine = self._department_entries(event_type) if event_type != "log_anchor" else []
        except LedgerQueryFailed:
            raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
        anchors = {e.get("event_id"): e for e in anchor_rows}
        named: dict[str, tuple] = {}
        for seq, line_sha, line_rk, kind, evs in lines:
            aid, apayload = self._anchor_ids(epoch, seq, line_sha)
            a = anchors.get(aid)
            if a is None or a.get("payload_sha256") != payload_sha256(apayload) or a.get("subject_id") != f"log:{epoch}":
                continue
            for ev in evs:
                named[ev["event_id"]] = (seq, line_rk, ev.get("payload") or {}, kind)
        out, counts = [], {"committed": 0, "attempted": 0}
        for e in mine:
            if e.get("event_type") == "log_anchor" or (event_type is not None and e.get("event_type") != event_type):
                continue
            row = {"event_id": e.get("event_id"), "event_type": e.get("event_type"), "subject_id": e.get("subject_id"),
                   "ledger_seq": e.get("seq"), "payload_sha256": e.get("payload_sha256"), "status": "attempted",
                   "seq": None, "rk": None, "log_kind": None}
            hit = named.get(e.get("event_id"))
            if hit is not None:
                seq, line_rk, payload, log_kind = hit
                if payload.get("seq") == seq and payload.get("rk") == line_rk \
                        and payload_sha256(payload) == e.get("payload_sha256"):
                    row.update(status="committed", seq=seq, rk=line_rk, log_kind=log_kind)
            counts[row["status"]] += 1
            out.append(row)
        return {"rule": "unanchored evidence = attempted, not done", "consistency": "eventual; re-read to settle",
                "total": len(out), **counts, "limit": limit, "offset": offset,
                "evidence": out[offset:offset + limit], "log_length": n_lines}

    def audit_events(self, since_seq: int, limit: int) -> dict:
        """The local log with personal data minimised: addresses, names, refs, time zones and request keys replaced by
        HMAC-SHA-256 under a key made for THIS export and returned once with it (V1-M2: an unsalted hash of a phone
        number is reversible by enumeration; two exports cannot be joined without both keys). Stored answers are
        dropped. Bodies are never in the log (only their keyed digests)."""
        key = os.urandom(32)
        out = []
        for r in self.log.iter_records(max(1, since_seq)):
            data = {k: v for k, v in r["data"].items() if k not in ("response", "request")}
            if data.get("ledger_evidence"):          # AEGIS L3: rk re-keyed per export (two exports cannot be joined)
                data["ledger_evidence"] = [{**ev, "payload": {**ev["payload"], "rk": _hmac(key, str(ev["payload"]
                                                                                                    .get("rk")))}}
                                           for ev in data["ledger_evidence"]]
            d = _minimise(data, key)
            if r["data"].get("request"):            # the key names a contact ref or address
                d["request_hmac"] = _hmac(key, json.dumps(r["data"]["request"], sort_keys=True))
            out.append({"seq": r["seq"], "kind": r["kind"], "at": r["at"], "data": d})
            if len(out) >= limit:
                break
        return {"events": out, "log_length": len(self.log), "hmac": "HMAC-SHA-256",
                "hmac_key": base64.b64encode(key).decode("ascii")}


CATALOG_CONTENT = {
    "kb": ("brands", "channels", "title", "answer", "questions"),
    "template": ("purpose", "brand", "channels", "text"),
    "offer": ("brand", "title", "terms", "price", "currency"),
}


def catalog_sha(catalog: str, content: dict) -> str:
    return payload_sha256({"catalog": catalog, **{k: content[k] for k in CATALOG_CONTENT[catalog]}})


# V4-M1: words and phrases an approved example question may never hold (approval time only; they do not label inbound
# messages). A question is also refused when it has more than QUESTION_MAX_WORDS words, a second clause
# (triage.single_intent), any opt-out wording or a negation near a channel word.
QUESTION_DENY_WORDS = frozenset("""
fund funds back rebate rebates deposit deposits waive waived renew renewal renews credit credits card wire sell sells
selling share shares sharing rid forget leave leaving out off offline dump safe problem problems broken shut close
closing closure unsubscribe stuff refundable owed return returned returns
""".split())
QUESTION_DENY_PHRASES = ("money back", "get rid", "have on me", "know about me", "shut down", "close my account",
                         "close out", "out of", "take off", "take down", "taken offline", "got into", "end things",
                         "free month", "auto renew", "my info", "my data", "my information", "my account",
                         # V4b-L1: the reviewer's round-4b near misses
                         "phone number", "my number", "my phone", "my email", "to others", "done with you", "pull my",
                         "come down")
QUESTION_DENY_STEMS = ("compensat", "deactivat", "discontinu", "downgrad")
# Not refused, but shown to Andre as a WARNING when he saves or approves a question that holds one (V4b-L1): the deny
# list is a word list and can never be complete; Andre's reading of each question is the main control.
QUESTION_WATCH_WORDS = frozenset("""
keep give pause policy profile history details personal plan subscription membership number email phone password
contact remove change update transfer move private privacy secure security bank charge charges billing bill pay
payment paid refund cancel delete account login access stop end
""".split())
QUESTION_MAX_WORDS = 12


def question_warnings(q: str) -> list[str]:
    """Near-miss terms for Andre to look at: watch words, and words one typo from a deny word or stem."""
    out = []
    for w in triage_mod.normalise(q).split():
        near = len(w) >= 4 and (any(triage_mod._one_edit(w, d) for d in QUESTION_DENY_WORDS if len(d) >= 4)
                                or any(triage_mod._one_edit(w[:len(st)], st) for st in QUESTION_DENY_STEMS))
        if (w in QUESTION_WATCH_WORDS or near) and w not in out:
            out.append(w)
    return out


def question_denied(q: str) -> bool:
    norm = triage_mod.normalise(q)
    words = norm.split()
    return (len(words) > QUESTION_MAX_WORDS or triage_mod.denied_question(q) or triage_mod.single_intent(q) is not None
            or channels.opt_out_level(q) is not None or channels.negated_channel(norm)
            or any(w in QUESTION_DENY_WORDS for w in words)
            or any(w.startswith(st) for w in words for st in QUESTION_DENY_STEMS)
            or any(f" {p} " in norm for p in QUESTION_DENY_PHRASES))


_HEX64 = re.compile(r"[0-9a-f]{64}")
PHONE_RE = re.compile(r"\+?\(?\d[\d\s().-]{8,16}\d")
EMAIL_RE = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8}")


def _digests(obj, out: set) -> None:
    """Every 64-hex value anywhere in a log line's data (the body digests it cites)."""
    if isinstance(obj, dict):
        for v in obj.values():
            _digests(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _digests(v, out)
    elif isinstance(obj, str) and _HEX64.fullmatch(obj):
        out.add(obj)


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a port that raises is unavailable; its text is dropped
        return None


def _is_date(v) -> bool:
    try:
        date.fromisoformat(v)
        return isinstance(v, str) and len(v) == 10
    except (TypeError, ValueError):
        return False


def _hmac(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _minimise(obj, key: bytes):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in PERSONAL_KEYS and isinstance(v, str):
                out[k + "_hmac"] = _hmac(key, v)
            else:
                out[k] = _minimise(v, key)
        return out
    if isinstance(obj, list):
        return [_minimise(x, key) for x in obj]
    return obj
