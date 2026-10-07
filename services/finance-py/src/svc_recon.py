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
from decimal import Decimal
from typing import Optional

import chart as C
import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from intelligences import i01_journal as J
from intelligences import i04_payout_run as I4
from intelligences import i06_tax as I6
from intelligences import i07_reconciliation as I7
from intelligences import i08_treasury as T
from intelligences import i09_controls as I9
from ledger import derived_id
from ports import BankBalance, BankTransfer, RailBalance, RailLookup
from service import RUNNABLE, Gather, IntegrityRefused, Op, PostingRefused, Refused, rid, sha

JOBS = ("accrual", "clawback-sync", "tax-sync", "rail-sync", "stripe-sessions")
REPEATABLE_JOBS = ("stripe-sessions",)        # safe to run many times a day (AEGIS N2): each run is recorded
PENDING_SWEEP = ("proposed", "approved", "executing", "bank_unknown")      # reserved when a sweep is proposed
LIVE_SWEEP = ("approved", "executing", "bank_unknown")                     # reserved at approval and execution
OPEN_OPS = ("proposed", "approved", "executing", "bank_unknown")
REFUND_OWED = ("approved", "paid", "payment_not_moved")   # a refund whose F6 posting stands (2010 -> 2050)
TRANSFER_FLOWS = {"sweep": ("F8", "1010", "1020"), "top_up": ("F5c", "1020", "1010")}


class ReconMixin:
    # ================================================================== reconciliation

    def _expected_l4_sub(self) -> dict[str, dict[str, Decimal]]:
        """The operational records' view of each control account, PER SUB-LEDGER (payee, item, campaign) -- AEGIS
        N17-1: a payable moved to another payee or campaign nets to zero in a total and is caught only here."""
        out: dict[str, dict[str, Decimal]] = {"2020": {}, "2030": {}, "1200": {}, "2010": {}}

        def add(acct: str, sub: str, amt: Decimal) -> None:
            out[acct][sub] = M.q(out[acct].get(sub, M.ZERO) + amt)

        items = self.db["items"]
        pays = self.db["payables"]
        for p in pays.values():
            if p["status"] in RUNNABLE:
                add("2020", f"payee:{p['payee_id']}", M.D(p["current_amount"]))
        for it in items.values():
            if it["status"] in ("proposed", "approved"):
                add("2020", f"payee:{it['payee_id']}", M.total(pays[x]["current_amount"] for x in it["payable_ids"]))
            elif it["status"] == "submitting":
                add("2020", f"payee:{it['payee_id']}", M.D(it["net"]))
            elif it["status"] == "submitted":
                add("2030", f"item:{it['item_id']}", M.D(it["net"]))
            if it["status"] in ("submitting", "submitted", "paid", "netted"):
                add("1200", f"payee:{it['payee_id']}", -M.D(it["netted"]))
        for c in self.db["clawbacks"].values():
            if c.get("status") in ("receivable", "written_off") and c.get("payee_id"):
                add("1200", f"payee:{c['payee_id']}", M.D(c.get("receivable") or "0.00"))
            if c.get("status") == "write_off":
                add("1200", f"payee:{c['payee_id']}", -M.D(c["amount"]))
        for r in self.db["receipts"].values():
            inv = self.db["invoices"].get(r.get("invoice_id") or "")
            if r["status"] in ("matched", "returned") and inv and inv["kind"] == "campaign_deposit":
                add("2010", f"campaign:{inv['campaign_id']}", M.D(r["amount"]))
                if r["status"] == "returned":
                    add("2010", f"campaign:{inv['campaign_id']}", -M.D((r.get("return") or {}).get("to_2010") or "0.00"))
        for p in pays.values():
            if p["status"] not in ("pending_checks", "over_budget_hold"):
                add("2010", f"campaign:{p['campaign_id']}", -M.D(p["current_revenue"]))
        for r in self.db["refunds"].values():
            if r["status"] in REFUND_OWED:
                add("2010", f"campaign:{r['campaign_id']}", -M.D(r["amount"]))
        return out

    def _expected_l4(self) -> dict:
        return {acct: M.total(v.values()) for acct, v in self._expected_l4_sub().items()}

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
        stripe_obs = (g.call("stripe", "balance", (), lambda: self.ports.stripe_in.balance(), RailBalance(False))
                      if self.cfg.stripe_incoming else RailBalance(False))
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
            # Stripe incoming (ADR 0009 amendment, Oct 5 2026): ZBM 1060 less payouts already on their way to the bank
            # (they left the Stripe balance at creation; F13p posts when they arrive) vs Stripe's own balance
            used_1060 = any(k[0] == "zbm" and k[1] == "1060" for k in self.balances)
            ok_s = isinstance(stripe_obs, RailBalance) and stripe_obs.available and self._fresh(stripe_obs.as_of, 26)
            if not ok_s and not used_1060 and not self.cfg.stripe_incoming:
                legs.append(I7.not_in_use("L3", "zbm:1060"))
            else:
                legs.append(I7.leg("L3", "zbm:1060", self.bal("1060", entity="zbm") - self._stripe_in_transit(),
                                   M.D(stripe_obs.balance) if ok_s else None, stripe_obs.source_sha256 if ok_s else None,
                                   "Stripe balance (available + pending) vs 1060 less payouts in transit" if ok_s
                                   else "Stripe balance unavailable"))
            bad_items = [i for i, lk in lookups.items() if not (isinstance(lk, RailLookup) and lk.available and lk.found
                                                                and lk.status in ("submitted", "paid"))]
            if lookups:
                legs.append({"leg": "L3", "subject": "items", "expected": str(len(lookups)),
                             "observed": str(len(lookups) - len(bad_items)), "observed_source_sha256": sha(sorted(lookups)),
                             "difference": str(len(bad_items)), "status": "matched" if not bad_items else "break",
                             "note": f"{len(bad_items)} open item(s) not confirmed by the rail"[:200]})
            sub = self._expected_l4_sub()
            for acct in ("2010", "2020", "2030", "1200"):
                legs.append(I7.leg("L4", f"zbc:{acct}", self.bal(acct), M.total(sub[acct].values()),
                                   sha({"records": acct}), "control balance vs the operational records"))
                # AEGIS N17-1: per sub-ledger too (payee / item / campaign), never totals only
                journal = J.subledgers(self.balances, "zbc", acct)
                for s in sorted(set(journal) | set(sub[acct])):
                    j, e = journal.get(s, M.ZERO), sub[acct].get(s, M.ZERO)
                    if j == 0 and e == 0:
                        continue
                    legs.append(I7.leg("L4", f"zbc:{acct}:{s}"[:160], j, e, sha({"records": acct, "sub": s}),
                                       "sub-ledger vs the operational records"))
            # ZBM media (ADR 0009 amendment, Oct 5 2026): 2120 and 1150 per buy vs the media buy records
            msub = self._expected_media_sub()
            for acct in ("2120", "1150"):
                legs.append(I7.leg("L4", f"zbm:{acct}", self.bal(acct, entity="zbm"), M.total(msub[acct].values()),
                                   sha({"records": f"zbm:{acct}"}), "media control balance vs the media buy records"))
                journal = J.subledgers(self.balances, "zbm", acct)
                for s in sorted(set(journal) | set(msub[acct])):
                    j, e = journal.get(s, M.ZERO), msub[acct].get(s, M.ZERO)
                    if j == 0 and e == 0:
                        continue
                    legs.append(I7.leg("L4", f"zbm:{acct}:{s}"[:160], j, e, sha({"records": f"zbm:{acct}", "sub": s}),
                                       "media sub-ledger vs the media buy records"))
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
        for sf in self._open_shortfalls():
            out.append(R.item("DEPOSIT_SHORTFALL", f"deposit shortfall {sf['shortfall_id']} ({sf['amount']}) open: Andre "
                                                   f"tops up first ({action} refused)", [sf["shortfall_id"]]))
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
                    "sweepable": M.fmt(T.sweepable(self.balances, self.cfg.restricted_buffer,
                                                   self._reserved_sweeps(PENDING_SWEEP))),
                    "independent": (last or {}).get("treasury", {}).get("independent"),
                    "account_title": C.DEPOSITS_ACCOUNT_TITLE, "custody_model": "own_deposit",
                    "open_operations": [{**o, "content_sha256": self.treasury_op_sha(o)}
                                        for o in self.db["treasury_ops"].values() if o["status"] in OPEN_OPS],
                    "shortfalls": [x for x in self.db["shortfalls"].values() if x["status"] == "open"]}

    def _reserved_sweeps(self, statuses: tuple, exclude: Optional[str] = None) -> Decimal:
        """AEGIS N17-2: sweeps already proposed/approved/in flight are spoken for; ``sweepable`` subtracts them."""
        return M.total(o["amount"] for o in self.db["treasury_ops"].values()
                       if o["kind"] == "sweep" and o["status"] in statuses and o["op_id"] != exclude)

    def _sweep_reasons(self, t: dict, when: str) -> list[dict]:
        """FIN-18 for a sweep, checked at approval AND at execution: controls green, and the amount within what is
        sweepable after every other sweep already approved or in flight."""
        reasons = self._control_block_reasons("sweep")
        sw = T.sweepable(self.balances, self.cfg.restricted_buffer, self._reserved_sweeps(LIVE_SWEEP, t["op_id"]))
        if M.D(t["amount"]) > sw:
            reasons.append(R.item("TREASURY_BREACH", f"sweep {t['amount']} exceeds the sweepable amount {M.fmt(sw)} at "
                                                     f"{when} (other approved sweeps included)"))
        return reasons

    @staticmethod
    def treasury_op_sha(t: dict) -> str:
        """The content hash Andre approves a treasury operation by: SHA-256 over its identity (op id, kind, entity,
        amount, reference). Stored on the record when it is created; computed the same way, deterministically, for a
        record written without one (refund payments before AEGIS c0869c4 H-N1)."""
        return t.get("content_sha256") or sha({k: t.get(k) for k in ("op_id", "kind", "entity", "amount", "ref_id")})

    def _new_treasury_op(self, op: Op, kind: str, amount, ref_id: Optional[str], by: str, request_id: str) -> dict:
        tid = rid("trx", kind, by, request_id)
        if op.get("treasury_ops", tid) is not None:
            # sweep B-F4: an existing operation is never rebuilt (that reset ``attempts`` and re-used a posting key
            # whose entry was already reversed: the bank moved the money while the journal netted to zero)
            raise Conflict(f"treasury operation {tid} already exists ({op.get('treasury_ops', tid)['status']}); it is "
                           "never re-created")
        t = {"op_id": tid, "kind": kind, "entity": "zbc", "amount": M.fmt(amount), "ref_id": ref_id,
             "status": "proposed", "proposed_by": by, "proposed_at": iso(self._now()), "attempts": 0}
        t["content_sha256"] = self.treasury_op_sha(t)
        op.put("treasury_ops", tid, t)
        return t

    def propose_sweep(self, principal: str, request_id: str, amount) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "sweeps", M.fmt(amount))
            if ent:
                return ent["response"]
            self.require_rules()
            reasons = self._control_block_reasons("sweep")
            sw = T.sweepable(self.balances, self.cfg.restricted_buffer, self._reserved_sweeps(PENDING_SWEEP))
            if amount > sw:
                reasons.append(R.item("TREASURY_BREACH", f"sweep {M.fmt(amount)} exceeds sweepable {M.fmt(sw)} (sweeps "
                                                         "already proposed or approved are subtracted)"))
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

    def _treasury_replay(self, ent_response: Optional[dict], tid: str, request_id: str) -> dict:
        """Sweep B-F4: a replayed treasury request answers the operation's CURRENT state, never a rebuilt one."""
        t = self.db["treasury_ops"][tid]
        base = dict(ent_response or {})
        return {**base, "operation": t,
                "transfer": base.get("transfer") or {"op_id": tid, "status": t["status"]},
                "ledger_event_ids": base.get("ledger_event_ids", []), "request_id": request_id, "replayed": True}

    def top_up(self, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "top-ups", {k: str(v) for k, v in body.items()})
            tid = rid("trx", "top_up", "andre", request_id)
            if ent:
                return self._treasury_replay(ent["response"], tid, request_id) if tid in self.db["treasury_ops"] \
                    else ent["response"]
            if tid in self.db["treasury_ops"]:
                return self._treasury_replay(None, tid, request_id)
            self.require_rules()
            sf_id = body.get("shortfall_id")
            if sf_id is not None:
                sf = self.db["shortfalls"].get(sf_id)
                if sf is None or sf["status"] != "open":
                    raise Conflict("no open deposit shortfall with that id")
                if M.D(body["amount"]) < M.D(sf["amount"]):
                    raise Refused("top-up below the shortfall", [R.item("DEPOSIT_SHORTFALL", f"a top-up for shortfall "
                                                                        f"{sf_id} must be at least {sf['amount']}")])
            op = Op(self, f"topup|{request_id}", "andre", "treasury")
            self._injection(op, {"notes": body.get("notes")})
            t = self._new_treasury_op(op, "top_up", body["amount"], body.get("reason_code"), "andre", request_id)
            t = {**t, "status": "approved", "approved_at": iso(self._now()), "approved_by": "andre",
                 "shortfall_id": sf_id}
            op.put("treasury_ops", t["op_id"], t)
            op.record(derived_id("tup", t["op_id"]), "top_up_approved", "andre", t["op_id"],
                      {"op_id": t["op_id"], "amount": t["amount"], "reason_code": body.get("reason_code")},
                      f"Andre approved an operating top-up of restricted cash: {t['amount']}")
            # sweep B-F4: the answer is persisted WITH the approval (a replay after a restart finds it)
            self._idem_add(op, key, h, {"operation": t, "transfer": None, "ledger_event_ids": op.events,
                                        "request_id": request_id})
            self._commit(op)
        res = self._execute_transfer(t["op_id"])
        with self.lock:
            return self._idem_mem(key, h, {"operation": self.db["treasury_ops"][t["op_id"]], "transfer": res,
                                           "ledger_event_ids": op.events, "request_id": request_id})

    def decide_treasury(self, request_id: str, kind: str, op_id: str, body: dict) -> dict:
        key, h, ent = self._idem("andre", request_id, f"treasury/{op_id}", body)
        if ent:
            with self.lock:
                return self._treasury_replay(ent["response"], op_id, request_id) \
                    if op_id in self.db["treasury_ops"] else ent["response"]
        with self.lock:
            self.require_rules()
            t = self.db["treasury_ops"].get(op_id)
            if t is None or t["kind"] != kind:
                raise NotFound("no such treasury operation")
            if t["status"] != "proposed":
                raise Conflict(f"operation is already {t['status']}")
            if body["content_sha256"] != self.treasury_op_sha(t):
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
                reasons += self._sweep_reasons(t, "approval")
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
            self._idem_add(op, key, h, {"operation": op.get("treasury_ops", op_id), "transfer": None,
                                        "request_id": request_id})
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

    def _execution_reasons(self, t: dict) -> list[dict]:
        """Re-checked at EXECUTION, not only at approval (AEGIS N17-2)."""
        if t["kind"] == "sweep":
            return self._sweep_reasons(t, "execution")
        if t["kind"] == "funding":
            b = self.db["batches"].get(t["ref_id"])
            if not b or b["status"] not in ("approved", "releasing") or M.D(t["amount"]) > M.D(b["totals"]["net"]):
                return [R.item("NOT_APPROVED", "funding only for an Andre-approved batch, up to its net total")]
        return []

    def _execute_transfer(self, tid: str) -> dict:
        """Record first, the bank too (AEGIS N17-2): (1) under the lock the Andre-approved operation is re-checked
        and its journal posting is validated, staged and COMMITTED (ledger events, local-log anchor, fsync) with
        the operation marked ``executing``; (2) only then is the bank adapter asked to move the money, with the
        operation id as the idempotency key, outside the lock; (3) the outcome is recorded: accepted -> ``done``;
        refused or unavailable -> the posting is REVERSED by a recorded reversal entry and the operation goes back
        to ``approved`` (retried by the rail-sync job); an unknown outcome (the adapter raised, or answered
        malformed) -> ``bank_unknown`` with a break for Andre, the posting kept, and the retry asks the bank again
        with the SAME key (never a second posting). A posting is never left dangling and the bank never moves money
        the journal has not recorded."""
        with self.lock:
            t = self.db["treasury_ops"].get(tid)
            # sweep B-F2: ``executing`` too -- an operation whose bank answer could not be booked (a 503 after the bank
            # call) is asked again with the SAME key, never left stuck
            if t is None or t["status"] not in ("approved", "bank_unknown", "executing"):
                return {"op_id": tid, "status": t["status"] if t else "unknown"}
            if tid in self.xfer_in_flight:
                # AEGIS 5a56a3a M2: another caller (a retry run, or the request that approved it) is asking the bank
                # for this operation right now; a second ask is never sent in parallel. That caller records the answer.
                return {"op_id": tid, "status": t["status"], "in_flight": True}
            # AEGIS f751017 C1: a re-entry into ``executing`` / ``bank_unknown`` means an earlier ask's outcome was
            # never recorded: money may have moved. From then on a refusal is never read as "nothing moved".
            reentry = t["status"] in ("executing", "bank_unknown")
            stop = self._bank_window_check(tid, t)
            if stop is not None:
                return stop
            memo, lines, (src, dst) = self._transfer_lines(t)
            if t["status"] == "approved":
                if t.get("outcome_unknown"):
                    # never a new attempt (a new posting and a new ask) once money may have moved (AEGIS f751017 C1)
                    return {"op_id": tid, "status": t["status"], "outcome_unknown": True}
                n = t.get("attempts", 0) + 1
                op = Op(self, f"xfer|{tid}|{n}", "intel_08_treasury", tid)
                reasons = self._execution_reasons(t)
                if reasons:
                    op.put("treasury_ops", tid, {**t, "status": "refused_at_execution", "reasons": R.dedupe(reasons),
                                                 "refused_at": iso(self._now())})
                    op.record(derived_id("trxr", tid, n), "treasury_execution_refused", "intel_08_treasury", tid,
                              {"op_id": tid, "codes": R.codes(reasons)},
                              f"Approved {t['kind']} refused at execution (FIN-18 re-checked): nothing posted or sent")
                    self._commit(op)
                    return {"op_id": tid, "status": "refused_at_execution", "reasons": R.dedupe(reasons)}
                try:
                    e = self._post(op, "zbc", lines, memo, {"kind": t["kind"], "id": tid}, f"{memo}|{tid}|{n}",
                                   approval_ref=tid, actor="intel_08_treasury")
                except PostingRefused as exc:
                    op.put("treasury_ops", tid, {**t, "status": "posting_refused", "reasons": exc.reasons})
                    bid = rid("brk", "xfer", tid)
                    op.put("breaks", bid, {"break_id": bid, "leg": "L1", "subject": f"transfer:{tid}",
                                           "difference": t["amount"], "opened_at": iso(self._now()),
                                           "opened_on": self._today_la().isoformat(), "owner": "andre",
                                           "explanation_code": "unknown", "status": "open", "resolution": None})
                    self._commit(op)
                    return {"op_id": tid, "status": "posting_refused", "reasons": exc.reasons}
                if e["entry_id"] in (t.get("reversed_entry_ids") or []):
                    # sweep B-F4 guard: a posting already reversed is never the one a new bank instruction relies on
                    raise IntegrityRefused(f"treasury operation {tid}: attempt {n} would re-use a reversed posting; "
                                           "nothing was sent to the bank")
                t = {**t, "status": "executing", "attempts": n, "entry_id": e["entry_id"],
                     "posted_at": iso(self._now()),
                     # AEGIS f751017 C1: the bank's de-duplication window runs from the FIRST ask ever, carried
                     # across attempts and never reset
                     "first_asked_at": t.get("first_asked_at") or iso(self._now())}
                op.put("treasury_ops", tid, t)
                op.record(derived_id("trxp", tid, n), "treasury_posting_committed", "intel_08_treasury", tid,
                          {"op_id": tid, "entry_id": e["entry_id"], "attempt": n},
                          f"{t['kind']} posted and anchored before the bank instruction (attempt {n})")
                self._commit(op)
            n = t["attempts"]
            if reentry and not t.get("outcome_unknown"):
                op = Op(self, f"xfer|{tid}|{n}|taint", "intel_08_treasury", tid)
                t = {**t, "outcome_unknown": True}
                op.put("treasury_ops", tid, t)
                self._commit(op)
            self.xfer_in_flight.add(tid)        # AEGIS 5a56a3a M2: claimed under the lock, released below
        try:
            bank = self.ports.bank
            g = Gather(self, f"xfer|{tid}|{n}|{t.get('asks', 0) + 1}", "intel_08_treasury", tid)
            ans = g.call("bank_feed", "transfer", ("zbc", src, dst, t["amount"], tid),
                         lambda: bank.transfer("zbc", src, dst, t["amount"], tid), BankTransfer("unknown"))
            outcome = ans.outcome if isinstance(ans, BankTransfer) else "unknown"
            try:
                return self._transfer_outcome(tid, n, t, g, ans, outcome, memo)
            except Unavailable as exc:
                if outcome in ("refused", "unavailable"):
                    raise
                raise self._money_unknown(exc, f"bank transfer {tid}") from None
        finally:
            with self.lock:
                self.xfer_in_flight.discard(tid)

    def _bank_window_check(self, tid: str, t: dict) -> Optional[dict]:
        """Under the lock: None when the bank may be asked now; else why not (AEGIS 5a56a3a M2, f751017 C1/M-N2).

        The bank de-duplicates a key only for a while, so once money may have moved Finance re-asks it only within
        ``RETRY_WINDOW_H`` (23 h) of the FIRST ask ever (``first_asked_at``, set once, carried across attempts).
        Records written before ``first_asked_at`` existed fall back to ``posted_at`` when only one attempt was made
        (that posting WAS the first ask); with no usable anchor the window counts as expired. Past the window the
        operation is marked ``retry_window_expired`` and a break tells Andre, who settles it (``settle_treasury``).
        A clock that reads earlier than the anchor never re-asks (it never extends the window)."""
        if t.get("retry_window_expired"):
            return {"op_id": tid, "status": t["status"], "retry_window_expired": True}
        # only an operation that may already have moved money is bounded: re-entry into ``executing`` /
        # ``bank_unknown``, or any earlier unknown answer. While every answer so far was a clean refusal nothing
        # moved, so there is no earlier move for a re-sent key to duplicate (a new attempt is a first payment).
        if t["status"] not in ("executing", "bank_unknown") and not t.get("outcome_unknown"):
            return None
        anchor = t.get("first_asked_at") or (t.get("posted_at") if t.get("attempts", 0) <= 1 else None)
        now = self._now()
        if anchor is not None:
            age = now - parse_iso(anchor)
            if age < timedelta(0):
                return {"op_id": tid, "status": t["status"], "clock_behind": True}
            if age < timedelta(hours=I4.RETRY_WINDOW_H):
                return None
        op = Op(self, f"xfer|{tid}|{t.get('attempts', 0)}|expired", "intel_08_treasury", tid)
        bid = rid("brk", "xfer-expired", tid)
        op.put("treasury_ops", tid, {**t, "retry_window_expired": True, "retry_window_expired_at": iso(now),
                                     "window_anchor": anchor})
        if bid not in self.db["breaks"]:
            op.put("breaks", bid, {"break_id": bid, "leg": "L1", "subject": f"transfer:{tid}", "difference": t["amount"],
                                   "opened_at": iso(now), "opened_on": self._today_la().isoformat(),
                                   "owner": "andre", "explanation_code": "unknown", "status": "open",
                                   "resolution": None, "kind": "bank_retry_window_expired"})
            op.record(derived_id("brk", bid), "break_opened", I7.ACTOR, bid,
                      {"break_id": bid, "leg": "L1", "subject": f"transfer:{tid}", "difference": t["amount"],
                       "kind": "bank_retry_window_expired"},
                      "Break opened: bank retry window passed; Finance stops asking the bank (Andre settles)")
        self._commit(op)
        return {"op_id": tid, "status": t["status"], "retry_window_expired": True, "break_id": bid}

    def _transfer_outcome(self, tid: str, n: int, t: dict, g: Gather, ans, outcome: str, memo: str) -> dict:
        with self.lock:
            t = self.db["treasury_ops"][tid]
            if t["status"] not in ("executing", "bank_unknown"):
                return {"op_id": tid, "status": t["status"]}
            op = Op(self, f"xfer|{tid}|{n}|{t.get('asks', 0) + 1}|{outcome}", "intel_08_treasury", tid, g)
            e = self.entries_by_id[t["entry_id"]]
            asks = t.get("asks", 0) + 1
            if outcome == "accepted":
                self._transfer_done(op, {**t, "asks": asks}, ans.ref)
                self._commit(op)
                return {"op_id": tid, "status": "done", "entry_id": e["entry_id"]}
            if outcome in ("refused", "unavailable") and not t.get("outcome_unknown"):
                rev = self._post(op, "zbc", J.reversal_lines(e), memo, {"kind": t["kind"], "id": tid},
                                 f"{memo}rev|{tid}|{n}", approval_ref=tid, reverses=e["entry_id"],
                                 actor="intel_08_treasury", fact=True)
                op.put("treasury_ops", tid, {**t, "status": "approved", "asks": asks, "last_outcome": outcome,
                                             "entry_id": None, "reversed_entry_ids": (t.get("reversed_entry_ids") or [])
                                             + [e["entry_id"], rev["entry_id"]]})
                op.record(derived_id("trxv", tid, n), "treasury_posting_reversed", "intel_08_treasury", tid,
                          {"op_id": tid, "entry_id": e["entry_id"], "reversal_entry_id": rev["entry_id"],
                           "outcome": outcome}, f"Bank {outcome} the {t['kind']}: posting reversed (recorded)")
                self._commit(op)
                return {"op_id": tid, "status": "approved", "transfer": outcome, "reversal_entry_id": rev["entry_id"]}
            # an unknown outcome -- or a refusal after an earlier unknown one (AEGIS f751017 C1: the earlier ask may
            # have moved the money; the bank's later "refused" / "unavailable" says nothing about it): the posting is
            # kept, the operation stays ``bank_unknown`` and only Andre settles it
            bid = rid("brk", "xfer-unknown", tid)
            op.put("treasury_ops", tid, {**t, "status": "bank_unknown", "asks": asks, "last_outcome": outcome,
                                         "outcome_unknown": True})
            if bid not in self.db["breaks"]:
                op.put("breaks", bid, {"break_id": bid, "leg": "L1", "subject": f"transfer:{tid}", "difference": t["amount"],
                                       "opened_at": iso(self._now()), "opened_on": self._today_la().isoformat(),
                                       "owner": "andre", "explanation_code": "unknown", "status": "open",
                                       "resolution": None, "kind": "bank_state_unknown"})
                op.record(derived_id("brk", bid), "break_opened", I7.ACTOR, bid,
                          {"break_id": bid, "leg": "L1", "subject": f"transfer:{tid}", "difference": t["amount"],
                           "kind": "bank_state_unknown"}, "Break opened: bank outcome of a posted transfer unknown")
            self._commit(op)
            return {"op_id": tid, "status": "bank_unknown", "break_id": bid, "transfer": outcome}

    def _transfer_done(self, op: Op, t: dict, bank_ref: Optional[str], **extra) -> None:
        """The money moved: the posting stands and the operation is ``done`` (its side effects with it)."""
        tid = t["op_id"]
        op.put("treasury_ops", tid, {**t, "status": "done", "bank_ref": bank_ref, "done_at": iso(self._now()), **extra})
        if t["kind"] == "refund_payment":
            r = self.db["refunds"][t["ref_id"]]
            op.put("refunds", r["refund_id"], {**r, "status": "paid", "paid_at": iso(self._now())})
            prof = self.db["profiles"].get(r["campaign_id"])
            if prof:
                op.put("profiles", r["campaign_id"], {**prof, "status": "closed"})
            op.record(derived_id("rfdpd", r["refund_id"]), "refund_paid", "intel_08_treasury", r["refund_id"],
                      {"refund_id": r["refund_id"], "amount": r["amount"]}, f"Refund paid: {r['amount']}")
        if t["kind"] == "top_up" and t.get("shortfall_id"):
            self._close_shortfall(op, t["shortfall_id"], tid)

    SETTLEABLE = ("executing", "bank_unknown")

    def settle_treasury(self, request_id: str, op_id: str, body: dict) -> dict:
        """AEGIS f751017 M-N1: Andre settles a treasury operation whose bank outcome Finance cannot know (an unknown
        answer, a refusal after one, or the retry window passed). He checked the bank statement:
          * ``moved``     -> the posting stands, the operation is ``done`` (bank_ref recorded);
          * ``not_moved`` -> the posting is reversed by a recorded reversal entry and the operation ends
                             ``not_moved`` (no longer reserved; a new proposal is needed to try again).
        A refund payment settled ``not_moved`` leaves its refund ``payment_not_moved`` (the client is still owed;
        ``repay_refund`` pays it again). Its open bank breaks are resolved with it. Andre-only (route), recorded, idempotent per request_id, bound to
        the operation's ``content_sha256``; refused while a bank call for it is in flight."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"treasury-settle/{op_id}",
                                     {k: str(v) for k, v in body.items()})
            if ent:
                return ent["response"]
            self.require_rules()
            t = self.db["treasury_ops"].get(op_id)
            if t is None:
                raise NotFound("no such treasury operation")
            want = self.treasury_op_sha(t)
            if not isinstance(body.get("content_sha256"), str) or body["content_sha256"] != want:
                raise Conflict("operation changed since you read it (content_sha256 mismatch: read it again from "
                               "GET /fin/v1/treasury)")
            if t["status"] not in self.SETTLEABLE or not t.get("entry_id") or t["entry_id"] not in self.entries_by_id:
                raise Conflict(f"only an operation whose bank outcome is unknown can be settled (it is {t['status']})")
            if op_id in self.xfer_in_flight:
                raise Conflict("a bank call for this operation is in flight; settle it once it returns")
            outcome = body["outcome"]
            op = Op(self, f"trsettle|{request_id}", "andre", op_id)
            e = self.entries_by_id[t["entry_id"]]
            settled = {"settled_by": "andre", "settled_at": iso(self._now()), "settled_outcome": outcome,
                       "settle_note": body.get("note")}
            rev_id = None
            if outcome == "moved":
                self._transfer_done(op, t, body.get("bank_ref"), **settled)
            else:
                memo = e["memo_code"]
                rev = self._post(op, "zbc", J.reversal_lines(e), memo, {"kind": t["kind"], "id": op_id},
                                 f"{memo}rev|{op_id}|settle", approval_ref=request_id, reverses=e["entry_id"],
                                 actor="andre", fact=True)
                rev_id = rev["entry_id"]
                op.put("treasury_ops", op_id, {**t, "status": "not_moved", "entry_id": None, **settled,
                                               "reversed_entry_ids": (t.get("reversed_entry_ids") or [])
                                               + [e["entry_id"], rev_id]})
                if t["kind"] == "refund_payment":
                    # the client is still owed (F6 stands in 2050): the refund waits for Andre's repay
                    r = self.db["refunds"][t["ref_id"]]
                    op.put("refunds", r["refund_id"], {**r, "status": "payment_not_moved",
                                                       "payment_not_moved_at": iso(self._now())})
            for kind in ("xfer-unknown", "xfer-expired"):
                bid = rid("brk", kind, op_id)
                b = self.db["breaks"].get(bid)
                if b is not None and b["status"] != "resolved":
                    op.put("breaks", bid, {**b, "status": "resolved", "resolution": {
                        "entry_id": rev_id or e["entry_id"], "approved_by": "andre", "at": iso(self._now()),
                        "settled_outcome": outcome}})
            op.record(derived_id("trset", op_id), "treasury_settled_by_andre", "andre", op_id,
                      {"op_id": op_id, "outcome": outcome, "entry_id": e["entry_id"], "reversal_entry_id": rev_id,
                       "bank_ref": body.get("bank_ref")},
                      f"Andre settled a {t['kind']} whose bank outcome was unknown: {outcome}")
            resp = {"operation": op.get("treasury_ops", op_id), "reversal_entry_id": rev_id,
                    "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def retry_transfers(self) -> int:
        """Re-drive open treasury operations (rail-sync job). Returns how many asked the bank or were settled now;
        one in flight elsewhere or past its retry window is skipped (AEGIS 5a56a3a M2)."""
        with self.lock:            # a snapshot under the lock: a concurrent commit never changes the dict mid-iteration
            todo = sorted(tid for tid, t in self.db["treasury_ops"].items()
                          if t["status"] in ("approved", "bank_unknown", "executing") and tid not in self.xfer_in_flight
                          and not t.get("retry_window_expired")
                          and not (t["status"] == "approved" and t.get("outcome_unknown")))
        n = 0
        for tid in todo:
            try:
                res = self._execute_transfer(tid)
            except (Unavailable, IntegrityRefused):
                continue                # sweep B-F2: one stuck operation never blocks the others
            if not (res.get("in_flight") or res.get("retry_window_expired") or res.get("clock_behind")
                    or res.get("outcome_unknown")):
                n += 1
        return n

    # ================================================================== jobs

    def run_job(self, principal: str, request_id: str, job: str) -> dict:
        if job not in JOBS:
            raise NotFound("unknown job")
        self._idem(principal, request_id, f"jobs/{job}", None)
        day = self._now().date().isoformat()
        jk = f"{job}|{day}" if job not in REPEATABLE_JOBS else f"{job}|{iso(self._now())}|{request_id}"[:200]
        prior = self.db["job_runs"].get(jk)
        if prior is not None:
            return {"job": job, "day": day, "summary": prior["summary"], "already_ran": True, "request_id": request_id}
        self.require_rules()
        prefix = f"job|{job}|{day}" if job not in REPEATABLE_JOBS else f"job|{jk}"
        fn = {"accrual": self.job_accrual, "clawback-sync": self.job_clawback_sync, "tax-sync": self.job_tax_sync,
              "rail-sync": self.job_rail_sync, "stripe-sessions": self.job_stripe_sessions}[job]
        summary = fn(prefix)
        with self.lock:
            op = Op(self, f"{prefix}|done", "intel_10_evidence_audit", job)
            op.put("job_runs", jk, {"job": job, "day": day, "at": iso(self._now()), "summary": summary})
            op.record(derived_id("job", job, day) if job not in REPEATABLE_JOBS else derived_id("job", jk),
                      "control_result_recorded", "intel_10_evidence_audit", jk[:128],
                      {"job": job, "day": day, "summary_sha256": sha(summary)}, f"Job {job} ran")
            self._commit(op)
        return {"job": job, "day": day, "summary": summary, "already_ran": False, "request_id": request_id}
