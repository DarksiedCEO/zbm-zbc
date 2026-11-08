"""
The conversation-mining + prompt-volume fireteam in the service (ADR 0049): Theo (conversation chains), Naomi (prompt
volume) and their lead Zara (the real-time cross-check), one run at a time per tenant.

  POST /seo/v1/tenants/{tid}/conversation-runs                       dashboard / seo_agent (a client tenant: Andre)
  GET  /seo/v1/tenants/{tid}/conversation-runs                       + hub (its own tenant only), compliance_38
  GET  /seo/v1/tenants/{tid}/conversation-runs/{rid}                 the three envelopes (drill-down)
  GET  /seo/v1/tenants/{tid}/conversation-runs/{rid}/crosscheck      Zara's zara.conversation_volume/1 (downstream)

Swarm semantics:
  - one topic list (agents/fireteam_topics.py) for both members, planned before anything runs;
  - Theo and Naomi run IN PARALLEL (two worker threads); each emits one topic at a time into Zara's inbox, and Zara
    checks a topic the moment both halves are in (agents/zara.py);
  - failure isolation: a member that raises is closed FAILED (its exception's type only) and the other member and
    Zara carry on: Zara reports PARTIAL and never fills in the missing half. A restricted member is RESTRICTED (it
    does not run). A restricted or retired ZARA refuses the whole run (403 AGENT_RESTRICTED): nothing may move
    downstream unchecked;
  - a kill switch (global, write, the tenant, ``capability:ai_probe``, ``capability:audit``) engaged mid-run stops
    the run at its next step: recorded ``interrupted`` with the switch's code, every result discarded (as Callum's
    citation runs). ``provider:<engine>`` stops that engine's chains (the engine is KILLED).
Rules otherwise Callum's citation runs' (svc_citations.py): a registered domain and the tenant's prompt set; a client
tenant needs Andre's approval, a bound Finance client and Compliance (38)'s current ALLOW (ADR 0024); record-first
(``conversation_run_requested`` before any engine is asked; each metered chain inside a recorded reservation under
``conversation_run:<id>``; ``conversation_run_recorded`` with the result's SHA-256); one running run per tenant; a
dead process's run is closed by the ``interrupted-audits`` job; tenant isolation (another tenant's run is 404).
Priya's hand-off is validated (agents/priya_handoff.py) and refused whole when invalid or of another tenant
(400 HANDOFF_INVALID / 403 HANDOFF_TENANT_MISMATCH). Its query text never reaches the log or the ledger: the
request record holds its SHA-256 and item count; topics from it are reported by key only.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Optional

from agents import fireteam_topics, naomi, priya_handoff, theo, zara
from envelope import sha
from errors import Conflict, Forbidden, Invalid, NotFound
from ledger import derived_id
from primitives import Killed
from reasons import R

log = logging.getLogger("seo.conversations")
FIRETEAM = ("theo", "naomi", "zara")


class ConversationsMixin:
    def request_conversation_run(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            actor = "andre" if andre else actor
            rk = self.rk("conversation_run", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.conversation_run_view(tid, prev[1])
            client = self._conversation_rules(t, tid, body, andre)
        cmp_ = self._verify_compliance(client) if client is not None else None     # ADR 0024, outside the lock
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            prev = self._idem(actor, rk, body)
            if prev:
                return self.conversation_run_view(tid, prev[1])
            if self._conversation_rules(t, tid, body, andre) != client:
                raise Conflict(R("TENANT_FINANCE_CLIENT_CHANGED"))
            if self.running_conversation_runs and any(
                    r["tenant_id"] == tid and r["conversation_run_id"] in self.running_conversation_runs
                    for r in self.conversation_runs.values()):
                raise Conflict(R("CONVERSATION_RUN_RUNNING"))
            p = self.prompt_sets[body["prompt_set_id"]]
            ps = {"prompt_set_id": p["prompt_set_id"], **p["versions"][str(p["current"])]}
            wanted = body.get("engines")
            ps["engines"] = sorted(set(ps["engines"]) & set(wanted)) if wanted else list(ps["engines"])
            if not ps["engines"]:
                raise Invalid(R("INVALID"), field="engines")
            handoff = body.get("priya_handoff")
            items, past_cap = priya_handoff.items(handoff)
            vid = derived_id("cnv", tid, actor, rk)
            ref = {"prompt_set_id": ps["prompt_set_id"], "version": ps["version"], "sha256": ps["sha256"]}
            verification = self._handoff_verification(tid, body)
            hand = None if handoff is None else {"sha256": priya_handoff.canonical_sha(handoff), "items": len(items),
                                                 "items_past_cap": past_cap, "priya_run_id_sha256":
                                                 sha(handoff.get("run_id")),
                                                 "verified_against_priya_record": verification["state"] == "VERIFIED",
                                                 "verification": verification}
            data = {"conversation_run_id": vid, "tenant_id": tid, "tenant_kind": t["kind"], "domain": body["domain"],
                    "prompt_set": ref, "engines": ps["engines"], "andre_approved": bool(andre),
                    "competitor_terms_sha256": sha(sorted(body.get("competitor_terms") or [])),
                    "priya_handoff": hand,
                    "compliance": None if cmp_ is None else {"ruling_id": cmp_.get("ruling_id"),
                                                             "verdict": cmp_.get("verdict")}}
            ev = [("conversation_run_requested", f"conversation_run:{vid}",
                   {"conversation_run_id": vid, "tenant_id": tid, "kind": t["kind"],
                    "domain_sha256": sha(body["domain"]), "prompt_set_id": ref["prompt_set_id"],
                    "prompt_set_version": ref["version"], "prompt_set_sha256": ref["sha256"],
                    "engines": ps["engines"], "andre_approved": bool(andre),
                    "priya_handoff_sha256": None if hand is None else hand["sha256"],
                    "priya_handoff_items": None if hand is None else hand["items"],
                    "priya_handoff_verification": verification["state"]}, (actor, rk))]
            self._commit("conversation_run_requested", self._req(data, actor, rk, body, vid), actor, evidence=ev)
            self.running_conversation_runs.add(vid)
        return self._execute_conversation_run(vid, tid, body["domain"], ps, items,
                                              list(body.get("competitor_terms") or []), verification)

    def _conversation_rules(self, t: dict, tid: str, body: dict, andre: bool) -> Optional[str]:
        """Under the lock: the authorisation rules. Returns the Finance client a client tenant is bound to."""
        self.check_kill(tenant=tid, capability="ai_probe", write=True)
        self.check_kill(capability="audit")
        if self.agent_blocked("zara"):                    # the lead is required: nothing may go downstream unchecked
            raise Forbidden(R("AGENT_RESTRICTED"))
        if body["domain"] not in t["domains"]:
            raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
        p = self.prompt_sets.get(body["prompt_set_id"])
        if p is None or p["tenant_id"] != tid:
            raise NotFound(R("PROMPT_SET_NOT_FOUND"))
        if body.get("priya_handoff") is not None:
            problems = priya_handoff.validate(body["priya_handoff"], tid)
            if any(x.endswith("HANDOFF_TENANT_MISMATCH") for x in problems):
                raise Forbidden(R("HANDOFF_TENANT_MISMATCH"))
            if problems:
                raise Invalid(R("HANDOFF_INVALID"), field="priya_handoff")
            if self._handoff_verification(tid, body)["state"] == "FAILED":
                raise Invalid(R("HANDOFF_VERIFICATION_FAILED"), field="priya_handoff_sha256")
        elif body.get("priya_run_id") is not None or body.get("priya_handoff_sha256") is not None:
            raise Invalid(R("HANDOFF_INVALID"), field="priya_handoff")
        if t["kind"] != "client":
            return None
        if not andre:
            raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
        client = t.get("finance_client_id")
        if not client:
            raise Conflict(R("TENANT_FINANCE_CLIENT_UNBOUND"))
        return client

    def _handoff_verification(self, tid: str, body: dict) -> dict:
        """Under the lock. The hand-off against Priya's stored run record (agents/priya_handoff.py RECORD_CONTRACT):
        Priya's service keeps ``keyword_runs`` (PR #50); on this base it does not exist, so nothing is VERIFIED."""
        doc = body.get("priya_handoff")
        if doc is None:
            return {"state": "NOT_VERIFIED", "reason": "NO_HANDOFF"}
        runs = getattr(self, "keyword_runs", None)
        rid_ = body.get("priya_run_id")
        record = runs.get(rid_) if isinstance(runs, dict) and isinstance(rid_, str) else None
        v = priya_handoff.verify(doc, tid, rid_, body.get("priya_handoff_sha256"), record)
        if v["state"] == "NOT_VERIFIED" and runs is None and v["reason"] == "PRIYA_RECORD_NOT_FOUND":
            v = {"state": "NOT_VERIFIED", "reason": "PRIYA_NOT_INSTALLED"}
        return v

    # ------------------------------------------------------------------ the swarm

    def _gsc_context(self, tid: str) -> dict:
        with self.lock:
            done = sorted((a for a in self.audits.values() if a["tenant_id"] == tid and a["status"] == "completed"),
                          key=lambda a: (a.get("completed_at") or "", a["audit_id"]))
            a = done[-1] if done else None
            return naomi.context_from_audit(None if a is None else a.get("report"),
                                            None if a is None else a["audit_id"])

    def _execute_conversation_run(self, vid: str, tid: str, domain: str, ps: dict, items: list,
                                  competitor_terms: list, verification: dict) -> dict:
        from svc_audits import _where
        try:
            guard = self.guard(tid)
            topics = fireteam_topics.plan(ps, items)
            plans = {t["topic_key"]: theo.plan_turns(t, items) for t in topics} if topics else {}
            box = zara.Crosscheck(tid, vid, topics)
            try:
                guard(agent="zara")
                out = self._swarm(vid, tid, domain, ps, items, competitor_terms, guard, topics, plans, box,
                                  verification)
            except Killed as k:
                return self._interrupt_conversation_run(tid, vid, k.code)
            except Exception as exc:  # noqa: BLE001 - a run that crashes is closed now, never left running
                where = _where(exc)
                log.error("conversation run failed: %s at %s", type(exc).__name__, where)
                return self._interrupt_conversation_run(tid, vid, R("RUN_FAILED"),
                                                        {"agent": "zara", "where": where,
                                                         "error_type": type(exc).__name__[:60]})
            with self.lock:
                self._gate()
                stop = self.kill_code(tenant=tid, capability="ai_probe", write=True) or self.kill_code(
                    capability="audit")
                if stop is not None:
                    return self._interrupt_conversation_run(tid, vid, stop)
                rsha = sha(out)
                zc = out["zara"]["facts"]["contract"]
                self._commit("conversation_run_recorded",
                             {"conversation_run_id": vid, "result": out, "result_sha256": rsha}, "seo",
                             evidence=("conversation_run_recorded", f"conversation_run:{vid}",
                                       {"conversation_run_id": vid, "result_sha256": rsha,
                                        "outcomes": {m: out[m]["outcome"] for m in FIRETEAM},
                                        "topics": len(topics), "verdicts": zc["verdicts"], "status": zc["status"]},
                                       ("result", vid)))
                return self.conversation_run_view(tid, vid)
        finally:
            with self.lock:
                self.running_conversation_runs.discard(vid)

    def _swarm(self, vid, tid, domain, ps, items, competitor_terms, guard, topics, plans, box,
               verification: dict) -> dict:
        ctx = {"domain": domain, "terms": ps["brand_terms"], "rules": ps.get("brand_term_rules"),
               "competitor_terms": competitor_terms, "competitor_domains": list(ps.get("competitor_domains") or [])}
        engines = self.ports.engines
        n = self.settings.probe_samples
        budget = self.engine_budget(tid, vid, ps, subject="conversation_run")
        cap = theo.CallCap(self.settings.theo_run_max_calls)

        cancel = {m: threading.Event() for m in ("theo", "naomi")}

        def member_guard(m):
            def g(**kw):
                if cancel[m].is_set():                     # LOW 7: past its wait, a member stops at its next step
                    raise Killed("MEMBER_TIMED_OUT")
                guard(**kw)
            return g

        def run_theo():
            g = member_guard("theo")
            g(agent="theo")
            return theo.run_chains(topics, plans, engines, ps["engines"], n, g, budget, cap, ctx, self.clock,
                                   emit=lambda k, chains: box.receive("theo", k, chains))

        def run_naomi():
            member_guard("naomi")(agent="naomi")
            return naomi.run(topics, items, tid, vid, self._gsc_context(tid),
                             emit=lambda k, est: box.receive("naomi", k, est), verification=verification)

        results: dict = {}
        killed: Optional[str] = None
        # one bounded overall wait for the members (SEO_FIRETEAM_MEMBER_TIMEOUT_SECONDS): a member still running at
        # the end is closed TIMED_OUT (Zara reports PARTIAL), told to stop, and never waited for. It is cancelled at
        # its NEXT guard check: at most the one request already in flight completes (bounded by that engine's own
        # hard deadline and inside its recorded reservation); no further request starts, and anything it emits after
        # the close is counted as late and never used
        end = time.monotonic() + self.settings.fireteam_member_timeout_s
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="seo-fireteam")
        try:
            futs = {}
            for m, fn in (("theo", run_theo), ("naomi", run_naomi)):
                if self.agent_blocked(m):
                    results[m] = ("RESTRICTED", None, "AGENT_RESTRICTED")
                    box.member_closed(m, "RESTRICTED", "AGENT_RESTRICTED")
                    continue
                futs[m] = pool.submit(fn)
            for m, f in futs.items():
                try:
                    res = f.result(timeout=max(0.0, end - time.monotonic()))
                except FutureTimeout:
                    cancel[m].set()
                    log.error("fireteam member %s did not finish within its wait", m)
                    results[m] = ("TIMED_OUT", None, "MEMBER_TIMED_OUT")
                    box.member_closed(m, "TIMED_OUT", "MEMBER_TIMED_OUT")
                    continue
                except Killed as k:
                    if k.code == "AGENT_RESTRICTED":       # restricted between the check and its first step
                        results[m] = ("RESTRICTED", None, k.code)
                        box.member_closed(m, "RESTRICTED", k.code)
                        continue
                    killed = killed or k.code
                    results[m] = ("KILLED", None, k.code)
                    box.member_closed(m, "KILLED", k.code)
                    continue
                except Exception as exc:  # noqa: BLE001 - member failure isolation: the other member carries on
                    log.error("fireteam member %s failed: %s", m, type(exc).__name__)
                    results[m] = ("FAILED", None, "MEMBER_FAILED:" + type(exc).__name__[:40])
                    box.member_closed(m, "FAILED", "MEMBER_FAILED")
                    continue
                results[m] = ("DONE", res, None)
                doc = theo.output(tid, vid, topics, plans, res, n) if m == "theo" else res
                box.member_contract(m, doc)
                box.member_closed(m, "DONE")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if killed is not None:
            raise Killed(killed)
        guard(agent="zara")
        zres = box.result()
        return {"theo": self._theo_env(results["theo"], topics, plans, tid, vid, n),
                "naomi": self._naomi_env(results["naomi"]), "zara": zara.envelope_for(zres),
                "topics": [fireteam_topics.public(t) for t in topics], "samples_per_chain": n,
                "theo_run_max_calls": self.settings.theo_run_max_calls}

    @staticmethod
    def _theo_env(r, topics, plans, tid, vid, n) -> dict:
        state, res, why = r
        if state != "DONE":
            outcome = {"RESTRICTED": "RESTRICTED", "KILLED": "KILLED"}.get(state, "FAILED")   # TIMED_OUT: FAILED
            return theo.envelope_for(outcome, None, None, topics, plans, reason=why)
        outcome, reason = theo.outcome_of(res) if topics else ("INSUFFICIENT_EVIDENCE", "NO_TOPICS")
        return theo.envelope_for(outcome, res, theo.output(tid, vid, topics, plans, res, n), topics, plans,
                                 reason=reason)

    @staticmethod
    def _naomi_env(r) -> dict:
        state, res, why = r
        if state != "DONE":
            outcome = {"RESTRICTED": "RESTRICTED", "KILLED": "KILLED"}.get(state, "FAILED")
            return naomi.envelope_for(outcome, None, reason=why)
        known = [t for t in res["topics"] if t["status"] != "UNKNOWN"]
        outcome = "OK" if known and len(known) == len(res["topics"]) else "PARTIAL" if known else \
            "INSUFFICIENT_EVIDENCE"
        return naomi.envelope_for(outcome, res, reason=None if known else "NO_OWNED_SIGNAL")

    def _interrupt_conversation_run(self, tid: str, vid: str, reason: str, failure: Optional[dict] = None) -> dict:
        with self.lock:
            self._gate()
            data = {"conversation_run_id": vid, "reason": reason}
            if failure:
                data["failure"] = failure
            self._commit("conversation_run_interrupted", data, "seo",
                         evidence=("conversation_run_interrupted", f"conversation_run:{vid}",
                                   {"conversation_run_id": vid, "reason": reason, **(failure or {})},
                                   ("interrupted", vid)))
            return self.conversation_run_view(tid, vid)

    def mark_interrupted_conversation_runs(self) -> int:
        n = 0
        with self.lock:
            stale = [r["conversation_run_id"] for r in self.conversation_runs.values()
                     if r["status"] == "running" and r["conversation_run_id"] not in self.running_conversation_runs]
            for vid in stale:
                self._gate()
                self._commit("conversation_run_interrupted", {"conversation_run_id": vid, "reason": "PROCESS_LOST"},
                             "scheduler", evidence=("conversation_run_interrupted", f"conversation_run:{vid}",
                                                    {"conversation_run_id": vid, "reason": "PROCESS_LOST"},
                                                    ("interrupted", vid)))
                n += 1
        return n

    # ------------------------------------------------------------------ replay

    def _a_conversation_run_requested(self, d, at):
        self.conversation_runs[d["conversation_run_id"]] = {
            **{k: v for k, v in d.items() if k not in ("actor", "request_id", "request_sha", "_obj", "evidence")},
            "status": "running", "requested_at": at, "requested_by": d["actor"], "completed_at": None,
            "result": None, "result_sha256": None}

    def _a_conversation_run_recorded(self, d, at):
        self.conversation_runs[d["conversation_run_id"]].update(status="completed", completed_at=at,
                                                               result=d["result"], result_sha256=d["result_sha256"])

    def _a_conversation_run_interrupted(self, d, at):
        r = self.conversation_runs[d["conversation_run_id"]]
        r.update(status="interrupted", completed_at=at, interrupted_reason=d.get("reason", "PROCESS_LOST"))
        if d.get("failure"):
            r["failure"] = dict(d["failure"])

    # ------------------------------------------------------------------ views

    def conversation_run_view(self, tid: str, vid: str, full: bool = True) -> dict:
        with self.lock:
            r = self.conversation_runs.get(vid)
            if r is None or r["tenant_id"] != tid:
                raise NotFound(R("CONVERSATION_RUN_NOT_FOUND"))
            out = {k: v for k, v in r.items() if k != "result"}
            if r["status"] == "running" and vid not in self.running_conversation_runs:
                out["status"] = "running_elsewhere_or_interrupted"
            if full:
                out["result"] = r["result"]
            else:
                out["outcomes"] = None if r["result"] is None else {m: r["result"][m]["outcome"] for m in FIRETEAM}
            return out

    def conversation_runs_view(self, tid: str) -> list:
        with self.lock:
            return [self.conversation_run_view(tid, r["conversation_run_id"], full=False)
                    for r in sorted(self.conversation_runs.values(),
                                    key=lambda r: (r["requested_at"], r["conversation_run_id"]))
                    if r["tenant_id"] == tid][:1000]

    def crosscheck_view(self, tid: str, vid: str) -> dict:
        """What moves downstream: Zara's contract only (never a member's raw output)."""
        v = self.conversation_run_view(tid, vid)
        if v["status"] != "completed":
            raise Conflict(R("CONVERSATION_RUN_NOT_COMPLETED"))
        z = v["result"]["zara"]
        return {**z["facts"]["contract"], "outcome": z["outcome"]}

    def conversation_scorecard(self, agent: str) -> dict:
        """Theo's, Naomi's or Zara's conversation runs, from recorded runs only (the department manager)."""
        out = {"runs": 0, "completed": 0, "interrupted": 0, "outcomes": {}}
        for r in self.conversation_runs.values():
            out["runs"] += 1
            if r["status"] == "completed":
                out["completed"] += 1
                o = r["result"][agent]["outcome"]
                out["outcomes"][o] = out["outcomes"].get(o, 0) + 1
            elif r["status"] == "interrupted":
                out["interrupted"] += 1
        out["outcomes"] = dict(sorted(out["outcomes"].items()))
        return out

