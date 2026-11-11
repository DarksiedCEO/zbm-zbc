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
the drift report is recorded: its detail (rules text, changes with URLs and keys) in the tenant's vault, and
``drift_recorded`` with the blob ref, its keyed HMAC (also on the ledger) and counts only (sink job B, svc_vaulted.py).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import logging

import vault as vault_mod
from agents import drift as drift_mod
from clock import iso, parse_iso
from envelope import sha
from errors import Conflict, Forbidden, NotFound, SeoError, Unavailable
from ledger import derived_id
from reasons import R
from svc_audits import _authorize, _check_paths
from svc_optext import path_view
from svc_vaulted import DRIFT_FIELD, normalised, owner_view

log = logging.getLogger("seo.schedules")
DRIFT_VIEW_MAX = 10                       # drift reports shown (and read from the vault) per schedule view


class SchedulesMixin:
    def create_schedule(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        _check_paths(body["paths"], self.settings.audit_max_pages, body.get("scheme", "https"), body["domain"])
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="schedules", write=True)
            actor = "andre" if andre else actor
            rk = self.rk("schedule", tid, body)
            hbody = self._paths_hbody(tid, body)          # review c0c09bd1: the paths' HMACs, never the paths
            prev = self._idem(actor, rk, hbody)
            if not prev:
                _authorize(t, body, andre)
                if body.get("prompt_set_id"):
                    p = self.prompt_sets.get(body["prompt_set_id"])
                    if p is None or p["tenant_id"] != tid:
                        raise NotFound(R("PROMPT_SET_NOT_FOUND"))
                    if p["versions"][str(p["current"])].get("vault_unavailable"):
                        # review 2b26d980 SHOULD 6: the set's text cannot be read (its blob is lost): a schedule
                        # naming it is refused as an audit and a citation run are, never created to fail every slot
                        raise Unavailable(R("VAULT_UNAVAILABLE"))
                sid = derived_id("sch", tid, actor, rk)
                plan = self._invoice_plan(tid, body, andre, owner=f"schedule:{sid}")
        if prev:
            return self.schedule_view(tid, prev[1])       # sink job B: drift detail is read outside the lock
        verification = self._verify_invoice(plan)         # Finance (31), outside the lock (W3-2)
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="schedules", write=True)
            prev = self._idem(actor, rk, hbody)
            if prev:
                return self.schedule_view(tid, prev[1], detail=False)   # created a moment ago: no drift to read
            _authorize(t, body, andre)
            self._recheck_invoice(plan)
            paths = self._seal_paths(tid, body["scheme"], body["domain"], list(body["paths"]))
            data = {"schedule_id": sid, "tenant_id": tid, "domain": body["domain"], "scheme": body["scheme"],
                    "paths": paths, "prompt_set_id": body.get("prompt_set_id"),
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
            try:
                self._commit("schedule_created", self._req(data, actor, rk, hbody, sid), actor, evidence=ev)
            except BaseException:
                self._optext_orphaned()
                raise
            self._optext_committed()
        return self.schedule_view(tid, sid)

    def _a_schedule_created(self, d, at):
        cache: dict = {}
        paths = [self._optext_open(d["tenant_id"], x, cache) for x in d["paths"]]
        self.schedules[d["schedule_id"]] = {**{k: v for k, v in d.items() if k not in ("actor", "request_id",
                                                                                        "request_sha", "_obj",
                                                                                        "evidence")},
                                            "paths": [x for x in paths if x is not None],
                                            "status": "active", "created_at": at, "slots": {}, "drifts": []}
        if any(x is None for x in paths):
            self.schedules[d["schedule_id"]]["vault_unavailable"] = True

    def set_schedule_status(self, actor: str, tid: str, sid: str, body: dict, andre: bool) -> dict:
        with self.lock:
            self._gate()
            s = self._schedule(tid, sid)
            self.check_kill(tenant=tid, write=True)
            actor = "andre" if andre else actor
            rk = self.rk("schedule_status", sid, body)
            if not self._idem(actor, rk, body):
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

    def _schedule_snapshot(self, tid: str, sid: str) -> dict:
        """Under the lock: the schedule's view with its last drifts as recorded in memory (machine facts, and the
        detail only for a record written before sink job B)."""
        with self.lock:
            s = self._schedule(tid, sid)
            out = {k: v for k, v in s.items() if k not in ("slots", "drifts")}
            out["paths"] = [path_view(s["scheme"], s["domain"], x) for x in s["paths"]]   # review c0c09bd1
            out["slots"] = {k: {**v, "status": self.audits[v["audit_id"]]["status"]} if v.get("audit_id") else dict(v)
                            for k, v in sorted(s["slots"].items(), key=lambda kv: int(kv[0]))}
            out["current_slot"] = self._slot(s)
            out["drifts"] = [dict(x) for x in s["drifts"][-DRIFT_VIEW_MAX:]]
            return out

    def _drift_shown(self, tid: str, d: dict, detail: bool) -> dict:
        """OUTSIDE the lock: one recorded drift for the owner's view. ``detail``: the drift report read from the
        tenant's vault and shown through the client-view masker; one that cannot be read is ``None`` with
        ``vault_unavailable: true``."""
        out = {k: v for k, v in d.items() if k not in ("drift", "drift_ref")}
        if detail:
            report = d.get("drift")
            if report is None and d.get("drift_ref") is not None:
                report = self._vaulted_open(tid, DRIFT_FIELD, d["drift_ref"], d.get("drift_hmac"))
                if report is None:
                    out["vault_unavailable"] = True
            out["drift"] = owner_view(report) if report is not None else None
        return out

    def schedule_view(self, tid: str, sid: str, detail: bool = True) -> dict:
        """The owner's view. The last drift reports are read from the vault outside the lock (sink job B); the list
        view (``detail=False``) carries their counts and keyed hashes only."""
        out = self._schedule_snapshot(tid, sid)
        out["drifts"] = [self._drift_shown(tid, d, detail) for d in out["drifts"]]
        return out

    def schedules_view(self, tid: str) -> list:
        with self.lock:
            ids = [k for k, s in self.schedules.items() if s["tenant_id"] == tid][:500]
        return [self.schedule_view(tid, k, detail=False) for k in ids]

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
                   "drift_recorded": 0, "invoice_unverifiable": 0, "failed": 0}
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
                if s.get("vault_unavailable") or not self.vault.key_configured:
                    # review c0c09bd1: its paths cannot be read: never run. Sink job B: nor is a run whose report
                    # could not be kept (no vault key); the slot is left for a later tick
                    summary["refused"] += 1
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
                    if exc.reason not in ("AUDIT_RUNNING", "TENANT_FINANCE_CLIENT_CHANGED"):     # transient: the
                        # slot waits for the next tick (L1: the binding moved while Finance was asked)
                        self._commit("schedule_slot_skipped", data, "scheduler",
                                     evidence=("schedule_slot_skipped", f"schedule:{sid}", data,
                                               ("skip", sid, slot)))
                    summary["refused"] += 1
                    continue
            view = self._execute_audit(aid, tid, body, ps, full=False)   # the tick needs the status only
            if view.get("interrupted_reason") == "RUN_FAILED":
                summary["failed"] += 1           # L2: a slot whose run crashed is consumed but is not counted as ran
                continue
            summary["ran"] += 1
            if view["status"] == "completed" and self._record_drift(sid, aid):
                summary["drift_recorded"] += 1
        return summary

    def _a_schedule_slot_skipped(self, d, at):
        self.schedules[d["schedule_id"]]["slots"][str(d["slot"])] = {"status": "skipped", "reason": d["reason"],
                                                                     "at": at}

    def _record_drift(self, sid: str, aid: str) -> bool:
        """Sink job B: both reports are read from the tenant's vault and the drift report is encrypted OUTSIDE the
        lock; under it one vault append and the commit. A report that cannot be read fails closed: no drift is
        recorded (logged, never compared with ``None``)."""
        with self.lock:
            if self.agent_blocked("osei"):              # the drift comparison is Osei's
                return False
            s = self.schedules[sid]
            tid = s["tenant_id"]
            done = [x["audit_id"] for x in s["slots"].values() if x.get("audit_id")
                    and self.audits[x["audit_id"]]["status"] == "completed"]
            if aid not in done or done.index(aid) == 0:
                return False
            prev = done[done.index(aid) - 1]
        try:
            # the same view of each report as the API's drift (svc_audits.drift_view): retention applied (AEGIS
            # gl-gsc L3)
            older, newer = self._audit_recorded(tid, prev)["report"], self._audit_recorded(tid, aid)["report"]
        except Unavailable as exc:
            log.warning("drift not recorded: %s", exc.reason)
            return False
        try:
            report = normalised(drift_mod.compare(older, newer))
        except drift_mod.NotComparable:
            return False
        finally:
            older = newer = None
        facts = drift_facts(report)
        try:
            sealed = self._vaulted_seal(tid, DRIFT_FIELD, report)
        except vault_mod.VaultError as exc:
            log.warning("drift not recorded: %s", type(exc).__name__)
            return False
        finally:
            report = None
        try:
            with self.lock:
                self._gate()
                data = {"schedule_id": sid, "older": prev, "newer": aid, "drift_ref": sealed.ref,
                        "drift_hmac": sealed.hmac, "facts": facts}
                ev = ("drift_recorded", f"schedule:{sid}",
                      {"schedule_id": sid, "older": prev, "newer": aid, "drift_hmac": sealed.hmac,
                       "counts": facts["counts"]}, ("drift", sid, aid))
                try:
                    self._vaulted_commit(sealed, lambda: self._commit("drift_recorded", data, "scheduler",
                                                                      evidence=ev))
                except vault_mod.VaultError as exc:       # the blob write failed: nothing of it is kept
                    log.warning("drift not recorded: %s", type(exc).__name__)
                    return False
        finally:
            sealed.discard()
        return True

    def _a_drift_recorded(self, d, at):
        """Sink job B: the record names the drift report's blob, its keyed HMAC and counts. A record written BEFORE
        it carries the whole drift report and its plain SHA-256: kept as recorded, its facts derived here."""
        if "drift" in d:
            entry = {"older": d["older"], "newer": d["newer"], "at": at, "drift_sha256": d["drift_sha256"],
                     "drift_hmac": None, "drift": d["drift"], "drift_ref": None, **drift_facts(d["drift"])}
        else:
            entry = {"older": d["older"], "newer": d["newer"], "at": at, "drift_sha256": None,
                     "drift_hmac": d["drift_hmac"], "drift": None, "drift_ref": d["drift_ref"], **d["facts"]}
        self.schedules[d["schedule_id"]]["drifts"].append(entry)

    def latest_drift(self, tid: str, sid: str) -> Optional[dict]:
        with self.lock:
            s = self._schedule(tid, sid)
            last = dict(s["drifts"][-1]) if s["drifts"] else None
        return None if last is None else self._drift_shown(tid, last, True)


def drift_facts(report: dict) -> dict:
    """The machine facts of a drift report that the log keeps: counts by class and by agent, and versions."""
    sp = report.get("search_performance")
    return {"counts": dict(report["counts"]), "changes": len(report["changes"]), "unchanged": report["unchanged"],
            "persisted_by_agent": dict(report["persisted_by_agent"]),
            "overturned_by_agent": dict(report["overturned_by_agent"]),
            "rules_version": report.get("rules_version"),
            "search_performance": None if not isinstance(sp, dict) else sp.get("class")}
