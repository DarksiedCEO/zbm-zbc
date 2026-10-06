"""
Apply, leases, re-detection, guided manual fixes, reports and refunds (founder decisions 4-7; ADR 0017 decisions
15-20).

Apply. Only an APPROVED job whose approval names the plan hash recomputed NOW, with Finance's payment on record, a
wired transport, every connection active and the client's own, and no resource frozen or leased by another run, may
start. ``apply_started`` records one LEASE per client resource (connector | account | target) on the ledger; a second
run touching any of them is refused ``RESOURCE_LEASED`` (two runs never apply to one resource at once), and a second
apply of the same job ``APPLY_IN_PROGRESS``. Each item then runs through the deterministic executor (executor.py)
OUTSIDE the lock; every executor step is its own committed line (record first), and ``live()`` is asked before every
request: a revoked connection (the client's revocation epoch moved), a frozen resource or client, or a closed service
stops the item at once. ``item_settled`` records the outcome and releases the item's leases. A rollback that cannot be
proven, an interrupted run, or a revocation after a write FREEZES the resources and opens a task for Andre: nothing
touches a frozen resource until Andre unfreezes it by its exact state hash.

Proof. ``applied_verified`` is not ``fixed``: only Revenue Recovery's re-detection answering ``cleared`` makes an item
``fixed_proven`` (delivery-py's rule: the engine's own claim is never enough). ``present`` is ``not_cleared`` (the
re-detection disagrees with the claimed fix: a task for Andre); anything else is unknown and counted, in job runs, never
wall hours, with ONE task after CFX_UNKNOWN_TICKS_BEFORE_TASK. A guided manual fix (Yelp) is counted only when the
platform's read shows the required value.

Report and refund. When every item is terminal a dated before/after report is committed (each item's snapshot, its
read-back, its rollback and re-detection results and the log lines that are its evidence). Payment is kept for items
``fixed_proven``; the sum of every other item's price is proposed as a refund that ONLY Andre approves, by its exact
hash, with his token; the approved refund goes to Finance (31) through the port (not wired: it stays ``queued``). An
unknown Finance answer leaves it ``sending`` and is reconciled through ``refund_status``; it is never sent twice.
"""

from __future__ import annotations

from typing import Optional

import executor
import money
from catalogue import CHECKS
from errors import Conflict, Forbidden, NotFound, Unavailable
from ledger import derived_id
from connectors.base import HttpAnswer, UnknownState
from ports import ConnView
from reasons import R
from svc_jobs import TERMINAL, UNFIXED, sha

ACTOR = "clientfix"     # internal lines (executor steps, settlements): the department itself
FREEZE_ON = ("rollback_failed", "interrupted", "halted_revoked", "halted_frozen")


class ApplyMixin:
    # ------------------------------------------------------------------ apply

    def apply(self, actor: str, job_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            rk = self.rk("apply", job_id, body)
            if self._idem(actor, rk, body):
                return self.job_view(job_id)
            if job_id in self._running or j["status"] == "applying":
                raise Conflict(R("APPLY_IN_PROGRESS"))
            if j["status"] in ("quoted", "accepted"):
                raise Conflict(R("PAYMENT_REQUIRED"))
            if j["status"] in ("paid",):
                raise Conflict(R("PLAN_REQUIRED"))
            if j["status"] == "planned":
                raise Conflict(R("APPROVAL_REQUIRED"))
            if j["status"] != "approved":
                raise Conflict(R("JOB_STATE"))
            if not j.get("payment"):
                raise Conflict(R("PAYMENT_REQUIRED"))
            now_sha = self._plan_sha(j)
            if not j.get("approval") or j["approval"]["plan_sha256"] != now_sha or now_sha != j["plan_sha256"]:
                raise Conflict(R("APPROVAL_STALE"))
            if j["client_id"] in self.frozen_clients:
                raise Conflict(R("RESOURCE_FROZEN"))
            if not self.ports.transport.wired:
                raise Unavailable(R("CONNECTOR_NOT_WIRED"))       # nothing recorded, nothing touched
            leases, items, leased = [], [], set()
            for it in sorted(j["items"].values(), key=lambda x: x["item_id"]):
                if it["status"] != "planned":
                    continue
                conn = self._conn_for(j["client_id"], it["connection_id"])
                if conn["status"] != "active" or conn["connection_id"] in self.revoked_now \
                        or j["client_id"] in self.revoked_clients_now:
                    raise Conflict(R("CONNECTION_REVOKED"))
                for op in it["ops"]:
                    key = self.connectors[conn["connector"]].lease_key(conn["account_ref"], op["target"])
                    if key in self.frozen:
                        raise Conflict(R("RESOURCE_FROZEN"))
                    if key in self.lease_by_resource or key in self._reaper_holding:
                        raise Conflict(R("RESOURCE_LEASED"))
                    if key not in leased:
                        leased.add(key)
                        leases.append({"lease_id": derived_id("lse", job_id, key), "resource_key": key,
                                       "item_id": it["item_id"]})
                items.append(it["item_id"])
            if not items:
                raise Conflict(R("JOB_EMPTY"))
            epoch = self.revocation_epoch.get(j["client_id"], 0)
            evidence = [("apply_started", f"job:{job_id}", {"job_id": job_id, "plan_sha256": now_sha,
                                                            "items": len(items)}, (actor, rk))]
            evidence += [("lease_acquired", f"lease:{x['lease_id']}", {"lease_id": x["lease_id"], "job_id": job_id,
                                                                      "resource_sha256": sha(x["resource_key"])},
                          (actor, rk, x["lease_id"])) for x in leases]
            self._commit("apply_started", self._req({"job_id": job_id, "epoch": epoch, "leases": leases}, actor, rk,
                                                    body, job_id), actor, evidence=evidence)
            self._running.add(job_id)
        try:
            for item_id in items:
                self._run_one(job_id, item_id, epoch)
        finally:
            with self.lock:
                self._running.discard(job_id)
                try:
                    if self.jobs[job_id]["status"] == "applying" and all(
                            self.jobs[job_id]["items"][i]["status"] != "planned" for i in items):
                        self._finish(job_id, "finished")
                        self._settle_if_done(job_id)
                except Unavailable:
                    pass                      # left ``applying``: the recover job settles it (interrupted)
        return self.job_view(job_id)

    def _a_apply_started(self, d, at):
        j = self.jobs[d["job_id"]]
        j.update(status="applying", apply_epoch=d["epoch"])
        for x in d["leases"]:
            self.leases[x["lease_id"]] = {**x, "job_id": d["job_id"], "status": "active", "acquired_at": at,
                                          "released_at": None}
            self.lease_by_resource[x["resource_key"]] = x["lease_id"]

    def _finish(self, job_id: str, how: str) -> None:
        """End a run: every lease it holds is released in the same line (leases are per run, never per item, so two
        items of one run touching one resource are covered until the run ends)."""
        released = sorted(lid for lid, lease in self.leases.items()
                          if lease["job_id"] == job_id and lease["status"] == "active")
        evidence = [("apply_finished", f"job:{job_id}", {"job_id": job_id, "how": how}, (job_id, how))]
        evidence += [("lease_released", f"lease:{lid}", {"lease_id": lid, "job_id": job_id}, (lid, "released"))
                     for lid in released]
        self._commit("apply_finished", {"job_id": job_id, "how": how, "released": released}, ACTOR, evidence=evidence)

    def _a_apply_finished(self, d, at):
        j = self.jobs[d["job_id"]]
        if j["status"] == "applying":
            j["status"] = "applied"
        for lid in d.get("released") or ():
            lease = self.leases[lid]
            lease.update(status="released", released_at=at)
            if self.lease_by_resource.get(lease["resource_key"]) == lid:
                del self.lease_by_resource[lease["resource_key"]]

    def _run_one(self, job_id: str, item_id: str, epoch: int) -> None:
        with self.lock:
            j = self.jobs[job_id]
            it = j["items"][item_id]
            if it["status"] != "planned":
                return
            c = self.connections[it["connection_id"]]
            if c["client_id"] != j["client_id"]:          # defence in depth (validated at plan and at apply)
                return
            conn = ConnView(c["connection_id"], c["client_id"], c["connector"], c["account_ref"], c["token_ref"])
            connector = self.connectors[c["connector"]]
            ops = [dict(op) for op in it["ops"]]
            keys = sorted({connector.lease_key(c["account_ref"], op["target"]) for op in ops})
        counter = {"n": 0}

        def live() -> Optional[str]:
            with self.lock:
                if self._closed:
                    return "SERVICE_CLOSED"
                cc = self.connections[conn.connection_id]
                if conn.connection_id in self.revoked_now or conn.client_id in self.revoked_clients_now \
                        or cc["status"] != "active" \
                        or self.revocation_epoch.get(conn.client_id, 0) != epoch:
                    return "CONNECTION_REVOKED"
                if conn.client_id in self.frozen_clients or any(k in self.frozen for k in keys):
                    return "RESOURCE_FROZEN"
            return None

        def step(kind: str, facts: dict) -> None:
            with self.lock:
                counter["n"] += 1
                try:
                    self._commit("apply_step", {"job_id": job_id, "item_id": item_id, "step": kind, "facts": facts},
                                 ACTOR, evidence=(f"apply_{kind}"[:64], f"item:{item_id}",
                                                  {"job_id": job_id, "item_id": item_id, "step": kind,
                                                   "facts_sha256": sha(facts)}, (job_id, item_id, counter["n"])))
                except Unavailable:
                    raise executor.Halt("LEDGER_UNAVAILABLE") from None

        def live_rollback() -> Optional[str]:
            """For the guarded rollback after Andre froze a resource mid-write: revocation and shutdown still stop it."""
            code = live()
            return None if code == "RESOURCE_FROZEN" else code

        result = executor.run_item(connector, conn, item_id, ops, self.ports.transport, step, live, live_rollback)
        with self.lock:
            try:
                self._settle_item(job_id, item_id, result, keys)
            except Unavailable:
                pass                           # stays ``planned`` under an ``applying`` job: recover() settles it

    def _settle_item(self, job_id: str, item_id: str, result: dict, keys: list) -> None:
        status = result["status"]
        # a halt freezes only when a write was actually sent (round 0 counted an op that never left)
        freeze = status in FREEZE_ON and (status not in ("halted_revoked", "halted_frozen")
                                          or bool(result.get("sent_writes")))
        tasks = []
        if freeze:
            code = {"rollback_failed": "ROLLBACK_FAILED", "interrupted": "APPLY_INTERRUPTED",
                    "halted_revoked": "REVOKED_MID_APPLY", "halted_frozen": "FROZEN_MID_APPLY"}[status]
            tasks.append(self._task("alert", item_id, code, code))
        if result.get("poisoned_version"):
            # AEGIS round 2 R2-1: name the version a future run would otherwise build on
            t = self._task("alert", item_id, "GTM_VERSION_POISONED", "GTM_VERSION_POISONED")
            t["ref"] = result["poisoned_version"]
            tasks.append(t)
        if result.get("release_may_be_unpublished"):
            # AEGIS round 3 L4 (accepted risk: no compare-and-swap on the live version): our re-publish of the
            # snapshot may have un-published a release the client made a moment before it — Andre checks THAT version
            t = self._task("alert", item_id, "GTM_RELEASE_MAY_BE_UNPUBLISHED", "GTM_RELEASE_MAY_BE_UNPUBLISHED")
            t["ref"] = result["release_may_be_unpublished"]
            tasks.append(t)
        data = {"job_id": job_id, "item_id": item_id, "status": status, "failure": result.get("failure"),
                "written": result["written"], "snapshot": result["snapshot"], "readback": result["readback"],
                "rollback": result["rollback"], "dry_run": result["dry_run"], "instructions": result["instructions"],
                "poisoned_version": result.get("poisoned_version"),
                "release_may_be_unpublished": result.get("release_may_be_unpublished"),
                "freeze": keys if freeze else [], "tasks": tasks}
        evidence = [("item_settled", f"item:{item_id}", {"job_id": job_id, "item_id": item_id, "status": status,
                                                         "failure": result.get("failure"), "frozen": len(data["freeze"])},
                     (job_id, item_id, "settled"))]
        if freeze:
            evidence.append(("resource_frozen", f"item:{item_id}", {"job_id": job_id, "item_id": item_id,
                                                                   "resources_sha256": sha(keys), "code": tasks[0]["code"]},
                             (job_id, item_id, "frozen")))
        self._commit("item_settled", data, ACTOR, evidence=evidence)

    def _a_apply_step(self, d, at):
        it = self.jobs[d["job_id"]]["items"][d["item_id"]]
        note = (d.get("facts") or {}).get("note") if d.get("step") == "request_sending" else None
        if isinstance(note, dict) and isinstance(note.get("gtm_run_workspace"), str) \
                and isinstance(note.get("account"), str):
            # M3: this name was recorded BEFORE its create request left; only such names are ever reaped
            self.run_workspaces[(note["account"], note["gtm_run_workspace"])] = {
                "job_id": d["job_id"], "item_id": d["item_id"], "client_id": self.jobs[d["job_id"]]["client_id"],
                "recorded_at": at}
        for ev in d.get("evidence") or ():
            it["evidence"].append({"seq": ev["payload"].get("seq"), "event_id": ev["event_id"], "step": d["step"]})

    def _a_item_settled(self, d, at):
        j = self.jobs[d["job_id"]]
        it = j["items"][d["item_id"]]
        it.update(status=d["status"], result={k: d[k] for k in ("failure", "written", "snapshot", "readback",
                                                                "rollback", "dry_run", "instructions")})
        if d["status"] in TERMINAL:
            it["settled_at"] = at
        for ev in d.get("evidence") or ():
            it["evidence"].append({"seq": ev["payload"].get("seq"), "event_id": ev["event_id"], "step": "settled"})
        for k in d.get("freeze") or ():
            fr = {"resource_key": k, "job_id": d["job_id"], "item_id": d["item_id"], "reason": d["status"], "at": at}
            fr["freeze_sha256"] = sha(fr)
            self.frozen[k] = fr

    # ------------------------------------------------------------------ recover, queue

    def recover_interrupted(self) -> dict:
        """A job left ``applying`` by a stopped process (or a ledger outage mid-run): every item still ``planned`` is
        ``interrupted`` (its outcome on the platform is unknown), its resources frozen, a task for Andre."""
        n = 0
        with self.lock:
            self._gate()
            for job_id in sorted(self.jobs):
                j = self.jobs[job_id]
                if j["status"] != "applying" or job_id in self._running:
                    continue
                for it in sorted(j["items"].values(), key=lambda x: x["item_id"]):
                    if it["status"] != "planned":
                        continue
                    c = self.connections[it["connection_id"]]
                    keys = sorted({self.connectors[c["connector"]].lease_key(c["account_ref"], op["target"])
                                   for op in it["ops"]})
                    self._settle_item(job_id, it["item_id"], {"status": "interrupted", "failure": "process_stopped",
                                                              "written": [], "snapshot": None, "readback": None,
                                                              "rollback": None, "dry_run": None,
                                                              "instructions": None}, keys)
                    n += 1
                self._finish(job_id, "recovered")
                self._settle_if_done(job_id)
        return {"interrupted": n, "workspaces_reaped": self.reap_run_workspaces()}

    def reap_run_workspaces(self) -> int:
        """Delete orphaned GTM run workspaces (AEGIS round 2 R2-5; round 3 M3 / L1). Only a workspace whose generated
        name this service recorded on the ledger BEFORE the create request (``run_workspaces``), of the SAME client,
        whose run has SETTLED (its item is no longer ``planned`` and its job no longer runs), is a candidate; the
        connector deletes it only when ``getStatus`` shows no change at all — a workspace with any change is HELD and
        an Andre task opened instead. The reaper holds the container (``_reaper_holding``: an apply is refused
        ``RESOURCE_LEASED`` meanwhile) and re-checks lease, freeze and revocation under the lock right before each
        DELETE. Every DELETE is recorded on the ledger before it leaves, and its answer after."""
        if not self.ports.transport.wired:
            return 0
        with self.lock:
            todo = []
            for c in sorted(self.connections.values(), key=lambda x: x["connection_id"]):
                key = self.connectors["gtm"].lease_key(c["account_ref"], "") if c["connector"] == "gtm" else None
                if not key or self._reaper_blocked(c, key):
                    continue
                owned = {}
                for (acct, name), rec in self.run_workspaces.items():
                    if acct != c["account_ref"] or rec["client_id"] != c["client_id"] or (acct, name) in self.reaper_held:
                        continue
                    job = self.jobs.get(rec["job_id"])
                    item = (job or {}).get("items", {}).get(rec["item_id"])
                    if job is None or item is None or item["status"] == "planned" or rec["job_id"] in self._running:
                        continue                              # that run has not settled: never touched
                    owned[name] = rec
                if owned:
                    self._reaper_holding.add(key)
                    todo.append((ConnView(c["connection_id"], c["client_id"], c["connector"], c["account_ref"],
                                          c["token_ref"]), key, owned))
        n = 0
        for conn, key, owned in todo:
            try:
                n += self._reap_one(conn, key, owned)
            finally:
                with self.lock:
                    self._reaper_holding.discard(key)
        return n

    def _reaper_blocked(self, c: dict, key: str) -> bool:
        """Under the lock: the container may not be reaped (leased, frozen, revoked, inactive, closed)."""
        return (self._closed or c["status"] != "active" or c["connection_id"] in self.revoked_now
                or c["client_id"] in self.revoked_clients_now or key in self.lease_by_resource
                or key in self.frozen or c["client_id"] in self.frozen_clients)

    def _reap_one(self, conn, key: str, owned: dict) -> int:
        counter = {"n": 0}

        def call(req):
            with self.lock:
                if self._reaper_blocked(self.connections[conn.connection_id], key):
                    raise executor.Halt("REAPER_STOPPED")
            if req.is_write:
                counter["n"] += 1
                self._reaper_step(conn, "request_sending", {"n": counter["n"], "request": req.describe()})
            try:
                ans = self.ports.transport.call(conn, req)
            except Exception:                              # noqa: BLE001
                ans = None
            ans = ans if isinstance(ans, HttpAnswer) else HttpAnswer(0, None)
            if req.is_write:
                self._reaper_step(conn, "request_answered", {"n": counter["n"], "status": ans.status})
            return ans

        def may_delete() -> bool:                          # L1: re-checked under the lock right before the DELETE
            with self.lock:
                return not self._reaper_blocked(self.connections[conn.connection_id], key)

        try:
            res = self.connectors["gtm"].reap(conn.account_ref, owned, call, may_delete)
        except (UnknownState, executor.Halt, Unavailable):
            return 0
        with self.lock:
            for name in res["deleted"]:
                try:
                    self._reaper_step(conn, "workspace_deleted", {"account": conn.account_ref, "name": name})
                except Unavailable:
                    pass
            for name, path in res["held"]:
                rec = owned[name]
                t = self._task("alert", f"{conn.account_ref}|{name}", "GTM_RUN_WORKSPACE_CHANGED",
                               "GTM_RUN_WORKSPACE_CHANGED")
                t["ref"] = path
                try:
                    self._reaper_step(conn, "workspace_held", {"account": conn.account_ref, "name": name,
                                                               "path": path, "job_id": rec["job_id"],
                                                               "item_id": rec["item_id"]}, tasks=[t])
                except Unavailable:
                    pass
        return len(res["deleted"])

    def _reaper_step(self, conn, kind: str, facts: dict, tasks: Optional[list] = None) -> None:
        with self.lock:
            d = {"connection_id": conn.connection_id, "step": kind, "facts": facts}
            if tasks:
                d["tasks"] = tasks
            self._commit("reaper_step", d, ACTOR,
                         evidence=(f"reaper_{kind}", f"connection:{conn.connection_id}",
                                   {"connection_id": conn.connection_id, "step": kind, "facts_sha256": sha(facts)},
                                   (conn.connection_id, kind, facts.get("n"), len(self.log))))

    def _a_reaper_step(self, d, at):
        f = d.get("facts") or {}
        if d.get("step") == "workspace_deleted":
            self.run_workspaces.pop((f.get("account"), f.get("name")), None)
        elif d.get("step") == "workspace_held":
            self.reaper_held[(f.get("account"), f.get("name"))] = {"path": f.get("path"), "at": at}

    def apply_queue(self) -> dict:
        out = {"recovered": self.recover_interrupted()["interrupted"], "applied": 0, "not_wired": 0, "refused": 0}
        with self.lock:
            ready = sorted(k for k, j in self.jobs.items() if j["status"] == "approved")
        for job_id in ready:
            if not self.ports.transport.wired:
                out["not_wired"] += 1
                continue
            try:
                self.apply("scheduler", job_id, {"request_id": sha(["queue", job_id, len(self.log)])[:32]})
                out["applied"] += 1
            except (Conflict, Forbidden, NotFound):
                out["refused"] += 1
            except Unavailable as exc:
                if exc.reason in ("LEDGER_UNAVAILABLE", "STORE_UNAVAILABLE", "INTEGRITY_UNVERIFIED", "SERVICE_CLOSED"):
                    raise
                out["refused"] += 1
        return out

    # ------------------------------------------------------------------ guided manual fixes

    def manual_done(self, actor: str, job_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            it = j["items"].get(body["item_id"])
            if it is None:
                raise NotFound(R("ITEM_NOT_FOUND"))
            rk = self.rk("manual_done", body["item_id"], body)
            if self._idem(actor, rk, body):
                return self.job_view(job_id)
            if it["status"] != "awaiting_manual":
                raise Conflict(R("MANUAL_NOT_PENDING"))
            self._commit("manual_reported", self._req({"job_id": job_id, "item_id": it["item_id"]}, actor, rk, body,
                                                      job_id), actor,
                         evidence=("manual_reported", f"item:{it['item_id']}", {"job_id": job_id,
                                                                               "item_id": it["item_id"], "by": actor},
                                   (actor, rk)))
            return self.job_view(job_id)

    def _a_manual_reported(self, d, at):
        self.jobs[d["job_id"]]["items"][d["item_id"]].update(status="manual_reported", unknown_ticks=0)

    def manual_verify_tick(self) -> dict:
        out = {"verified": 0, "unverified": 0}
        with self.lock:
            self._gate()
            todo = []
            for j in self.jobs.values():
                for it in j["items"].values():
                    if it["status"] == "manual_reported" and it["unknown_ticks"] < self.settings.unknown_ticks_before_task:
                        c = self.connections[it["connection_id"]]
                        todo.append((j["job_id"], it["item_id"], ConnView(c["connection_id"], c["client_id"],
                                                                          c["connector"], c["account_ref"], None),
                                     [dict(op) for op in it["ops"]]))
        for job_id, item_id, conn, ops in sorted(todo, key=lambda t: (t[0], t[1])):
            def live(cid=conn.connection_id):
                with self.lock:
                    return "CONNECTION_REVOKED" if cid in self.revoked_now else None
            if self.ports.transport.wired:
                verdict, back = executor.verify_manual(self.connectors[conn.connector], conn, ops,
                                                       self.ports.transport, live)
            else:
                verdict, back = "unknown", None
            with self.lock:
                it = self.jobs[job_id]["items"][item_id]
                if it["status"] != "manual_reported":
                    continue
                if verdict == "match":
                    self._commit("manual_verified", {"job_id": job_id, "item_id": item_id, "readback": back}, ACTOR,
                                 evidence=("manual_verified", f"item:{item_id}", {"job_id": job_id, "item_id": item_id,
                                                                                 "readback_sha256": sha(back)},
                                           (job_id, item_id, "manual_verified")))
                    out["verified"] += 1
                else:
                    self._tick_unknown(job_id, item_id, "MANUAL_UNVERIFIED")
                    out["unverified"] += 1
        return out

    def _a_manual_verified(self, d, at):
        it = self.jobs[d["job_id"]]["items"][d["item_id"]]
        it.update(status="manual_verified", unknown_ticks=0)
        it["result"] = {**(it.get("result") or {}), "readback": d["readback"]}

    def _tick_unknown(self, job_id: str, item_id: str, code: str) -> None:
        it = self.jobs[job_id]["items"][item_id]
        n = it["unknown_ticks"] + 1
        tasks = [self._task("review", item_id, code, code)] if n >= self.settings.unknown_ticks_before_task else []
        self._commit("unknown_tick", {"job_id": job_id, "item_id": item_id, "n": n, "code": code, "tasks": tasks},
                     ACTOR, evidence=("unknown_tick", f"item:{item_id}", {"job_id": job_id, "item_id": item_id,
                                                                         "n": n, "code": code},
                                      (job_id, item_id, code, n)))

    def _a_unknown_tick(self, d, at):
        self.jobs[d["job_id"]]["items"][d["item_id"]]["unknown_ticks"] = d["n"]

    # ------------------------------------------------------------------ re-detection

    def redetect_tick(self) -> dict:
        out = {"cleared": 0, "present": 0, "unknown": 0}
        with self.lock:
            self._gate()
            by_client: dict = {}
            for j in sorted(self.jobs.values(), key=lambda x: x["job_id"]):
                for it in sorted(j["items"].values(), key=lambda x: x["item_id"]):
                    if it["status"] in ("applied_verified", "manual_verified") \
                            and it["unknown_ticks"] < self.settings.unknown_ticks_before_task:
                        by_client.setdefault(j["client_id"], []).append(
                            (j["job_id"], it["item_id"], {"finding_id": it["finding_id"],
                                                          "check_code": it["check_code"],
                                                          "resource": dict(it["resource"])}))
        for client_id in sorted(by_client):
            rows = by_client[client_id]
            try:
                answer = self.ports.detection.rescan(client_id, [r[2] for r in rows])
            except Exception:                                    # noqa: BLE001 — unavailable: unknown
                answer = {}
            if not isinstance(answer, dict):
                answer = {}
            with self.lock:
                for job_id, item_id, chk in rows:
                    it = self.jobs[job_id]["items"][item_id]
                    if it["status"] not in ("applied_verified", "manual_verified"):
                        continue
                    verdict = answer.get(chk["finding_id"])
                    if verdict in ("cleared", "present"):
                        status = "fixed_proven" if verdict == "cleared" else "not_cleared"
                        tasks = [] if verdict == "cleared" else [self._task("review", item_id, "REDETECTION_DISAGREES",
                                                                           "REDETECTION_DISAGREES")]
                        self._commit("redetected", {"job_id": job_id, "item_id": item_id, "verdict": verdict,
                                                    "status": status, "tasks": tasks}, ACTOR,
                                     evidence=("redetected", f"item:{item_id}", {"job_id": job_id, "item_id": item_id,
                                                                                "finding_id": chk["finding_id"][:128],
                                                                                "verdict": verdict},
                                               (job_id, item_id, "redetected")))
                        out["cleared" if verdict == "cleared" else "present"] += 1
                    else:
                        self._tick_unknown(job_id, item_id, "REDETECTION_UNKNOWN")
                        out["unknown"] += 1
        with self.lock:
            for job_id in sorted({r[0] for rows in by_client.values() for r in rows}):
                self._settle_if_done(job_id)
        return out

    def _a_redetected(self, d, at):
        it = self.jobs[d["job_id"]]["items"][d["item_id"]]
        it.update(status=d["status"], redetection={"verdict": d["verdict"], "at": at}, settled_at=at)
        f = self.findings.get(it["finding_id"])
        if f is not None and d["status"] == "fixed_proven":
            f["status"] = "fixed"

    def close_item_unfixed(self, job_id: str, item_id: str, body: dict) -> dict:
        """Andre ends an item that cannot be proven (re-detection or a manual check unknown for good): unfixed."""
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            it = j["items"].get(item_id)
            if it is None:
                raise NotFound(R("ITEM_NOT_FOUND"))
            rk = self.rk("close_item", item_id, body)
            if self._idem("andre", rk, body):
                return self.job_view(job_id)
            if it["status"] not in ("applied_verified", "manual_verified", "awaiting_manual", "manual_reported"):
                raise Conflict(R("ITEM_STATE"))
            if body["state_sha256"] != self.item_state_sha(it):
                raise Conflict(R("STATE_HASH_MISMATCH"))
            self._commit("item_abandoned", self._req({"job_id": job_id, "item_id": item_id}, "andre", rk, body, job_id),
                         "andre", evidence=("item_abandoned", f"item:{item_id}", {"job_id": job_id, "item_id": item_id},
                                            ("andre", rk)))
            self._settle_if_done(job_id)
            return self.job_view(job_id)

    @staticmethod
    def item_state_sha(it: dict) -> str:
        return sha({"item_id": it["item_id"], "status": it["status"], "unknown_ticks": it["unknown_ticks"]})

    def _a_item_abandoned(self, d, at):
        self.jobs[d["job_id"]]["items"][d["item_id"]].update(status="abandoned", settled_at=at)

    # ------------------------------------------------------------------ settle: report and refund

    def _settle_if_done(self, job_id: str) -> None:
        """Under the lock. When every item is terminal: the dated report, then the refund proposal (or close)."""
        j = self.jobs[job_id]
        if j["status"] in ("applying", "reported", "refund_pending", "closed") or j["report"] is not None:
            return
        if any(it["status"] not in TERMINAL for it in j["items"].values()):
            return
        report = self._report(j)
        report_sha = sha(report)
        self._commit("report_issued", {"job_id": job_id, "report": report, "report_sha256": report_sha}, ACTOR,
                     evidence=("report_issued", f"job:{job_id}", {"job_id": job_id, "report_sha256": report_sha},
                               (job_id, "report")))
        unfixed = sorted(it["item_id"] for it in j["items"].values() if it["status"] in UNFIXED)
        amount = money.fmt(money.total(j["items"][i]["price"] for i in unfixed)) if unfixed else "0.00"
        if j.get("payment") and money.parse(amount) > 0:
            refund_id = derived_id("rfd", job_id)
            terms = {"refund_id": refund_id, "kind": "unfixed", "job_id": job_id, "client_id": j["client_id"],
                     "items": unfixed,
                     "amount": amount, "currency": j["quote"]["currency"],
                     "finance_event_id": j["payment"]["finance_event_id"], "report_sha256": report_sha}
            rsha = sha(terms)
            self._commit("refund_proposed", {"terms": terms, "refund_sha256": rsha,
                                             "tasks": [self._task("decide", refund_id, "REFUND_DECISION",
                                                                  "REFUND_DECISION")]}, ACTOR,
                         evidence=("refund_proposed", f"refund:{refund_id}",
                                   {"refund_id": refund_id, "job_id": job_id, "items": len(unfixed),
                                    "terms_sha256": rsha}, (job_id, "refund")))
        else:
            self._commit("job_closed", {"job_id": job_id, "reason": "nothing_to_refund"}, ACTOR,
                         evidence=("job_closed", f"job:{job_id}", {"job_id": job_id, "reason": "nothing_to_refund"},
                                   (job_id, "closed")))

    def _report(self, j: dict) -> dict:
        items = []
        for it in sorted(j["items"].values(), key=lambda x: x["item_id"]):
            r = it.get("result") or {}
            items.append({"item_id": it["item_id"], "finding_id": it["finding_id"], "check_code": it["check_code"],
                          "lane": CHECKS[it["check_code"]][0], "status": it["status"],
                          "fixed": it["status"] == "fixed_proven", "price": it["price"],
                          "ops": it.get("ops") or [], "before": r.get("snapshot"), "after": r.get("readback"),
                          "failure": r.get("failure"), "rollback": r.get("rollback"), "dry_run": r.get("dry_run"),
                          "redetection": it.get("redetection"), "evidence": list(it["evidence"])})
        return {"job_id": j["job_id"], "client_id": j["client_id"], "issued_at": _iso(self.now()),
                "quote_sha256": j["quote_sha256"], "plan_sha256": j["plan_sha256"],
                "payment": j.get("payment"), "items": items,
                "rule": "fixed only when re-detection cleared it; the engine's own claim is never enough"}

    def _a_report_issued(self, d, at):
        j = self.jobs[d["job_id"]]
        j.update(report={**d["report"], "report_sha256": d["report_sha256"]}, status="reported")

    def _a_refund_proposed(self, d, at):
        t = d["terms"]
        self.refunds[t["refund_id"]] = {**t, "refund_sha256": d["refund_sha256"], "status": "proposed",
                                        "proposed_at": at, "approved_at": None, "finance_ref": None,
                                        "unknown_ticks": 0}
        self.jobs[t["job_id"]].update(status="refund_pending", refund_id=t["refund_id"])

    def _a_job_closed(self, d, at):
        self.jobs[d["job_id"]].update(status="closed", closed_at=at)

    def approve_refund(self, refund_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            r = self._get(self.refunds, refund_id, "REFUND_NOT_FOUND")
            rk = self.rk("refund_approve", refund_id, body)
            if self._idem("andre", rk, body):
                return dict(self.refunds[refund_id])
            if r["status"] != "proposed":
                raise Conflict(R("REFUND_STATE"))
            if body["sha256"] != r["refund_sha256"]:
                raise Conflict(R("REFUND_HASH_MISMATCH"))
            self._commit("refund_approved", self._req({"refund_id": refund_id}, "andre", rk, body, refund_id), "andre",
                         evidence=("refund_approved", f"refund:{refund_id}",
                                   {"refund_id": refund_id, "terms_sha256": r["refund_sha256"]}, ("andre", rk)))
            return dict(self.refunds[refund_id])

    def _a_refund_approved(self, d, at):
        self.refunds[d["refund_id"]].update(status="queued", approved_at=at)
        for code in ("REFUND_DECISION", "ORPHANED_PAYMENT"):
            tid = derived_id("tsk", "decide", d["refund_id"], code)
            if tid in self.tasks and self.tasks[tid]["status"] == "open":
                self.tasks[tid].update(status="closed", closed_at=at, outcome="approved")

    def refund_tick(self) -> dict:
        out = {"with_finance": 0, "not_wired": 0, "refused": 0, "unknown": 0}
        with self.lock:
            self._gate()
            todo = sorted(k for k, r in self.refunds.items() if r["status"] in ("queued", "sending"))
        for rid in todo:
            with self.lock:
                r = self.refunds[rid]
                if r["status"] == "queued":
                    if not self.ports.finance.wired:
                        out["not_wired"] += 1
                        continue
                    self._commit("refund_sending", {"refund_id": rid}, ACTOR,
                                 evidence=("refund_sending", f"refund:{rid}", {"refund_id": rid,
                                                                              "terms_sha256": r["refund_sha256"]},
                                           (rid, "sending", r.get("attempt", 0))))
                    payload = {k: r[k] for k in ("refund_id", "job_id", "client_id", "items", "amount", "currency",
                                                 "finance_event_id", "refund_sha256")}
                    fresh = True
                else:
                    fresh = False
            try:
                got = self.ports.finance.request_refund(rid, payload) if fresh else self.ports.finance.refund_status(rid)
                status, ref = got.status, got.reference
            except Exception:                                    # noqa: BLE001 — unknown, never success
                status, ref = "unknown", None
            with self.lock:
                if status == "delivered" and isinstance(ref, str) and ref:
                    self._commit("refund_with_finance", {"refund_id": rid, "finance_ref": ref[:128]}, ACTOR,
                                 evidence=("refund_with_finance", f"refund:{rid}", {"refund_id": rid,
                                                                                   "finance_ref_sha256": sha(ref)},
                                           (rid, "with_finance")))
                    out["with_finance"] += 1
                elif status == "refused":
                    self._commit("refund_requeued", {"refund_id": rid}, ACTOR,
                                 evidence=("refund_requeued", f"refund:{rid}", {"refund_id": rid},
                                           (rid, "requeued", self.refunds[rid].get("attempt", 0))))
                    out["refused"] += 1
                else:
                    out["unknown"] += 1                # stays ``sending``: never resent, reconciled next run
        return out

    def _a_refund_sending(self, d, at):
        r = self.refunds[d["refund_id"]]
        r.update(status="sending", attempt=r.get("attempt", 0) + 1)

    def _a_refund_requeued(self, d, at):
        self.refunds[d["refund_id"]]["status"] = "queued"

    def _a_refund_with_finance(self, d, at):
        r = self.refunds[d["refund_id"]]
        r.update(status="with_finance", finance_ref=d["finance_ref"])
        if r.get("kind") != "orphaned_payment":          # an orphaned payment's refund never closes a running job
            self.jobs[r["job_id"]].update(status="closed", closed_at=at)

    def refunds_view(self, status: Optional[str]) -> list:
        with self.lock:
            return [dict(r) for k, r in sorted(self.refunds.items()) if status is None or r["status"] == status][:1000]

    # ------------------------------------------------------------------ frozen resources and leases

    def frozen_view(self) -> dict:
        with self.lock:
            return {"resources": [dict(v) for _, v in sorted(self.frozen.items())],
                    "clients": sorted(self.frozen_clients)}

    def leases_view(self, status: Optional[str]) -> list:
        with self.lock:
            return [dict(v) for _, v in sorted(self.leases.items()) if status is None or v["status"] == status][:2000]

    def unfreeze(self, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("unfreeze_resource", body["state_sha256"], body)
            if self._idem("andre", rk, body):
                return self.frozen_view()
            hit = [k for k, v in self.frozen.items() if v["freeze_sha256"] == body["state_sha256"]]
            if len(hit) != 1:
                raise Conflict(R("STATE_HASH_MISMATCH"))
            fr = self.frozen[hit[0]]
            self._commit("resource_unfrozen", self._req({"resource_key": hit[0]}, "andre", rk, body, hit[0]), "andre",
                         evidence=("resource_unfrozen", f"item:{fr['item_id']}",
                                   {"freeze_sha256": fr["freeze_sha256"]}, ("andre", rk)))
            return self.frozen_view()

    def _a_resource_unfrozen(self, d, at):
        self.frozen.pop(d["resource_key"], None)


def _iso(dt) -> str:
    from clock import iso
    return iso(dt)
