"""
Reconciliation (intelligence 7), treasury (intelligence 8), controls (intelligence 9) and the scheduler's jobs.

Reconciliation runs legs L1-L4 against sources the payout system does not produce (bank feed, rail balance API and
item statuses, the operational records behind each control account); zero tolerance; each non-zero difference or
unreadable source opens a break that only Andre resolves (a reconciling entry, a documented timing item clearing
within 2 business days, or evidence once the leg matches again). Treasury moves (sweeps F8, rail funding F4a,
top-ups F5c, refund payments F6p) are Andre-approved, go through the bank adapter with an idempotency key, and post
only when the bank accepts; the bank adapter is a stand-in, so none moves on day one.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import chart as C
import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, Forbidden, Invalid, NotFound
from intelligences import i01_journal as J
from intelligences import i06_tax as I6
from intelligences import i07_reconciliation as I7
from intelligences import i08_treasury as T
from intelligences import i09_controls as I9
from ledger import derived_id
from ports import BankBalance, BankTransfer, RailBalance, RailLookup
from service import Gather, Op, PostingRefused, Refused, rid, sha

JOBS = ("accrual", "clawback-sync", "tax-sync", "rail-sync")
TRANSFER_FLOWS = {"sweep": ("F8", "1010", "1020"), "top_up": ("F5c", "1020", "1010")}


class ReconMixin:
    # ================================================================== reconciliation

    def _expected_l4(self) -> dict:
        items = self.db["items"]
        e2020 = M.ZERO
        for p in self.db["payables"].values():
            if p["status"] == "accrued":
                e2020 += M.D(p["current_amount"])
        for it in items.values():
            if it["status"] in ("proposed", "approved"):
                e2020 += M.total(self.db["payables"][x]["current_amount"] for x in it["payable_ids"])
            elif it["status"] == "submitting":
                e2020 += M.D(it["net"])
        e2030 = M.total(it["net"] for it in items.values() if it["status"] == "submitted")
        e1200 = M.ZERO
        for c in self.db["clawbacks"].values():
            if c.get("status") in ("receivable", "written_off"):
                e1200 += M.D(c.get("receivable") or "0.00")
            if c.get("status") == "write_off":
                e1200 -= M.D(c["amount"])
        e1200 -= M.total(it["netted"] for it in items.values() if it["status"] in ("submitting", "submitted", "paid",
                                                                                     "netted"))
        e2010 = M.ZERO
        for r in self.db["receipts"].values():
            inv = self.db["invoices"].get(r.get("invoice_id") or "")
            if r["status"] == "matched" and inv and inv["kind"] == "campaign_deposit":
                e2010 += M.D(r["amount"])
        for p in self.db["payables"].values():
            if p["status"] not in ("pending_checks", "over_budget_hold"):
                e2010 -= M.D(p["current_revenue"])
        e2010 -= M.total(r["amount"] for r in self.db["refunds"].values() if r["status"] in ("approved", "paid"))
        return {"2020": M.q(e2020), "2030": M.q(e2030), "1200": M.q(e1200), "2010": M.q(e2010)}

    def run_recon(self, principal: str, request_id: str) -> dict:
        key, h, ent = self._idem(principal, request_id, "reconciliations/run", None)
        if ent:
            return ent["response"]
        self.require_rules()
        integ = self.integrity(record=True)
        bank, rails = self.ports.bank, self.ports.rails
        g = Gather(self, f"recon|{principal}|{request_id}", I7.ACTOR, "reconciliation")
        obs = {}
        for ent_, acct in (("zbc", "1020"), ("zbc", "1010"), ("zbm", "1010")):
            obs[(ent_, acct)] = g.call("bank_feed", "balance", (ent_, acct), lambda e=ent_, a=acct: bank.balance(e, a),
                                       BankBalance(False))
        rail_obs, lookups = {}, {}
        for r in self.cfg.rails:
            rp = rails.get(r)
            rail_obs[r] = g.call(f"rail_{r}", "balance", (), lambda x=rp: x.balance(), RailBalance(False))
        for it in self.db["items"].values():
            if it["status"] == "submitted":
                rp = rails.get(it["rail"])
                lookups[it["item_id"]] = g.call(f"rail_{it['rail']}", "lookup", (it["idempotency_key"],),
                                                lambda x=rp, i=it: x.lookup(i["idempotency_key"], i["item_id"]),
                                                RailLookup(False))
        with self.lock:
            legs = []

            def bank_leg(leg_id, ent_, acct):
                b = obs[(ent_, acct)]
                ok = isinstance(b, BankBalance) and b.available and self._fresh(b.as_of, 26)
                legs.append(I7.leg(leg_id, f"{ent_}:{acct}", self.bal(acct, entity=ent_),
                                   M.D(b.balance) if ok else None, b.source_sha256 if ok else None,
                                   "" if ok else "bank feed unavailable or stale (stand-in)"))

            bank_leg("L1", "zbc", "1020")
            bank_leg("L2", "zbc", "1010")
            bank_leg("L2", "zbm", "1010")
            for r in self.cfg.rails:
                acct = C.RAIL_ACCOUNT[r]
                b = rail_obs[r]
                ok = isinstance(b, RailBalance) and b.available and self._fresh(b.as_of, 26)
                used = any(k[1] == acct for k in self.balances)
                if not ok and not used:
                    legs.append(I7.not_in_use("L3", f"zbc:{acct}"))
                    continue
                legs.append(I7.leg("L3", f"zbc:{acct}", self.bal(acct), M.D(b.balance) if ok else None,
                                   b.source_sha256 if ok else None, "" if ok else f"rail {r} balance unavailable"))
            bad_items = [i for i, lk in lookups.items() if not (isinstance(lk, RailLookup) and lk.available and lk.found
                                                                and lk.status in ("submitted", "paid"))]
            if lookups:
                legs.append({"leg": "L3", "subject": "items", "expected": str(len(lookups)),
                             "observed": str(len(lookups) - len(bad_items)), "observed_source_sha256": sha(sorted(lookups)),
                             "difference": str(len(bad_items)), "status": "matched" if not bad_items else "break",
                             "note": f"{len(bad_items)} open item(s) not confirmed by the rail"[:200]})
            exp = self._expected_l4()
            for acct in ("2010", "2020", "2030", "1200"):
                legs.append(I7.leg("L4", f"zbc:{acct}", self.bal(acct), exp[acct], sha({"records": acct}),
                                   "control balance vs the operational records"))
            ind = T.independent(M.D(obs[("zbc", "1020")].balance) if legs[0]["status"] != "source_unavailable" else None,
                                {r: M.D(rail_obs[r].balance) for r in self.cfg.rails
                                 if isinstance(rail_obs[r], RailBalance) and rail_obs[r].available}, self.balances)
            pos = T.view(T.position(self.balances))
            now = self._now()
            recon_id = rid("rec", principal, request_id)
            seq = len(self.db["recon_runs"]) + 1
            op = Op(self, f"recon|{principal}|{request_id}", I7.ACTOR, "reconciliation", g)
            opened = []
            for l in legs:
                if l["status"] in ("matched", "not_in_use"):
                    continue
                existing = next((b for b in self.db["breaks"].values() if b["status"] in ("open", "explained")
                                 and b["leg"] == l["leg"] and b["subject"] == l["subject"]), None)
                if existing:
                    op.put("breaks", existing["break_id"], {**existing, "difference": l["difference"],
                                                            "last_seen": iso(now), "recon_id": recon_id,
                                                            "last_seen_seq": seq})
                    continue
                bid = rid("brk", l["leg"], l["subject"], recon_id)
                b = {"break_id": bid, "leg": l["leg"], "subject": l["subject"], "difference": l["difference"],
                     "kind": "source_unavailable" if l["status"] == "source_unavailable" else "difference",
                     "opened_at": iso(now), "opened_on": self._today_la().isoformat(), "owner": "andre",
                     "explanation_code": "unknown", "status": "open", "resolution": None, "recon_id": recon_id,
                     "last_seen_seq": seq}
                op.put("breaks", bid, b)
                op.record(derived_id("brk", bid), "break_opened", I7.ACTOR, bid,
                          {"break_id": bid, "leg": l["leg"], "subject": l["subject"], "difference": l["difference"],
                           "kind": b["kind"]}, f"Break opened on {l['leg']} {l['subject']}")
                opened.append(bid)
            run = {"recon_id": recon_id, "seq": seq, "entity": "zbc+zbm", "business_date": self._today_la().isoformat(),
                   "run_at": iso(now), "legs": legs, "treasury": {"journal": pos, "independent": ind},
                   "integrity": integ["status"], "breaks_opened": opened}
            op.put("recon_runs", recon_id, run)
            op.record(derived_id("recr", recon_id), "recon_run", I7.ACTOR, recon_id,
                      {"recon_id": recon_id, "legs": len(legs), "matched": sum(l["status"] == "matched" for l in legs),
                       "breaks_opened": len(opened), "legs_sha256": sha(legs)},
                      f"Reconciliation run: {sum(l['status'] == 'matched' for l in legs)}/{len(legs)} legs matched")
            ok = pos["ok"] and ind["known"] and ind["ok"]
            op.record(derived_id("trs", recon_id), "treasury_ok" if ok else "treasury_breach", T.ACTOR, recon_id,
                      {"journal_gap": pos["gap"], "independent_known": ind["known"], "independent_ok": ind["ok"]},
                      "Treasury invariant holds" if ok else "Treasury invariant not proven (FC-03 red)")
            resp = {"recon": run, "fc01": I7.all_matched(legs), "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def list_breaks(self) -> dict:
        with self.lock:
            out = []
            today = self._today_la()
            for b in sorted(self.db["breaks"].values(), key=lambda b: b["opened_at"]):
                out.append({**b, "aging": I7.aging_bucket(date.fromisoformat(b["opened_on"]), today)})
            return {"breaks": out}

    def resolve_break(self, request_id: str, break_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"break/{break_id}",
                                     {k: str(v) for k, v in body.items()})
            if ent:
                return ent["response"]
            b = self.db["breaks"].get(break_id)
            if b is None:
                raise NotFound("no such break")
            if b["status"] == "resolved":
                raise Conflict("break already resolved")
            now = self._now()
            status = None
            if body.get("entry_id"):
                e = self.entries_by_id.get(body["entry_id"])
                if e is None or e["posted_at"] < b["opened_at"]:
                    raise Invalid("entry_id must name a journal entry posted after the break opened")
                status = "resolved"
            elif body["explanation_code"] == "timing_in_transit" and body.get("clears_by"):
                limit = I6.add_business_days(self._today_la(), 2)
                if body["clears_by"] > limit:
                    raise Invalid("a timing item must clear within 2 business days")
                status = "explained"
            elif body.get("evidence"):
                last = self._latest_recon()
                legs = [l for l in (last or {}).get("legs", []) if l["leg"] == b["leg"] and l["subject"] == b["subject"]]
                if not last or last.get("seq", 0) <= b.get("last_seen_seq", 0) or not legs or not I7.all_matched(legs):
                    raise Refused("break still shows", [R.item("RECON_BREAK", "the leg does not match in a reconciliation "
                                                                              "run after the break: post a reconciling "
                                                                              "entry or fix the source first")])
                status = "resolved"
            else:
                raise Invalid("resolution needs a reconciling entry_id, a timing item with clears_by, or evidence after "
                              "the leg matches again")
            new = {**b, "status": status, "explanation_code": body["explanation_code"],
                   "clears_by": body["clears_by"].isoformat() if body.get("clears_by") else None,
                   "resolution": {"entry_id": body.get("entry_id"), "evidence": [dict(x) for x in body.get("evidence") or []],
                                  "approved_by": "andre", "at": iso(now)}}
            op = Op(self, f"brkr|{request_id}", "andre", break_id)
            op.put("breaks", break_id, new)
            op.record(derived_id("brkr", break_id, request_id), "break_resolved_by_andre", "andre", break_id,
                      {"break_id": break_id, "status": status, "explanation_code": body["explanation_code"],
                       "entry_id": body.get("entry_id")}, f"Andre {status} a reconciliation break")
            resp = {"break": new, "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    # ================================================================== controls

    def _control_block_reasons(self, action: str) -> list[dict]:
        out = []
        last = self._latest_recon()
        if last is None or not self._fresh(last["run_at"], I7.FC01_MAX_AGE_H) or not I7.all_matched(last["legs"]):
            out.append(R.item("RECON_BREAK", f"FC-01 red: no fresh, fully matched reconciliation ({action} refused)"))
        if self._open_breaks():
            out.append(R.item("RECON_BREAK", f"FC-02 red: reconciliation break(s) open ({action} refused)"))
        pos = T.position(self.balances)
        ind = (last or {}).get("treasury", {}).get("independent", {})
        if pos["gap"] < 0 or not ind.get("known") or not ind.get("ok"):
            out.append(R.item("TREASURY_BREACH", f"FC-03 red: treasury invariant not proven ({action} refused)"))
        return out

    def controls_view(self) -> dict:
        with self.lock:
            now = self._now()
            out = []
            last = self._latest_recon()
            checks = sorted(self.db["integrity_checks"].values(), key=lambda c: (c["checked_at"], c.get("seq", 0)))
            for cid, (test, owner, sla, blocks) in I9.CATALOG.items():
                green, why = False, "never run"
                if cid == "FC-01":
                    green = bool(last and self._fresh(last["run_at"], sla) and I7.all_matched(last["legs"]))
                    why = "latest run fresh and matched" if green else "no fresh fully matched run"
                elif cid == "FC-02":
                    n = len(self._open_breaks())
                    green, why = n == 0 and last is not None, f"{n} open break(s)"
                elif cid == "FC-03":
                    green = last is not None and all(r["code"] != "TREASURY_BREACH"
                                                     for r in self._control_block_reasons("check"))
                    why = "journal and independent invariant hold" if green else "not proven"
                elif cid == "FC-04":
                    c = checks[-1] if checks else None
                    green = bool(c and c["status"] == "green" and self._fresh(c["checked_at"], sla))
                    why = "integrity green" if green else "integrity check missing, stale or red"
                elif cid == "FC-06":
                    overdue = I6.overdue_timers(list(self.db["tax"].values()), self._today_la())
                    synced = any(k.startswith("tax-sync|") and self._fresh(v.get("at"), sla)
                                 for k, v in self.db["job_runs"].items())
                    green = synced and not overdue
                    why = f"{len(overdue)} overdue B-notice timer(s); tax sync {'fresh' if synced else 'missing'}"
                elif cid == "FC-08":
                    f = self.db["tax_readiness"].get("filing") or {}
                    green = all(f.get(k) for k in ("tcc_obtained_at", "iris_test_passed_at", "ftb_swift_ready_at"))
                    why = "readiness facts recorded" if green else ("missing readiness facts" +
                                                                    (" past 2026-12-01" if now.date() >= date(2026, 12, 1)
                                                                     else ""))
                elif cid == "FC-09":
                    prev = (self._today_la().replace(day=1) - timedelta(days=1)).isoformat()[:7]
                    green = all(f"{e}|{prev}" in self.db["locks"] for e in C.ENTITIES)
                    why = f"{prev} locked" if green else f"{prev} not locked"
                elif cid == "FC-10":
                    green, why = False, "GL adapter (QBO) is a stand-in: L5 cannot run"
                else:
                    r = self.db["controls"].get(cid)
                    green = bool(r and r["result"] == "pass" and self._fresh(r["at"], sla))
                    why = "attested" if green else ("owner not built" if owner not in ("andre",) else "not attested")
                out.append({"control_id": cid, "test": test, "owner": owner, "sla_hours": sla, "blocks": list(blocks),
                            "status": "green" if green else "red", "why": why})
            return {"controls": out, "as_of": iso(now)}

    def record_control_result(self, who: str, request_id: str, control_id: str, body: dict) -> dict:
        if control_id not in I9.CATALOG:
            raise NotFound("no such control")
        owner = I9.CATALOG[control_id][1]
        if not ((owner == "andre" and who == "andre") or (owner == "compliance_38" and who == "compliance_38")):
            raise Forbidden("only the control's owner records its result (computed controls take no result)")
        with self.lock:
            key, h, ent = self._idem(who, request_id, f"controls/{control_id}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            rec = {"control_id": control_id, "result": body["result"], "evidence_ref": body["evidence_ref"],
                   "by": who, "at": iso(self._now())}
            op = Op(self, f"ctl|{who}|{request_id}", who, control_id)
            op.put("controls", control_id, rec)
            op.record(derived_id("ctl", control_id, who, request_id), "control_result_recorded", who, control_id,
                      {"control_id": control_id, "result": body["result"], "evidence_ref_sha256": sha(body["evidence_ref"])},
                      f"Control {control_id}: {body['result']}")
            resp = {"control": rec, "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    # ================================================================== treasury

    def treasury_view(self) -> dict:
        with self.lock:
            last = self._latest_recon()
            return {"journal": T.view(T.position(self.balances)),
                    "sweepable": M.fmt(T.sweepable(self.balances, self.cfg.restricted_buffer)),
                    "independent": (last or {}).get("treasury", {}).get("independent"),
                    "account_title": C.DEPOSITS_ACCOUNT_TITLE, "custody_model": "own_deposit",
                    "open_operations": [o for o in self.db["treasury_ops"].values() if o["status"] in
                                        ("proposed", "approved")]}

    def _new_treasury_op(self, op: Op, kind: str, amount, ref_id: Optional[str], by: str, request_id: str) -> dict:
        tid = rid("trx", kind, by, request_id)
        t = {"op_id": tid, "kind": kind, "entity": "zbc", "amount": M.fmt(amount), "ref_id": ref_id,
             "status": "proposed", "proposed_by": by, "proposed_at": iso(self._now()), "attempts": 0}
        t["content_sha256"] = sha({k: t[k] for k in ("op_id", "kind", "entity", "amount", "ref_id")})
        op.put("treasury_ops", tid, t)
        return t

    def propose_sweep(self, principal: str, request_id: str, amount) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "sweeps", M.fmt(amount))
            if ent:
                return ent["response"]
            self.require_rules()
            reasons = self._control_block_reasons("sweep")
            sw = T.sweepable(self.balances, self.cfg.restricted_buffer)
            if amount > sw:
                reasons.append(R.item("TREASURY_BREACH", f"sweep {M.fmt(amount)} exceeds sweepable {M.fmt(sw)}"))
            if reasons:
                raise Refused("sweep refused", R.dedupe(reasons))
            op = Op(self, f"sweep|{request_id}", "intel_08_treasury", "treasury")
            t = self._new_treasury_op(op, "sweep", amount, None, principal, request_id)
            op.record(derived_id("swp", t["op_id"]), "sweep_proposed", "intel_08_treasury", t["op_id"],
                      {"op_id": t["op_id"], "amount": t["amount"]}, f"Margin sweep proposed: {t['amount']}")
            resp = {"operation": t, "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def propose_funding(self, principal: str, request_id: str, batch_id: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "funding", batch_id)
            if ent:
                return ent["response"]
            self.require_rules()
            b = self.db["batches"].get(batch_id)
            if b is None:
                raise NotFound("no such batch")
            if b["status"] != "approved":
                raise Conflict("only an Andre-approved batch is funded")
            need = M.q(M.D(b["totals"]["net"]) - max(M.ZERO, self.bal(C.RAIL_ACCOUNT[b["rail"]])))
            if need <= 0:
                raise Conflict("the rail already holds the batch's net total")
            op = Op(self, f"fund|{request_id}", "intel_08_treasury", batch_id)
            t = self._new_treasury_op(op, "funding", need, batch_id, principal, request_id)
            t["rail"] = b["rail"]
            op.put("treasury_ops", t["op_id"], t)
            op.record(derived_id("fnd", t["op_id"]), "funding_proposed", "intel_08_treasury", t["op_id"],
                      {"op_id": t["op_id"], "amount": t["amount"], "batch_id": batch_id},
                      f"Rail funding proposed for an approved batch: {t['amount']}")
            resp = {"operation": t, "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def top_up(self, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "top-ups", {k: str(v) for k, v in body.items()})
            if ent:
                return ent["response"]
            self.require_rules()
            op = Op(self, f"topup|{request_id}", "andre", "treasury")
            self._injection(op, {"notes": body.get("notes")})
            t = self._new_treasury_op(op, "top_up", body["amount"], body.get("reason_code"), "andre", request_id)
            t = {**t, "status": "approved", "approved_at": iso(self._now()), "approved_by": "andre"}
            op.put("treasury_ops", t["op_id"], t)
            op.record(derived_id("tup", t["op_id"]), "top_up_approved", "andre", t["op_id"],
                      {"op_id": t["op_id"], "amount": t["amount"], "reason_code": body.get("reason_code")},
                      f"Andre approved an operating top-up of restricted cash: {t['amount']}")
            self._commit(op)
        res = self._execute_transfer(t["op_id"])
        with self.lock:
            return self._idem_mem(key, h, {"operation": self.db["treasury_ops"][t["op_id"]], "transfer": res,
                                           "ledger_event_ids": op.events, "request_id": request_id})

    def decide_treasury(self, request_id: str, kind: str, op_id: str, body: dict) -> dict:
        key, h, ent = self._idem("andre", request_id, f"treasury/{op_id}", body)
        if ent:
            return ent["response"]
        with self.lock:
            self.require_rules()
            t = self.db["treasury_ops"].get(op_id)
            if t is None or t["kind"] != kind:
                raise NotFound("no such treasury operation")
            if t["status"] != "proposed":
                raise Conflict(f"operation is already {t['status']}")
            if body["content_sha256"] != t["content_sha256"]:
                raise Conflict("operation changed since you read it (content_sha256 mismatch)")
            op = Op(self, f"trd|{request_id}", "andre", op_id)
            if body["decision"] == "reject":
                op.put("treasury_ops", op_id, {**t, "status": "rejected"})
                op.record(derived_id("trr", op_id), "sweep_approved" if kind == "sweep" else "funding_approved", "andre",
                          op_id, {"op_id": op_id, "decision": "reject"}, f"Andre rejected a {kind}")
                resp = {"operation": {**t, "status": "rejected"}, "ledger_event_ids": op.events, "request_id": request_id}
                self._idem_add(op, key, h, resp)
                self._commit(op)
                return resp
            reasons = []
            if kind == "sweep":
                reasons += self._control_block_reasons("sweep")
                if M.D(t["amount"]) > T.sweepable(self.balances, self.cfg.restricted_buffer):
                    reasons.append(R.item("TREASURY_BREACH", "sweep exceeds the sweepable amount now"))
            else:
                b = self.db["batches"].get(t["ref_id"])
                if not b or b["status"] not in ("approved", "releasing"):
                    reasons.append(R.item("NOT_APPROVED", "funding only for an Andre-approved batch"))
                elif M.D(t["amount"]) > M.D(b["totals"]["net"]):
                    reasons.append(R.item("TREASURY_BREACH", "funding above the approved batch total"))
            if reasons:
                raise Refused(f"{kind} refused", R.dedupe(reasons))
            op.put("treasury_ops", op_id, {**t, "status": "approved", "approved_at": iso(self._now()),
                                           "approved_by": "andre"})
            op.record(derived_id("tra", op_id), "sweep_approved" if kind == "sweep" else "funding_approved", "andre",
                      op_id, {"op_id": op_id, "amount": t["amount"], "ref_id": t.get("ref_id")},
                      f"Andre approved a {kind} of {t['amount']}")
            self._commit(op)
        res = self._execute_transfer(op_id)
        resp = {"operation": self.db["treasury_ops"][op_id], "transfer": res, "request_id": request_id}
        with self.lock:
            return self._idem_mem(key, h, resp)

    def _transfer_lines(self, t: dict):
        amt = M.D(t["amount"])
        if t["kind"] == "sweep":
            return "F8", [J.dr("1010", amt), J.cr("1020", amt)], ("1020", "1010")
        if t["kind"] == "top_up":
            return "F5c", [J.dr("1020", amt), J.cr("1010", amt)], ("1010", "1020")
        if t["kind"] == "funding":
            acct = C.RAIL_ACCOUNT[t.get("rail") or "stripe"]
            return "F4a", [J.dr(acct, amt), J.cr("1020", amt)], ("1020", acct)
        if t["kind"] == "refund_payment":
            r = self.db["refunds"][t["ref_id"]]
            return "F6p", [J.dr("2050", amt, f"client:{r['client_id']}"), J.cr("1020", amt)], ("1020", t["to"])
        raise Invalid("unknown treasury operation")

    def _execute_transfer(self, tid: str) -> dict:
        """Ask the bank adapter to move an Andre-approved amount (idempotency key = the operation id); post only on
        acceptance. The stand-in answers unavailable: the operation stays approved and nothing posts."""
        t = self.db["treasury_ops"][tid]
        if t["status"] != "approved":
            return {"op_id": tid, "status": t["status"]}
        memo, lines, (src, dst) = self._transfer_lines(t)
        bank = self.ports.bank
        g = Gather(self, f"xfer|{tid}|{t.get('attempts', 0) + 1}", "intel_08_treasury", tid)
        ans = g.call("bank_feed", "transfer", ("zbc", src, dst, t["amount"], tid),
                     lambda: bank.transfer("zbc", src, dst, t["amount"], tid), BankTransfer("unavailable"))
        ans = ans if isinstance(ans, BankTransfer) else BankTransfer("unavailable")
        with self.lock:
            t = self.db["treasury_ops"][tid]
            if t["status"] != "approved":
                return {"op_id": tid, "status": t["status"]}
            op = Op(self, f"xfer|{tid}|{t.get('attempts', 0) + 1}", "intel_08_treasury", tid, g)
            if ans.outcome != "accepted":
                op.put("treasury_ops", tid, {**t, "attempts": t.get("attempts", 0) + 1, "last_outcome": ans.outcome})
                self._commit(op)
                return {"op_id": tid, "status": "approved", "transfer": ans.outcome}
            try:
                e = self._post(op, "zbc", lines, memo, {"kind": t["kind"], "id": tid}, f"{memo}|{tid}",
                               approval_ref=tid, actor="intel_08_treasury")
            except PostingRefused as exc:
                op.put("treasury_ops", tid, {**t, "status": "posting_refused", "reasons": exc.reasons})
                bid = rid("brk", "xfer", tid)
                op.put("breaks", bid, {"break_id": bid, "leg": "L1", "subject": f"transfer:{tid}", "difference": t["amount"],
                                       "opened_at": iso(self._now()), "opened_on": self._today_la().isoformat(),
                                       "owner": "andre", "explanation_code": "unknown", "status": "open",
                                       "resolution": None})
                self._commit(op)
                return {"op_id": tid, "status": "posting_refused", "reasons": exc.reasons}
            op.put("treasury_ops", tid, {**t, "status": "done", "bank_ref": ans.ref, "entry_id": e["entry_id"],
                                         "done_at": iso(self._now())})
            if t["kind"] == "refund_payment":
                r = self.db["refunds"][t["ref_id"]]
                op.put("refunds", r["refund_id"], {**r, "status": "paid", "paid_at": iso(self._now())})
                prof = self.db["profiles"].get(r["campaign_id"])
                if prof:
                    op.put("profiles", r["campaign_id"], {**prof, "status": "closed"})
                op.record(derived_id("rfdpd", r["refund_id"]), "refund_paid", "intel_08_treasury", r["refund_id"],
                          {"refund_id": r["refund_id"], "amount": r["amount"]}, f"Refund paid: {r['amount']}")
            self._commit(op)
            return {"op_id": tid, "status": "done", "entry_id": e["entry_id"]}

    def retry_transfers(self) -> int:
        n = 0
        for tid, t in list(self.db["treasury_ops"].items()):
            if t["status"] == "approved":
                self._execute_transfer(tid)
                n += 1
        return n

    # ================================================================== jobs

    def run_job(self, principal: str, request_id: str, job: str) -> dict:
        if job not in JOBS:
            raise NotFound("unknown job")
        self._idem(principal, request_id, f"jobs/{job}", None)
        day = self._now().date().isoformat()
        jk = f"{job}|{day}"
        prior = self.db["job_runs"].get(jk)
        if prior is not None:
            return {"job": job, "day": day, "summary": prior["summary"], "already_ran": True, "request_id": request_id}
        self.require_rules()
        prefix = f"job|{job}|{day}"
        fn = {"accrual": self.job_accrual, "clawback-sync": self.job_clawback_sync, "tax-sync": self.job_tax_sync,
              "rail-sync": self.job_rail_sync}[job]
        summary = fn(prefix)
        with self.lock:
            op = Op(self, f"{prefix}|done", "intel_10_evidence_audit", job)
            op.put("job_runs", jk, {"job": job, "day": day, "at": iso(self._now()), "summary": summary})
            op.record(derived_id("job", job, day), "control_result_recorded", "intel_10_evidence_audit", jk,
                      {"job": job, "day": day, "summary_sha256": sha(summary)}, f"Job {job} ran")
            self._commit(op)
        return {"job": job, "day": day, "summary": summary, "already_ran": False, "request_id": request_id}
