"""
Findings, jobs and quotes, payment, fire-team plans and the client's approval (founder decisions 2, 3, 5, 7; ADR 0017
decisions 8-10).

The flow is stateful and every step is a committed, ledger-anchored line:
  finding (Revenue Recovery) -> job + quote -> the client accepts the quote (by its hash, in a client session) ->
  Finance confirms payment of exactly that quote -> the fire team proposes a change set per item (validated here
  against each connector's allowlist, the job's client and the finding's resource) -> the client approves the exact
  fix plan (by its hash, in a client session) -> apply (svc_apply.py).

No work starts before payment: engaging the fire team and submitting a plan are refused ``PAYMENT_REQUIRED`` until
Finance has confirmed the quote's exact amount. Any new plan version voids the client's approval.
"""

from __future__ import annotations

import hashlib
import json

import money
from catalogue import CHECK_OPS, CHECKS, FIRE_TEAMS, op_allowed_for, team_for
from connectors.base import HttpAnswer
from connectors.base import OpRefused
from errors import Conflict, Invalid, NotFound, Unavailable
from ledger import derived_id
from ports import ModelNotWired
from reasons import R

TERMINAL = ("fixed_proven", "not_cleared", "drifted", "dry_run_refused", "dry_run_unknown", "snapshot_unknown",
            "rolled_back", "rollback_failed", "halted_revoked", "halted_frozen", "interrupted", "cancelled",
            "cancelled_revoked", "abandoned")
UNFIXED = tuple(s for s in TERMINAL if s != "fixed_proven")


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def resource_key(connector: str, account: str, target: str) -> str:
    return f"{connector}|{account}|{target}"


def _no_redirect_chain(planned: list, connections: dict) -> None:
    """AEGIS round 1 M2: across the whole plan, per store, no redirect may point at a path another planned redirect
    moves (a chain) and no set of them may form a loop. The store's EXISTING redirects are checked at apply time,
    against what the connector reads (ShopifyConnector.dry_run)."""
    from connectors.shopify import _norm
    by_shop: dict = {}
    for p in planned:
        shop = connections[p["connection_id"]]["account_ref"]
        for op in p["ops"]:
            if op["op"] == "shopify.redirect.set":
                by_shop.setdefault(shop, {})[_norm(op["target"][len("redirect:"):])] = _norm(op["after"])
    for edges in by_shop.values():
        for src, dst in edges.items():
            if dst in edges:
                raise Invalid(R("REDIRECT_CHAIN"))


class JobsMixin:
    # ------------------------------------------------------------------ findings

    def add_finding(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("finding", body["finding_id"], body)
            if self._idem(actor, rk, body):
                return dict(self.findings[body["finding_id"]])
            if body["finding_id"] in self.findings:
                raise Conflict(R("FINDING_EXISTS"))
            lane, allowed = CHECKS[body["check_code"]]
            if all(self.connectors[c].status == "not_built" for c in allowed):
                raise Invalid(R("CONNECTOR_NOT_BUILT"))
            conn = self._conn_for(body["client_id"], body["resource"]["connection_id"])
            if conn["status"] != "active" or conn["connection_id"] in self.revoked_now:
                raise Conflict(R("CONNECTION_REVOKED"))
            if conn["connector"] not in allowed:
                raise Invalid(R("CHECK_CONNECTOR_MISMATCH"))
            data = {"finding_id": body["finding_id"], "agent_id": body["agent_id"],
                    "leak_category": body.get("leak_category"), "client_id": body["client_id"],
                    "check_code": body["check_code"], "lane": lane, "resource": dict(body["resource"])}
            self._commit("finding_added", self._req(data, actor, rk, body, body["finding_id"]), actor,
                         evidence=("finding_added", f"finding:{body['finding_id']}"[:128],
                                   {"finding_id": body["finding_id"], "client_id": body["client_id"],
                                    "check_code": body["check_code"], "connection_id": conn["connection_id"],
                                    "target_sha256": sha(body["resource"]["target"])}, (actor, rk)))
            return dict(self.findings[body["finding_id"]])

    def _a_finding_added(self, d, at):
        self.findings[d["finding_id"]] = {k: d[k] for k in ("finding_id", "agent_id", "leak_category", "client_id",
                                                            "check_code", "lane", "resource")}
        self.findings[d["finding_id"]].update(status="open", job_id=None, added_at=at)

    # ------------------------------------------------------------------ jobs and quotes

    def create_job(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("job", body["client_id"], body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.job_view(prev[1])
            if len(body["items"]) > self.settings.max_items_per_job:
                raise Invalid(R("JOB_TOO_LARGE"))
            ids = [x["finding_id"] for x in body["items"]]
            if len(set(ids)) != len(ids):
                raise Invalid(R("INVALID"), field="items")
            job_id = derived_id("job", body["client_id"], rk)
            items, lanes = [], set()
            for x in body["items"]:
                f = self.findings.get(x["finding_id"])
                if f is None:
                    raise NotFound(R("FINDING_NOT_FOUND"))
                if f["client_id"] != body["client_id"]:
                    raise Conflict(R("TENANT_MISMATCH"))
                if f["status"] != "open":
                    raise Conflict(R("FINDING_NOT_OPEN"))
                try:
                    price = money.fmt(money.parse(x["price"], positive=True))
                except money.MoneyError:
                    raise Invalid(R("MONEY_INVALID"), field="price") from None
                lanes.add(f["lane"])
                items.append({"item_id": derived_id("itm", job_id, f["finding_id"]), "finding_id": f["finding_id"],
                              "check_code": f["check_code"], "lane": f["lane"], "resource": dict(f["resource"]),
                              "price": price})
            total = money.fmt(money.total(i["price"] for i in items))
            quote = {"job_id": job_id, "client_id": body["client_id"], "currency": body.get("currency", "USD"),
                     "total": total, "items": sorted([i["item_id"], i["finding_id"], i["check_code"], i["price"]]
                                                     for i in items)}
            data = {"job_id": job_id, "client_id": body["client_id"], "items": items, "quote": quote,
                    "quote_sha256": sha(quote), "team": team_for(lanes)}
            self._commit("job_created", self._req(data, actor, rk, body, job_id), actor,
                         evidence=("job_created", f"job:{job_id}",
                                   {"job_id": job_id, "client_id": body["client_id"], "items": len(items),
                                    "quote_sha256": data["quote_sha256"], "team": data["team"]}, (actor, rk)))
            return self.job_view(job_id)

    def _a_job_created(self, d, at):
        items = {}
        for i in d["items"]:
            items[i["item_id"]] = {**i, "status": "open", "connection_id": None, "ops": None, "result": None,
                                   "redetection": None, "unknown_ticks": 0, "settled_at": None, "evidence": []}
            self.items[i["item_id"]] = d["job_id"]
            self.findings[i["finding_id"]].update(status="in_job", job_id=d["job_id"])
        self.jobs[d["job_id"]] = {"job_id": d["job_id"], "client_id": d["client_id"], "items": items,
                                  "quote": d["quote"], "quote_sha256": d["quote_sha256"], "team": d["team"],
                                  "status": "quoted", "created_at": at, "accepted_at": None, "payment": None,
                                  "plan_version": 0, "plan_sha256": None, "plan_team": None, "approval": None,
                                  "apply_epoch": None, "report": None, "refund_id": None, "closed_at": None}

    def job_view(self, job_id: str) -> dict:
        with self.lock:
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            out = json.loads(json.dumps(j))
            out["items"] = [out["items"][k] for k in sorted(out["items"])]
            out["plan_sha256_now"] = self._plan_sha(j) if j["plan_version"] else None
            return out

    def jobs_view(self, client_id, status) -> list:
        with self.lock:
            return [self.job_view(k) for k in sorted(self.jobs, key=lambda k: (self.jobs[k]["created_at"], k))
                    if (client_id is None or self.jobs[k]["client_id"] == client_id)
                    and (status is None or self.jobs[k]["status"] == status)][:500]

    def accept_quote(self, session_token, job_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            self.client_of_session(session_token, j["client_id"])
            rk = self.rk("quote_accept", job_id, body)
            if self._idem("hub", rk, body):
                return self.job_view(job_id)
            if j["status"] != "quoted":
                raise Conflict(R("JOB_STATE"))
            if body["sha256"] != j["quote_sha256"]:
                raise Conflict(R("QUOTE_HASH_MISMATCH"))
            self._commit("quote_accepted", self._req({"job_id": job_id}, "hub", rk, body, job_id), "hub",
                         evidence=("quote_accepted", f"job:{job_id}",
                                   {"job_id": job_id, "quote_sha256": j["quote_sha256"]}, ("hub", rk)))
            payload = {"job_id": job_id, "client_id": j["client_id"], "amount": j["quote"]["total"],
                       "currency": j["quote"]["currency"], "quote_sha256": j["quote_sha256"]}
        # the up-front invoice (Stripe, through Finance): outside the lock; the stand-in is not wired
        try:
            got = self.ports.finance.request_invoice(job_id, payload)
            status = got.status
        except Exception:                                    # noqa: BLE001 — a raising port is unavailable
            status = "unavailable"
        # answered, never stored: Finance's own record is the truth about the invoice
        return {**self.job_view(job_id), "invoice": status}

    def _a_quote_accepted(self, d, at):
        self.jobs[d["job_id"]].update(status="accepted", accepted_at=at)

    def payment_event(self, actor: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            facts = {k: body[k] for k in ("finance_event_id", "job_id", "kind", "amount", "currency", "quote_sha256")}
            seen = self.finance_events.get(body["finance_event_id"])
            if seen is not None:
                if seen != facts:
                    raise Conflict(R("FINANCE_EVENT_REUSED"))
                return self.job_view(body["job_id"])
            rk = self.rk("payment", body["finance_event_id"], body)
            if self._idem(actor, rk, body):
                return self.job_view(body["job_id"])
            if body["currency"] != "USD":
                raise Invalid(R("CURRENCY_NOT_SUPPORTED"))
            j = self._get(self.jobs, body["job_id"], "JOB_NOT_FOUND")
            if j["status"] == "quoted":
                raise Conflict(R("QUOTE_NOT_ACCEPTED"))
            if body["quote_sha256"] != j["quote_sha256"] or body["amount"] != j["quote"]["total"]:
                raise Conflict(R("PAYMENT_MISMATCH"))
            if j["status"] == "closed" and not j.get("payment") and not j.get("orphan_payment"):
                return self._orphaned_payment(actor, rk, body, facts, j)
            if j["status"] != "accepted":
                raise Conflict(R("JOB_STATE"))
            self._commit("payment_confirmed", self._req({"facts": facts}, actor, rk, body, body["job_id"]), actor,
                         evidence=("payment_confirmed", f"job:{body['job_id']}",
                                   {"job_id": body["job_id"], "finance_event_id": body["finance_event_id"],
                                    "terms_sha256": sha([body["amount"], body["currency"]]),
                                    "quote_sha256": body["quote_sha256"]}, (actor, rk)))
            return self.job_view(body["job_id"])

    def _orphaned_payment(self, actor: str, rk: str, body: dict, facts: dict, j: dict) -> dict:
        """AEGIS round 1 Low: the client paid a quote whose job was already closed unpaid (cancelled, or every item
        revoked). The money is real: the Finance event is recorded and a full refund is proposed for Andre (task),
        never a bare refusal that leaves the payment nobody's."""
        job_id = j["job_id"]
        refund_id = derived_id("rfd", job_id)
        terms = {"refund_id": refund_id, "job_id": job_id, "client_id": j["client_id"],
                 "items": sorted(j["items"]), "amount": body["amount"], "currency": body["currency"],
                 "finance_event_id": body["finance_event_id"], "report_sha256": None}
        rsha = sha(terms)
        tasks = [self._task("decide", refund_id, "ORPHANED_PAYMENT", "ORPHANED_PAYMENT")]
        self._commit("payment_orphaned", self._req({"facts": facts, "terms": terms, "refund_sha256": rsha,
                                                    "tasks": tasks}, actor, rk, body, job_id), actor,
                     evidence=[("payment_orphaned", f"job:{job_id}",
                                {"job_id": job_id, "finance_event_id": body["finance_event_id"],
                                 "terms_sha256": sha([body["amount"], body["currency"]]),
                                 "quote_sha256": body["quote_sha256"]}, (actor, rk)),
                               ("refund_proposed", f"refund:{refund_id}",
                                {"refund_id": refund_id, "job_id": job_id, "items": len(terms["items"]),
                                 "terms_sha256": rsha}, (actor, rk, "refund"))])
        return self.job_view(job_id)

    def _a_payment_orphaned(self, d, at):
        f, t = d["facts"], d["terms"]
        self.finance_events[f["finance_event_id"]] = dict(f)
        self.jobs[f["job_id"]]["orphan_payment"] = {"finance_event_id": f["finance_event_id"], "amount": f["amount"],
                                                    "at": at}
        self.refunds[t["refund_id"]] = {**t, "refund_sha256": d["refund_sha256"], "status": "proposed",
                                        "proposed_at": at, "approved_at": None, "finance_ref": None,
                                        "unknown_ticks": 0}

    def _a_payment_confirmed(self, d, at):
        f = d["facts"]
        self.finance_events[f["finance_event_id"]] = dict(f)
        self.jobs[f["job_id"]].update(status="paid", payment={"finance_event_id": f["finance_event_id"],
                                                              "amount": f["amount"], "at": at})

    # ------------------------------------------------------------------ fire teams and plans

    def _paid(self, j: dict) -> None:
        if j["status"] in ("quoted", "accepted"):
            raise Conflict(R("PAYMENT_REQUIRED"))
        if j["status"] not in ("paid", "planned"):
            raise Conflict(R("JOB_STATE"))
        if not j.get("payment"):
            raise Conflict(R("PAYMENT_REQUIRED"))

    def brief(self, job_id: str) -> dict:
        """What the fire team is given: findings, checks, lanes and resources (connector, account, target). Never a
        token, a vault reference or a snapshot of client data (the engineers never hold client credentials)."""
        j = self.jobs[job_id]
        items = []
        for it in sorted(j["items"].values(), key=lambda x: x["item_id"]):
            if it["status"] not in ("open", "planned"):
                continue
            c = self.connections[it["resource"]["connection_id"]]
            items.append({"item_id": it["item_id"], "finding_id": it["finding_id"], "check_code": it["check_code"],
                          "lane": it["lane"], "connection_id": c["connection_id"], "connector": c["connector"],
                          "account_ref": c["account_ref"], "target": it["resource"]["target"],
                          "allowed_ops": sorted(self.connectors[c["connector"]].ops)})
        return {"job_id": job_id, "client_id": j["client_id"], "team": j["team"],
                "team_lanes": list(FIRE_TEAMS[j["team"]]), "items": items}

    def brief_with_before(self, job_id: str) -> dict:
        """The brief plus each item's CURRENT values (AEGIS round 1 Low: plans written against reality). They are read
        through the connector's read API outside the lock — reads only, a write is refused — and are client business
        data, not secrets: still, any value the secrets scan flags is withheld."""
        import secrets_guard
        from connectors.base import UnknownState
        from executor import Halt
        from ports import ConnView
        with self.lock:
            self._gate()
            self._paid(self._get(self.jobs, job_id, "JOB_NOT_FOUND"))
            brief = self.brief(job_id)
            views = {}
            for it in brief["items"]:
                c = self.connections[it["connection_id"]]
                views[it["item_id"]] = ConnView(c["connection_id"], c["client_id"], c["connector"], c["account_ref"],
                                                c["token_ref"])
        for it in brief["items"]:
            connector = self.connectors[it["connector"]]
            keys = [(it["target"], f) for op, fields in CHECK_OPS.get(it["check_code"], {}).items()
                    if op in connector.ops for f in fields if not f.endswith(":")]
            it["before"], it["before_status"] = None, "unavailable"
            if not keys:
                it["before_status"] = "not_applicable"
                continue
            if not self.ports.transport.wired:
                continue
            conn = views[it["item_id"]]

            def call(req, conn=conn):
                with self.lock:
                    if conn.connection_id in self.revoked_now or conn.client_id in self.revoked_clients_now:
                        raise Halt("CONNECTION_REVOKED")
                if req.is_write:
                    raise Halt("CONNECTOR_NOT_WIRED")          # the brief never writes anything
                try:
                    ans = self.ports.transport.call(conn, req)
                except Exception:                              # noqa: BLE001
                    ans = None
                return ans if isinstance(ans, HttpAnswer) else HttpAnswer(0, None)
            try:
                values = connector.read(conn.account_ref, keys, {}, call)
            except (UnknownState, Halt):
                it["before_status"] = "unknown"
                continue
            rows = sorted([k[0], k[1], v] for k, v in values.items())
            safe = [r for r in rows if not secrets_guard.secret_value(r[2])]
            it["before"] = safe
            it["before_status"] = "read" if len(safe) == len(rows) else "read_some_withheld"
        return brief

    def engage(self, actor: str, job_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            self._paid(j)
            if not self.ports.engineers.wired:
                raise Unavailable(R("MODEL_NOT_WIRED"))
            team = j["team"]
        brief = self.brief_with_before(job_id)
        try:
            proposed = self.ports.engineers.propose(team, brief)
        except ModelNotWired:
            raise Unavailable(R("MODEL_NOT_WIRED")) from None
        except Exception:                                    # noqa: BLE001
            raise Unavailable(R("ENGINEERS_FAILED")) from None
        if not isinstance(proposed, list):
            raise Unavailable(R("ENGINEERS_FAILED"))
        # the proposal is untrusted data: the same secrets scan, strict model and validation as POST /plan
        import models as m
        import secrets_guard
        from pydantic import ValidationError
        if secrets_guard.forbidden_keys(proposed):
            raise Invalid(R("FORBIDDEN_FIELD"))
        if secrets_guard.secret_value(proposed):
            raise Invalid(R("SECRET_REFUSED"))
        try:
            plan = m.PlanSubmit.model_validate({"request_id": body["request_id"], "team": team, "items": proposed})
        except ValidationError:
            raise Invalid(R("INVALID"), field="proposal") from None
        return self.submit_plan("fire_team", job_id, plan.model_dump(mode="json"))

    def submit_plan(self, actor: str, job_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            rk = self.rk("plan", job_id, body)
            if self._idem(actor, rk, body):
                return self.job_view(job_id)
            self._paid(j)
            if body["team"] != j["team"]:
                raise Invalid(R("INVALID"), field="team")
            want = {k for k, it in j["items"].items() if it["status"] in ("open", "planned")}
            got = [x["item_id"] for x in body["items"]]
            if len(set(got)) != len(got) or any(i not in j["items"] for i in got):
                raise NotFound(R("ITEM_NOT_FOUND"))
            if set(got) != want:
                raise Conflict(R("PLAN_INCOMPLETE"))
            seen: set = set()
            planned = []
            for x in body["items"]:
                it = j["items"][x["item_id"]]
                conn = self._conn_for(j["client_id"], x["connection_id"])
                if x["connection_id"] != it["resource"]["connection_id"]:
                    raise Invalid(R("RESOURCE_MISMATCH"))
                if conn["status"] != "active" or conn["connection_id"] in self.revoked_now:
                    raise Conflict(R("CONNECTION_REVOKED"))
                connector = self.connectors[conn["connector"]]
                if conn["connector"] not in CHECKS[it["check_code"]][1]:
                    raise Invalid(R("CHECK_CONNECTOR_MISMATCH"))
                if len(x["ops"]) > self.settings.max_ops_per_item:
                    raise Invalid(R("OPS_TOO_MANY"))
                ops = []
                for op in x["ops"]:
                    try:
                        connector.validate(conn["account_ref"], op)
                    except OpRefused as exc:
                        raise Invalid(R(exc.code)) from None
                    # AEGIS round 1 M1: the change set is bound to its finding — the finding's resource, and only
                    # the operations and fields its check allows
                    if op["target"] != it["resource"]["target"]:
                        raise Invalid(R("RESOURCE_MISMATCH"))
                    if not op_allowed_for(it["check_code"], op["op"], op["field"]):
                        raise Invalid(R("OP_NOT_FOR_CHECK"))
                    key = resource_key(conn["connector"], conn["account_ref"], op["target"]) + "|" + op["field"]
                    if key in seen:
                        raise Invalid(R("OP_DUPLICATE_KEY"))
                    seen.add(key)
                    ops.append({k: op[k] for k in ("op", "target", "field", "before", "after")})
                planned.append({"item_id": x["item_id"], "connection_id": conn["connection_id"], "ops": ops})
            _no_redirect_chain(planned, self.connections)
            version = j["plan_version"] + 1
            data = {"job_id": job_id, "version": version, "team": body["team"], "items": planned}
            shadow = {**j, "plan_version": version, "plan_team": body["team"],
                      "items": {k: {**v, **next(({"connection_id": p["connection_id"], "ops": p["ops"]}
                                                 for p in planned if p["item_id"] == k), {})}
                                for k, v in j["items"].items()}}
            plan_sha = self._plan_sha(shadow)
            data["plan_sha256"] = plan_sha
            self._commit("plan_submitted", self._req(data, actor, rk, body, job_id), actor,
                         evidence=("plan_submitted", f"job:{job_id}",
                                   {"job_id": job_id, "version": version, "plan_sha256": plan_sha,
                                    "items": len(planned), "ops": sum(len(p["ops"]) for p in planned)}, (actor, rk)))
            return self.job_view(job_id)

    def _a_plan_submitted(self, d, at):
        j = self.jobs[d["job_id"]]
        for p in d["items"]:
            j["items"][p["item_id"]].update(connection_id=p["connection_id"], ops=p["ops"], status="planned")
        j.update(plan_version=d["version"], plan_team=d["team"], plan_sha256=d["plan_sha256"], approval=None,
                 status="planned")

    def _plan_sha(self, j: dict) -> str:
        """The fix plan the client approves, recomputed from current state every time it is checked."""
        items = []
        for it in sorted(j["items"].values(), key=lambda x: x["item_id"]):
            if it.get("ops") is None:
                continue
            c = self.connections.get(it.get("connection_id") or "", {})
            items.append({"item_id": it["item_id"], "finding_id": it["finding_id"], "check_code": it["check_code"],
                          "connection_id": it.get("connection_id"), "connector": c.get("connector"),
                          "account_ref": c.get("account_ref"), "ops": it["ops"]})
        return sha({"job_id": j["job_id"], "client_id": j["client_id"], "quote_sha256": j["quote_sha256"],
                    "version": j["plan_version"], "team": j.get("plan_team"), "items": items})

    def approve_plan(self, session_token, job_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            self.client_of_session(session_token, j["client_id"])
            rk = self.rk("plan_approve", job_id, body)
            if self._idem("hub", rk, body):
                return self.job_view(job_id)
            if j["status"] != "planned":
                raise Conflict(R("PLAN_REQUIRED") if j["status"] in ("paid",) else R("JOB_STATE"))
            now_sha = self._plan_sha(j)
            if body["sha256"] != now_sha or now_sha != j["plan_sha256"]:
                raise Conflict(R("PLAN_HASH_MISMATCH"))
            self._commit("plan_approved", self._req({"job_id": job_id, "plan_sha256": now_sha,
                                                     "version": j["plan_version"]}, "hub", rk, body, job_id), "hub",
                         evidence=("plan_approved", f"job:{job_id}",
                                   {"job_id": job_id, "plan_sha256": now_sha, "version": j["plan_version"]},
                                   ("hub", rk)))
            return self.job_view(job_id)

    def _a_plan_approved(self, d, at):
        self.jobs[d["job_id"]].update(status="approved", approval={"plan_sha256": d["plan_sha256"],
                                                                   "version": d["version"], "at": at})

    def cancel_job(self, job_id: str, body: dict) -> dict:
        """Andre stops a job before it is applied: every open item is cancelled (unfixed: refundable once paid)."""
        with self.lock:
            self._gate()
            j = self._get(self.jobs, job_id, "JOB_NOT_FOUND")
            rk = self.rk("cancel", job_id, body)
            if self._idem("andre", rk, body):
                return self.job_view(job_id)
            if j["status"] not in ("quoted", "accepted", "paid", "planned", "approved"):
                raise Conflict(R("JOB_STATE"))
            self._commit("job_cancelled", self._req({"job_id": job_id}, "andre", rk, body, job_id), "andre",
                         evidence=("job_cancelled", f"job:{job_id}", {"job_id": job_id}, ("andre", rk)))
            self._settle_if_done(job_id)
            return self.job_view(job_id)

    def _a_job_cancelled(self, d, at):
        j = self.jobs[d["job_id"]]
        for it in j["items"].values():
            if it["status"] not in TERMINAL:
                it.update(status="cancelled", settled_at=at)
        j["status"] = "settling" if j.get("payment") else "closed"
        if not j.get("payment"):
            j["closed_at"] = at
        for it in j["items"].values():
            f = self.findings.get(it["finding_id"])
            if f is not None and not j.get("payment"):
                f.update(status="open", job_id=None)
