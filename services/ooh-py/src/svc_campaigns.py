"""
The OOH-1 operating spine (ADR 0019 decisions 9-20): campaigns and their state machine, inventory availability,
the separate price objects, the commercial model, client approval, client funds, the purchase gate, and the
retry-safe submission states.

Rules enforced here, each with its own reason code (reasons.py) and its own test:

* Every campaign transition goes through ``transition`` (or one of the five gates: client approval, funds, purchase
  authorization, submission, outcome), each of which commits through ``_commit`` — evidence on the ledger first. An
  illegal or skipped transition is refused (``TRANSITION_NOT_ALLOWED``); a gated state can never be reached through
  the generic route (``TRANSITION_GATED``); a stale caller is refused (``STATE_STALE`` / ``VERSION_STALE``).
* Leaving CLIENT_HOLD, VENDOR_HOLD or DISPUTED goes back exactly to the state it was entered from, and never restores
  an authorization (entering a hold, DISPUTED, CHANGE_REQUESTED or CANCELLED voids every unsubmitted authorization).
* A hold requested is not a hold confirmed, and only Andre confirms one (founder, Oct 16 round 2; replaces AEGIS L4):
  an agent attaches the vendor's evidence (reference, expiry, document) to a HOLD_REQUESTED unit and submits it;
  Andre confirms or rejects that one named record. The availability route refuses HOLD_CONFIRMED for everyone. The
  gate wants every unit HOLD_CONFIRMED by Andre and unexpired. Booking states are reached only through the order flow.
* The six price objects never stand in for each other; an evidence class is fixed when a record is created and no
  route changes it. Andre's gate gives funds the class OBSERVED, never FINANCIALLY_VERIFIED (that is Finance's).
* No markup is ever applied by default: a client sell price must equal the quotes it names marked up by the markup
  Andre set for this deal (``MARKUP_MISMATCH``). 15% is shown as a suggestion only.
* The purchase gate (section 12) re-runs in full at submission time. Client funds must be verified cleared and cover
  the client-approved price (100% prepaid) and every vendor commitment, including one whose outcome is unknown.
* Submission (section 51): NOT_SUBMITTED -> SUBMISSION_UNKNOWN -> ACCEPTED | REJECTED. A timeout is not an outcome. A
  second submission of an open order — by retry, by a new request id, or through a new authorization of the same
  quotes — is refused ``DUPLICATE_ORDER_BLOCKED``.
* Vendor text is data (section 52): stored bounded, marked untrusted, and read by nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import policy as P
from clock import parse_iso
from errors import Conflict, Forbidden, Invalid, NotFound
from ledger import canonical, derived_id
from reasons import R

OBSERVED_MAX = 600
PLANNING_STATES = ("INTAKE", "BRIEF_VALIDATED", "PLAN_DRAFTED", "PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED",
                   "CHANGE_REQUESTED")
PRE_ORDER_WRITABLE = PLANNING_STATES + ("PROPOSAL_SENT", "CLIENT_APPROVED", "FUNDING_REQUIRED", "FUNDS_VERIFIED",
                                        "PURCHASE_AUTHORIZED")
SELL_PRICE_STATES = ("PLAN_DRAFTED", "PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED")
# Unit states that carry something agreed for one flight (availability asked, a quote, a hold): a new flight resets them.
FLIGHT_BOUND_STATES = ("AVAILABILITY_REQUESTED", "QUOTED", "HOLD_REQUESTED", "HOLD_CONFIRMED")
VOIDING_STATES = P.HOLD_STATES + ("DISPUTED", "CHANGE_REQUESTED", "CANCELLED")
FUNDABLE = ("CLIENT_APPROVED", "FUNDING_REQUIRED")


def observed(text: Optional[str]) -> Optional[dict]:
    """Vendor text as reportable data: control characters removed, bounded, marked untrusted (section 52)."""
    if text is None:
        return None
    s = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return {"untrusted": True, "text": s[:OBSERVED_MAX], "truncated": len(s) > OBSERVED_MAX,
            "sha256": hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()}


# Words a bank or a person puts around a transfer reference. RAILS name a payment rail: a rail word in a reference must
# agree with the structured ``method`` (AEGIS R2-L1: BOA-ACH-12345 and BOA-WIRE-12345 are different transfers on
# different rails, never merged). NOISE words carry no identity. Either is removed only as a whole token, or as a
# token's leading prefix directly followed by a digit (ACH12345): never from inside a word (REFUND77 is not UND77).
RAILS = {"ach": "ach", "eft": "ach", "wire": "wire", "check": "check", "cheque": "check", "chk": "check"}
NOISE_WORDS = ("transfer", "xfer", "txn", "trace", "ref", "reference", "conf", "confirmation")
METHOD_WORDS = tuple(RAILS) + NOISE_WORDS


def _alnum_tokens(value: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", unicodedata.normalize("NFKC", value).casefold()) if t]


def _split_method(token: str) -> tuple[Optional[str], str]:
    """(the method word the token is or starts with, the rest), or (None, token)."""
    if token in METHOD_WORDS:
        return token, ""
    for w in sorted(METHOD_WORDS, key=len, reverse=True):
        if token.startswith(w) and token[len(w):len(w) + 1].isdigit():
            return w, token[len(w):]
    return None, token


def _spelled_out(value: str) -> list[str]:
    """The alphanumeric tokens, with a run of one-character tokens joined by one same separator read as one token
    spelled out: "a.c.h" is "ach", "0-0-12345" is "0012345", and "B.O.A-A.C.H" is "boa", "ach" (the separator
    changes between the two runs)."""
    text = unicodedata.normalize("NFKC", value).casefold()
    parts = [(m.group(1), m.group(2)) for m in re.finditer(r"([^0-9a-z]*)([0-9a-z]+)", text)]
    out: list[str] = []
    i = 0
    while i < len(parts):
        tok = parts[i][1]
        j = i + 1
        if len(tok) == 1:
            sep = parts[j][0] if j < len(parts) else None
            while j < len(parts) and len(parts[j][1]) == 1 and parts[j][0] == sep and sep:
                tok += parts[j][1]
                j += 1
            # the next token closes the run when it follows the same separator ("0-0-012345")
            if j < len(parts) and len(tok) > 1 and parts[j][0] == sep and sep:
                tok += parts[j][1]
                j += 1
        out.append(tok)
        i = j
    return out


def funds_ref_parts(value: str) -> tuple[set, str]:
    """(the rails the reference names, its identity without method words). Every digit run of the identity loses its
    leading zeros (AEGIS R2-H1: 000012345 and 0-0-012345 are 12345; an all-zero run stays "0")."""
    rails, out = set(), []
    for t in _spelled_out(value):
        word, rest = _split_method(t)
        if word in RAILS:
            rails.add(RAILS[word])
        if rest:
            out.append(rest)
    return rails, re.sub(r"(?<![0-9])0+(?=[0-9])", "", "".join(out))


def funds_ref_key(value: str, method: str) -> str:
    """The key one bank transfer is known by: ``<rail>:<identity>``, the rail from the structured method (AEGIS H1,
    R2-H1, R2-L1). ``BOA-ACH-12345`` and ``boa-ach-12345`` are one ACH transfer; so are ``12345``, ``ach:12345``,
    ``ACH12345`` and ``000012345``. ``REFUND77`` and ``UND77`` are two. An empty identity is refused as invalid; a
    reference naming another rail than the method is refused by the caller (FUNDS_METHOD_CONFLICT)."""
    return f"{RAILS.get(method, method)}:{funds_ref_parts(value)[1]}"


def unit_ref_key(value: str) -> str:
    """The key one physical face is known by at its vendor (AEGIS M1): NFKC, case-folded, separators removed."""
    return "".join(_alnum_tokens(value))


def order_key(tenant: str, campaign: str, vendor: str, quotes: list, amount: str, currency: str, version: int) -> str:
    """The duplicate-order key (section 50): bound to tenant, campaign, vendor, the exact quotes, amount and version."""
    return hashlib.sha256(canonical([tenant, campaign, vendor, sorted(quotes), amount, currency, version])
                          .encode()).hexdigest()


class CampaignsMixin:
    # ================================================================================================ helpers

    def _campaign(self, tid: str, cid: str) -> dict:
        c = self.campaigns.get(cid)
        if c is None or c["tenant_id"] != tid:            # another tenant's campaign: the same 404 as a missing one
            raise NotFound(R("CAMPAIGN_NOT_FOUND"))
        return c

    def _refuse(self, code: str, c: dict, rk: str, exc=Conflict, **extra):
        """A refusal of a money or authority gate stands whether or not it is recorded; it is recorded best effort."""
        payload = {"campaign_id": c["campaign_id"], "code": code, "rk": rk}
        self.rec.try_record(derived_id("ref", rk, code), "purchase_gate_refused", "ooh", f"campaign:{c['campaign_id']}",
                            payload, f"refused {code}")
        raise exc(R(code), **extra)

    @staticmethod
    def _expect(c: dict, body: dict) -> None:
        if body["expected_version"] != c["version"]:
            raise Conflict(R("VERSION_STALE"))

    def _writable(self, c: dict, states: tuple) -> None:
        if c["state"] not in states:
            raise Conflict(R("CAMPAIGN_CLOSED_FOR_WRITES"))

    def _future(self, value: Optional[str], code: str) -> str:
        if value is None:
            raise Invalid(R(code))
        try:
            t = parse_iso(value)
        except ValueError:
            raise Invalid(R(code)) from None
        if t <= self.now():
            raise Conflict(R(code))
        return value

    def _expired(self, value: Optional[str]) -> bool:
        return value is None or parse_iso(value) <= self.now()

    @staticmethod
    def _unit_in_two_quotes(qs: list) -> bool:
        """AEGIS H2: one unit named by more than one of these quotes would be bought (or priced) twice."""
        seen: set = set()
        for q in qs:
            for u in set(q["inventory_ids"]):
                if u in seen:
                    return True
                seen.add(u)
        return False

    def _ev(self, kind: str, cid: str, payload: dict, actor: str, rk: str):
        return (kind, f"campaign:{cid}", payload, (actor, rk))

    # ================================================================================================ campaigns

    def create_campaign(self, actor: str, tid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("campaign", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.campaign_view(tid, prev[1])
            self.check_kill(tenant=tid, capability="campaigns")
            if len(set(body["markets"])) != len(body["markets"]):
                raise Invalid(R("INVALID"), field="markets")
            for mk in body["markets"]:
                self.require_market(mk)                          # OOH-2: a market of the registry, never free text
            cid = derived_id("cmp", tid, rk)
            data = {"campaign_id": cid, "tenant_id": tid, "name": body["name"], "markets": list(body["markets"])}
            self._commit("campaign_created", self._req(data, actor, rk, body, cid), actor,
                         evidence=self._ev("campaign_created", cid, {"campaign_id": cid, "tenant_id": tid}, actor, rk))
            return self.campaign_view(tid, cid)

    def _a_campaign_created(self, d, at):
        self.campaigns[d["campaign_id"]] = {
            "campaign_id": d["campaign_id"], "tenant_id": d["tenant_id"], "name": d["name"], "markets": d["markets"],
            "state": "INTAKE", "version": 1, "resume_to": None, "created_at": at,
            "history": [{"from": None, "to": "INTAKE", "reason": "CREATED", "actor": d["actor"], "at": at}],
            "commercial": None, "current_sell_price_id": None, "buy_limit_id": None, "client_approval": None,
            "funds": [], "inventory_ids": [], "price_ids": [], "authorization_ids": []}

    def _move(self, c: dict, to: str, reason: str, actor: str, at: str) -> None:
        """The ONE place a campaign's state changes (only ever called from an ``_a_*`` handler, i.e. after the
        evidence and the anchored log line). Entering an exception state voids every unsubmitted authorization."""
        frm = c["state"]
        if to in P.RESUMABLE:
            c["resume_to"] = P.RESUME_MAP.get(frm, frm)
        elif frm in P.RESUMABLE:
            c["resume_to"] = None
        if to in VOIDING_STATES:
            for aid in c["authorization_ids"]:
                a = self.authorizations[aid]
                if a["status"] == "AUTHORIZED" and a["submission_state"] == "NOT_SUBMITTED":
                    a["status"] = "VOID"
                    a["void_reason"] = f"CAMPAIGN_{to}"
        if to == "CHANGE_REQUESTED":
            c["client_approval"] = None              # the client approves the changed campaign again
        c["state"] = to
        c["version"] += 1
        c["history"].append({"from": frm, "to": to, "reason": reason, "actor": actor, "at": at})

    def transition(self, actor: str, tid: str, cid: str, body: dict, andre: bool) -> dict:
        to = body["to"]
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            actor = "andre" if andre else actor
            rk = self.rk("transition", cid, body)
            if self._idem(actor, rk, body):
                return self.campaign_view(tid, cid)
            self.check_kill(tenant=tid, capability="campaigns")
            frm = c["state"]
            if frm != body["expected_state"]:
                raise Conflict(R("STATE_STALE"))
            self._expect(c, body)
            if frm in P.RESUMABLE:
                if to != c["resume_to"]:
                    raise Conflict(R("RESUME_TARGET_INVALID"))
                if frm == "DISPUTED" and not andre:
                    raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
            else:
                if to in P.CAMPAIGN_NOT_BUILT:
                    raise Conflict(R("STATE_NOT_BUILT"))              # defined in section 4, not built: never reached
                if to in P.CAMPAIGN_GATED:
                    raise Conflict(R("TRANSITION_GATED"))
                if to not in P.CAMPAIGN_ALLOWED.get(frm, ()):
                    raise Conflict(R("TRANSITION_NOT_ALLOWED"))
                if (frm, to) in P.CAMPAIGN_ANDRE and not andre:
                    raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
                guard = self._guard(c, to)
                if guard:
                    raise Conflict(R("TRANSITION_GUARD_FAILED"), guard=guard)
            data = {"campaign_id": cid, "from": frm, "to": to, "reason": body["reason"], "version": c["version"]}
            self._commit("campaign_transition", self._req(data, actor, rk, body, cid), actor,
                         evidence=self._ev("campaign_transition", cid, data, actor, rk))
            return self.campaign_view(tid, cid)

    def _guard(self, c: dict, to: str) -> Optional[str]:
        live = [u for u in (self.inventory[i] for i in c["inventory_ids"]) if u["state"] != "WITHDRAWN"]
        if to == "AVAILS_REQUESTED" and not live:
            return "NO_INVENTORY"
        if to == "PLAN_APPROVED" and not all(self.flag_met(mk, "PLANNING_COVERAGE") for mk in c["markets"]):
            return "MARKET_PLANNING_NOT_COVERED"                 # OOH-2: a plan only where ZBM has planning coverage
        if to == "PROPOSAL_SENT" and c["current_sell_price_id"] is None:
            return "NO_CURRENT_SELL_PRICE"                   # AEGIS L5: a model reset after the price unseated it
        if to == "QUOTES_VALIDATED":
            if c["commercial"] is None:
                return "NO_COMMERCIAL_MODEL"
            sp = self.prices.get(c["current_sell_price_id"] or "")
            if sp is None:
                return "NO_CURRENT_SELL_PRICE"
            if any(self.prices[q].get("retired") for q in sp["quote_refs"]):
                return "SELL_PRICE_QUOTE_RETIRED"
            if any(self._expired(self.prices[q]["valid_until"]) for q in sp["quote_refs"]):
                return "SELL_PRICE_QUOTE_EXPIRED"
        return None

    def _a_campaign_transition(self, d, at):
        self._move(self.campaigns[d["campaign_id"]], d["to"], d["reason"], d["actor"], at)

    # ================================================================================================ inventory

    def add_inventory(self, actor: str, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("inventory", cid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.unit_view(prev[1])
            self.check_kill(tenant=tid, capability="inventory")
            self._writable(c, PRE_ORDER_WRITABLE)
            if len(c["inventory_ids"]) >= self.settings.inventory_max:
                raise Conflict(R("INVENTORY_LIMIT"))
            face = self._face(body["face_id"])                  # OOH-2: a unit is a registry face for a flight
            if face["market"] not in c["markets"]:
                raise Conflict(R("MARKET_NOT_IN_CAMPAIGN"))
            self.require_vendor(face["vendor"])
            key = face["unit_key"]
            flight = self._flight(body)
            if self._registration_clash(tid, face["vendor"], key, flight, None):
                raise Conflict(R("UNIT_ALREADY_REGISTERED"))
            uid = derived_id("inv", tid, rk)
            data = {"unit_id": uid, "campaign_id": cid, "tenant_id": tid, "face_id": face["face_id"],
                    "vendor": face["vendor"], "format": face["format"], "market": face["market"],
                    "vendor_unit_ref": face["vendor_face_ref"], "unit_key": key,
                    "proof_requirement": face["proof_requirement"], "flight": flight,
                    "vendor_text": observed(body.get("vendor_text"))}
            ev = {"unit_id": uid, "campaign_id": cid, "face_id": face["face_id"], "vendor": face["vendor"],
                  "vendor_text_sha256": (data["vendor_text"] or {}).get("sha256")}
            self._commit("inventory_added", self._req(data, actor, rk, body, uid), actor,
                         evidence=self._ev("inventory_added", cid, ev, actor, rk))
            return self.unit_view(uid)

    def _a_inventory_added(self, d, at):
        u = {k: d[k] for k in ("unit_id", "campaign_id", "tenant_id", "face_id", "vendor", "format", "market",
                               "vendor_unit_ref", "unit_key", "proof_requirement", "flight", "vendor_text")}
        u.update(state="IDENTIFIED", version=1, vendor_ref=None, hold_expires_at=None, evidence_class=None,
                 hold_flight=None, hold_confirmed_by=None, hold_evidence_id=None, hold_evidence=[],
                 pre_booking_state=None, history=[{"from": None, "to": "IDENTIFIED", "actor": d["actor"], "at": at}])
        self.inventory[d["unit_id"]] = u
        c = self.campaigns[d["campaign_id"]]
        c["inventory_ids"].append(d["unit_id"])
        c["version"] += 1

    # ------------------------------------------------------------------ flights (AEGIS R2-M2)

    @staticmethod
    def _flight(body: dict) -> Optional[list]:
        """``[start, end]`` (ISO dates, start <= end), or None when neither is given; one without the other is invalid."""
        start, end = body.get("flight_start"), body.get("flight_end")
        if start is None and end is None:
            return None
        if start is None or end is None:
            raise Invalid(R("FLIGHT_INVALID"))
        try:
            s, e = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            raise Invalid(R("FLIGHT_INVALID")) from None
        if s > e or (e - s).days + 1 > P.FLIGHT_MAX_DAYS:
            raise Invalid(R("FLIGHT_INVALID"))
        return [s.isoformat(), e.isoformat()]

    def _flight_started(self, flight: Optional[list]) -> bool:
        """The flight begins before today (the injected clock's UTC date): nothing is quoted or bought for it."""
        return flight is not None and flight[0] < self.today()

    @staticmethod
    def _overlap(a: Optional[list], b: Optional[list]) -> bool:
        """Two flights share a day. A face with no flight yet may run any day: it overlaps everything (fail closed)."""
        if a is None or b is None:
            return True
        return not (a[1] < b[0] or b[1] < a[0])

    def _registration_clash(self, tid: str, vendor: str, key: str, flight: Optional[list], own: Optional[str]) -> bool:
        """Within one tenant, one face (vendor, unit ref) has one live record per flight: a duplicate is refused only
        when the flights overlap, among records not withdrawn and not in a cancelled campaign (AEGIS R2-M2 item 4: a
        booked face is free again for another flight)."""
        return any(u["tenant_id"] == tid and u["vendor"] == vendor and u["unit_key"] == key and u["unit_id"] != own
                   and u["state"] != "WITHDRAWN" and self.campaigns[u["campaign_id"]]["state"] != "CANCELLED"
                   and self._overlap(u["flight"], flight) for u in self.inventory.values())

    def set_flight(self, actor: str, tid: str, cid: str, uid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            u = self.inventory.get(uid)
            if u is None or u["campaign_id"] != cid:
                raise NotFound(R("INVENTORY_NOT_FOUND"))
            rk = self.rk("flight", uid, body)
            if self._idem(actor, rk, body):
                return self.unit_view(uid)
            self.check_kill(tenant=tid, capability="inventory")
            if c["state"] not in PLANNING_STATES:
                # AEGIS R3-H1: from PROPOSAL_SENT on, the flight is what the client approved and the funds cover;
                # changing it goes through CHANGE_REQUESTED (which voids approvals and authorizations)
                raise Conflict(R("FLIGHT_LOCKED"))
            if u["version"] != body["expected_version"]:
                raise Conflict(R("VERSION_STALE"))
            flight = self._flight(body)
            if flight is None:
                raise Invalid(R("FLIGHT_INVALID"))
            if self._registration_clash(tid, u["vendor"], u["unit_key"], flight, uid):
                raise Conflict(R("UNIT_ALREADY_REGISTERED"))
            # a unit priced or held for another flight goes back to VERIFIED; its quotes are retired
            reset = u["state"] in FLIGHT_BOUND_STATES and u["flight"] != flight
            retired = sorted(p for p in c["price_ids"] if self.prices[p]["price_type"] == "VENDOR_QUOTE"
                             and not self.prices[p].get("retired") and uid in self.prices[p]["inventory_ids"]
                             and u["flight"] != flight)
            data = {"unit_id": uid, "campaign_id": cid, "flight": flight, "reset_to_verified": reset,
                    "retired_quotes": retired}
            self._commit("flight_set", self._req(data, actor, rk, body, uid), actor,
                         evidence=self._ev("unit_flight_set", cid, data, actor, rk))
            return self.unit_view(uid)

    def _a_flight_set(self, d, at):
        u = self.inventory[d["unit_id"]]
        c = self.campaigns[d["campaign_id"]]
        u["flight"] = list(d["flight"])
        if d.get("reset_to_verified"):
            self._unit_move(u, "VERIFIED", d["actor"], at)
            self._clear_hold(u)
        else:
            u["version"] += 1
            for ev in u.get("hold_evidence", ()):         # evidence about the unit before this change (AEGIS S2-L3)
                if ev["status"] == "PENDING_ANDRE":
                    ev["status"] = "STALE"
        for pid in d.get("retired_quotes", []):
            self.prices[pid].update(retired=True, retired_reason="FLIGHT_CHANGED")
        sp = self.prices.get(c["current_sell_price_id"] or "")
        if sp is not None and set(sp["quote_refs"]) & set(d.get("retired_quotes", [])):
            c["current_sell_price_id"] = None              # its cost basis no longer exists
        c["version"] += 1

    def move_availability(self, actor: str, tid: str, cid: str, uid: str, body: dict) -> dict:
        to = body["to"]
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            u = self.inventory.get(uid)
            if u is None or u["campaign_id"] != cid:
                raise NotFound(R("INVENTORY_NOT_FOUND"))
            rk = self.rk("availability", uid, body)
            if self._idem(actor, rk, body):
                return self.unit_view(uid)
            self.check_kill(tenant=tid, capability="inventory")
            self._writable(c, PRE_ORDER_WRITABLE)
            if u["state"] != body["expected_state"]:
                raise Conflict(R("STATE_STALE"))
            if u["version"] != body["expected_version"]:
                raise Conflict(R("VERSION_STALE"))
            if to in P.AVAILABILITY_GATED:
                raise Conflict(R("AVAILABILITY_GATED"))
            if to in P.AVAILABILITY_ANDRE_ONLY:
                raise Conflict(R("HOLD_CONFIRMATION_ANDRE_ONLY"))       # evidence, then Andre's decision
            if to not in P.AVAILABILITY_ALLOWED[u["state"]]:
                raise Conflict(R("AVAILABILITY_TRANSITION_NOT_ALLOWED"))
            if to == "QUOTED" and not any(self.prices[p]["price_type"] == "VENDOR_QUOTE" and uid in
                                          self.prices[p]["inventory_ids"] and not self.prices[p].get("retired")
                                          and self.prices[p]["flights"].get(uid) == u["flight"]
                                          for p in c["price_ids"]):
                raise Conflict(R("TRANSITION_GUARD_FAILED"), guard="NO_VENDOR_QUOTE_FOR_UNIT")
            if to == "HOLD_REQUESTED":
                av = self.avails_for(u["face_id"], u["flight"])
                if av is not None and av["answer"] == "UNAVAILABLE":
                    raise Conflict(R("AVAILS_UNAVAILABLE"))         # the vendor's latest answer for this flight is no
            data = {"unit_id": uid, "campaign_id": cid, "from": u["state"], "to": to}
            self._commit("availability_moved", self._req(data, actor, rk, body, uid), actor,
                         evidence=self._ev("availability_moved", cid, data, actor, rk))
            return self.unit_view(uid)

    def _a_availability_moved(self, d, at):
        u = self.inventory[d["unit_id"]]
        self._unit_move(u, d["to"], d["actor"], at)
        self._clear_hold(u)              # no route here reaches HOLD_CONFIRMED; leaving it (an extension) ends the hold
        self.campaigns[d["campaign_id"]]["version"] += 1

    @staticmethod
    def _clear_hold(u: dict) -> None:
        u.update(vendor_ref=None, hold_expires_at=None, hold_flight=None, evidence_class=None, hold_confirmed_by=None,
                 hold_evidence_id=None)

    @staticmethod
    def _unit_move(u: dict, to: str, actor: str, at: str) -> None:
        """Every availability change of a unit. Hold evidence still waiting for Andre goes STALE: it was about the
        unit as it was."""
        u["history"].append({"from": u["state"], "to": to, "actor": actor, "at": at})
        u["state"] = to
        u["version"] += 1
        for ev in u.get("hold_evidence", ()):
            if ev["status"] == "PENDING_ANDRE":
                ev["status"] = "STALE"

    # ------------------------------------------------------------------ hold evidence and Andre's decision (Oct 16 r2)

    def _unit(self, cid: str, uid: str) -> dict:
        u = self.inventory.get(uid)
        if u is None or u["campaign_id"] != cid:
            raise NotFound(R("INVENTORY_NOT_FOUND"))
        return u

    def submit_hold_evidence(self, actor: str, tid: str, cid: str, uid: str, body: dict) -> dict:
        """An agent (or the dashboard) attaches the vendor's hold evidence to a HOLD_REQUESTED unit for Andre's
        decision. The unit stays HOLD_REQUESTED; nothing is confirmed. Newer evidence supersedes older pending
        evidence."""
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            u = self._unit(cid, uid)
            rk = self.rk("hold_evidence", uid, body)
            if self._idem(actor, rk, body):
                return self.unit_view(uid)
            self.check_kill(tenant=tid, capability="inventory")
            self._writable(c, PRE_ORDER_WRITABLE)
            if u["state"] != "HOLD_REQUESTED":
                raise Conflict(R("HOLD_NOT_REQUESTED"))
            if u["version"] != body["expected_version"]:
                raise Conflict(R("VERSION_STALE"))
            if u["flight"] is None:
                raise Conflict(R("FLIGHT_REQUIRED"))
            expires = self._future(body["hold_expires_at"], "HOLD_EXPIRY_REQUIRED")
            eid = derived_id("hev", uid, actor, rk)       # the caller too: one id, one record (AEGIS step 2 L1)
            data = {"unit_id": uid, "campaign_id": cid, "evidence_id": eid, "vendor_ref": body["vendor_ref"],
                    "hold_expires_at": expires, "evidence_doc_ref": body["evidence_doc_ref"],
                    "flight": list(u["flight"]), "evidence_class": P.EVIDENCE_ALLOWED["hold_evidence"][0],
                    "vendor_text": observed(body.get("vendor_text"))}
            ev = {k: data[k] for k in ("unit_id", "evidence_id", "vendor_ref", "hold_expires_at", "evidence_doc_ref",
                                       "flight", "evidence_class")}
            ev["vendor_text_sha256"] = (data["vendor_text"] or {}).get("sha256")
            self._commit("hold_evidence_submitted", self._req(data, actor, rk, body, uid), actor,
                         evidence=self._ev("hold_evidence_submitted", cid, ev, actor, rk))
            return self.unit_view(uid)

    def _a_hold_evidence_submitted(self, d, at):
        u = self.inventory[d["unit_id"]]
        for old in u["hold_evidence"]:
            if old["status"] == "PENDING_ANDRE":
                old["status"] = "SUPERSEDED"
        u["version"] += 1
        u["hold_evidence"].append({
            "evidence_id": d["evidence_id"], "status": "PENDING_ANDRE", "vendor_ref": d["vendor_ref"],
            "hold_expires_at": d["hold_expires_at"], "evidence_doc_ref": d["evidence_doc_ref"],
            "flight": list(d["flight"]), "evidence_class": d["evidence_class"], "vendor_text": d["vendor_text"],
            "submitted_by": d["actor"], "submitted_at": at, "unit_version": u["version"],
            "decided_by": None, "decided_at": None, "reject_reason": None})
        self.campaigns[d["campaign_id"]]["version"] += 1

    def decide_hold(self, tid: str, cid: str, uid: str, body: dict) -> dict:
        """Andre's decision on one named hold-evidence record. CONFIRM is the ONLY way a unit becomes HOLD_CONFIRMED:
        the evidence must still be the unit's pending record, nothing recorded on the unit since (its version), the
        unit still HOLD_REQUESTED for the same flight, and the vendor's expiry still ahead."""
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            u = self._unit(cid, uid)
            rk = self.rk("hold_decision", uid, body)
            if self._idem("andre", rk, body):
                return self.unit_view(uid)
            self.check_kill(tenant=tid, capability="inventory")
            self._writable(c, PRE_ORDER_WRITABLE)
            if u["version"] != body["expected_version"]:
                raise Conflict(R("VERSION_STALE"))
            ev = next((e for e in u["hold_evidence"] if e["evidence_id"] == body["evidence_id"]), None)
            if ev is None:
                raise NotFound(R("HOLD_EVIDENCE_NOT_FOUND"))
            if ev["status"] == "STALE":
                raise Conflict(R("HOLD_EVIDENCE_STALE"))
            if ev["status"] != "PENDING_ANDRE":
                raise Conflict(R("HOLD_EVIDENCE_NOT_PENDING"))
            if u["state"] != "HOLD_REQUESTED" or u["version"] != ev["unit_version"] or u["flight"] != ev["flight"]:
                raise Conflict(R("HOLD_EVIDENCE_STALE"))
            confirm = body["decision"] == "CONFIRM"
            if confirm == (body.get("reject_reason") is not None):
                raise Invalid(R("INVALID"), field="reject_reason")    # a rejection names its reason; a confirm none
            if confirm and self._expired(ev["hold_expires_at"]):
                raise Conflict(R("HOLD_EXPIRED"))
            data = {"unit_id": uid, "campaign_id": cid, "evidence_id": ev["evidence_id"], "decision": body["decision"],
                    "reject_reason": body.get("reject_reason")}
            self._commit("hold_decided", self._req(data, "andre", rk, body, uid), "andre",
                         evidence=self._ev("hold_confirmed_by_andre" if confirm else "hold_evidence_rejected_by_andre",
                                           cid, {**data, "vendor_ref": ev["vendor_ref"],
                                                 "hold_expires_at": ev["hold_expires_at"], "flight": ev["flight"]},
                                           "andre", rk))
            return self.unit_view(uid)

    def _a_hold_decided(self, d, at):
        u = self.inventory[d["unit_id"]]
        ev = next(e for e in u["hold_evidence"] if e["evidence_id"] == d["evidence_id"])
        if d["decision"] == "CONFIRM":
            self._unit_move(u, "HOLD_CONFIRMED", d["actor"], at)
            # the hold is for the flight the evidence was for, which is the unit's flight (AEGIS R3-H1)
            u.update(vendor_ref=ev["vendor_ref"], hold_expires_at=ev["hold_expires_at"], hold_flight=list(ev["flight"]),
                     evidence_class=P.EVIDENCE_ALLOWED["hold_confirmation"][0], hold_confirmed_by=d["actor"],
                     hold_evidence_id=ev["evidence_id"])
            ev["status"] = "CONFIRMED_BY_ANDRE"
        else:
            u["version"] += 1
            ev["status"] = "REJECTED_BY_ANDRE"
            ev["reject_reason"] = d["reject_reason"]
        ev.update(decided_by=d["actor"], decided_at=at)
        self.campaigns[d["campaign_id"]]["version"] += 1

    # ================================================================================================ prices (s. 19)

    def record_price(self, actor: str, tid: str, cid: str, body: dict, andre: bool) -> dict:
        pt = body["price_type"]
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            actor = "andre" if andre else actor
            rk = self.rk("price", cid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.price_view(prev[1])
            self.check_kill(tenant=tid, capability="prices")
            self.require_vendor(body.get("vendor"))
            if pt in P.PRICE_TYPES_NOT_IN_WAVE:
                raise Invalid(R("PRICE_TYPE_NOT_IN_WAVE"))
            if pt in P.PRICE_TYPES_ANDRE and not andre:
                raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
            self._writable(c, SELL_PRICE_STATES if pt == "CLIENT_SELL_PRICE" else PRE_ORDER_WRITABLE)
            if body["currency"] not in P.CURRENCIES:
                raise Invalid(R("CURRENCY_UNSUPPORTED"))
            if Decimal(body["amount"]) <= 0:
                raise Invalid(R("INVALID"), field="amount")
            allowed = P.EVIDENCE_ALLOWED[pt]
            ec = body.get("evidence_class")
            if ec is None:
                ec = allowed[0]
            elif ec not in allowed:
                raise Invalid(R("EVIDENCE_CLASS_NOT_ALLOWED"))
            inv = list(body.get("inventory_ids") or [])
            for i in inv:
                u = self.inventory.get(i)
                if u is None or u["campaign_id"] != cid:
                    raise NotFound(R("INVENTORY_NOT_FOUND"))
                if body.get("vendor") and u["vendor"] != body["vendor"]:
                    raise Conflict(R("VENDOR_MISMATCH"))
            if len(set(inv)) != len(inv):
                raise Invalid(R("INVALID"), field="inventory_ids")
            quotes = list(body.get("quote_refs") or [])
            if quotes and pt != "CLIENT_SELL_PRICE":
                raise Invalid(R("INVALID"), field="quote_refs")
            valid_until = body.get("valid_until")
            extra: dict = {}
            if pt == "VENDOR_QUOTE":
                if not body.get("vendor") or not inv:
                    raise Invalid(R("INVALID"), field="vendor/inventory_ids")
                if any(self.inventory[i]["flight"] is None for i in inv):
                    raise Conflict(R("FLIGHT_REQUIRED"))             # a price is for a face AND a flight
                if any(self._flight_started(self.inventory[i]["flight"]) for i in inv):
                    raise Conflict(R("FLIGHT_IN_PAST"))
                extra = {"flights": {i: list(self.inventory[i]["flight"]) for i in inv}}
                valid_until = self._future(valid_until, "QUOTE_VALIDITY_REQUIRED")
            elif valid_until is not None:
                raise Invalid(R("INVALID"), field="valid_until")
            if pt == "CLIENT_SELL_PRICE":
                cm = c["commercial"]
                if cm is None:
                    raise Conflict(R("COMMERCIAL_MODEL_MISSING"))
                if not quotes or len(set(quotes)) != len(quotes):
                    raise Conflict(R("SELL_PRICE_QUOTES_INVALID"))
                qs = [self.prices.get(q) for q in quotes]
                if any(q is None or q["campaign_id"] != cid or q["price_type"] != "VENDOR_QUOTE" or q.get("retired")
                       for q in qs):
                    raise Conflict(R("SELL_PRICE_QUOTES_INVALID"))
                if any(self._expired(q["valid_until"]) for q in qs):
                    raise Conflict(R("QUOTE_EXPIRED"))
                if self._unit_in_two_quotes(qs):
                    raise Conflict(R("UNIT_IN_MULTIPLE_QUOTES"))
                if any(q["currency"] != body["currency"] for q in qs):
                    raise Conflict(R("CURRENCY_MISMATCH"))
                cost = sum((Decimal(q["amount"]) for q in qs), Decimal(0))
                expected = P.apply_markup(cost, Decimal(cm["markup_pct"]))
                if Decimal(body["amount"]) != expected:
                    raise Conflict(R("MARKUP_MISMATCH"))
                extra = {"cost_basis": P.fmt(cost), "markup_pct": cm["markup_pct"], "terms_id": cm["terms_id"]}
            pid = derived_id("prc", tid, rk)
            data = {"price_id": pid, "campaign_id": cid, "tenant_id": tid, "price_type": pt, "amount": body["amount"],
                    "currency": body["currency"], "vendor": body.get("vendor"), "inventory_ids": inv,
                    "quote_refs": quotes, "evidence_class": ec, "valid_until": valid_until,
                    "source_ref": body.get("source_ref"), "vendor_text": observed(body.get("vendor_text")), **extra}
            ev = {k: data[k] for k in ("price_id", "price_type", "amount", "currency", "vendor", "inventory_ids",
                                       "quote_refs", "evidence_class", "valid_until")}
            ev["vendor_text_sha256"] = (data["vendor_text"] or {}).get("sha256")
            self._commit("price_recorded", self._req(data, actor, rk, body, pid), actor,
                         evidence=self._ev("price_recorded", cid, ev, actor, rk))
            return self.price_view(pid)

    def _a_price_recorded(self, d, at):
        p = {k: v for k, v in d.items() if k not in ("actor", "request_id", "request_sha", "_obj", "evidence")}
        p.update(recorded_by=d["actor"], recorded_at=at)
        self.prices[d["price_id"]] = p
        c = self.campaigns[d["campaign_id"]]
        c["price_ids"].append(d["price_id"])
        if d["price_type"] == "CLIENT_SELL_PRICE":
            c["current_sell_price_id"] = d["price_id"]
        elif d["price_type"] == "APPROVED_BUY_LIMIT":
            c["buy_limit_id"] = d["price_id"]
        c["version"] += 1

    # ================================================================================================ commercial (s. 9-10)

    def principal_agent_state(self) -> str:
        d = self.pa_determination
        return d["determination"] if d else P.PRINCIPAL_AGENT_DEFAULT

    def determine_principal_agent(self, body: dict) -> dict:
        """Andre records the CPA's or counsel's confirmation (locked decision, Oct 16 round 2). Department-wide."""
        with self.lock:
            self._gate()
            rk = self.rk("pa_determination", "ooh", body)
            if self._idem("andre", rk, body):
                return self.pa_view()
            self.check_kill(capability="purchase")
            data = {k: body[k] for k in ("determination", "confirmed_by", "confirmation_ref")}
            self._commit("pa_determined", self._req(data, "andre", rk, body, "ooh"), "andre",
                         evidence=("principal_agent_determination_recorded_by_andre", "ooh:principal-agent", data,
                                   ("andre", rk)))
            return self.pa_view()

    def _a_pa_determined(self, d, at):
        self.pa_determination = None if d["determination"] == "UNDETERMINED" else {
            k: d[k] for k in ("determination", "confirmed_by", "confirmation_ref")} | {"recorded_by": d["actor"],
                                                                                         "recorded_at": at}

    def pa_view(self) -> dict:
        with self.lock:
            return {"principal_agent_state": self.principal_agent_state(),
                    "determination": dict(self.pa_determination) if self.pa_determination else None,
                    "purchases_blocked": self.pa_determination is None}

    def set_commercial_model(self, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("commercial", cid, body)
            if self._idem("andre", rk, body):
                return self.campaign_view(tid, cid)
            self.check_kill(tenant=tid, capability="prices")
            if c["state"] not in PLANNING_STATES:
                raise Conflict(R("COMMERCIAL_MODEL_LOCKED"))
            self._expect(c, body)
            pa = body["principal_agent_state"]
            if pa != "UNDETERMINED" and pa != self.principal_agent_state():
                raise Conflict(R("PRINCIPAL_AGENT_NOT_CONFIRMED"))     # only what a CPA / counsel confirmed
            model = {k: body[k] for k in ("principal_agent_state", "pricing_method", "markup_pct", "markup_disclosed",
                                          "markup_basis_ref", "presentation", "cancellation_terms_ref")}
            terms_id = derived_id("ctm", cid, c["version"], model)
            data = {"campaign_id": cid, "model": model, "terms_id": terms_id}
            self._commit("commercial_model_set", self._req(data, "andre", rk, body, cid), "andre",
                         evidence=self._ev("commercial_model_set", cid, {"terms_id": terms_id, **model}, "andre", rk))
            return self.campaign_view(tid, cid)

    def _a_commercial_model_set(self, d, at):
        c = self.campaigns[d["campaign_id"]]
        c["commercial"] = {**d["model"], "terms_id": d["terms_id"], "funding_policy": P.FUNDING_POLICY,
                           "suggested_markup_pct": P.SUGGESTED_MARKUP_PCT, "suggested_markup_applied": False,
                           "set_by": d["actor"], "set_at": at}
        c["current_sell_price_id"] = None            # a sell price made under other terms is no longer current
        c["version"] += 1

    # ================================================================================================ client approval

    def client_approve(self, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("client_approval", cid, body)
            if self._idem("andre", rk, body):
                return self.campaign_view(tid, cid)
            self.check_kill(tenant=tid, capability="campaigns")
            if c["state"] != "PROPOSAL_SENT":
                self._refuse("STATE_NOT_ELIGIBLE", c, rk)
            self._expect(c, body)
            if body["sell_price_id"] != c["current_sell_price_id"]:
                self._refuse("SELL_PRICE_NOT_CURRENT", c, rk)
            sp = self.prices[body["sell_price_id"]]
            approval_id = derived_id("cap", cid, rk)
            data = {"campaign_id": cid, "approval_id": approval_id, "sell_price_id": sp["price_id"],
                    "amount": sp["amount"], "currency": sp["currency"], "quote_refs": list(sp["quote_refs"]),
                    "doc_ref": body["client_approval_doc_ref"], "terms_id": sp["terms_id"]}
            self._commit("client_approved", self._req(data, "andre", rk, body, cid), "andre",
                         evidence=self._ev("client_approval_recorded_by_andre", cid, data, "andre", rk))
            return self.campaign_view(tid, cid)

    def _a_client_approved(self, d, at):
        c = self.campaigns[d["campaign_id"]]
        self._move(c, "CLIENT_APPROVED", "CLIENT_APPROVAL_RECORDED", d["actor"], at)
        c["client_approval"] = {k: d[k] for k in ("approval_id", "sell_price_id", "amount", "currency", "quote_refs",
                                                  "doc_ref", "terms_id")}

    # ================================================================================================ funds (s. 11)

    def exposure(self, c: dict) -> dict:
        """The section 11 cash-exposure view, computed from the records (never stored, so it cannot drift)."""
        received = sum((Decimal(f["amount"]) for f in c["funds"] if f["source"] in P.FUNDS_SOURCES_CONNECTED),
                       Decimal(0))
        approved = Decimal(c["client_approval"]["amount"]) if c["client_approval"] else Decimal(0)
        auths = [self.authorizations[a] for a in c["authorization_ids"]]
        vendor = sum((Decimal(a["authorized_amount"]) for a in auths if a["submission_state"] in P.SUBMISSION_OPEN),
                     Decimal(0))
        pending = sum((Decimal(a["authorized_amount"]) for a in auths
                       if a["status"] == "AUTHORIZED" and a["submission_state"] == "NOT_SUBMITTED"), Decimal(0))
        production = Decimal(0)
        state = "NONE" if received == 0 else ("CLEARED_FULL" if approved > 0 and received >= approved else "PARTIAL")
        return {"funding_policy": P.FUNDING_POLICY, "funding_state": state,
                "client_funds_received": P.fmt(received),
                "client_funds_available": P.fmt(received - vendor - production - pending),
                "approved_client_commitment": P.fmt(approved),
                "vendor_commitment": P.fmt(vendor),
                "authorized_not_submitted": P.fmt(pending),
                "production_commitment": P.fmt(production), "production_commitment_tracked": False,
                "cancellation_liability": None, "cancellation_liability_tracked": False,
                "unsecured_exposure": P.fmt(max(Decimal(0), vendor + production - received)),
                "authorized_exposure_limit": P.fmt(received),
                "currency": "USD"}

    def verify_funds(self, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("funds", cid, body)
            if self._idem("andre", rk, body):
                return self.campaign_view(tid, cid)
            self.check_kill(tenant=tid, capability="funds")
            if c["state"] not in FUNDABLE:
                self._refuse("STATE_NOT_ELIGIBLE", c, rk)
            self._expect(c, body)
            if Decimal(body["amount"]) <= 0:
                raise Invalid(R("INVALID"), field="amount")             # AEGIS M3: a funds record moves money
            if body["currency"] != c["client_approval"]["currency"]:
                self._refuse("FUNDS_CURRENCY_MISMATCH", c, rk)
            rails, ident = funds_ref_parts(body["evidence_ref"])
            if not ident:
                raise Invalid(R("INVALID"), field="evidence_ref")
            if rails - {RAILS[body["method"]]}:
                raise Invalid(R("FUNDS_METHOD_CONFLICT"))           # the reference names another rail (R2-L1)
            key = funds_ref_key(body["evidence_ref"], body["method"])
            if key in self.funds_evidence:
                self._refuse("FUNDS_EVIDENCE_REUSED", c, rk)          # one transfer, one record (AEGIS H1)
            data = {"campaign_id": cid, "amount": body["amount"], "currency": body["currency"],
                    "method": body["method"], "evidence_ref": body["evidence_ref"], "evidence_key": key,
                    "source": "ANDRE_GATE",
                    "evidence_class": P.EVIDENCE_ALLOWED["funds_andre_gate"][0]}
            received = Decimal(self.exposure(c)["client_funds_received"]) + Decimal(body["amount"])
            data["clears_in_full"] = received >= Decimal(c["client_approval"]["amount"])
            # AEGIS R3-L: different rails never merge, but the same reference already recorded on another rail is
            # worth Andre's second look: allowed, warned, and the warning is on the ledger and the record
            ident = key.split(":", 1)[1]
            other = sorted({k.split(":", 1)[0] for k in self.funds_evidence if k.split(":", 1)[1] == ident} - {
                key.split(":", 1)[0]})
            data["warnings"] = [{"code": "SAME_REFERENCE_OTHER_RAIL", "rails": other}] if other else []
            self._commit("funds_verified", self._req(data, "andre", rk, body, cid), "andre",
                         evidence=self._ev("client_funds_verified_by_andre", cid, data, "andre", rk))
            return {**self.campaign_view(tid, cid), "warnings": data["warnings"]}

    def _a_funds_verified(self, d, at):
        c = self.campaigns[d["campaign_id"]]
        c["funds"].append({k: d[k] for k in ("amount", "currency", "method", "evidence_ref", "evidence_key", "source",
                                             "evidence_class")}
                          | {"verified_by": d["actor"], "at": at, "warnings": list(d.get("warnings", []))})
        self.funds_evidence[d["evidence_key"]] = d["campaign_id"]
        if d["clears_in_full"]:
            self._move(c, "FUNDS_VERIFIED", "CLIENT_FUNDS_CLEARED_IN_FULL", d["actor"], at)
        elif c["state"] == "CLIENT_APPROVED":
            self._move(c, "FUNDING_REQUIRED", "CLIENT_FUNDS_PARTIAL", d["actor"], at)
        else:
            c["version"] += 1

    def reconfirm_funds(self, tid: str, cid: str, body: dict) -> dict:
        """AEGIS M3: after a change request and a new client approval, Andre re-confirms that the cleared funds
        already recorded on this campaign cover the newly approved amount. No new reference, no new money: the
        campaign moves to FUNDS_VERIFIED only if what is already recorded covers the approval in full."""
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("funds_reconfirm", cid, body)
            if self._idem("andre", rk, body):
                return self.campaign_view(tid, cid)
            self.check_kill(tenant=tid, capability="funds")
            if c["state"] not in FUNDABLE:
                self._refuse("STATE_NOT_ELIGIBLE", c, rk)
            self._expect(c, body)
            ap = c["client_approval"]
            if body["approved_amount"] != ap["amount"] or body["currency"] != ap["currency"]:
                self._refuse("CLIENT_APPROVAL_MISMATCH", c, rk)
            x = self.exposure(c)
            if Decimal(x["client_funds_received"]) - Decimal(x["vendor_commitment"]) < Decimal(ap["amount"]):
                self._refuse("FUNDS_INSUFFICIENT", c, rk)
            data = {"campaign_id": cid, "approval_id": ap["approval_id"], "approved_amount": ap["amount"],
                    "client_funds_received": x["client_funds_received"]}
            self._commit("funds_reconfirmed", self._req(data, "andre", rk, body, cid), "andre",
                         evidence=self._ev("client_funds_reconfirmed_by_andre", cid, data, "andre", rk))
            return self.campaign_view(tid, cid)

    def _a_funds_reconfirmed(self, d, at):
        self._move(self.campaigns[d["campaign_id"]], "FUNDS_VERIFIED", "CLEARED_FUNDS_RECONFIRMED", d["actor"], at)

    # ================================================================================================ the gate (s. 12)

    def _gate_problem(self, c: dict, a: dict, at_submit: bool) -> Optional[str]:
        """Every section 12 refusal condition plus 100% prepaid funds, in a fixed order; the first failing code."""
        tid = c["tenant_id"]
        code = self.kill_code(tenant=tid, capability="submission" if at_submit else "purchase") \
            or self.kill_code(capability="purchase")
        if code:
            return code
        if at_submit:
            if c["state"] != "PURCHASE_AUTHORIZED":
                return "STATE_NOT_ELIGIBLE"
        elif c["state"] in FUNDABLE:
            return "FUNDS_NOT_VERIFIED"
        elif c["state"] not in ("FUNDS_VERIFIED", "PURCHASE_AUTHORIZED"):
            return "STATE_NOT_ELIGIBLE"
        cm = c["commercial"]
        if cm is None or cm["principal_agent_state"] == "UNDETERMINED":
            return "PRINCIPAL_AGENT_UNDETERMINED"
        if cm["principal_agent_state"] != self.principal_agent_state():
            return "PRINCIPAL_AGENT_NOT_CONFIRMED"           # the confirmation was withdrawn or changed since
        if a["signer"] != P.SIGNER or a["authority_scope"] not in P.AUTHORITY_SCOPES:
            return "AUTHORITY_SCOPE_INVALID"
        try:
            expires = parse_iso(a["expires_at"])
        except ValueError:
            return "AUTHORITY_EXPIRED"
        if expires <= self.now():
            return "AUTHORITY_EXPIRED"
        if not at_submit and expires > self.now() + timedelta(days=self.settings.authority_max_days):
            return "AUTHORITY_TOO_LONG"
        if a["authorized_currency"] not in P.CURRENCIES:
            return "CURRENCY_UNSUPPORTED"
        quotes = a["exact_quote_refs"]
        if len(set(quotes)) != len(quotes):
            return "QUOTES_DUPLICATED"
        qs = []
        for qid in quotes:
            q = self.prices.get(qid)
            if q is None or q["campaign_id"] != c["campaign_id"]:
                return "QUOTE_NOT_FOUND"
            if q["price_type"] != "VENDOR_QUOTE":
                return "PRICE_TYPE_MISMATCH"
            qs.append(q)
        if a["vendor"] not in self.vendors:
            return "VENDOR_UNKNOWN"
        for q in qs:
            if q["vendor"] != a["vendor"]:
                return "VENDOR_MISMATCH"
            if q["currency"] != a["authorized_currency"]:
                return "CURRENCY_MISMATCH"
            if self._expired(q["valid_until"]):
                return "QUOTE_EXPIRED"
            if q.get("retired"):
                return "QUOTE_RETIRED"
        if self._unit_in_two_quotes(qs):
            return "UNIT_IN_MULTIPLE_QUOTES"
        quoted_units = {i for q in qs for i in q["inventory_ids"]}
        if set(a["inventory_ids"]) != quoted_units or len(set(a["inventory_ids"])) != len(a["inventory_ids"]):
            return "INVENTORY_MISMATCH"
        for uid in a["inventory_ids"]:
            u = self.inventory.get(uid)
            if u is None or u["campaign_id"] != c["campaign_id"]:
                return "INVENTORY_MISMATCH"
            if u["vendor"] != a["vendor"]:
                return "VENDOR_MISMATCH"
            if u["state"] != "HOLD_CONFIRMED" or u.get("hold_confirmed_by") != P.SIGNER:
                return "INVENTORY_NOT_HELD"                          # only Andre's confirmation counts as a hold
            if self._expired(u["hold_expires_at"]):
                return "HOLD_EXPIRED"
            if u["flight"] is None:
                return "FLIGHT_REQUIRED"
            # AEGIS R3-H1: what is bought is what was quoted and held, for the flight it was quoted and held for
            if any(uid in q["inventory_ids"] and q.get("flights", {}).get(uid) != u["flight"] for q in qs):
                return "QUOTE_FLIGHT_MISMATCH"
            if u.get("hold_flight") != u["flight"]:
                return "HOLD_FLIGHT_MISMATCH"
            if self._flight_started(u["flight"]):
                return "FLIGHT_IN_PAST"
            if self._face_on_open_order(u, a.get("authorization_id")):
                return "FACE_ALREADY_ORDERED"
            if not self.flag_met(u["market"], "EXECUTION_AUTHORIZED"):
                return "MARKET_EXECUTION_NOT_AUTHORIZED"         # OOH-2: buying only where Andre authorized execution
        amount = Decimal(a["authorized_amount"])
        if amount != sum((Decimal(q["amount"]) for q in qs), Decimal(0)):
            return "QUOTE_ORDER_MISMATCH"
        limit = self.prices.get(c["buy_limit_id"] or "")
        if limit is None:
            return "BUY_LIMIT_MISSING"
        if amount > Decimal(limit["amount"]):
            return "AMOUNT_EXCEEDS_APPROVAL"
        if amount > Decimal(self.settings.max_purchase_amount):
            return "SPEND_LIMIT_EXCEEDED"
        ap = c["client_approval"]
        if ap is None or a["client_approval_ref"] != ap["approval_id"] or not set(quotes) <= set(ap["quote_refs"]):
            return "CLIENT_APPROVAL_MISMATCH"
        if a["commercial_terms_ref"] != cm["terms_id"] or ap["terms_id"] != cm["terms_id"] \
                or a["cancellation_terms_ref"] != cm["cancellation_terms_ref"]:
            return "TERMS_CHANGED"
        x = self.exposure(c)
        if x["funding_state"] != "CLEARED_FULL":
            return "FUNDS_NOT_VERIFIED"
        others = sum((Decimal(o["authorized_amount"]) for o in (self.authorizations[i] for i in c["authorization_ids"])
                      if o["submission_state"] in P.SUBMISSION_OPEN and o["authorization_id"] != a.get(
                          "authorization_id")), Decimal(0))
        if others + amount > Decimal(x["client_funds_received"]):
            return "FUNDS_INSUFFICIENT"
        return None

    def _face_on_open_order(self, unit: dict, own: Optional[str]) -> bool:
        """AEGIS R2-M3: the same physical face (registered vendor id, unit ref) on any open order (SUBMISSION_UNKNOWN or
        ACCEPTED) of ANY tenant — ZBM is the one buyer — for a flight that overlaps. The caller answers one code that
        names no other tenant, campaign or order."""
        for o in self.authorizations.values():
            if o["submission_state"] not in P.SUBMISSION_OPEN or o["authorization_id"] == own:
                continue
            for uid in o["inventory_ids"]:
                v = self.inventory[uid]
                if uid != unit["unit_id"] and v["vendor"] == unit["vendor"] and v["unit_key"] == unit["unit_key"] \
                        and self._overlap(o["unit_flights"].get(uid), unit["flight"]):
                    return True
        return False

    def authorize(self, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("authorize", cid, body)
            prev = self._idem("andre", rk, body)
            if prev:
                return self.authorization_view(prev[1])
            self._expect(c, body)
            a = {k: body[k] for k in ("inventory_ids", "vendor", "exact_quote_refs", "authorized_amount",
                                      "authorized_currency", "client_approval_ref", "commercial_terms_ref",
                                      "cancellation_terms_ref", "signer", "authority_scope", "expires_at")}
            code = self._gate_problem(c, a, at_submit=False)
            if code:
                self._refuse(code, c, rk, Forbidden if code.startswith("KILLED_") else Conflict)
            aid = derived_id("aut", cid, rk)
            data = {"authorization_id": aid, "campaign_id": cid, "tenant_id": tid, **a,
                    "exact_quote_refs": sorted(a["exact_quote_refs"]), "inventory_ids": sorted(a["inventory_ids"]),
                    "funding_state": "CLEARED_FULL", "authorized_at_version": c["version"]}
            self._commit("purchase_authorized", self._req(data, "andre", rk, body, aid), "andre",
                         evidence=self._ev("purchase_authorized_by_andre", cid,
                                           {k: v for k, v in data.items() if k != "tenant_id"}, "andre", rk))
            return self.authorization_view(aid)

    def _a_purchase_authorized(self, d, at):
        c = self.campaigns[d["campaign_id"]]
        for old in c["authorization_ids"]:            # a new authorization supersedes any unsubmitted one
            o = self.authorizations[old]
            if o["status"] == "AUTHORIZED" and o["submission_state"] == "NOT_SUBMITTED":
                o["status"], o["void_reason"] = "VOID", "SUPERSEDED"
        a = {k: v for k, v in d.items() if k not in ("actor", "request_id", "request_sha", "_obj", "evidence")}
        self._move(c, "PURCHASE_AUTHORIZED", "PURCHASE_AUTHORIZED_BY_ANDRE", d["actor"], at)
        a.update(status="AUTHORIZED", void_reason=None, submission_state="NOT_SUBMITTED", channel=None,
                 vendor_ref=None, outcome_evidence_class=None, authorized_at=at, campaign_version=c["version"],
                 inventory_versions={u: self.inventory[u]["version"] for u in d["inventory_ids"]},
                 unit_flights={u: self.inventory[u]["flight"] for u in d["inventory_ids"]})
        a["idempotency_key"] = order_key(d["tenant_id"], d["campaign_id"], d["vendor"], d["exact_quote_refs"],
                                         d["authorized_amount"], d["authorized_currency"], c["version"])
        self.authorizations[d["authorization_id"]] = a
        c["authorization_ids"].append(d["authorization_id"])

    # ================================================================================================ submission (s. 51)

    def submit(self, tid: str, cid: str, aid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            a = self.authorizations.get(aid)
            if a is None or a["campaign_id"] != cid:
                raise NotFound(R("AUTHORIZATION_NOT_FOUND"))
            rk = self.rk("submit", aid, body)
            if self._idem("andre", rk, body):              # the same request again: the same answer, no new order
                return self.authorization_view(aid)
            if a["submission_state"] in P.SUBMISSION_OPEN:
                self._refuse("DUPLICATE_ORDER_BLOCKED", c, rk, submission_state="DUPLICATE_BLOCKED")
            if a["status"] != "AUTHORIZED":
                self._refuse("AUTHORIZATION_VOID", c, rk)
            if body["vendor"] != a["vendor"]:
                self._refuse("VENDOR_MISMATCH", c, rk)
            if body["amount"] != a["authorized_amount"]:
                self._refuse("AMOUNT_MISMATCH", c, rk)
            if body["currency"] != a["authorized_currency"]:
                self._refuse("CURRENCY_MISMATCH", c, rk)
            if sorted(body["quote_refs"]) != a["exact_quote_refs"]:
                self._refuse("QUOTE_ORDER_MISMATCH", c, rk)
            if body["campaign_version"] != a["campaign_version"]:
                self._refuse("STALE_VERSION", c, rk)
            key = order_key(tid, cid, body["vendor"], body["quote_refs"], body["amount"], body["currency"],
                            body["campaign_version"])
            if key != a["idempotency_key"]:
                self._refuse("STALE_VERSION", c, rk)
            prior = self.orders.get(key)
            if prior is not None and self.authorizations[prior]["submission_state"] in P.SUBMISSION_OPEN:
                self._refuse("DUPLICATE_ORDER_BLOCKED", c, rk, submission_state="DUPLICATE_BLOCKED")
            open_quotes = {q for o in self.authorizations.values() if o["submission_state"] in P.SUBMISSION_OPEN
                           for q in o["exact_quote_refs"]}
            if open_quotes & set(a["exact_quote_refs"]):
                self._refuse("DUPLICATE_ORDER_BLOCKED", c, rk, submission_state="DUPLICATE_BLOCKED")
            if any(self.inventory[u]["version"] != v for u, v in a["inventory_versions"].items()) \
                    or any(self.inventory[u]["flight"] != f for u, f in a["unit_flights"].items()):
                self._refuse("INVENTORY_CHANGED", c, rk)
            if c["version"] != a["campaign_version"]:
                self._refuse("STALE_VERSION", c, rk)
            code = self._gate_problem(c, a, at_submit=True)
            if code:
                self._refuse(code, c, rk, Forbidden if code.startswith("KILLED_") else Conflict)
            data = {"authorization_id": aid, "campaign_id": cid, "idempotency_key": key, "channel": body["channel"],
                    "amount": a["authorized_amount"], "vendor": a["vendor"]}
            self._commit("order_submitted", self._req(data, "andre", rk, body, aid), "andre",
                         evidence=self._ev("purchase_order_submitted_by_andre", cid, data, "andre", rk))
            return self.authorization_view(aid)

    def _a_order_submitted(self, d, at):
        a = self.authorizations[d["authorization_id"]]
        c = self.campaigns[d["campaign_id"]]
        a.update(submission_state="SUBMISSION_UNKNOWN", channel=d["channel"], submitted_at=at)
        self.orders[d["idempotency_key"]] = d["authorization_id"]
        for uid in a["inventory_ids"]:
            u = self.inventory[uid]
            u["pre_booking_state"] = u["state"]
            self._unit_move(u, "BOOKING_PENDING", d["actor"], at)
        self._move(c, "ORDER_SUBMITTED", "ORDER_SUBMITTED_BY_ANDRE", d["actor"], at)

    def record_outcome(self, tid: str, cid: str, aid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            a = self.authorizations.get(aid)
            if a is None or a["campaign_id"] != cid:
                raise NotFound(R("AUTHORIZATION_NOT_FOUND"))
            rk = self.rk("outcome", aid, body)
            if self._idem("andre", rk, body):
                return self.authorization_view(aid)
            self.check_kill(tenant=tid)
            if a["submission_state"] != "SUBMISSION_UNKNOWN":
                self._refuse("SUBMISSION_NOT_PENDING", c, rk)
            if c["state"] not in ("ORDER_SUBMITTED", "DISPUTED"):
                self._refuse("STATE_NOT_ELIGIBLE", c, rk)
            data = {"authorization_id": aid, "campaign_id": cid, "outcome": body["outcome"],
                    "vendor_ref": body["vendor_ref"], "evidence_class": P.EVIDENCE_ALLOWED["order_outcome"][0]}
            self._commit("order_outcome", self._req(data, "andre", rk, body, aid), "andre",
                         evidence=self._ev("purchase_order_outcome_recorded", cid, data, "andre", rk))
            return self.authorization_view(aid)

    def _a_order_outcome(self, d, at):
        a = self.authorizations[d["authorization_id"]]
        c = self.campaigns[d["campaign_id"]]
        accepted = d["outcome"] == "ACCEPTED"
        a.update(submission_state=d["outcome"], vendor_ref=d["vendor_ref"], outcome_evidence_class=d["evidence_class"],
                 outcome_at=at)
        for uid in a["inventory_ids"]:
            u = self.inventory[uid]
            # AEGIS M4: a rejected order never leaves a confirmed hold behind (the vendor's reference was for the order
            # that failed): the face goes back to QUOTED and must be held, and confirmed, again
            self._unit_move(u, "BOOKING_CONFIRMED" if accepted else "QUOTED", d["actor"], at)
            u["pre_booking_state"] = None
            if not accepted:
                self._clear_hold(u)
        if not accepted:
            a["status"], a["void_reason"] = "VOID", "VENDOR_REJECTED"
        target = "BOOKED" if accepted else "FUNDS_VERIFIED"
        if c["state"] == "DISPUTED":
            # AEGIS M2: a dispute is Andre's to close. The answer is recorded; the campaign stays DISPUTED and will
            # resume where the answer leads, only when Andre resumes it.
            c["resume_to"] = target
            c["version"] += 1
            c["history"].append({"from": "DISPUTED", "to": "DISPUTED", "reason": "VENDOR_ACCEPTED_DURING_DISPUTE"
                                 if accepted else "VENDOR_REJECTED_DURING_DISPUTE", "actor": d["actor"], "at": at,
                                 "resume_to": target})                 # AEGIS R2-L4
        else:
            self._move(c, target, "VENDOR_ACCEPTED" if accepted else "VENDOR_REJECTED", d["actor"], at)

    # ================================================================================================ views

    def hold_state(self, c: dict) -> Optional[str]:
        """The section 4 campaign-level HOLD_REQUESTED / HOLD_CONFIRMED, derived from the units (policy.DERIVED_STATES):
        never stored, never read by the purchase gate (which checks every unit itself)."""
        live = [self.inventory[u] for u in c["inventory_ids"] if self.inventory[u]["state"] != "WITHDRAWN"]
        if live and all(u["state"] == "HOLD_CONFIRMED" and u.get("hold_confirmed_by") == P.SIGNER
                        and not self._expired(u["hold_expires_at"]) for u in live):
            return "HOLD_CONFIRMED"
        if any(u["state"] in ("HOLD_REQUESTED", "HOLD_CONFIRMED") for u in live):
            return "HOLD_REQUESTED"
        return None

    def campaign_view(self, tid: str, cid: str, hub: bool = False) -> dict:
        with self.lock:
            c = self._campaign(tid, cid)
            x = self.exposure(c)
            if hub:                                       # a client's view: its campaign, never vendor cost or terms
                ap = c["client_approval"]
                return {"campaign_id": cid, "tenant_id": tid, "name": c["name"], "state": c["state"],
                        "approved_price": ap["amount"] if ap else None, "currency": "USD",
                        "funding_state": x["funding_state"], "client_funds_received": x["client_funds_received"]}
            return {**{k: v for k, v in c.items() if k not in ("history", "funds")},
                    "history": [dict(h) for h in c["history"]], "funds": [dict(f) for f in c["funds"]],
                    "exposure": x, "hold_state": self.hold_state(c)}

    def unit_view(self, uid: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.inventory[uid]))       # a deep copy (AEGIS S2-L6)

    def price_view(self, pid: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.prices[pid]))

    def authorization_view(self, aid: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.authorizations[aid]))

    def authorization_for(self, tid: str, cid: str, aid: str) -> dict:
        with self.lock:
            self._campaign(tid, cid)
            a = self.authorizations.get(aid)
            if a is None or a["campaign_id"] != cid:
                raise NotFound(R("AUTHORIZATION_NOT_FOUND"))
            return self.authorization_view(aid)

    def unit_for(self, tid: str, cid: str, uid: str) -> dict:
        with self.lock:
            self._campaign(tid, cid)
            u = self.inventory.get(uid)
            if u is None or u["campaign_id"] != cid:
                raise NotFound(R("INVENTORY_NOT_FOUND"))
            return self.unit_view(uid)

    def snapshot(self) -> dict:
        """Everything the log rebuilds (the restart / replay tests compare two of these)."""
        with self.lock:
            return json.loads(json.dumps({"tenants": self.tenants, "switches": self.switches,
                                          "campaigns": self.campaigns, "inventory": self.inventory,
                                          "prices": self.prices, "authorizations": self.authorizations,
                                          "orders": self.orders, "funds_evidence": self.funds_evidence,
                                          "vendors": self.vendors, "pa_determination": self.pa_determination,
                                          "markets": self.markets, "sources": self.sources, "faces": self.faces},
                                         sort_keys=True))
