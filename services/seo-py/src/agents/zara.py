"""
Zara — the conversation fireteam's lead (ADR 0049). ONE job: cross-check Theo's chain output against Naomi's volume
output AS IT ARRIVES, catch conflicts, and resolve or flag them before either output moves downstream. Zara does NOT
mine conversations, estimate volume, ask an engine, or change a member's number: she pairs, checks, flags, downgrades
and holds.

Real time. Theo and Naomi run in parallel (svc_conversations.py); each emits one topic at a time into Zara's inbox
(``receive``). The moment both halves of a topic are in, Zara checks that topic (``checked_on_arrival``); a member
that ends (done, failed, restricted) closes its side (``member_closed``), and every topic still waiting for it is
checked then (``checked_at_close``). Only Zara's output (``CONTRACT``) goes downstream; the members' raw outputs stay
in the run record for drill-down.

Per-topic checks (a member payload first passes its contract's shape check; a payload that fails, names another
tenant or run, names a topic nobody planned, or contradicts itself is HELD, never repaired):
  STRONG_CHAIN_THIN_VOLUME     Theo: a STRONG chain on some engine; Naomi: THIN volume
  STRONG_CHAIN_UNKNOWN_VOLUME  Theo: a STRONG chain; Naomi: UNKNOWN (no owned signal)
  VOLUME_WITHOUT_CHAIN         Naomi: a measured proxy; Theo: no complete chain on any engine
  UNSTABLE_CHAIN               Theo: the complete chains of one engine recommend different entities
  ENGINES_DISAGREE             Theo: one engine's chains recommend the client, another's a competitor
  HANDOFF_NOT_VERIFIED         Naomi: the volume rests on a hand-off not verified against Priya's record (BLOCKING:
                               the topic is never CLEARED, never TEST, and volume is never a priority basis)
Verdicts (closed): CLEARED (both halves, no conflict), FLAGGED (both halves, conflicts listed; passes downstream with
its decision downgraded), PARTIAL (one member's half is missing because that member failed / was restricted / did not
run: the present half passes, the missing half is ``null`` with the reason — Zara never fills it in), NOTHING_OBSERVED
(both halves valid, but no complete chain and no volume signal: nothing to pass on, and not called "cleared"), HELD (a
half is invalid or contradictory, or both are missing: nothing passes downstream for that topic).
Downstream decision (closed vocabulary of the envelope): never ACT. TEST only for a CLEARED topic with a STRONG
chain and at least MODERATE volume; WATCH for other CLEARED topics; INSUFFICIENT_EVIDENCE for FLAGGED and PARTIAL.
``priority_basis`` says what a consumer may rank on: ``chain_and_volume`` (CLEARED with verified MODERATE+ volume),
``chain_only`` (volume thin or unknown: a consumer must not rank by volume), ``volume_only``, ``none``.
Priya's emerging-query loop receives, for each topic that came from her hand-off and was not HELD, the chain pattern,
the volume reliability and the conflicts (``to_priya``); nothing consumes it yet (contract only).
"""

from __future__ import annotations

import threading
from typing import Optional

from agents import naomi, theo
from envelope import envelope, finding

AGENT, TASK = "zara", "fireteam_crosscheck"
VERSION = "zara-rules/2026-11-08.1"
CONTRACT = "zara.conversation_volume/1"
VERDICTS = ("CLEARED", "FLAGGED", "PARTIAL", "NOTHING_OBSERVED", "HELD")
CONFLICTS = ("STRONG_CHAIN_THIN_VOLUME", "STRONG_CHAIN_UNKNOWN_VOLUME", "VOLUME_WITHOUT_CHAIN", "UNSTABLE_CHAIN",
             "ENGINES_DISAGREE", "HANDOFF_NOT_VERIFIED")
HOLDS = ("THEO_PAYLOAD_INVALID", "NAOMI_PAYLOAD_INVALID", "THEO_SELF_CONTRADICTION", "NAOMI_SELF_CONTRADICTION",
         "TOPIC_NOT_PLANNED", "BOTH_MEMBERS_MISSING", "DUPLICATE_PAYLOAD", "THEO_TENANT_OR_RUN_MISMATCH",
         "NAOMI_TENANT_OR_RUN_MISMATCH")
_CONTRACTS = {"theo": theo.CONTRACT, "naomi": naomi.CONTRACT}
MEMBERS = ("theo", "naomi")
MEMBER_STATES = ("DONE", "FAILED", "RESTRICTED", "KILLED", "TIMED_OUT", "NOT_RUN")
RULE = "Zara changes no member's number: she pairs, flags, downgrades and holds; a missing half is never filled in"
LIMITS = [
    "Zara checks consistency between two members' outputs; she does not re-measure either. A CLEARED topic is "
    "internally consistent, not proven.",
    "Volume is a directional proxy (Naomi) and chain outcomes are lexical (Theo); a conflict between them is a "
    "reason to hold back a priority, not proof that either is wrong.",
]

_CHAIN_KEYS = {"topic_key", "engine", "context_mode", "native_status", "injection_suspected",
               "divergent_under_history", "chains", "complete",
               "loop_stopped", "not_complete",
               "turns_planned", "plan_flags", "by_turn", "final_recommendation", "client_recommended",
               "client_outcome", "unstable", "pattern", "strength"}
_VOL_KEYS = {"topic_key", "proxy_for", "estimator", "status", "verified", "reliability", "confidence", "band", "unit",
             "window", "signals", "flags", "unknown_reason"}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _counts(box) -> Optional[int]:
    if not isinstance(box, dict) or not isinstance(box.get("client"), int) or not isinstance(box.get("competitor"),
                                                                                             dict):
        return None
    vals = list(box["competitor"].values())
    if not all(isinstance(v, int) and v >= 0 for v in vals) or box["client"] < 0:
        return None
    return box["client"] + sum(vals)


def check_chains(chains, topic_key: str) -> Optional[str]:
    """None when Theo's half for a topic is well-formed and consistent; else the hold reason. The pattern, outcome
    and strength are RE-DERIVED here from the counts with Theo's own rule and must agree (review round 1 MED 4)."""
    if not isinstance(chains, list) or not chains:
        return "THEO_PAYLOAD_INVALID"
    for c in chains:
        if not isinstance(c, dict) or set(c) != _CHAIN_KEYS or c["topic_key"] != topic_key:
            return "THEO_PAYLOAD_INVALID"
        if c["pattern"] not in theo.PATTERNS or c["strength"] not in theo.STRENGTHS \
                or c["context_mode"] not in theo.CONTEXT_MODES \
                or c["client_outcome"] not in theo.OUTCOMES + (None,):
            return "THEO_PAYLOAD_INVALID"
        if not all(isinstance(c[k], int) and c[k] >= 0 for k in ("chains", "complete", "loop_stopped")):
            return "THEO_PAYLOAD_INVALID"
        fr, bt = c["final_recommendation"], c["by_turn"]
        if not isinstance(fr, dict) or not isinstance(bt, list) or not bt or not isinstance(bt[0], dict):
            return "THEO_PAYLOAD_INVALID"
        inj = c["injection_suspected"]
        if not isinstance(inj, dict) or not isinstance(inj.get("chains"), int) or inj["chains"] < 0:
            return "THEO_PAYLOAD_INVALID"
        rec, named = _counts(fr.get("recommended")), _counts(fr.get("named_first_only"))
        none, unknown = fr.get("none"), fr.get("unknown")
        if rec is None or named is None or not isinstance(none, int) or not isinstance(unknown, int):
            return "THEO_PAYLOAD_INVALID"
        try:
            vis = bt[0]["client_mentioned"]["k"]
            low = bt[0]["client_mentioned_unconfirmed"]
        except (KeyError, TypeError):
            return "THEO_PAYLOAD_INVALID"
        nc = c["complete"]
        if nc > c["chains"] or rec + named + none != nc or unknown != c["chains"] - nc \
                or inj["chains"] > c["chains"] - nc:
            return "THEO_SELF_CONTRADICTION"
        outcome = theo.client_outcome(fr["recommended"]["client"], nc)
        if outcome != c["client_outcome"] or c["client_recommended"].get("k") != fr["recommended"]["client"]:
            return "THEO_SELF_CONTRADICTION"
        if theo.pattern_of(outcome, vis, vis + low, nc, c["context_mode"]) != c["pattern"]:
            return "THEO_SELF_CONTRADICTION"
        strength = "STRONG" if nc >= theo.MIN_USABLE and outcome in ("RECOMMENDED", "NOT_RECOMMENDED") else \
            "WEAK" if nc else "NONE"
        if strength != c["strength"]:
            return "THEO_SELF_CONTRADICTION"
    return None


def check_volume(v, topic_key: str) -> Optional[str]:
    """None when Naomi's half is well-formed and consistent; else the hold reason."""
    if not isinstance(v, dict) or set(v) != _VOL_KEYS or v["topic_key"] != topic_key:
        return "NAOMI_PAYLOAD_INVALID"
    if v["status"] not in naomi.STATUSES or v["reliability"] not in naomi.RELIABILITY \
            or v["confidence"] not in naomi.CONFIDENCE:
        return "NAOMI_PAYLOAD_INVALID"
    if v["status"] == "UNKNOWN":
        if v["band"] is not None or v["signals"] or v["reliability"] != "NONE" or v["confidence"] != "NONE" \
                or v["unknown_reason"] not in naomi.UNKNOWN_REASONS or v["verified"] is not None:
            return "NAOMI_SELF_CONTRADICTION"
        return None
    b = v["band"]
    if not isinstance(b, dict) or not _num(b.get("low")) or not _num(b.get("high")) or b["low"] < 0 \
            or b["low"] > b["high"] or not v["signals"] or v["reliability"] == "NONE" \
            or not isinstance(v["verified"], bool) or not isinstance(v["flags"], list):
        return "NAOMI_SELF_CONTRADICTION"
    k = v["signals"][0].get("observed_impressions")
    if not isinstance(k, int) or isinstance(k, bool) or not b["low"] <= k <= b["high"]:
        return "NAOMI_SELF_CONTRADICTION"
    # an unverified signal is THIN whatever its count, and says so (review round 1 HIGH 1c)
    want = naomi._reliability(k) if v["verified"] else "THIN"
    if v["reliability"] != want or ("HANDOFF_NOT_VERIFIED" in v["flags"]) == v["verified"]:
        return "NAOMI_SELF_CONTRADICTION"
    if v["confidence"] != ("LOW" if v["reliability"] == "THIN" else "MEDIUM"):
        return "NAOMI_SELF_CONTRADICTION"
    return None


def conflicts(chains: list, vol: dict) -> list:
    out = []
    strong = any(c["strength"] == "STRONG" for c in chains)
    if strong and vol["reliability"] == "THIN":
        out.append("STRONG_CHAIN_THIN_VOLUME")
    if strong and vol["status"] == "UNKNOWN":
        out.append("STRONG_CHAIN_UNKNOWN_VOLUME")
    if vol["status"] != "UNKNOWN" and not any(c["complete"] for c in chains):
        out.append("VOLUME_WITHOUT_CHAIN")
    if any(c["unstable"] for c in chains):
        out.append("UNSTABLE_CHAIN")
    pro = any(c["client_outcome"] == "RECOMMENDED" for c in chains)
    con = any(c["complete"] and sum(c["final_recommendation"]["recommended"]["competitor"].values()) * 2 >
              c["complete"] for c in chains)
    if pro and con:
        out.append("ENGINES_DISAGREE")
    if vol["status"] != "UNKNOWN" and vol["verified"] is not True:
        out.append("HANDOFF_NOT_VERIFIED")                 # blocking: never CLEARED, never TEST, never ranked
    return out


class Crosscheck:
    """Zara's inbox and checks. Thread-safe: the members call ``receive`` / ``member_closed`` from their own
    threads; the result is ordered by topic, so it does not depend on arrival order."""

    def __init__(self, tenant_id: str, run_id: str, topics: list):
        self.tenant_id, self.run_id = tenant_id, run_id
        self.topics = {t["topic_key"]: t for t in topics}
        self.order = [t["topic_key"] for t in topics]
        self.halves: dict = {m: {} for m in MEMBERS}
        self.closed: dict = {}
        self.items: dict = {}
        self.unplanned: list = []
        self.dupes: list = []
        self.bad: dict = {}
        self.late: list = []
        self.checks = {"received": {m: 0 for m in MEMBERS}, "checked_on_arrival": 0, "checked_at_close": 0}
        self._lock = threading.Lock()

    def receive(self, member: str, topic_key: str, payload) -> None:
        with self._lock:
            if member in self.closed:                     # a member past its wait (or closed): never used
                self.late.append({"member": member, "topic_key": str(topic_key)[:40]})
                return
            self.checks["received"][member] += 1
            if topic_key not in self.topics:
                self.unplanned.append({"member": member, "topic_key": str(topic_key)[:40]})
                return
            if topic_key in self.halves[member] or topic_key in self.items:
                self.dupes.append({"member": member, "topic_key": topic_key})
                if topic_key in self.items:
                    self.items[topic_key] = self._held(topic_key, "DUPLICATE_PAYLOAD")
                return
            self.halves[member][topic_key] = payload
            if all(topic_key in self.halves[m] for m in MEMBERS):
                self.items[topic_key] = self._check(topic_key)
                self.checks["checked_on_arrival"] += 1

    def member_contract(self, member: str, doc) -> None:
        """The member's whole output: it must name this tenant, this run and its own contract, or every half that
        member sent is held (a payload of another tenant or run never moves downstream)."""
        ok = isinstance(doc, dict) and doc.get("contract") == _CONTRACTS[member] and doc.get("producer") == member \
            and doc.get("tenant_id") == self.tenant_id and doc.get("run_id") == self.run_id
        if ok:
            return
        with self._lock:
            self.bad[member] = f"{member.upper()}_TENANT_OR_RUN_MISMATCH"
            for k in list(self.items):
                if k in self.halves[member]:
                    self.items[k] = self._held(k, self.bad[member])

    def member_closed(self, member: str, state: str, reason: Optional[str] = None) -> None:
        with self._lock:
            self.closed[member] = {"state": state, "reason": reason}
            for k in self.order:
                if k in self.items or not all(m in self.closed or k in self.halves[m] for m in MEMBERS):
                    continue
                self.items[k] = self._check(k)
                self.checks["checked_at_close"] += 1

    def _missing(self, m: str) -> str:
        c = self.closed.get(m) or {"state": "NOT_RUN", "reason": None}
        return f"{m.upper()}_{c['state']}" + (f":{c['reason']}" if c.get("reason") else "")

    def _held(self, k: str, why: str) -> dict:
        t = self.topics[k]
        return {"topic_key": k, "source": t["source"], "priya_key": t["priya_key"], "verdict": "HELD",
                "hold_reason": why, "conflicts": [], "chain": None, "volume": None,
                "downstream": {"passes": False, "decision": None, "priority_basis": "none"}}

    def _check(self, k: str) -> dict:
        ch, vo = self.halves["theo"].get(k), self.halves["naomi"].get(k)
        has_c, has_v = k in self.halves["theo"], k in self.halves["naomi"]
        if not has_c and not has_v:
            return self._held(k, "BOTH_MEMBERS_MISSING")
        for m, has in (("theo", has_c), ("naomi", has_v)):
            if has and m in self.bad:
                return self._held(k, self.bad[m])
        if has_c:
            bad = check_chains(ch, k)
            if bad:
                return self._held(k, bad)
        if has_v:
            bad = check_volume(vo, k)
            if bad:
                return self._held(k, bad)
        t = self.topics[k]
        item = {"topic_key": k, "source": t["source"], "priya_key": t["priya_key"], "hold_reason": None,
                "chain": None if not has_c else [_chain_view(c) for c in ch],
                "chain_missing": None if has_c else self._missing("theo"),
                "volume": None if not has_v else _volume_view(vo),
                "volume_missing": None if has_v else self._missing("naomi")}
        if not (has_c and has_v):
            # review round 2: an unverified volume half is named on a PARTIAL item too
            unv = has_v and vo["status"] != "UNKNOWN" and vo["verified"] is not True
            item.update(verdict="PARTIAL", conflicts=["HANDOFF_NOT_VERIFIED"] if unv else [],
                        downstream={"passes": True, "decision": "INSUFFICIENT_EVIDENCE",
                                    "priority_basis": "chain_only" if has_c else
                                    "volume_only" if vo["status"] != "UNKNOWN" and vo["verified"] is True else "none"})
            return item
        if not any(c["complete"] for c in ch) and vo["status"] == "UNKNOWN":
            item.update(verdict="NOTHING_OBSERVED", conflicts=[],
                        downstream={"passes": False, "decision": None, "priority_basis": "none"})
            return item
        cf = conflicts(ch, vo)
        strong = any(c["strength"] == "STRONG" for c in ch)
        vol_ok = vo["reliability"] in ("MODERATE", "STRONG") and vo["verified"] is True
        vol_usable = vo["status"] != "UNKNOWN" and vo["verified"] is True
        if cf:
            item.update(verdict="FLAGGED", conflicts=cf,
                        downstream={"passes": True, "decision": "INSUFFICIENT_EVIDENCE",
                                    "priority_basis": "chain_only" if any(c["complete"] for c in ch) and not vol_ok
                                    else "chain_and_volume" if vol_ok and any(c["complete"] for c in ch)
                                    else "volume_only" if vol_usable else "none"})
        else:
            item.update(verdict="CLEARED", conflicts=[],
                        downstream={"passes": True, "decision": "TEST" if strong and vol_ok else "WATCH",
                                    "priority_basis": "chain_and_volume" if vol_ok and any(c["complete"] for c in ch)
                                    else "chain_only" if any(c["complete"] for c in ch) else "none"})
        return item

    def result(self) -> dict:
        with self._lock:
            for k in self.order:                          # anything never checked (a member never closed)
                if k not in self.items:
                    self.items[k] = self._check(k)
                    self.checks["checked_at_close"] += 1
            items = [self.items[k] for k in self.order]
            members = {m: self.closed.get(m, {"state": "NOT_RUN", "reason": None}) for m in MEMBERS}
            verdicts = {v: sum(1 for i in items if i["verdict"] == v) for v in VERDICTS}
            if not items or verdicts["HELD"] + verdicts["NOTHING_OBSERVED"] == len(items):
                status = "HELD_ALL"
            elif all(members[m]["state"] == "DONE" for m in MEMBERS) and not verdicts["PARTIAL"] \
                    and not verdicts["HELD"] and not verdicts["NOTHING_OBSERVED"]:
                status = "OK"
            else:
                status = "PARTIAL"
            to_priya = [{"priya_key": i["priya_key"], "topic_key": i["topic_key"], "verdict": i["verdict"],
                         "patterns": sorted({c["engine"] + ":" + c["pattern"] for c in i["chain"] or []}),
                         "volume_reliability": (i["volume"] or {}).get("reliability"), "conflicts": i["conflicts"]}
                        for i in items if i["source"] == "priya" and i["verdict"] not in ("HELD", "NOTHING_OBSERVED")]
            return {"contract": CONTRACT, "producer": AGENT, "tenant_id": self.tenant_id, "run_id": self.run_id,
                    "rules": VERSION, "status": status, "members": members, "verdicts": verdicts,
                    "items": items, "to_priya": {"contract": "zara.priya_feedback/1", "consumer": "priya",
                                                 "state": "NOT_CONSUMED", "items": to_priya},
                    "unplanned_payloads": self.unplanned[:50], "duplicate_payloads": self.dupes[:50],
                    "late_payloads": self.late[:50],
                    "checks": {**self.checks, "received": dict(self.checks["received"])}, "rule": RULE}


def _chain_view(c: dict) -> dict:
    return {k: c[k] for k in ("engine", "context_mode", "native_status", "injection_suspected",
                              "divergent_under_history", "chains", "complete",
                              "pattern", "strength", "unstable",
                              "client_outcome", "client_recommended", "final_recommendation")}


def _volume_view(v: dict) -> dict:
    return {k: v[k] for k in ("status", "verified", "reliability", "confidence", "band", "unit", "window",
                              "unknown_reason", "flags")}


def envelope_for(result: dict) -> dict:
    method = (f"Theo's and Naomi's outputs paired by topic as they arrived; each half shape- and consistency-checked "
              f"against its contract ({theo.CONTRACT}, {naomi.CONTRACT}); conflicts {', '.join(CONFLICTS)} flagged; "
              f"invalid or contradictory halves held; a missing half never filled in. Rules {VERSION}.")
    outcome = {"OK": "OK", "PARTIAL": "PARTIAL", "HELD_ALL": "INSUFFICIENT_EVIDENCE"}[result["status"]]
    fl = []
    for i in result["items"]:
        if i["verdict"] == "FLAGGED":
            fl.append(finding("ZARA_CONFLICT_FLAGGED", "medium", "inferred", "INSUFFICIENT_EVIDENCE",
                              capability="P4", detail={"topic_key": i["topic_key"], "conflicts": i["conflicts"]}))
        elif i["verdict"] == "HELD":
            fl.append(finding("ZARA_OUTPUT_HELD", "medium", "inferred", "DO_NOT_PUBLISH", capability="P4",
                              detail={"topic_key": i["topic_key"], "hold_reason": i["hold_reason"]}))
    for m, st in result["members"].items():
        if st["state"] != "DONE":
            fl.append(finding("ZARA_MEMBER_MISSING", "low", "measured", "WATCH", capability="P4",
                              detail={"member": m, **st}))
    return envelope(AGENT, TASK, outcome, fl, methodology=method, limitations=LIMITS,
                    facts={"contract": result}, reason=None if outcome != "INSUFFICIENT_EVIDENCE" else "HELD_ALL")
