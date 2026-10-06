"""Big pursuits: enterprise and government bids, RFP / RFQ responses and formal pitches (ADR 0016 decisions 7-14 and
18). A mixin of BizDevService.

Stages: ``identified -> qualifying -> responding -> submitted -> won | lost``; ``qualifying -> no_bid``; any open stage
-> ``withdrawn`` (Andre). Qualification is the agent's (i01 recommends); a ``bid`` decision is always Andre's, bound to
the exact qualification he saw; ``no_bid`` may be recorded by the agent. A deadline is required for every kind but a
formal pitch, is stored (never taken from a later request) and only Andre moves it. A government bid carries the i05
checklist, every item attested by Andre one at a time by its hash; sensitive items open review tasks. Any deal whose
counterparty group aggregates over the threshold needs Andre's deal approval, bound to the deal's value, the
aggregate and the group (i07), re-checked at every gate. A pursuit is won only by Andre, after a submission was
delivered; the win hands off to Onboarding and Finance as ``pending_delivery``."""

from __future__ import annotations

from typing import Optional

import config as config_mod
import money
from errors import Conflict, FounderRefused, Invalid, NotFound, Unavailable
from intelligences import i01_qualification, i02_identity, i03_deadline, i04_assembly, i05_gov_checklist, \
    i06_sensitivity, i07_deal_threshold
from ledger import derived_id
from ports import NotWired
from reasons import R

OPEN_STAGES = ("identified", "qualifying", "responding", "submitted")
DEAL_VALUE_MAX = money.D("1000000000.00")       # AEGIS round 1 M3: no deal is worth more; above it is 422


def deal_value(raw, positive: bool) -> str:
    """A pursuit or partner-deal value: the canonical money string, at most DEAL_VALUE_MAX (else 422)."""
    try:
        v = money.parse(raw, positive=positive)
    except money.MoneyError:
        raise Invalid(R("MONEY_INVALID")) from None
    if v > DEAL_VALUE_MAX:
        raise Invalid(R("VALUE_INVALID"))
    return money.fmt(v)
CLOSED_STAGES = ("won", "lost", "no_bid", "withdrawn")
NEEDS_DEADLINE = ("rfp", "rfq", "enterprise_bid", "government_bid")


class PursuitsMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_pursuit_opened(self, d, at):
        p = {k: d[k] for k in ("pursuit_id", "brand", "kind", "title", "counterparty", "keys", "value", "deadline",
                               "notes", "source_ref", "flags", "checklist")}
        for item in p["checklist"]:
            item.update(attested_at=None)
        p.update(stage="identified", opened_at=at, updated_at=at, opened_by=d["actor"], qualification=None, bid=None,
                 deal_approval=None, closed_reason=None, handoff_ids=[])
        self.pursuits[d["pursuit_id"]] = p

    def _a_qualification_recorded(self, d, at):
        p = self.pursuits[d["pursuit_id"]]
        p.update(stage="qualifying", updated_at=at, qualification={k: d[k] for k in ("answers", "result",
                                                                                      "qualification_sha256")})

    def _a_bid_decided(self, d, at):
        p = self.pursuits[d["pursuit_id"]]
        p["bid"] = {"decision": d["decision"], "by": d["actor"], "qualification_sha256": d["qualification_sha256"],
                    "at": at}
        p.update(stage="responding" if d["decision"] == "bid" else "no_bid", updated_at=at)
        if d["decision"] == "no_bid":
            p["closed_reason"] = "no_bid"

    def _a_pursuit_value_set(self, d, at):
        self.pursuits[d["pursuit_id"]].update(value=d["value"], updated_at=at)

    def _a_pursuit_deadline_set(self, d, at):
        self.pursuits[d["pursuit_id"]].update(deadline=d["deadline"], updated_at=at)

    def _a_deal_approved(self, d, at):
        table = self.pursuits if d["deal_id"] in self.pursuits else self.partner_deals
        table[d["deal_id"]]["deal_approval"] = {"binding_sha256": d["binding_sha256"], "at": at}

    def _a_checklist_attested(self, d, at):
        p = self.pursuits[d["pursuit_id"]]
        for item in p["checklist"]:
            if item["item_id"] == d["item_id"]:
                item["attested_at"] = at
        tid = d.get("close_task")
        if tid and tid in self.tasks and self.tasks[tid]["status"] == "open":
            self.tasks[tid].update(status="closed", closed_at=at, outcome="attested")

    def _a_checklist_extended(self, d, at):
        p = self.pursuits[d["pursuit_id"]]
        for item in d["items"]:
            p["checklist"].append({**item, "attested_at": None})
        p["updated_at"] = at

    def _a_pursuit_won(self, d, at):
        p = self.pursuits[d["pursuit_id"]]
        p.update(stage="won", updated_at=at, closed_reason="won")
        for h in d["handoffs"]:
            self.handoffs[h["handoff_id"]] = {**h, "status": "pending_delivery", "created_at": at,
                                              "delivered_at": None, "reference": None}
            p["handoff_ids"].append(h["handoff_id"])

    def _a_pursuit_lost(self, d, at):
        self.pursuits[d["pursuit_id"]].update(stage="lost", updated_at=at, closed_reason=d["reason_code"])

    def _a_pursuit_withdrawn(self, d, at):
        self.pursuits[d["pursuit_id"]].update(stage="withdrawn", updated_at=at, closed_reason="withdrawn")
        for sid in d.get("cancel_submissions", ()):
            self.submissions[sid].update(status="cancelled", reason="PURSUIT_CLOSED", updated_at=at)

    def _a_handoff_delivered(self, d, at):
        self.handoffs[d["handoff_id"]].update(status="delivered", delivered_at=at, reference=d.get("reference"))

    def _a_deadline_swept(self, d, at):
        for sid in d.get("cancel", ()):
            self.submissions[sid].update(status="cancelled", reason="DEADLINE_PASSED", updated_at=at)

    # ------------------------------------------------------------------------------------------------ views

    def pursuit_view(self, p: dict) -> dict:
        out = {k: v for k, v in p.items()}
        out["checklist"] = [dict(i) for i in p["checklist"]]
        out["deadline_passed"] = bool(p["deadline"]) and i03_deadline.passed(p["deadline"], self.now())
        out["deal_gate"] = self._deal_gate(p["pursuit_id"])
        out["responses"] = sorted(r["response_id"] for r in self.responses.values() if r["pursuit_id"] ==
                                  p["pursuit_id"])
        return out

    def pursuit(self, pid: str) -> dict:
        with self.lock:
            return self.pursuit_view(self._get(self.pursuits, pid, "PURSUIT_NOT_FOUND"))

    def pursuits_view(self, stage: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.pursuit_view(p) for p in sorted(self.pursuits.values(), key=lambda x: (x["opened_at"],
                                                                                                x["pursuit_id"]))
                    if stage is None or p["stage"] == stage][:1000]

    # ------------------------------------------------------------------------------------------------ deal gate

    def _counterparty(self, cp: dict) -> dict:
        dom = i02_identity.domain(cp["domain"])
        org = i02_identity.org_key(cp["name"])
        if dom is None or org is None:
            raise Invalid(R("COUNTERPARTY_INVALID"))
        reg = config_mod.registrable(dom)
        return {"counterparty": {"ref": cp["ref"], "name": cp["name"], "domain": dom},
                "keys": i07_deal_threshold.keys(cp["ref"], reg, org)}

    def _delivered(self, pid: str) -> bool:
        """A submission of this pursuit was delivered, or may have been (``sending``)."""
        return any(s["pursuit_id"] == pid and s["status"] in ("sending", "submitted") for s in self.submissions.values())

    def _deal_pool(self) -> dict:
        pool = {}
        for p in self.pursuits.values():
            status = p["stage"]
            if status in ("lost", "withdrawn", "no_bid") and self._delivered(p["pursuit_id"]):
                status = "delivered"            # AEGIS round 1 H2: a delivered bid stays in the aggregate
            pool[p["pursuit_id"]] = {"keys": p["keys"], "value": p["value"], "opened_at": p["opened_at"],
                                     "status": status}
        for d in self.partner_deals.values():
            pool[d["deal_id"]] = {"keys": d["keys"], "value": d["deal_value"], "opened_at": d["opened_at"],
                                  "status": d["status"]}
        return pool

    def _deal_gate(self, deal_id: str) -> dict:
        pool = self._deal_pool()
        threshold = money.fmt(self.settings.deal_approval_threshold)
        try:
            total, members = i07_deal_threshold.aggregate(deal_id, pool, self.now(),
                                                          self.settings.aggregation_window_days)
        except i07_deal_threshold.Overflow:
            # AEGIS round 1 M3: an aggregate too large for money fails closed (needs Andre, never approvable), never 500
            members = i07_deal_threshold.group(deal_id, pool, self.now(), self.settings.aggregation_window_days)
            return {"aggregate": None, "members": members, "needs_andre": True, "binding_sha256": None,
                    "approved": False, "threshold": threshold, "overflow": True}
        needs = i07_deal_threshold.needs_andre(total, self.settings.deal_approval_threshold)
        binding = i04_assembly.sha({"deal_id": deal_id, "value": pool[deal_id]["value"], "aggregate": money.fmt(total),
                                    "members": members})
        rec = self.pursuits.get(deal_id) or self.partner_deals.get(deal_id)
        appr = rec.get("deal_approval")
        approved = (not needs) or bool(appr and appr["binding_sha256"] == binding)
        return {"aggregate": money.fmt(total), "members": members, "needs_andre": needs, "binding_sha256": binding,
                "approved": approved, "threshold": threshold}

    def approve_deal(self, deal_id: str, body: dict) -> dict:
        """Andre's deal approval: exactly the binding the gate shows now (value, aggregate, group)."""
        with self.lock:
            self._gate()
            rk = self.rk("deal_approve", deal_id, body)
            if self._idem("andre", rk, body):
                return self._deal_gate(deal_id)
            if deal_id not in self.pursuits and deal_id not in self.partner_deals:
                raise NotFound(R("DEAL_NOT_FOUND"))
            rec = self.pursuits.get(deal_id) or self.partner_deals[deal_id]
            if rec.get("stage", rec.get("status")) in CLOSED_STAGES + ("lost",):
                raise Conflict(R("DEAL_CLOSED"))
            g = self._deal_gate(deal_id)
            if not g["needs_andre"]:
                raise Conflict(R("DEAL_APPROVAL_NOT_NEEDED"))
            if g["binding_sha256"] is None or body["binding_sha256"] != g["binding_sha256"]:
                raise Conflict(R("DEAL_APPROVAL_STALE"))
            data = {"deal_id": deal_id, "binding_sha256": g["binding_sha256"]}
            self._commit("deal_approved", self._req(data, "andre", rk, body, deal_id), "andre",
                         evidence=("deal_approved", f"deal:{deal_id}", {**data, "members": g["members"]},
                                   ("andre", rk)))
            return self._deal_gate(deal_id)

    # ------------------------------------------------------------------------------------------------ open

    def open_pursuit(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("pursuit_open", body["counterparty"]["ref"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.pursuit_view(self.pursuits[prev[1]])
            cp = self._counterparty(body["counterparty"])
            value = deal_value(body["value"], positive=False)
            deadline = body.get("deadline")
            if deadline is None and body["kind"] in NEEDS_DEADLINE:
                raise Invalid(R("DEADLINE_REQUIRED"))
            if deadline is not None and i03_deadline.passed(deadline, self.now()):
                raise Invalid(R("DEADLINE_PASSED"))
            pid = derived_id("pur", caller, rk)
            checklist = []
            if body["kind"] == "government_bid":
                try:
                    checklist = i05_gov_checklist.build(pid, [dict(x) for x in body.get("checklist") or []])
                except ValueError as exc:
                    raise Invalid(R(str(exc))) from None
                for item in checklist:
                    item["item_id"] = derived_id("chk", pid, item["code"], item["label"])
            elif body.get("checklist"):
                raise Invalid(R("NOT_A_GOVERNMENT_BID"))
            flags = i06_sensitivity.flags(body["title"], body.get("notes") or "")
            tasks = [self._task("checklist_sensitive", f"pursuit:{pid}", i["item_id"], i["code"])
                     for i in checklist if i["sensitive"]]
            tasks += [self._task("sensitivity_flag", f"pursuit:{pid}", f, f) for f in flags]
            data = {"pursuit_id": pid, "brand": body["brand"], "kind": body["kind"], "title": body["title"],
                    **cp, "value": value, "deadline": deadline, "notes": body.get("notes"),
                    "source_ref": body.get("source_ref"), "flags": flags, "checklist": checklist, "tasks": tasks}
            ev = ("pursuit_opened", f"pursuit:{pid}",
                  {"pursuit_id": pid, "brand": body["brand"], "kind": body["kind"],
                   "counterparty_keys_sha256": i04_assembly.sha({"keys": cp["keys"]}),
                   "terms_sha256": i04_assembly.sha({"value": value, "deadline": deadline}),
                   "checklist": [i["item_sha256"] for i in checklist], "flags": flags}, (caller, rk))
            self._commit("pursuit_opened", self._req(data, caller, rk, body, pid), caller, evidence=ev)
            return self.pursuit_view(self.pursuits[pid])

    def import_pursuits(self, caller: str, body: dict) -> dict:
        """Bid-portal sourcing is a NOT_BUILT port: no fetching code exists here (Andre, Oct 6)."""
        self._gate()
        try:
            self.ports.bid_source.fetch(body["limit"])
        except NotWired:
            raise Unavailable(R("SOURCE_NOT_WIRED")) from None
        except Exception:      # noqa: BLE001 - a port that raises is unavailable
            raise Unavailable(R("SOURCE_NOT_WIRED")) from None
        raise Unavailable(R("SOURCE_NOT_WIRED"))       # even a wired source: ingestion is not built (unlock list)

    # ------------------------------------------------------------------------------------------------ qualification

    def _open(self, pid: str) -> dict:
        p = self._get(self.pursuits, pid, "PURSUIT_NOT_FOUND")
        if p["stage"] in CLOSED_STAGES:
            raise Conflict(R("PURSUIT_CLOSED"))
        return p

    def qualify(self, caller: str, pid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("qualify", pid, body)
            if self._idem(caller, rk, body):
                return self.pursuit_view(self.pursuits[pid])
            p = self._open(pid)
            if p["stage"] not in ("identified", "qualifying"):
                raise Conflict(R("STAGE_NOT_ALLOWED"))
            answers = {c: body[c] for c in i01_qualification.CRITERIA}
            result = i01_qualification.recommend(answers)
            qsha = i04_assembly.sha({"pursuit_id": pid, "answers": answers, "result": result})
            data = {"pursuit_id": pid, "answers": answers, "result": result, "qualification_sha256": qsha}
            self._commit("qualification_recorded", self._req(data, caller, rk, body, pid), caller,
                         evidence=("qualification_recorded", f"pursuit:{pid}",
                                   {"pursuit_id": pid, "qualification_sha256": qsha,
                                    "recommendation": result["recommendation"]}, (caller, rk)))
            return self.pursuit_view(p)

    def decide_bid(self, caller: str, pid: str, body: dict, andre: bool) -> dict:
        """``bid`` is Andre's alone; ``no_bid`` may be recorded by the agent. Either names the exact qualification."""
        actor = "andre" if andre else caller
        with self.lock:
            self._gate()
            rk = self.rk("bid_decision", pid, body)
            if self._idem(actor, rk, body):
                return self.pursuit_view(self.pursuits[pid])
            if body["decision"] == "bid" and not andre:
                raise FounderRefused(R("ANDRE_APPROVAL_REQUIRED"))
            p = self._open(pid)
            if p["stage"] != "qualifying" or p["qualification"] is None:
                raise Conflict(R("QUALIFICATION_REQUIRED"))
            if body["qualification_sha256"] != p["qualification"]["qualification_sha256"]:
                raise Conflict(R("QUALIFICATION_STALE"))
            data = {"pursuit_id": pid, "decision": body["decision"],
                    "qualification_sha256": p["qualification"]["qualification_sha256"]}
            self._commit("bid_decided", self._req(data, actor, rk, body, pid), actor,
                         evidence=("bid_decided", f"pursuit:{pid}",
                                   {**data, "recommendation": p["qualification"]["result"]["recommendation"]},
                                   (actor, rk)))
            return self.pursuit_view(p)

    # ------------------------------------------------------------------------------------------------ value, deadline

    def set_pursuit_value(self, caller: str, pid: str, body: dict) -> dict:
        """A new value un-approves the deal (the approval binds the value) and is re-checked at every gate."""
        with self.lock:
            self._gate()
            rk = self.rk("pursuit_value", pid, body)
            if self._idem(caller, rk, body):
                return self.pursuit_view(self.pursuits[pid])
            p = self._open(pid)
            if p["stage"] == "submitted":
                raise Conflict(R("STAGE_NOT_ALLOWED"))
            value = deal_value(body["value"], positive=False)
            data = {"pursuit_id": pid, "value": value}
            self._commit("pursuit_value_set", self._req(data, caller, rk, body, pid), caller,
                         evidence=("deal_value_set", f"pursuit:{pid}",
                                   {"deal_id": pid, "terms_sha256": i04_assembly.sha({"value": value})}, (caller, rk)))
            return self.pursuit_view(p)

    def set_deadline(self, pid: str, body: dict) -> dict:
        """Andre only (a buyer's addendum): the agent can never move a deadline."""
        with self.lock:
            self._gate()
            rk = self.rk("pursuit_deadline", pid, body)
            if self._idem("andre", rk, body):
                return self.pursuit_view(self.pursuits[pid])
            p = self._open(pid)
            if p["stage"] == "submitted":
                raise Conflict(R("STAGE_NOT_ALLOWED"))
            if i03_deadline.passed(body["deadline"], self.now()):
                raise Invalid(R("DEADLINE_PASSED"))
            data = {"pursuit_id": pid, "deadline": body["deadline"]}
            self._commit("pursuit_deadline_set", self._req(data, "andre", rk, body, pid), "andre",
                         evidence=("deadline_set", f"pursuit:{pid}",
                                   {"pursuit_id": pid, "terms_sha256": i04_assembly.sha({"deadline": body["deadline"]})},
                                   ("andre", rk)))
            return self.pursuit_view(p)

    # ------------------------------------------------------------------------------------------------ checklist

    def attest(self, pid: str, item_id: str, body: dict) -> dict:
        """Andre attests ONE item, naming its exact hash. Nothing is attested any other way."""
        with self.lock:
            self._gate()
            rk = self.rk("attest", f"{pid}.{item_id}", body)
            if self._idem("andre", rk, body):
                return self.pursuit_view(self.pursuits[pid])
            p = self._open(pid)
            if p["kind"] != "government_bid":
                raise Conflict(R("NOT_A_GOVERNMENT_BID"))
            item = next((i for i in p["checklist"] if i["item_id"] == item_id), None)
            if item is None:
                raise NotFound(R("CHECKLIST_ITEM_NOT_FOUND"))
            if body["item_sha256"] != item["item_sha256"] or \
                    i05_gov_checklist.item_sha256(pid, item["code"], item["label"]) != item["item_sha256"]:
                raise Conflict(R("CHECKLIST_HASH_MISMATCH"))
            if item["attested_at"] is not None:
                raise Conflict(R("CHECKLIST_ALREADY_ATTESTED"))
            close = self._task("checklist_sensitive", f"pursuit:{pid}", item_id, item["code"])["task_id"] \
                if item["sensitive"] else None
            data = {"pursuit_id": pid, "item_id": item_id, "item_sha256": item["item_sha256"], "close_task": close}
            self._commit("checklist_attested", self._req(data, "andre", rk, body, pid), "andre",
                         evidence=("checklist_attested", f"pursuit:{pid}",
                                   {"pursuit_id": pid, "item_id": item_id, "item_sha256": item["item_sha256"],
                                    "code": item["code"]}, ("andre", rk)))
            return self.pursuit_view(p)

    def extend_checklist(self, caller: str, pid: str, body: dict) -> dict:
        """A buyer's addendum adds certifications: items may be ADDED (never removed or edited) until submission;
        each new item is unattested and a sensitive one opens a task for Andre."""
        with self.lock:
            self._gate()
            rk = self.rk("checklist_extend", pid, body)
            if self._idem(caller, rk, body):
                return self.pursuit_view(self.pursuits[pid])
            p = self._open(pid)
            if p["kind"] != "government_bid":
                raise Conflict(R("NOT_A_GOVERNMENT_BID"))
            if p["stage"] == "submitted" or any(s["pursuit_id"] == pid and s["status"] in ("sending", "submitted")
                                                for s in self.submissions.values()):
                raise Conflict(R("STAGE_NOT_ALLOWED"))
            existing = [{"code": i["code"], "label": i["label"]} for i in p["checklist"]
                        if i["code"] not in i05_gov_checklist.BASELINE]
            try:
                full = i05_gov_checklist.build(pid, existing + [dict(x) for x in body["items"]])
            except ValueError as exc:
                raise Invalid(R(str(exc))) from None
            have = {i["item_sha256"] for i in p["checklist"]}
            new = [i for i in full if i["item_sha256"] not in have]
            if not new:
                return self.pursuit_view(p)
            for item in new:
                item["item_id"] = derived_id("chk", pid, item["code"], item["label"])
            tasks = [self._task("checklist_sensitive", f"pursuit:{pid}", i["item_id"], i["code"])
                     for i in new if i["sensitive"]]
            data = {"pursuit_id": pid, "items": new, "tasks": tasks}
            self._commit("checklist_extended", self._req(data, caller, rk, body, pid), caller,
                         evidence=("checklist_extended", f"pursuit:{pid}",
                                   {"pursuit_id": pid, "items": [i["item_sha256"] for i in new]}, (caller, rk)))
            return self.pursuit_view(p)

    # ------------------------------------------------------------------------------------------------ gates

    def _pursuit_submit_problem(self, p: dict) -> Optional[tuple]:
        """(error class, code) when a pursuit may not be submitted now, from CURRENT state; None when it may."""
        if p["stage"] in CLOSED_STAGES:
            return Conflict, "PURSUIT_CLOSED"
        if p["stage"] != "responding" or not p["bid"] or p["bid"]["decision"] != "bid":
            return Conflict, "BID_DECISION_REQUIRED"
        if p["kind"] in NEEDS_DEADLINE and not p["deadline"]:
            return Conflict, "DEADLINE_REQUIRED"
        if p["deadline"] and i03_deadline.passed(p["deadline"], self.now()):
            return Conflict, "DEADLINE_PASSED"
        if p["kind"] == "government_bid" and any(i["attested_at"] is None for i in p["checklist"]):
            return Conflict, "CHECKLIST_INCOMPLETE"
        if not self._deal_gate(p["pursuit_id"])["approved"]:
            return Conflict, "DEAL_APPROVAL_REQUIRED"
        return None

    # ------------------------------------------------------------------------------------------------ win, loss

    def pursuit_won(self, pid: str, body: dict) -> dict:
        """Andre only, after a submission was delivered; the deal gate is re-checked; hands off to Onboarding and
        Finance (``pending_delivery`` until their ports are wired)."""
        with self.lock:
            self._gate()
            rk = self.rk("pursuit_won", pid, body)
            if self._idem("andre", rk, body):
                return {**self.pursuit_view(self.pursuits[pid]), "handoffs": self._handoffs_of(pid)}
            p = self._open(pid)
            if p["stage"] != "submitted":
                raise Conflict(R("STAGE_NOT_ALLOWED"))
            if not self._deal_gate(pid)["approved"]:
                raise Conflict(R("DEAL_APPROVAL_REQUIRED"))
            sub = next((s for s in self.submissions.values() if s["pursuit_id"] == pid and s["status"] == "submitted"),
                       None)
            if sub is None:
                raise Conflict(R("STAGE_NOT_ALLOWED"))
            common = {"pursuit_id": pid, "brand": p["brand"], "counterparty_ref": p["counterparty"]["ref"],
                      "submission_id": sub["submission_id"], "content_sha256": sub["content_sha256"]}
            handoffs = [
                {"handoff_id": derived_id("hof", "onboarding", pid), "kind": "onboarding_create_client",
                 "payload": {**common, "kind": p["kind"]}},
                {"handoff_id": derived_id("hof", "finance", pid), "kind": "finance_invoice_draft",
                 "payload": {**common, "currency": "USD", "value": p["value"]}}]
            data = {"pursuit_id": pid, "handoffs": handoffs}
            self._commit("pursuit_won", self._req(data, "andre", rk, body, pid), "andre",
                         evidence=("pursuit_won", f"pursuit:{pid}",
                                   {"pursuit_id": pid, "submission_id": sub["submission_id"],
                                    "content_sha256": sub["content_sha256"],
                                    "terms_sha256": i04_assembly.sha({"value": p["value"]}),
                                    "handoffs": [h["handoff_id"] for h in handoffs]}, ("andre", rk)))
            ids = [h["handoff_id"] for h in handoffs]
        for hid in ids:
            self._deliver(hid)
        with self.lock:
            return {**self.pursuit_view(self.pursuits[pid]), "handoffs": self._handoffs_of(pid)}

    def pursuit_lost(self, caller: str, pid: str, body: dict, andre: bool = False) -> dict:
        """The agent may close a pursuit as lost only before anything went out. Once a submission was delivered (or
        may have been: ``sending``), it is Andre's alone (AEGIS round 1 H2); the pursuit stays in its counterparty's
        aggregate either way (``_deal_pool``)."""
        actor = "andre" if andre else caller
        with self.lock:
            self._gate()
            rk = self.rk("pursuit_lost", pid, body)
            if self._idem(actor, rk, body):
                return self.pursuit_view(self.pursuits[pid])
            p = self._open(pid)
            if p["stage"] not in ("responding", "submitted"):
                raise Conflict(R("STAGE_NOT_ALLOWED"))
            if not andre and (p["stage"] == "submitted" or self._delivered(pid)):
                raise FounderRefused(R("ANDRE_APPROVAL_REQUIRED"))
            data = {"pursuit_id": pid, "reason_code": body["reason_code"]}
            self._commit("pursuit_lost", self._req(data, actor, rk, body, pid), actor,
                         evidence=("pursuit_lost", f"pursuit:{pid}", data, (actor, rk)))
            return self.pursuit_view(p)

    def withdraw(self, pid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("pursuit_withdraw", pid, body)
            if self._idem("andre", rk, body):
                return self.pursuit_view(self.pursuits[pid])
            p = self._open(pid)
            cancel = sorted(s["submission_id"] for s in self.submissions.values()
                            if s["pursuit_id"] == pid and s["status"] == "queued")
            data = {"pursuit_id": pid, "cancel_submissions": cancel}
            self._commit("pursuit_withdrawn", self._req(data, "andre", rk, body, pid), "andre",
                         evidence=("pursuit_withdrawn", f"pursuit:{pid}", data, ("andre", rk)))
            return self.pursuit_view(p)

    # ------------------------------------------------------------------------------------------------ hand-offs

    def _handoffs_of(self, pid: str) -> list[dict]:
        return [self._handoff_view(self.handoffs[h]) for h in self.pursuits[pid]["handoff_ids"]]

    def _handoff_view(self, h: dict) -> dict:
        return {**{k: h[k] for k in ("handoff_id", "kind", "status", "created_at", "delivered_at", "reference")},
                "pursuit_id": h["payload"]["pursuit_id"], **self.attempts.get(h["handoff_id"], {})}

    def handoffs_view(self) -> list[dict]:
        with self.lock:
            return [self._handoff_view(h) for h in self.handoffs.values()]

    def _deliver(self, hid: str) -> str:
        with self.lock:
            if self._closed:
                return "pending_delivery"
            h = self.handoffs[hid]
            if h["status"] != "pending_delivery":
                return h["status"]
            port = self.ports.onboarding if h["kind"] == "onboarding_create_client" else self.ports.finance
            payload = dict(h["payload"])
        try:
            res = port.deliver(hid, payload)            # outside the lock
            status, ref = res.status, res.reference
        except Exception:      # noqa: BLE001 - an adapter error is an undelivered hand-off, retried later
            status, ref = "unavailable", None
        with self.lock:
            att = self.attempts.setdefault(hid, {"attempts": 0, "last_result": None})
            att["attempts"] += 1
            att["last_result"] = status if status in ("delivered", "refused", "unavailable", "not_wired") \
                else "unavailable"
            if status == "delivered":
                try:
                    self._commit("handoff_delivered", {"handoff_id": hid, "reference": ref}, "bizdev",
                                 evidence=("handoff_delivered", f"handoff:{hid}", {"handoff_id": hid},
                                           (hid,)))
                except Unavailable:
                    return "pending_delivery"     # the adapter is idempotent on handoff_id: the retry re-delivers
            return self.handoffs[hid]["status"]

    def retry_handoffs(self) -> dict:
        with self.lock:
            ids = sorted(h["handoff_id"] for h in self.handoffs.values() if h["status"] == "pending_delivery")
        results = {hid: self._deliver(hid) for hid in ids}
        return {"delivered": sum(1 for v in results.values() if v == "delivered"),
                "pending_delivery": sum(1 for v in results.values() if v == "pending_delivery")}

    # ------------------------------------------------------------------------------------------------ deadline sweep

    def deadline_sweep(self) -> dict:
        """Queued submissions past their pursuit's stored deadline are cancelled; every open pursuit whose deadline
        passed without a delivered submission opens one task for Andre."""
        with self.lock:
            self._gate()
            now = self.now()
            cancel = sorted(s["submission_id"] for s in self.submissions.values() if s["status"] == "queued"
                            and i03_deadline.passed(self.pursuits[s["pursuit_id"]]["deadline"] or
                                                    "9999-12-31T00:00:00Z", now))
            tasks = []
            for p in sorted(self.pursuits.values(), key=lambda x: x["pursuit_id"]):
                if p["stage"] in ("identified", "qualifying", "responding") and p["deadline"] and \
                        i03_deadline.passed(p["deadline"], now):
                    t = self._task("deadline_passed", f"pursuit:{p['pursuit_id']}", p["deadline"], "DEADLINE_PASSED")
                    if t["task_id"] not in self.tasks:
                        tasks.append(t)
            if cancel or tasks:
                self._commit("deadline_swept", {"cancel": cancel, "tasks": tasks}, "scheduler",
                             evidence=[("submission_cancelled", f"submission:{sid}",
                                        {"submission_id": sid, "reason": "DEADLINE_PASSED"}, (sid, "deadline"))
                                       for sid in cancel] or None)
            return {"cancelled": len(cancel), "tasks_opened": len(tasks)}
