"""
Scheduled re-audits with change-detect (ADR 0017 Wave 2, decision W2-3).

A schedule names one tenant, one of its registered domains, the paths, an optional prompt set and an interval in days.
A paying client's schedule needs Andre's approval AND a Finance (31) invoice id when it is created (the same rules as a
one-off audit); ZBM's own properties need neither.

The ``schedule-tick`` job (scheduler caller) runs at most ONE audit per schedule per slot, where
slot = whole intervals elapsed since the schedule's start. The audit id is derived from (schedule, slot), so a
duplicate tick, a retried tick, or a tick after a restart finds the slot's audit already recorded and does nothing:
a slot is never run twice, and a missed slot is not back-filled (only the current slot is ever due). A run whose
process died stays ``interrupted`` (the ``interrupted-audits`` job records it); its slot is consumed.

Budget: at most SEO_SCHEDULE_BUDGET_RUNS scheduled audits per tenant within any SEO_SCHEDULE_PERIOD_DAYS window
(counted from the recorded requests, on the injected clock); a slot over budget is recorded ``skipped:
BUDGET_EXHAUSTED``. Kill switches (global, write, tenant, capability:schedules, capability:audit) leave a due slot
untouched until the next tick; a switch engaged mid-run interrupts the run (AEGIS M3 rules).

After a scheduled audit completes, it is compared with the schedule's previous completed audit (agents/drift.py) and
the drift report is recorded (``drift_recorded``, its SHA-256 on the ledger).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from agents import drift as drift_mod
from clock import iso, parse_iso
from envelope import sha
from errors import Conflict, Forbidden, NotFound, SeoError
from ledger import derived_id
from reasons import R
from svc_audits import _authorize, _check_paths


class SchedulesMixin:
    def create_schedule(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        _check_paths(body["paths"], self.settings.audit_max_pages)
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="schedules", write=True)
            actor = "andre" if andre else actor
            rk = self.rk("schedule", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.schedule_view(tid, prev[1])
            _authorize(t, body, andre)
            if body.get("prompt_set_id"):
                p = self.prompt_sets.get(body["prompt_set_id"])
                if p is None or p["tenant_id"] != tid:
                    raise NotFound(R("PROMPT_SET_NOT_FOUND"))
            sid = derived_id("sch", tid, actor, rk)
            plan = self._invoice_plan(tid, body, andre, owner=f"schedule:{sid}")
        verification = self._verify_invoice(plan)         # Finance (31), outside the lock (W3-2)
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="schedules", write=True)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.schedule_view(tid, prev[1])
            _authorize(t, body, andre)
            self._recheck_invoice(plan)
            data = {"schedule_id": sid, "tenant_id": tid, "domain": body["domain"], "scheme": body["scheme"],
                    "paths": list(body["paths"]), "prompt_set_id": body.get("prompt_set_id"),
                    "every_days": body["every_days"], "invoice_id": body.get("invoice_id"),
                    "andre_approved": bool(andre), "start_at": iso(self.now()), "invoice_verification": verification}
            ev = [("schedule_created", f"schedule:{sid}",
                   {"schedule_id": sid, "tenant_id": tid, "every_days": body["every_days"],
                    "domain_sha256": sha(body["domain"]), "invoice_id": body.get("invoice_id")}, (actor, rk))]
            if andre:
                ev.append(("schedule_approved_by_andre", f"schedule:{sid}",
                           {"schedule_id": sid, "invoice_id": body.get("invoice_id")}, ("andre", rk)))
            ev += self._invoice_evidence(verification, f"schedule:{sid}", {"schedule_id": sid},
                                         body.get("invoice_id"), actor, rk)
            self._commit("schedule_created", self._req(data, actor, rk, body, sid), actor, evidence=ev)
            return self.schedule_view(tid, sid)

    def _a_schedule_created(self, d, at):
        self.schedules[d["schedule_id"]] = {**{k: v for k, v in d.items() if k not in ("actor", "request_id",
                                                                                        "request_sha", "_obj",
                                                                                        "evidence")},
                                            "status": "active", "created_at": at, "slots": {}, "drifts": []}

    def set_schedule_status(self, actor: str, tid: str, sid: str, body: dict, andre: bool) -> dict:
        with self.lock:
            self._gate()
            s = self._schedule(tid, sid)
            self.check_kill(tenant=tid, write=True)
            actor = "andre" if andre else actor
            rk = self.rk("schedule_status", sid, body)
            if self._idem(actor, rk, body):
                return self.schedule_view(tid, sid)
            t = self.tenants[tid]
            if body["status"] == "active" and t["kind"] == "client" and not andre:
                raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))       # resuming paid work is Andre's
            if s["status"] == body["status"]:
                raise Conflict(R("SCHEDULE_STATE"))
            data = {"schedule_id": sid, "status": body["status"]}
            self._commit("schedule_status", self._req(data, actor, rk, body, sid), actor,
                         evidence=("schedule_status", f"schedule:{sid}", data, (actor, rk)))
            return self.schedule_view(tid, sid)

    def _a_schedule_status(self, d, at):
        self.schedules[d["schedule_id"]]["status"] = d["status"]

    def _schedule(self, tid: str, sid: str) -> dict:
        s = self.schedules.get(sid)
        if s is None or s["tenant_id"] != tid:
            raise NotFound(R("SCHEDULE_NOT_FOUND"))
        return s

    def schedule_view(self, tid: str, sid: str) -> dict:
        with self.lock:
            s = self._schedule(tid, sid)
            out = {k: v for k, v in s.items() if k not in ("slots", "drifts")}
            out["slots"] = {k: {**v, "status": self.audits[v["audit_id"]]["status"]} if v.get("audit_id") else dict(v)
                            for k, v in sorted(s["slots"].items(), key=lambda kv: int(kv[0]))}
            out["current_slot"] = self._slot(s)
            out["drifts"] = [dict(x) for x in s["drifts"][-10:]]
            return out

    def schedules_view(self, tid: str) -> list:
        with self.lock:
            return [self.schedule_view(tid, k) for k, s in self.schedules.items() if s["tenant_id"] == tid][:500]

    def _slot(self, s: dict) -> int:
        elapsed = self.now() - parse_iso(s["start_at"])
        return max(0, elapsed // timedelta(days=s["every_days"]))

    def _budget_used(self, tid: str) -> int:
        since = self.now() - timedelta(days=self.settings.schedule_period_days)
        return sum(1 for a in self.audits.values() if a["tenant_id"] == tid and a.get("schedule_id")
                   and parse_iso(a["requested_at"]) > since)

    # ------------------------------------------------------------------ the tick

    def schedule_tick(self) -> dict:
        summary = {"ran": 0, "already_done": 0, "budget_exhausted": 0, "killed": 0, "paused": 0, "refused": 0,
                   "drift_recorded": 0, "invoice_unverifiable": 0}
        with self.lock:
            due = sorted(self.schedules)
        for sid in due:
            with self.lock:
                self._gate()
                s = self.schedules[sid]
                slot = self._slot(s)               # AEGIS L2: computed now, after the earlier audits of this tick
                tid = s["tenant_id"]
                if s["status"] != "active":
                    summary["paused"] += 1
                    continue
                aid = derived_id("aud", "schedule", sid, slot)
                if str(slot) in s["slots"] or aid in self.audits:
                    summary["already_done"] += 1
                    continue
                if self.kill_code(tenant=tid, capability="schedules", write=True) or \
                        self.kill_code(capability="audit"):
                    summary["killed"] += 1
                    continue
                if self._budget_used(tid) >= self.settings.schedule_budget_runs:
                    data = {"schedule_id": sid, "slot": slot, "reason": "BUDGET_EXHAUSTED"}
                    self._commit("schedule_slot_skipped", data, "scheduler",
                                 evidence=("schedule_slot_skipped", f"schedule:{sid}", data, ("skip", sid, slot)))
                    summary["budget_exhausted"] += 1
                    continue
                body = {"domain": s["domain"], "scheme": s["scheme"], "paths": list(s["paths"]),
                        "invoice_id": s["invoice_id"], "prompt_set_id": s["prompt_set_id"]}
                try:
                    plan = self._invoice_plan(tid, body, False, owner=f"schedule:{sid}")
                except SeoError as exc:
                    plan, early = None, exc
                else:
                    early = None
            # W3-2: a paying client's slot runs only while Finance (31) still confirms the schedule's invoice (paid,
            # not refunded or charged back since). Asked outside the lock; no override at a tick (Andre is not there).
            verification, refusal = None, early
            if refusal is None:
                try:
                    verification = self._verify_invoice(plan)
                except SeoError as exc:
                    refusal = exc
            with self.lock:
                self._gate()
                s = self.schedules[sid]                    # re-checked: the lock was released for Finance
                if s["status"] != "active":
                    summary["paused"] += 1
                    continue
                if str(slot) in s["slots"] or aid in self.audits:
                    summary["already_done"] += 1
                    continue
                if self.kill_code(tenant=tid, capability="schedules", write=True) or \
                        self.kill_code(capability="audit"):
                    summary["killed"] += 1
                    continue
                if refusal is not None and refusal.status_code == 503:
                    summary["invoice_unverifiable"] += 1     # Finance could not answer: the slot waits for a tick
                    continue
                try:
                    if refusal is not None:
                        raise refusal
                    self._recheck_invoice(plan)
                    ps = self._open_audit("scheduler", tid, f"schedule|{sid}|{slot}", body, s["andre_approved"],
                                          aid, schedule={"schedule_id": sid, "slot": slot}, verification=verification)
                except SeoError as exc:          # tenant changed since (domain removed, prompt set gone, busy), or
                    data = {"schedule_id": sid, "slot": slot, "reason": exc.reason}     # Finance said no
                    if exc.reason != "AUDIT_RUNNING":
                        self._commit("schedule_slot_skipped", data, "scheduler",
                                     evidence=("schedule_slot_skipped", f"schedule:{sid}", data,
                                               ("skip", sid, slot)))
                    summary["refused"] += 1
                    continue
            view = self._execute_audit(aid, tid, body, ps)
            summary["ran"] += 1
            if view["status"] == "completed" and self._record_drift(sid, aid):
                summary["drift_recorded"] += 1
        return summary

    def _a_schedule_slot_skipped(self, d, at):
        self.schedules[d["schedule_id"]]["slots"][str(d["slot"])] = {"status": "skipped", "reason": d["reason"],
                                                                     "at": at}

    def _record_drift(self, sid: str, aid: str) -> bool:
        with self.lock:
            if self.agent_blocked("osei"):              # the drift comparison is Osei's
                return False
            s = self.schedules[sid]
            done = [x["audit_id"] for x in s["slots"].values() if x.get("audit_id")
                    and self.audits[x["audit_id"]]["status"] == "completed"]
            if aid not in done or done.index(aid) == 0:
                return False
            prev = done[done.index(aid) - 1]
            older, newer = self.audits[prev]["report"], self.audits[aid]["report"]
        try:
            report = drift_mod.compare(older, newer)
        except drift_mod.NotComparable:
            return False
        with self.lock:
            self._gate()
            dsha = sha(report)
            data = {"schedule_id": sid, "older": prev, "newer": aid, "drift": report, "drift_sha256": dsha}
            self._commit("drift_recorded", data, "scheduler",
                         evidence=("drift_recorded", f"schedule:{sid}",
                                   {"schedule_id": sid, "older": prev, "newer": aid, "drift_sha256": dsha,
                                    "counts": report["counts"]}, ("drift", sid, aid)))
        return True

    def _a_drift_recorded(self, d, at):
        self.schedules[d["schedule_id"]]["drifts"].append({"older": d["older"], "newer": d["newer"], "at": at,
                                                           "drift_sha256": d["drift_sha256"],
                                                           "counts": d["drift"]["counts"], "drift": d["drift"]})

    def latest_drift(self, tid: str, sid: str) -> Optional[dict]:
        with self.lock:
            s = self._schedule(tid, sid)
            return dict(s["drifts"][-1]) if s["drifts"] else None
