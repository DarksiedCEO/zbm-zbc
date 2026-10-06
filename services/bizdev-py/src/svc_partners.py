"""Partnerships: referral partners, agency alliances and white-label deals (ADR 0016 decisions 15-19). A mixin of
BizDevService.

A partner's commission rate is proposed by the agent and approved by Andre (versioned; the approval binds partner,
version and rate). A partner deal is registered against a counterparty and goes through the same aggregated deal gate
as a pursuit. Only Andre marks it won, and only with an approved rate (snapshotted into the deal) and an agreement
Legal (37) confirms in force; with Legal a stand-in that is refused ``LEGAL_UNAVAILABLE``. Commission accrues only on
client money Finance (31) reports as actually paid (``finance_31`` caller), net of refunds and chargebacks, which claw
back unpaid commission first (i11). Payouts are requests to Finance through a port (Finance owns payees, tax checks
and the Stripe rail; this service never calls Stripe): not wired, so they stay ``queued``. Tax information is a vault
reference or token only (i12): a raw TIN / SSN / EIN anywhere in a partner body is refused 422."""

from __future__ import annotations

from typing import Optional

import money
from errors import Conflict, Invalid, Unavailable
from intelligences import i04_assembly, i06_sensitivity, i11_commission, i12_tax_refs
from legal_client import AgreementHandoff
from ledger import derived_id
from reasons import R

AGREEMENTS_FOR = {"referral": ("referral_agreement", "nda"), "agency_alliance": ("alliance_agreement", "nda"),
                  "white_label": ("white_label_agreement", "nda")}


def _no_raw_tax_id(body: dict) -> None:
    if i12_tax_refs.body_has_raw_tax_id(body):
        raise Invalid(R("TAX_ID_RAW_REFUSED"))


class PartnersMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_partner_registered(self, d, at):
        self.partners[d["partner_id"]] = {**{k: d[k] for k in ("partner_id", "partner_key", "kind", "brands", "name",
                                                               "domain", "notes", "flags")},
                                          "created_at": at, "created_by": d["actor"], "rate_version": 0,
                                          "rate_proposed": None, "rate_approved": None, "payee": None,
                                          "agreements": []}

    def _a_rate_proposed(self, d, at):
        p = self.partners[d["partner_id"]]
        p["rate_version"] = d["version"]
        p["rate_proposed"] = {"version": d["version"], "rate_pct": d["rate_pct"], "binding_sha256": d["binding_sha256"],
                              "at": at}

    def _a_rate_approved(self, d, at):
        p = self.partners[d["partner_id"]]
        p["rate_approved"] = {**p["rate_proposed"], "approved_at": at}

    def _a_payee_set(self, d, at):
        self.partners[d["partner_id"]]["payee"] = {"finance_payee_ref": d["finance_payee_ref"],
                                                   "tax_info_ref": d["tax_info_ref"],
                                                   "payee_sha256": d["payee_sha256"], "at": at}

    def _a_agreement_sent(self, d, at):
        rec = {k: d[k] for k in ("handoff_id", "kind", "reference")}
        rec["at"] = at
        if d.get("partner_id"):
            self.partners[d["partner_id"]]["agreements"].append(rec)

    def _a_partner_deal_registered(self, d, at):
        self.partner_deals[d["deal_id"]] = {
            **{k: d[k] for k in ("deal_id", "partner_id", "brand", "counterparty", "keys", "deal_value", "notes")},
            "status": "registered", "opened_at": at, "updated_at": at, "opened_by": d["actor"], "deal_approval": None,
            "rate": None, "agreement": None, "commission": i11_commission.fresh(), "finance_events": [],
            "closed_reason": None}

    def _a_partner_deal_value_set(self, d, at):
        self.partner_deals[d["deal_id"]].update(deal_value=d["deal_value"], updated_at=at)

    def _a_partner_deal_won(self, d, at):
        self.partner_deals[d["deal_id"]].update(status="won", updated_at=at, rate=d["rate"], agreement=d["agreement"],
                                                closed_reason="won")

    def _a_partner_deal_lost(self, d, at):
        self.partner_deals[d["deal_id"]].update(status="lost", updated_at=at, closed_reason=d["reason_code"])

    def _a_client_money_event(self, d, at):
        deal = self.partner_deals[d["deal_id"]]
        deal["commission"] = dict(d["state"])
        deal["finance_events"].append(d["finance_event_id"])
        deal["updated_at"] = at
        self.finance_events[d["finance_event_id"]] = {"deal_id": d["deal_id"], "kind": d["kind"], "amount": d["amount"],
                                                      "at": at, "accrued_delta": d["accrued_delta"]}
        for c in d["payout_cuts"]:
            p = self.payouts[c["payout_id"]]
            p["amount"] = money.fmt(money.q(money.D(p["amount"]) - money.D(c["amount"])))
            if money.D(p["amount"]) == 0:
                p["status"] = "cancelled"
            p["updated_at"] = at

    def _a_payout_requested(self, d, at):
        self.payouts[d["payout_id"]] = {**{k: d[k] for k in ("payout_id", "deal_id", "partner_id", "amount",
                                                             "finance_payee_ref", "tax_info_ref")},
                                        "status": "queued", "requested_at": at, "updated_at": at, "finance_ref": None}
        deal = self.partner_deals[d["deal_id"]]
        deal["commission"]["settled"] = money.fmt(money.q(money.D(deal["commission"]["settled"]) +
                                                          money.D(d["amount"])))

    def _a_payout_sending(self, d, at):
        self.payouts[d["payout_id"]].update(status="sending", updated_at=at)

    def _a_payout_result(self, d, at):
        self.payouts[d["payout_id"]].update(status=d["status"], finance_ref=d.get("finance_ref"), updated_at=at)

    def _a_payout_paid(self, d, at):
        self.payouts[d["payout_id"]].update(status="paid", finance_ref=d["finance_ref"], updated_at=at)

    # ------------------------------------------------------------------------------------------------ views

    def partner_view(self, p: dict) -> dict:
        out = dict(p)
        if p["payee"]:
            out["payee"] = {"set": True, "payee_sha256": p["payee"]["payee_sha256"], "at": p["payee"]["at"]}
        return out

    def partner(self, pid: str) -> dict:
        with self.lock:
            return self.partner_view(self._get(self.partners, pid, "PARTNER_NOT_FOUND"))

    def partners_view(self) -> list[dict]:
        with self.lock:
            return [self.partner_view(p) for p in self.partners.values()][:2000]

    def deal_view(self, d: dict) -> dict:
        out = dict(d)
        c = d["commission"]
        out["commission"] = {**c, "unpaid": money.sfmt(i11_commission.unpaid(c))}
        out["deal_gate"] = self._deal_gate(d["deal_id"]) if d["status"] == "registered" else None
        return out

    def partner_deal(self, deal_id: str) -> dict:
        with self.lock:
            return self.deal_view(self._get(self.partner_deals, deal_id, "DEAL_NOT_FOUND"))

    def partner_deals_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.deal_view(d) for d in self.partner_deals.values()
                    if status is None or d["status"] == status][:2000]

    def payouts_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in p.items() if k != "tax_info_ref"} for p in self.payouts.values()
                    if status is None or p["status"] == status][:2000]

    # ------------------------------------------------------------------------------------------------ partners

    def register_partner(self, caller: str, body: dict) -> dict:
        _no_raw_tax_id(body)
        with self.lock:
            self._gate()
            pid = derived_id("ptn", body["partner_key"])
            rk = self.rk("partner_register", pid, body)
            if self._idem(caller, rk, body):
                return self.partner_view(self.partners[pid])
            if pid in self.partners:
                raise Conflict(R("PARTNER_EXISTS"))
            flags = i06_sensitivity.flags(body["name"], body.get("notes") or "")
            data = {"partner_id": pid, "partner_key": body["partner_key"], "kind": body["kind"],
                    "brands": sorted(body["brands"]), "name": body["name"], "domain": body["domain"],
                    "notes": body.get("notes"), "flags": flags,
                    "tasks": [self._task("sensitivity_flag", f"partner:{pid}", f, f) for f in flags]}
            self._commit("partner_registered", self._req(data, caller, rk, body, pid), caller,
                         evidence=("partner_registered", f"partner:{pid}",
                                   {"partner_id": pid, "kind": body["kind"], "brands": sorted(body["brands"]),
                                    "flags": flags}, (caller, rk)))
            return self.partner_view(self.partners[pid])

    def propose_rate(self, caller: str, pid: str, body: dict) -> dict:
        _no_raw_tax_id(body)
        with self.lock:
            self._gate()
            rk = self.rk("rate_propose", pid, body)
            if self._idem(caller, rk, body):
                return self.partner_view(self.partners[pid])
            p = self._get(self.partners, pid, "PARTNER_NOT_FOUND")
            if body["version"] != p["rate_version"] + 1:
                raise Conflict(R("RATE_VERSION_STALE"))
            try:
                rate = f"{money.parse_rate(body['rate_pct']):f}"
            except money.MoneyError:
                raise Invalid(R("RATE_INVALID")) from None
            binding = i04_assembly.sha({"partner_id": pid, "version": body["version"], "rate_pct": rate})
            data = {"partner_id": pid, "version": body["version"], "rate_pct": rate, "binding_sha256": binding}
            self._commit("rate_proposed", self._req(data, caller, rk, body, pid), caller)
            return self.partner_view(p)

    def approve_rate(self, pid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("rate_approve", pid, body)
            if self._idem("andre", rk, body):
                return self.partner_view(self.partners[pid])
            p = self._get(self.partners, pid, "PARTNER_NOT_FOUND")
            prop = p["rate_proposed"]
            if prop is None or body["version"] != prop["version"]:
                raise Conflict(R("RATE_VERSION_STALE"))
            if p["rate_approved"] and p["rate_approved"]["version"] == prop["version"]:
                raise Conflict(R("RATE_ALREADY_APPROVED"))
            again = i04_assembly.sha({"partner_id": pid, "version": prop["version"], "rate_pct": prop["rate_pct"]})
            if body["binding_sha256"] != prop["binding_sha256"] or again != prop["binding_sha256"]:
                raise Conflict(R("RATE_VERSION_STALE"))
            data = {"partner_id": pid, "version": prop["version"], "binding_sha256": prop["binding_sha256"]}
            self._commit("rate_approved", self._req(data, "andre", rk, body, pid), "andre",
                         evidence=("rate_approved", f"partner:{pid}", data, ("andre", rk)))
            return self.partner_view(p)

    def set_payee(self, pid: str, body: dict) -> dict:
        """Andre only: where a partner is paid is the classic redirect fraud. References only, never a raw id."""
        _no_raw_tax_id(body)
        if not i12_tax_refs.ref_ok(body["tax_info_ref"]):
            raise Invalid(R("TAX_REF_INVALID"))
        if not i12_tax_refs.payee_ref_ok(body["finance_payee_ref"]):
            raise Invalid(R("PAYEE_REF_INVALID"))
        with self.lock:
            self._gate()
            rk = self.rk("payee_set", pid, body)
            if self._idem("andre", rk, body):
                return self.partner_view(self.partners[pid])
            p = self._get(self.partners, pid, "PARTNER_NOT_FOUND")
            psha = i04_assembly.sha({"finance_payee_ref": body["finance_payee_ref"],
                                     "tax_info_ref": body["tax_info_ref"]})
            data = {"partner_id": pid, "finance_payee_ref": body["finance_payee_ref"],
                    "tax_info_ref": body["tax_info_ref"], "payee_sha256": psha}
            self._commit("payee_set", self._req(data, "andre", rk, body, pid), "andre",
                         evidence=("payee_set", f"partner:{pid}", {"partner_id": pid, "payee_sha256": psha},
                                   ("andre", rk)))
            return self.partner_view(p)

    # ------------------------------------------------------------------------------------------------ agreements

    def request_agreement(self, caller: str, target: str, target_id: str, body: dict) -> dict:
        """Hand an agreement to Legal (37), outside the lock. Anything but ``delivered`` is refused
        LEGAL_UNAVAILABLE and nothing is recorded (the caller retries; the hand-off id is stable per request)."""
        with self.lock:
            self._gate()
            rk = self.rk(f"agreement_{target}", target_id, body)
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(prev[1])
            if target == "partner":
                p = self._get(self.partners, target_id, "PARTNER_NOT_FOUND")
                if body["kind"] not in AGREEMENTS_FOR[p["kind"]]:
                    raise Invalid(R("AGREEMENT_KIND_INVALID"))
                brand = p["brands"][0] if len(p["brands"]) == 1 else "both"
                req = AgreementHandoff(derived_id("agr", caller, rk), body["kind"], brand, target_id, None)
            else:
                p = self._get(self.pursuits, target_id, "PURSUIT_NOT_FOUND")
                if p["stage"] in ("won", "lost", "no_bid", "withdrawn"):
                    raise Conflict(R("PURSUIT_CLOSED"))
                if body["kind"] != "nda":
                    raise Invalid(R("AGREEMENT_KIND_INVALID"))
                req = AgreementHandoff(derived_id("agr", caller, rk), "nda", p["brand"], None, target_id)
        try:
            ans = self.ports.legal.send(req)
            status, ref = ans.status, ans.reference
        except Exception:      # noqa: BLE001 - a port that raises is unavailable
            status, ref = "unavailable", None
        if status != "delivered" or not ref:
            raise Unavailable(R("LEGAL_UNAVAILABLE"))
        with self.lock:
            self._gate()
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(prev[1])
            out = {"handoff_id": req.handoff_id, "kind": req.kind, "reference": ref, "status": "delivered"}
            data = {"handoff_id": req.handoff_id, "kind": req.kind, "reference": ref, "partner_id": req.partner_id,
                    "pursuit_id": req.pursuit_id}
            self._commit("agreement_sent", self._req(data, caller, rk, body, out), caller,
                         evidence=("agreement_handoff", f"agreement:{req.handoff_id}",
                                   {"handoff_id": req.handoff_id, "kind": req.kind,
                                    "target": f"{target}:{target_id}"}, (caller, rk)))
            return out

    # ------------------------------------------------------------------------------------------------ partner deals

    def register_deal(self, caller: str, body: dict) -> dict:
        _no_raw_tax_id(body)
        with self.lock:
            self._gate()
            rk = self.rk("deal_register", body["partner_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.deal_view(self.partner_deals[prev[1]])
            p = self._get(self.partners, body["partner_id"], "PARTNER_NOT_FOUND")
            if body["brand"] not in p["brands"]:
                raise Invalid(R("PARTNER_BRAND_MISMATCH"))
            cp = self._counterparty(body["counterparty"])
            try:
                value = money.fmt(money.parse(body["deal_value"], positive=True))
            except money.MoneyError:
                raise Invalid(R("MONEY_INVALID")) from None
            did = derived_id("pdl", caller, rk)
            data = {"deal_id": did, "partner_id": p["partner_id"], "brand": body["brand"], **cp,
                    "deal_value": value, "notes": body.get("notes")}
            self._commit("partner_deal_registered", self._req(data, caller, rk, body, did), caller,
                         evidence=("partner_deal_registered", f"deal:{did}",
                                   {"deal_id": did, "partner_id": p["partner_id"], "brand": body["brand"],
                                    "counterparty_keys_sha256": i04_assembly.sha({"keys": cp["keys"]}),
                                    "terms_sha256": i04_assembly.sha({"deal_value": value})}, (caller, rk)))
            return self.deal_view(self.partner_deals[did])

    def set_deal_value(self, caller: str, did: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("deal_value", did, body)
            if self._idem(caller, rk, body):
                return self.deal_view(self.partner_deals[did])
            d = self._get(self.partner_deals, did, "DEAL_NOT_FOUND")
            if d["status"] != "registered":
                raise Conflict(R("DEAL_CLOSED"))
            try:
                value = money.fmt(money.parse(body["deal_value"], positive=True))
            except money.MoneyError:
                raise Invalid(R("MONEY_INVALID")) from None
            data = {"deal_id": did, "deal_value": value}
            self._commit("partner_deal_value_set", self._req(data, caller, rk, body, did), caller,
                         evidence=("deal_value_set", f"deal:{did}",
                                   {"deal_id": did, "terms_sha256": i04_assembly.sha({"deal_value": value})},
                                   (caller, rk)))
            return self.deal_view(d)

    def _won_problem(self, d: dict) -> Optional[str]:
        if d["status"] != "registered":
            return "DEAL_CLOSED"
        p = self.partners[d["partner_id"]]
        if p["rate_approved"] is None:
            return "RATE_NOT_APPROVED"
        if not self._deal_gate(d["deal_id"])["approved"]:
            return "DEAL_APPROVAL_REQUIRED"
        return None

    def deal_won(self, did: str, body: dict) -> dict:
        """Andre only. An approved rate (snapshotted), the deal gate, and an agreement Legal shows in force (asked
        outside the lock; the stand-in answers ``unavailable``: 503 LEGAL_UNAVAILABLE)."""
        with self.lock:
            self._gate()
            rk = self.rk("deal_won", did, body)
            if self._idem("andre", rk, body):
                return self.deal_view(self.partner_deals[did])
            d = self._get(self.partner_deals, did, "DEAL_NOT_FOUND")
            problem = self._won_problem(d)
            if problem:
                raise Conflict(R(problem))
            p = self.partners[d["partner_id"]]
            if body["agreement_kind"] not in AGREEMENTS_FOR[p["kind"]]:
                raise Invalid(R("AGREEMENT_KIND_INVALID"))
            rate = dict(p["rate_approved"])
            partner_id = p["partner_id"]
        try:
            ans = self.ports.legal.in_force(partner_id, body["agreement_kind"])
            status, ref = ans.status, ans.reference
        except Exception:      # noqa: BLE001
            status, ref = "unavailable", None
        if status == "not_in_force":
            raise Conflict(R("AGREEMENT_NOT_IN_FORCE"))
        if status != "in_force" or not ref:
            raise Unavailable(R("LEGAL_UNAVAILABLE"))
        with self.lock:
            self._gate()
            if self._idem("andre", rk, body):
                return self.deal_view(self.partner_deals[did])
            problem = self._won_problem(d)
            if problem:
                raise Conflict(R(problem))
            if self.partners[partner_id]["rate_approved"] != rate:
                raise Conflict(R("RATE_VERSION_STALE"))
            snap = {"version": rate["version"], "rate_pct": rate["rate_pct"], "binding_sha256": rate["binding_sha256"]}
            data = {"deal_id": did, "rate": snap, "agreement": {"kind": body["agreement_kind"], "reference": ref}}
            self._commit("partner_deal_won", self._req(data, "andre", rk, body, did), "andre",
                         evidence=("partner_deal_won", f"deal:{did}",
                                   {"deal_id": did, "rate_binding_sha256": rate["binding_sha256"],
                                    "agreement_kind": body["agreement_kind"],
                                    "terms_sha256": i04_assembly.sha({"deal_value": d["deal_value"],
                                                                      "rate_pct": rate["rate_pct"]})},
                                   ("andre", rk)))
            return self.deal_view(d)

    def deal_lost(self, caller: str, did: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("deal_lost", did, body)
            if self._idem(caller, rk, body):
                return self.deal_view(self.partner_deals[did])
            d = self._get(self.partner_deals, did, "DEAL_NOT_FOUND")
            if d["status"] != "registered":
                raise Conflict(R("DEAL_CLOSED"))
            data = {"deal_id": did, "reason_code": body["reason_code"]}
            self._commit("partner_deal_lost", self._req(data, caller, rk, body, did), caller,
                         evidence=("partner_deal_lost", f"deal:{did}", data, (caller, rk)))
            return self.deal_view(d)

    # ------------------------------------------------------------------------------------------------ Finance events

    def finance_event(self, caller: str, body: dict) -> dict:
        """A client payment, refund or chargeback reported by Finance (31). Idempotent per request key AND per
        Finance event id: the same id with different facts is refused (409 FINANCE_EVENT_REUSED)."""
        with self.lock:
            self._gate()
            did = body["deal_id"]
            rk = self.rk("finance_event", did, body)
            if self._idem(caller, rk, body):
                return self.deal_view(self.partner_deals[did])
            seen = self.finance_events.get(body["finance_event_id"])
            if seen is not None:
                if (seen["deal_id"], seen["kind"], seen["amount"]) == (did, body["kind"], body["amount"]):
                    return self.deal_view(self.partner_deals[did])
                raise Conflict(R("FINANCE_EVENT_REUSED"))
            d = self._get(self.partner_deals, did, "DEAL_NOT_FOUND")
            if d["status"] != "won":
                raise Conflict(R("DEAL_NOT_WON"))
            try:
                amount = money.fmt(money.parse(body["amount"], positive=True))
            except money.MoneyError:
                raise Invalid(R("MONEY_INVALID")) from None
            open_payouts = [{"payout_id": p["payout_id"], "amount": p["amount"]}
                            for p in sorted(self.payouts.values(), key=lambda x: (x["requested_at"], x["payout_id"]),
                                            reverse=True)
                            if p["deal_id"] == did and p["status"] == "queued"]
            res = i11_commission.apply(d["commission"], body["kind"], amount, d["rate"]["rate_pct"], d["deal_value"],
                                       open_payouts)
            tasks = []
            if money.D(res["shortfall_delta"]) > 0:
                tasks.append(self._task("clawback_shortfall", f"deal:{did}", body["finance_event_id"],
                                        "CLAWBACK_SHORTFALL"))
            data = {"deal_id": did, "finance_event_id": body["finance_event_id"], "kind": body["kind"],
                    "amount": amount, "state": res["state"], "accrued_delta": res["accrued_delta"],
                    "payout_cuts": res["payout_cuts"], "tasks": tasks}
            evidence = [("client_money_event", f"deal:{did}",
                         {"deal_id": did, "finance_event_id": body["finance_event_id"], "kind": body["kind"],
                          "terms_sha256": i04_assembly.sha({"amount": amount, "accrued_delta": res["accrued_delta"],
                                                            "state": res["state"]})}, (caller, rk))]
            if money.D(res["accrued_delta"]) < 0:
                evidence.append(("commission_clawback", f"deal:{did}",
                                 {"deal_id": did, "finance_event_id": body["finance_event_id"],
                                  "payouts_cut": [c["payout_id"] for c in res["payout_cuts"]],
                                  "terms_sha256": i04_assembly.sha({"accrued_delta": res["accrued_delta"],
                                                                    "cuts": res["payout_cuts"],
                                                                    "shortfall": res["shortfall_delta"]})},
                                 (caller, rk)))
            self._commit("client_money_event", self._req(data, caller, rk, body, did), caller, evidence=evidence)
            return self.deal_view(d)

    def payout_paid(self, caller: str, payout_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("payout_paid", payout_id, body)
            if self._idem(caller, rk, body):
                return dict(self.payouts[payout_id])
            p = self._get(self.payouts, payout_id, "PAYOUT_NOT_FOUND")
            if p["status"] != "with_finance":
                raise Conflict(R("PAYOUT_NOT_WITH_FINANCE"))
            data = {"payout_id": payout_id, "finance_ref": body["finance_ref"]}
            self._commit("payout_paid", self._req(data, caller, rk, body, payout_id), caller,
                         evidence=("payout_paid", f"payout:{payout_id}", data, (caller, rk)))
            return {k: v for k, v in p.items() if k != "tax_info_ref"}

    # ------------------------------------------------------------------------------------------------ payouts

    def payout_tick(self) -> dict:
        """The ``payout-request`` job. For each won deal with an unpaid balance and a partner payee, a payout request
        is recorded (its amount is settled at once, so it is never requested twice); then every queued request is
        handed to Finance through the port, outside the lock, marked ``sending`` first so a clawback cannot cut an
        amount Finance is being sent. Not wired: requests stay ``queued``. Nothing is ever paid here."""
        summary = {"requested": 0, "with_finance": 0, "not_wired": 0, "failed": 0, "payee_missing": 0}
        with self.lock:
            self._gate()
            for d in sorted(self.partner_deals.values(), key=lambda x: x["deal_id"]):
                if d["status"] != "won":
                    continue
                owed = i11_commission.unpaid(d["commission"])
                if owed <= 0:
                    continue
                p = self.partners[d["partner_id"]]
                if p["payee"] is None:
                    summary["payee_missing"] += 1
                    continue
                n = sum(1 for x in self.payouts.values() if x["deal_id"] == d["deal_id"]) + 1
                pay_id = derived_id("pay", d["deal_id"], n)
                amount = money.fmt(owed)
                data = {"payout_id": pay_id, "deal_id": d["deal_id"], "partner_id": p["partner_id"],
                        "amount": amount, "finance_payee_ref": p["payee"]["finance_payee_ref"],
                        "tax_info_ref": p["payee"]["tax_info_ref"]}
                self._commit("payout_requested", data, "scheduler",
                             evidence=("commission_payout_requested", f"payout:{pay_id}",
                                       {"payout_id": pay_id, "deal_id": d["deal_id"], "partner_id": p["partner_id"],
                                        "payee_sha256": p["payee"]["payee_sha256"],
                                        "terms_sha256": i04_assembly.sha({"amount": amount})}, (pay_id,)))
                summary["requested"] += 1
            queued = [x["payout_id"] for x in sorted(self.payouts.values(), key=lambda x: (x["requested_at"],
                                                                                           x["payout_id"]))
                      if x["status"] == "queued"]
        for pay_id in queued:
            with self.lock:
                self._gate()
                p = self.payouts[pay_id]
                if p["status"] != "queued":
                    continue
                if not self.ports.payouts.wired:
                    summary["not_wired"] += 1
                    continue
                payload = {k: p[k] for k in ("payout_id", "deal_id", "partner_id", "amount", "finance_payee_ref",
                                             "tax_info_ref")}
                payload["currency"] = "USD"
                self._commit("payout_sending", {"payout_id": pay_id}, "scheduler",
                             evidence=("payout_sending", f"payout:{pay_id}", {"payout_id": pay_id}, (pay_id, "send")))
            try:
                res = self.ports.payouts.request_payout(pay_id, payload)     # outside the lock
                status, ref = res.status, res.reference
            except Exception:      # noqa: BLE001
                status, ref = "unavailable", None
            with self.lock:
                done = status == "delivered" and bool(ref)
                self._commit("payout_result", {"payout_id": pay_id, "status": "with_finance" if done else "queued",
                                               "finance_ref": ref if done else None}, "scheduler",
                             evidence=("payout_result", f"payout:{pay_id}",
                                       {"payout_id": pay_id, "status": "with_finance" if done else "queued"},
                                       (pay_id, "result", ref or "none")))
                summary["with_finance" if done else "failed"] += 1
        return summary
