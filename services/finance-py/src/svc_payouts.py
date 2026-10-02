"""
Payout run (intelligence 4): maker (scheduler), checker (Andre), release worker (scheduler), exceptions (Andre).

Gates (§C.4), every one evaluated every time, all reasons collected:
  1 certification still certified/revised, no open V&I finding     (V&I)                 FIN-04
  2 Compliance payout ruling allowed; no open Compliance hold       (Compliance)          FIN-04
  3 tax: W-9 + TIN matched, or W-8 current + outside-US attestation (tax agent)           FIN-11
  4 OFAC: fresh clear screen (payee; each owner of an entity payee) (Compliance sanctions) FIN-13
  5 rail account verified + payouts enabled; rail reaches country; country operate/conditional  FIN-30
  6 no payee hold; a confirmed callback + cooling-off after a change; a vault-verifiable callback channel  FIN-14
  7 net >= FIN_MIN_PAYOUT (carry forward, not an error)                                   FIN-10
  8 velocity / exposure limits -> exception queue                                          FIN-15
  9 reconciliation green and no open break  (whole run)                                    FIN-17
 10 treasury invariant, journal and independent (bank + rail); rail funded at release (whole run)  FIN-18
 11 journal integrity FC-04, access review FC-05, B-notice timers FC-06 (whole run)
 12 every Finance rule and Compliance row relied on is in force                            FIN-08
An input that is absent, stale or answered by a stand-in FAILS its gate. Release re-runs every gate per item and may
only narrow a batch (exclude an item); an item whose amounts would differ from what Andre approved is excluded.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import timedelta
from decimal import Decimal
from typing import Optional

import chart as C
import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, Invalid, NotFound
from intelligences import i01_journal as J
from intelligences import i04_payout_run as I4
from intelligences import i05_clawback as I5
from intelligences import i06_tax as I6
from intelligences import i07_reconciliation as I7
from intelligences import i08_treasury as T
from ledger import derived_id
from ports import (BankBalance, Certification, ComplianceRuling, HoldsAnswer, JurisdictionAnswer, RailAccount,
                   RailBalance, RailLookup, RailSubmit, SanctionsAnswer, TaxAgentAnswer)
from service import RUNNABLE, Gather, Op, PostingRefused, Refused, rid, sha, unbatched

OFAC_ROWS = ("US-OFAC-01", "US-OFAC-02", "US-OFAC-03")
CERT_ROWS = ("HR-12", "HR-13")
RAIL_ROWS = ("US-STRIPE-KYC",)


def _asdict(x):
    return dataclasses.asdict(x) if dataclasses.is_dataclass(x) else x


def _gate_view(inputs: dict) -> dict:
    """Hashable view of every gate input. ``ofac`` values are (role, subject, answer) triples; ``rows`` values are
    lists of reason items (a Compliance row that is unavailable or not verified) -- before fix 18 a non-empty
    ``rows`` list was unpacked as a triple and the whole run answered 500 instead of excluding the payee."""
    out: dict = {}
    for k, v in inputs.items():
        if not isinstance(v, dict):
            out[k] = _asdict(v)
        elif k == "ofac":
            out[k] = {kk: [[a, b, _asdict(c)] for a, b, c in vv] for kk, vv in v.items()}
        else:
            out[k] = {kk: (list(vv) if isinstance(vv, list) else _asdict(vv)) for kk, vv in v.items()}
    return out


class PayoutsMixin:
    # ================================================================== gate inputs (gathered OUTSIDE the lock)

    def _rails_in_use(self, rail: str) -> list[str]:
        used = {rail}
        for r, acct in C.RAIL_ACCOUNT.items():
            if r in self.cfg.rails and any(k[1] == acct for k in self.balances):
                used.add(r)
        return sorted(used)

    def _gather_gates(self, g: Gather, rail: str, payees: dict[str, list[dict]]) -> dict:
        vi, cmp_, tax, vault, bank = self.ports.vi, self.ports.compliance, self.ports.tax, self.ports.vault, self.ports.bank
        out: dict = {"certs": {}, "rulings": {}, "holds": {}, "tax": {}, "ofac": {}, "rail": {}, "juris": {},
                     "vault": {}, "rows": {}}
        for pid, payables in payees.items():
            payee = self.db["payees"].get(pid) or {}
            for p in payables:
                sid, rul = p["submission_id"], p["compliance_ruling_id"]
                out["certs"][p["payable_id"]] = g.call("verification_integrity", "certification", (sid,),
                                                       lambda s=sid: vi.certification(s), Certification(False))
                out["rulings"][p["payable_id"]] = g.call("compliance_38", "ruling", (rul,), lambda r=rul: cmp_.ruling(r),
                                                         ComplianceRuling(False))
            subjects = tuple(sorted({("payee", pid)} | {("submission", p["submission_id"]) for p in payables}
                                    | {("campaign", p["campaign_id"]) for p in payables}))
            out["holds"][pid] = g.call("compliance_38", "holds", (list(map(list, subjects)),),
                                       lambda s=subjects: cmp_.holds(s), HoldsAnswer(False))
            out["tax"][pid] = g.call("tax_agent", "status", (pid,), lambda x=pid: tax.status(x), TaxAgentAnswer(False))
            screens = [("payee", pid)] + [("owner", o) for o in payee.get("owner_subject_ids") or []]
            out["ofac"][pid] = [(role, sub, g.call("compliance_38", "sanctions_status", (sub, role),
                                                   lambda s=sub, r=role: cmp_.sanctions_status(s, r), SanctionsAnswer(False)))
                                for role, sub in screens]
            railp = self._rail(payee.get("rail"))
            ref = payee.get("rail_account_ref")
            out["rail"][pid] = (g.call(f"rail_{payee['rail']}", "account_status", (ref,),
                                       lambda rp=railp, r=ref: rp.account_status(r), RailAccount(False))
                                if railp is not None and ref else RailAccount(False))
            country, region = payee.get("declared_country"), payee.get("declared_region")
            out["juris"][pid] = (g.call("compliance_38", "jurisdiction", (country, region),
                                        lambda c=country, r=region: cmp_.jurisdiction(c, r), JurisdictionAnswer(False))
                                 if country else JurisdictionAnswer(False))
            ref2 = payee.get("callback_contact_ref")
            out["vault"][pid] = (g.call("vault", "contact_ref_valid", (sha(ref2),),
                                        lambda r=ref2: vault.contact_ref_valid(r), None) if ref2 else None)
            ta = out["tax"][pid]
            for oid in CERT_ROWS + OFAC_ROWS + RAIL_ROWS + I6.obligations_for(ta if isinstance(ta, TaxAgentAnswer)
                                                                              else TaxAgentAnswer(False)):
                if oid not in out["rows"]:
                    out["rows"][oid] = self._rows_reasons(g, (oid,), "FIN-08")
        out["bank_1020"] = g.call("bank_feed", "balance", ("zbc", "1020"), lambda: bank.balance("zbc", "1020"),
                                  BankBalance(False))
        out["rail_balance"] = {}
        for r in self._rails_in_use(rail):
            rp = self._rail(r)
            out["rail_balance"][r] = (g.call(f"rail_{r}", "balance", (), lambda x=rp: x.balance(), RailBalance(False))
                                      if rp is not None else RailBalance(False))
        return out

    # ================================================================== gate judgment (inside the lock)

    def _fresh(self, at: Optional[str], max_h: float) -> bool:
        try:
            t = parse_iso(at) if at else None
        except ValueError:
            return False
        return t is not None and timedelta(0) <= self._now() - t <= timedelta(hours=max_h) or \
            (t is not None and self._now() - t < timedelta(0) and t - self._now() <= timedelta(minutes=5))

    def _open_breaks(self) -> list[dict]:
        out = []
        today = self._today_la()
        for b in self.db["breaks"].values():
            if b["status"] == "open":
                out.append(b)
            elif b["status"] == "explained" and (b.get("clears_by") or "") < today.isoformat():
                out.append(b)
        return out

    def _latest_recon(self) -> Optional[dict]:
        runs = [r for r in self.db["recon_runs"].values()]
        return max(runs, key=lambda r: (r["run_at"], r.get("seq", 0))) if runs else None

    def _run_gate_reasons(self, inputs: dict, rail: str, release_need: Optional[Decimal] = None) -> list[dict]:
        """Gates 9, 10, 11 and the rules part of 12 — each blocks the whole run/release."""
        out = list(self._rules_reasons())
        last = self._latest_recon()
        if last is None:
            out.append(R.item("RECON_BREAK", "no reconciliation run on record (FC-01 red)"))
        else:
            if not self._fresh(last["run_at"], I7.FC01_MAX_AGE_H):
                out.append(R.item("RECON_BREAK", f"latest reconciliation run is stale ({last['run_at']}; max "
                                                 f"{I7.FC01_MAX_AGE_H} h)", [last["recon_id"]]))
            if not I7.all_matched(last["legs"]):
                bad = [f"{l['leg']}:{l['subject']}" for l in last["legs"] if l["status"] not in ("matched", "not_in_use")]
                out.append(R.item("RECON_BREAK", f"latest reconciliation has unmatched legs: {', '.join(bad)[:120]}",
                                  [last["recon_id"]]))
        brks = self._open_breaks()
        if brks:
            out.append(R.item("RECON_BREAK", f"{len(brks)} reconciliation break(s) open (FC-02 red)",
                              [b["break_id"] for b in brks]))
        for sf in self._open_shortfalls():          # AEGIS N17-9: a returned deposit creators were paid against
            out.append(R.item("DEPOSIT_SHORTFALL", f"deposit shortfall {sf['shortfall_id']} ({sf['amount']}) open: "
                                                   "Andre tops up restricted cash first", [sf["shortfall_id"]]))
        pos = T.position(self.balances)
        if pos["gap"] < 0:
            out.append(R.item("TREASURY_BREACH", f"journal: restricted pool {M.sfmt(pos['pool'])} below creator/client "
                                                 f"liabilities {M.sfmt(pos['liabilities'])} (FC-03 red; Andre top-up "
                                                 "F5c needed)"))
        bank = inputs.get("bank_1020")
        bank_val = None
        if not isinstance(bank, BankBalance) or not bank.available or not self._fresh(bank.as_of, 26):
            out.append(R.item("TREASURY_BREACH", "bank feed unavailable or stale: the independent restricted balance "
                                                 "is unknown"))
        else:
            bank_val = M.D(bank.balance)
        rails: dict = {}
        for r, rb in (inputs.get("rail_balance") or {}).items():
            if not isinstance(rb, RailBalance) or not rb.available or not self._fresh(rb.as_of, 26):
                out.append(R.item("TREASURY_BREACH", f"rail balance unavailable or stale ({r}): the independent "
                                                     "position is unknown"))
                rails[r] = None
            else:
                rails[r] = M.D(rb.balance)
        ind = T.independent(bank_val, rails, self.balances)
        if ind["known"] and not ind["ok"]:
            out.append(R.item("TREASURY_BREACH", f"independent: bank + rail {ind['pool']} below liabilities "
                                                 f"{ind['liabilities']}"))
        if release_need is not None and release_need > 0:
            acct = C.RAIL_ACCOUNT[rail]
            jb = self.bal(acct)
            if jb < release_need:
                out.append(R.item("TREASURY_BREACH", f"rail {rail} not funded: journal {acct} {M.fmt(max(M.ZERO, jb))} "
                                                     f"< {M.fmt(release_need)} to release (F4a funding needed)"))
            if rails.get(rail) is not None and rails[rail] < release_need:
                out.append(R.item("TREASURY_BREACH", f"rail {rail} balance below the release total"))
        out += self._control_reasons_for_runs()
        return R.dedupe(out)

    def _control_reasons_for_runs(self) -> list[dict]:
        out = []
        checks = sorted(self.db["integrity_checks"].values(), key=lambda c: (c["checked_at"], c.get("seq", 0)))
        last = checks[-1] if checks else None
        if last is None or last["status"] != "green" or not self._fresh(last["checked_at"], 24):
            out.append(R.item("CONTROL_RED:FC-04", "journal integrity check (chain + ledger anchor + /ledger/verify) "
                                                   "missing, stale or red", rule="FIN-02"))
        if not self.log.verify():
            out.append(R.item("CONTROL_RED:FC-04", "local log hash chain does not verify", rule="FIN-02"))
        fc5 = self.db["controls"].get("FC-05")
        if not fc5 or fc5["result"] != "pass" or not self._fresh(fc5["at"], self.cfg.access_review_days * 24):
            out.append(R.item("CONTROL_RED:FC-05", "access review (token roster) not attested within "
                                                   f"{self.cfg.access_review_days} days", rule="FIN-20"))
        overdue = I6.overdue_timers(list(self.db["tax"].values()), self._today_la())
        if overdue:
            out.append(R.item("CONTROL_RED:FC-06", f"B-notice timer past due for {len(overdue)} payee(s)", rule="FIN-12"))
        return out

    def _payable_reasons(self, p: dict, inputs: dict) -> list[dict]:
        out = []
        cert = inputs["certs"].get(p["payable_id"])
        if not isinstance(cert, Certification) or not cert.available:
            out.append(R.item("DEPENDENCY_UNAVAILABLE:verification_integrity", "V&I certification unavailable",
                              [p["payable_id"]], rule="FIN-04"))
        elif (cert.status not in ("certified", "revised") or cert.open_finding
              or cert.certification_id != p["certification_id"]):
            out.append(R.item("CERT_CHANGED", f"V&I certification now {str(cert.status)[:20]}"
                              f"{' with an open finding' if cert.open_finding else ''}", [p["payable_id"]]))
        elif cert.certified_views != p["certified_views"] and not p["adjustments"]:
            out.append(R.item("CERT_CHANGED", "certified count changed and no clawback record explains it yet (run "
                                              "clawback-sync)", [p["payable_id"]]))
        rul = inputs["rulings"].get(p["payable_id"])
        if not isinstance(rul, ComplianceRuling) or not rul.available:
            out.append(R.item("DEPENDENCY_UNAVAILABLE:compliance_38", "Compliance ruling unavailable", [p["payable_id"]],
                              rule="FIN-04"))
        elif not rul.allowed or rul.gate != "payout" or rul.subject_id != p["submission_id"]:
            out.append(R.item("COMPLIANCE_HOLD", "Compliance payout ruling no longer allows this payable",
                              [p["payable_id"]]))
        return out

    def _payee_reasons(self, payee: dict, inputs: dict) -> tuple[list[dict], bool]:
        """Gates 3, 4, 5, 6 and the payee's part of 12. Returns (reasons, withhold_for_unmatched)."""
        pid = payee["payee_id"]
        out = []
        holds = inputs["holds"].get(pid)
        if not isinstance(holds, HoldsAnswer) or not holds.available:
            out.append(R.item("DEPENDENCY_UNAVAILABLE:compliance_38", "Compliance holds unavailable", rule="FIN-04"))
        elif holds.open_hold_ids:
            out.append(R.item("COMPLIANCE_HOLD", f"{len(holds.open_hold_ids)} open Compliance hold(s) on the payee, "
                                                 "a clip or the campaign", list(holds.open_hold_ids)[:20]))
        ta = inputs["tax"].get(pid)
        ta = ta if isinstance(ta, TaxAgentAnswer) else TaxAgentAnswer(False)
        treasons, withhold_unmatched = I6.gate(ta, self.cfg.unmatched_tin_policy)
        out += treasons
        for role, sub, s in inputs["ofac"].get(pid) or []:
            if not isinstance(s, SanctionsAnswer) or not s.available:
                out.append(R.item("OFAC_STALE", f"sanctions status unavailable for the {role}", obligation_id="US-OFAC-02"))
                continue
            if s.result != "clear":
                out.append(R.item("OFAC_NOT_CLEAR", f"{role} screen result is {str(s.result)[:20]}", [s.screen_id or ""],
                                  obligation_id="US-OFAC-02"))
            if not s.fresh or not self._fresh(s.screened_at, 24 * self.cfg.ofac_max_age_days):
                out.append(R.item("OFAC_STALE", f"{role} screen older than {self.cfg.ofac_max_age_days} day(s) or not "
                                                "fresh", [s.screen_id or ""], obligation_id="US-OFAC-02"))
            if not s.list_current:
                out.append(R.item("OFAC_STALE", f"{role} screened against a list version that is not current",
                                  [s.screen_id or ""], obligation_id="US-OFAC-02"))
        if payee.get("legal_form") == "entity" and not payee.get("owner_subject_ids"):
            out.append(R.item("OFAC_NOT_CLEAR", "entity payee without screened 25%+ owners", obligation_id="US-OFAC-03"))
        acct = inputs["rail"].get(pid)
        if not isinstance(acct, RailAccount) or not acct.available:
            out.append(R.item("RAIL_NOT_READY", "rail account status unavailable (rail stand-in)"))
        elif acct.status != "verified" or not acct.payouts_enabled:
            out.append(R.item("RAIL_NOT_READY", f"rail account {acct.status}, payouts "
                                                f"{'enabled' if acct.payouts_enabled else 'disabled'}",
                              obligation_id="US-STRIPE-KYC"))
        if self.rail_for(payee.get("declared_country")) != payee.get("rail"):
            out.append(R.item("RAIL_NOT_READY", "the payee's rail does not reach the declared country"))
        ju = inputs["juris"].get(pid)
        if not isinstance(ju, JurisdictionAnswer) or not ju.available:
            out.append(R.item("RAIL_NOT_READY", "Compliance jurisdiction answer unavailable"))
        elif ju.status not in ("operate", "conditional"):
            out.append(R.item("RAIL_NOT_READY", f"country is {ju.status} at Compliance"))
        active, why = self._hold_active(payee)
        if active:
            out.append(R.item("PAYEE_HOLD", why, [(payee.get("hold") or {}).get("change_event_id") or ""]))
        if not payee.get("callback_contact_ref"):
            out.append(R.item("PAYEE_HOLD", "no callback contact on file (FIN-14 channel)"))
        elif inputs["vault"].get(pid) is not True:
            out.append(R.item("PAYEE_HOLD", "vault cannot confirm the callback contact on file (vault stand-in)"))
        rows = list(CERT_ROWS + OFAC_ROWS + RAIL_ROWS + I6.obligations_for(ta))
        for oid in rows:
            out += inputs["rows"][oid] if oid in inputs["rows"] else [R.item(
                "DEPENDENCY_UNAVAILABLE:compliance_38", f"Compliance row {oid} not read", obligation_id=oid)]
        return R.dedupe(out), withhold_unmatched

    def _amounts(self, payee: dict, payables: list[dict], withhold_unmatched: bool, op: Optional[Op] = None) -> dict:
        gross = M.total(p["current_amount"] for p in payables)
        open_rcv = max(M.ZERO, self.bal("1200", f"payee:{payee['payee_id']}", op=op))
        flagged = bool(((self.db["tax"].get(payee["payee_id"]) or {}).get("backup_withholding") or {}).get("flag"))
        withhold = flagged or withhold_unmatched
        basis = self.withholding_basis()
        if withhold and basis == "gross":
            # AEGIS N17-12 / spec §C.6: 24% of the GROSS payment; netting takes only what is left after withholding
            withheld = M.pct(gross, I6.BACKUP_PCT)
            netted = I5.netted(open_rcv, M.q(gross - withheld))
        else:
            netted = I5.netted(open_rcv, gross)
            withheld = M.pct(M.q(gross - netted), I6.BACKUP_PCT) if withhold else M.ZERO
        net = M.q(gross - netted - withheld)
        return {"gross": gross, "netted": netted, "withheld": withheld, "net": net}

    def withholding_basis(self) -> str:
        """``FIN_WITHHOLDING_BASIS`` (default ``gross``, spec §C.6). ``gross_minus_netting`` takes effect only while
        the CPA row FIN-CQ-16 is verified; until then the spec's gross basis applies (never less withholding than
        the spec without a memo)."""
        if self.cfg.withholding_basis == "gross_minus_netting" and self.counsel_verified("FIN-CQ-16"):
            return "gross_minus_netting"
        return "gross"

    def _rolling_30d(self, pid: str) -> Decimal:
        since = self._now() - timedelta(days=30)
        return M.total(i["net"] for i in self.db["items"].values() if i["payee_id"] == pid
                       and i["status"] in ("submitting", "submitted", "paid") and i.get("submitted_at")
                       and parse_iso(i["submitted_at"]) >= since)

    def _first_payout(self, pid: str) -> bool:
        p = self.db["payees"].get(pid) or {}
        return not p.get("first_paid_at") and not any(i["payee_id"] == pid and i["status"] in ("submitted", "paid")
                                                      for i in self.db["items"].values())

    def _live_payables(self) -> set:
        out = set()
        for it in self.db["items"].values():
            if it["status"] in I4.LIVE_ITEM:
                out.update(it["payable_ids"])
        return out

    # ================================================================== maker

    def _expire_stale(self, op: Op) -> None:
        now = self._now()
        for b in self.db["batches"].values():
            if b["status"] == "proposed" and now - parse_iso(b["proposed_at"]) > timedelta(hours=self.cfg.approval_ttl_h):
                self._end_batch(op, b, "expired", "batch_approval_expired")
            elif b["status"] == "approved" and now > parse_iso(b["approval_expires_at"]):
                self._end_batch(op, b, "expired", "batch_approval_expired")

    def _end_batch(self, op: Op, b: dict, status: str, event: str) -> None:
        for iid in b["item_ids"]:
            it = op.get("items", iid)
            if it["status"] in ("approved", "proposed"):
                op.put("items", iid, {**it, "status": "cancelled"})
                for pid in it["payable_ids"]:
                    p = op.get("payables", pid)
                    if p and p["status"] == "batched":
                        op.put("payables", pid, unbatched(p))
        op.put("batches", b["batch_id"], {**op.get("batches", b["batch_id"]), "status": status, "ended_at": iso(self._now())})
        op.record(derived_id("bend", b["batch_id"], status), event, I4.ACTOR, b["batch_id"],
                  {"batch_id": b["batch_id"], "status": status}, f"Payout batch {status}")

    def run_payouts(self, principal: str, request_id: str, rail: str) -> dict:
        key, h, ent = self._idem(principal, request_id, "payout-runs", rail)
        if ent:
            return ent["response"]
        if rail not in self.cfg.rails:
            raise Invalid("rail not enabled")
        self.require_rules()
        period = I4.iso_week(self._now().astimezone(I6.LA))
        live = self._live_payables()
        cands: dict[str, list[dict]] = {}
        spoken_for: list[dict] = []
        for p in self.db["payables"].values():
            payee = self.db["payees"].get(p["payee_id"])
            if p["status"] not in RUNNABLE or not payee or payee.get("rail") != rail:
                continue
            if p["payable_id"] in live:          # FIN-09: a payable sits in at most one live item
                spoken_for.append({"payee_id": p["payee_id"], "payable_ids": [p["payable_id"]], "reasons": [
                    R.item("PAYABLE_IN_LIVE_ITEM", "payable already belongs to a live payout item", [p["payable_id"]])]})
                continue
            cands.setdefault(p["payee_id"], []).append(p)
        g = Gather(self, f"run|{principal}|{request_id}", I4.ACTOR, f"run:{rail}:{period}")
        inputs = self._gather_gates(g, rail, cands)
        with self.lock:
            op = Op(self, f"run|{principal}|{request_id}", I4.ACTOR, f"run:{rail}:{period}", g)
            self._expire_stale(op)
            self._lift_expired_holds(op)
            for b in self.db["batches"].values():
                cur = op.get("batches", b["batch_id"])
                if cur["rail"] == rail and cur["period"] == period and cur["status"] not in ("rejected", "expired"):
                    raise Conflict(f"a {rail} batch for {period} already exists ({cur['batch_id']}, {cur['status']})")
            run_reasons = self._run_gate_reasons(inputs, rail)
            bid = rid("bat", rail, period, request_id)
            items, excluded = [], list(spoken_for)
            exceptions_opened = []
            overrides = {e["payee_id"]: e for e in self.db["exceptions"].values() if e["status"] == "approved"
                         and not e.get("consumed_by")}
            batch_net = M.ZERO
            live_now = self._live_payables()
            for pid in sorted(cands):
                payee = op.get("payees", pid)
                # re-read under the lock: a payable changed since the gather (a clawback, another run) is never used stale
                payables = sorted((self.db["payables"][p["payable_id"]] for p in cands[pid]
                                   if self.db["payables"][p["payable_id"]]["status"] in RUNNABLE
                                   and p["payable_id"] not in live_now), key=lambda p: p["payable_id"])
                good = []
                for p in payables:
                    rs = self._payable_reasons(p, inputs)
                    if rs:
                        excluded.append({"payee_id": pid, "payable_ids": [p["payable_id"]], "reasons": rs})
                    else:
                        good.append(p)
                if not good:
                    continue
                preasons, withhold_unmatched = self._payee_reasons(payee, inputs)
                amts = self._amounts(payee, good, withhold_unmatched, op)
                minimum = self.cfg.final_payout_min if payee["status"] == "offboarding" else self.cfg.min_payout
                if amts["net"] < minimum and not (amts["net"] == 0 and amts["netted"] > 0):
                    preasons.append(R.item("BELOW_MINIMUM", f"net {M.fmt(amts['net'])} below {M.fmt(minimum)}: carried "
                                                            "forward to the next run"))
                ids = sorted(p["payable_id"] for p in good)
                ov = overrides.get(pid)
                ov_ok = ov is not None and set(ids) <= set(ov["payable_ids"]) and M.D(ov["net"]) >= amts["net"]
                limit = I4.limit_breaches(amts["net"], self._first_payout(pid), self._rolling_30d(pid), self.cfg)
                if M.q(batch_net + amts["net"]) > self.cfg.limit_batch_net:
                    limit.append(f"batch net would exceed {M.fmt(self.cfg.limit_batch_net)}")
                if len(items) >= self.cfg.limit_batch_items:
                    limit.append(f"batch already holds {self.cfg.limit_batch_items} items")
                if limit and not ov_ok and not preasons:
                    preasons.append(R.item("LIMIT_EXCEEDED", "; ".join(limit)[:200]))
                    eid = rid("exc", pid, sha(ids))
                    if eid not in self.db["exceptions"]:
                        exc = {"exception_id": eid, "payee_id": pid, "payable_ids": ids, "net": M.fmt(amts["net"]),
                               "limits": limit, "status": "open", "opened_at": iso(self._now()), "batch_id": bid}
                        op.put("exceptions", eid, exc)
                        op.record(derived_id("exc", eid), "limit_exception_opened", "intel_09_controls", eid,
                                  {"exception_id": eid, "payee_id": pid, "net": exc["net"], "limits": len(limit)},
                                  "Velocity/exposure limit exceeded: exception queued for Andre")
                        exceptions_opened.append(eid)
                if preasons:
                    excluded.append({"payee_id": pid, "payable_ids": ids, "reasons": R.dedupe(preasons)})
                    continue
                iid = rid("itm", bid, pid)
                items.append({"item_id": iid, "batch_id": bid, "payee_id": pid, "payable_ids": ids,
                              "gross": M.fmt(amts["gross"]), "netted": M.fmt(amts["netted"]),
                              "withheld": M.fmt(amts["withheld"]), "net": M.fmt(amts["net"]),
                              "idempotency_key": I4.idempotency_key(bid, pid, period), "rail": rail, "rail_ref": None,
                              "status": "proposed", "reasons": [], "submitted_at": None, "paid_at": None,
                              "override_exception_id": ov["exception_id"] if ov_ok else None,
                              "payable_amounts": {p["payable_id"]: p["current_amount"] for p in good}})
                batch_net = M.q(batch_net + amts["net"])
            gate_sha = sha(_gate_view(inputs))
            if run_reasons or not items:
                reasons = run_reasons or [R.item("BELOW_MINIMUM", "no payee passed every gate this run", rule="FIN-08")]
                op.record(derived_id("rempty", principal, request_id), "run_empty", I4.ACTOR, f"run:{rail}:{period}",
                          {"rail": rail, "period": period, "codes": R.codes(reasons), "excluded": len(excluded),
                           "blocked": bool(run_reasons)}, f"Payout run {'blocked' if run_reasons else 'empty'}")
                op.put("job_runs", f"run|{principal}|{request_id}", {"kind": "payout_run", "rail": rail, "period": period,
                                                                    "blocked": bool(run_reasons), "at": iso(self._now()),
                                                                    "reasons": reasons, "excluded": excluded})
                resp = {"batch": None, "run_empty": True, "blocked": bool(run_reasons), "reasons": reasons,
                        "reason_lines": R.lines(reasons), "excluded": excluded, "exceptions_opened": exceptions_opened,
                        "ledger_event_ids": op.events, "request_id": request_id}
                if run_reasons:                      # a blocked run is re-evaluated on a retry (never a stored answer)
                    self._commit(op)
                    raise Refused("payout run blocked", run_reasons, excluded=excluded, ledger_event_ids=op.events)
                self._idem_add(op, key, h, resp)
                self._commit(op)
                return resp
            totals = I4.totals(items)
            content = I4.content_sha256(items, totals, gate_sha)
            batch = {"batch_id": bid, "entity": "zbc", "period": period, "rail": rail, "status": "proposed",
                     "item_ids": [i["item_id"] for i in items], "excluded": excluded, "totals": totals,
                     "gate_inputs_sha256": gate_sha, "content_sha256": content, "proposed_at": iso(self._now()),
                     "proposed_by": I4.ACTOR, "approval": None, "release_not_before": None,
                     "approval_expires_at": None, "rules_version": self.rules_version}
            for it in items:
                op.put("items", it["item_id"], it)
                for pid in it["payable_ids"]:
                    p = op.get("payables", pid)
                    op.put("payables", pid, {**p, "status": "batched", "batch_item_id": it["item_id"],
                                             "resume_status": p["status"]})
                if it["override_exception_id"]:
                    ex = op.get("exceptions", it["override_exception_id"])
                    op.put("exceptions", ex["exception_id"], {**ex, "consumed_by": it["item_id"]})
            op.put("batches", bid, batch)
            op.record(derived_id("brun", bid), "payout_run_proposed", I4.ACTOR, bid,
                      {"batch_id": bid, "period": period, "rail": rail, "content_sha256": content, **totals,
                       "gate_inputs_sha256": gate_sha, "excluded": len(excluded)},
                      f"Payout batch proposed: {totals['count']} item(s), net {totals['net']} (Andre approves)")
            try:
                self.ports.push.push("batch_proposed", {"batch_id": bid, "net": totals["net"], "count": totals["count"]})
            except Exception:  # noqa: BLE001 - a push is best effort (stand-in); the batch waits in the queue
                pass
            resp = {"batch": self._batch_view(bid, op), "run_empty": False, "exceptions_opened": exceptions_opened,
                    "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def _batch_view(self, bid: str, op: Optional[Op] = None) -> dict:
        get = (lambda c, k: op.get(c, k)) if op else (lambda c, k: self.db[c].get(k))
        b = get("batches", bid)
        if b is None:
            raise NotFound("no such batch")
        items = [get("items", i) for i in b["item_ids"]]
        return {**b, "items": [{k: v for k, v in it.items() if k not in ("payable_amounts",)} for it in items]}

    def get_batch(self, bid: str) -> dict:
        with self.lock:
            return self._batch_view(bid)

    def list_batches(self, status: Optional[str]) -> dict:
        with self.lock:
            bs = [self._batch_view(b) for b in sorted(self.db["batches"]) if status in (None, self.db["batches"][b]["status"])]
            return {"batches": bs}

    # ================================================================== checker (Andre)

    def _needs_second(self, b: dict) -> bool:
        return bool(self.cfg.second_approver_token) and M.D(b["totals"]["net"]) >= self.cfg.dual_human_threshold

    def _approve_batch(self, op: Op, b: dict, request_id: str, second: Optional[dict]) -> dict:
        """Both approvals present (or only Andre's needed): the batch is approved, bound to its content hash."""
        now = self._now()
        batch_id = b["batch_id"]
        did = rid("dec", batch_id, request_id)
        andre_ap = b.get("andre_approval") or {}
        approval = {"decision_id": did, "approved_by": "andre", "approved_at": andre_ap.get("at") or iso(now),
                    "content_sha256": b["content_sha256"],
                    "second_approver": "second_approver" if second else None,
                    "second_approval_id": second["approval_id"] if second else None,
                    "note_sha256": andre_ap.get("note_sha256")}
        nb = {**b, "status": "approved", "approval": approval,
              "release_not_before": iso(now + timedelta(hours=self.cfg.release_delay_h)),
              "approval_expires_at": iso(now + timedelta(hours=self.cfg.approval_ttl_h))}
        op.put("batches", batch_id, nb)
        for iid in b["item_ids"]:
            it = op.get("items", iid)
            op.put("items", iid, {**it, "status": "approved"})
        op.record(derived_id("bapp", batch_id, b["content_sha256"]), "batch_approved_by_andre", "andre", batch_id,
                  {"batch_id": batch_id, "content_sha256": b["content_sha256"], "decision_id": did,
                   "net": b["totals"]["net"], "second_approver": approval["second_approver"],
                   "second_approval_id": approval["second_approval_id"]},
                  f"Andre approved payout batch ({b['totals']['count']} items, net {b['totals']['net']})")
        return nb

    def decide_batch(self, request_id: str, batch_id: str, body: dict) -> dict:
        """Andre's decision. Above the dual-human threshold the second approver approves on a SEPARATE request with
        a separate identity (``second_approval``, AEGIS N17-13); whichever of the two comes second completes the
        approval. Andre's approval alone is recorded as ``andre_approval`` and the batch stays ``proposed``."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"batch/{batch_id}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            b = self.db["batches"].get(batch_id)
            if b is None:
                raise NotFound("no such batch")
            op = Op(self, f"bdec|{request_id}", "andre", batch_id)
            self._expire_stale(op)
            b = op.get("batches", batch_id)
            if b["status"] != "proposed":
                if op.ops:
                    self._commit(op)
                raise Conflict(f"batch is {b['status']}, not proposed")
            if body["content_sha256"] != b["content_sha256"]:
                raise Conflict("batch content changed since you read it (content_sha256 mismatch); nothing approved")
            self._injection(op, {"note": body.get("note")})
            if body["decision"] == "reject":
                self._end_batch(op, b, "rejected", "batch_rejected")
                resp = {"batch_id": batch_id, "status": "rejected", "ledger_event_ids": op.events,
                        "request_id": request_id}
                self._idem_add(op, key, h, resp)
                self._commit(op)
                return resp
            if b.get("andre_approval"):
                raise Conflict("Andre already approved this batch; it waits for the second approver")
            andre_ap = {"at": iso(self._now()), "content_sha256": b["content_sha256"], "request_id": request_id,
                        "note_sha256": sha(body.get("note")) if body.get("note") else None}
            b = {**b, "andre_approval": andre_ap}
            second = b.get("second_approval")
            if self._needs_second(b) and not (second and second.get("content_sha256") == b["content_sha256"]):
                op.put("batches", batch_id, b)
                op.record(derived_id("bapa", batch_id, b["content_sha256"]), "batch_approval_pending_second", "andre",
                          batch_id, {"batch_id": batch_id, "content_sha256": b["content_sha256"],
                                     "net": b["totals"]["net"]},
                          "Andre approved; the second approver must approve on its own request")
                resp = {"batch_id": batch_id, "status": "awaiting_second_approver", "andre_approval": andre_ap,
                        "ledger_event_ids": op.events, "request_id": request_id}
                self._idem_add(op, key, h, resp)
                self._commit(op)
                return resp
            nb = self._approve_batch(op, b, request_id, second if self._needs_second(b) else None)
            resp = {"batch_id": batch_id, "status": "approved", "approval": nb["approval"],
                    "release_not_before": nb["release_not_before"], "approval_expires_at": nb["approval_expires_at"],
                    "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def second_approve_batch(self, request_id: str, batch_id: str, body: dict) -> dict:
        """The second human approver's own request (own token, own actor ``second_approver``), AEGIS N17-13."""
        with self.lock:
            key, h, ent = self._idem("second_approver", request_id, f"batch2/{batch_id}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            b = self.db["batches"].get(batch_id)
            if b is None:
                raise NotFound("no such batch")
            op = Op(self, f"bdec2|{request_id}", "second_approver", batch_id)
            self._expire_stale(op)
            b = op.get("batches", batch_id)
            if b["status"] != "proposed":
                if op.ops:
                    self._commit(op)
                raise Conflict(f"batch is {b['status']}, not proposed")
            if not self._needs_second(b):
                raise Conflict("this batch does not need a second approver (below FIN_DUAL_HUMAN_THRESHOLD)")
            if body["content_sha256"] != b["content_sha256"]:
                raise Conflict("batch content changed since you read it (content_sha256 mismatch); nothing approved")
            if b.get("second_approval"):
                raise Conflict("the second approver already approved this batch")
            if body["decision"] == "reject":
                self._end_batch(op, b, "rejected", "batch_rejected")
                resp = {"batch_id": batch_id, "status": "rejected", "by": "second_approver",
                        "ledger_event_ids": op.events, "request_id": request_id}
                self._idem_add(op, key, h, resp)
                self._commit(op)
                return resp
            second = {"approval_id": rid("dec2", batch_id, request_id), "by": "second_approver",
                      "at": iso(self._now()), "content_sha256": b["content_sha256"], "request_id": request_id}
            b = {**b, "second_approval": second}
            op.record(derived_id("bap2", batch_id, b["content_sha256"]), "batch_second_approved", "second_approver",
                      batch_id, {"batch_id": batch_id, "content_sha256": b["content_sha256"],
                                 "approval_id": second["approval_id"]},
                      "Second approver approved the payout batch (own request, own token)")
            andre_ap = b.get("andre_approval")
            if andre_ap and andre_ap.get("content_sha256") == b["content_sha256"]:
                nb = self._approve_batch(op, b, andre_ap["request_id"], second)
                resp = {"batch_id": batch_id, "status": "approved", "approval": nb["approval"],
                        "release_not_before": nb["release_not_before"],
                        "approval_expires_at": nb["approval_expires_at"], "ledger_event_ids": op.events,
                        "request_id": request_id}
            else:
                op.put("batches", batch_id, b)
                resp = {"batch_id": batch_id, "status": "awaiting_andre", "second_approval": second,
                        "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    # ================================================================== release worker

    def _batch_lock(self, bid: str) -> threading.Lock:
        with self._xlock:
            return self.release_locks.setdefault(bid, threading.Lock())

    def release_batch(self, principal: str, request_id: str, batch_id: str) -> dict:
        self._idem(principal, request_id, f"release/{batch_id}", None)
        lk = self._batch_lock(batch_id)
        if not lk.acquire(blocking=False):
            raise Conflict("a release of this batch is already in progress; nothing was submitted by this call")
        try:
            return self._release(principal, request_id, batch_id)
        finally:
            lk.release()

    def _release(self, principal: str, request_id: str, batch_id: str) -> dict:
        with self.lock:
            b = self.db["batches"].get(batch_id)
            if b is None:
                raise NotFound("no such batch")
            self.require_rules()
            now = self._now()
            if b["status"] == "proposed":
                raise Refused("batch not approved", [R.item("NOT_APPROVED", "Andre has not approved this batch")])
            if b["status"] not in ("approved", "releasing"):
                raise Conflict(f"batch is {b['status']}")
            appr = b.get("approval") or {}
            if appr.get("content_sha256") != b["content_sha256"] or appr.get("approved_by") != "andre":
                raise Refused("approval does not match", [R.item("NOT_APPROVED", "approval does not bind this batch's "
                                                                                 "content_sha256")])
            if self._needs_second(b) and ((b.get("second_approval") or {}).get("content_sha256") != b["content_sha256"]
                                          or not appr.get("second_approval_id")):
                raise Refused("second approval missing", [R.item("NOT_APPROVED", "this batch needs the second "
                                                                 "approver's own approval of its content hash")])
            if now < parse_iso(b["release_not_before"]):
                raise Refused("too early", [R.item("RELEASE_TOO_EARLY", f"release not before {b['release_not_before']} "
                                                   f"({self.cfg.release_delay_h} h after approval)")])
            expired = now > parse_iso(b["approval_expires_at"])
            if expired and b["status"] == "approved":
                op = Op(self, f"rel|exp|{batch_id}", I4.ACTOR, batch_id)
                self._end_batch(op, b, "expired", "batch_approval_expired")
                self._commit(op)
                raise Refused("approval expired", [R.item("APPROVAL_EXPIRED", "Andre's approval expired "
                                                          f"({self.cfg.approval_ttl_h} h); re-propose")])
            todo = [self.db["items"][i] for i in b["item_ids"] if self.db["items"][i]["status"] == "approved"]
            todo = [] if expired else todo
            payees = {}
            for it in todo:
                payees.setdefault(it["payee_id"], [self.db["payables"][p] for p in it["payable_ids"]])
        g = Gather(self, f"rel|{principal}|{request_id}", I4.ACTOR, batch_id)
        inputs = self._gather_gates(g, b["rail"], payees)
        results = []
        with self.lock:
            todo = [self.db["items"][it["item_id"]] for it in todo if self.db["items"][it["item_id"]]["status"] == "approved"]
            need = M.total(it["net"] for it in todo)
            run_reasons = self._run_gate_reasons(inputs, b["rail"], release_need=need) if todo else []
            probe = Op(self, f"probe|{request_id}", I4.ACTOR, batch_id)
            per_item: dict[str, list[dict]] = {}
            for it in todo:                         # every gate, every item, every time (complete reasons)
                payee = self.db["payees"][it["payee_id"]]
                pays = [self.db["payables"][p] for p in it["payable_ids"]]
                rs = []
                for p in pays:
                    rs += self._payable_reasons(p, inputs)
                    if p["status"] != "batched" or p.get("batch_item_id") != it["item_id"] or \
                            p["current_amount"] != it["payable_amounts"].get(p["payable_id"]):
                        rs.append(R.item("CERT_CHANGED", "payable changed since Andre approved the batch (clawback or "
                                                         "revision): re-propose", [p["payable_id"]]))
                prs, withhold_unmatched = self._payee_reasons(payee, inputs)
                rs += prs
                amts = self._amounts(payee, pays, withhold_unmatched, probe)
                if (M.fmt(amts["gross"]), M.fmt(amts["netted"]), M.fmt(amts["withheld"]), M.fmt(amts["net"])) != \
                        (it["gross"], it["netted"], it["withheld"], it["net"]):
                    rs.append(R.item("CERT_CHANGED", "netting or withholding differs from the approved item: re-propose",
                                     [it["item_id"]]))
                per_item[it["item_id"]] = R.dedupe(rs)
            if run_reasons:
                op = Op(self, f"rel|{principal}|{request_id}", I4.ACTOR, batch_id, g)
                op.record(derived_id("rblk", batch_id, request_id), "release_blocked", I4.ACTOR, batch_id,
                          {"batch_id": batch_id, "codes": R.codes(run_reasons), "items": len(todo)},
                          f"Release blocked: {len(run_reasons)} whole-run reason(s); nothing submitted")
                op.add("evidence", {"batch_id": batch_id, "blocked": True, "codes": R.codes(run_reasons)})
                self._commit(op)
                raise Refused("release blocked", run_reasons, submitted=0,
                              items=[{"item_id": it["item_id"], "status": it["status"],
                                      "reasons": R.dedupe(run_reasons + per_item[it["item_id"]])} for it in todo],
                              ledger_event_ids=op.events)
            op = Op(self, f"rel|{principal}|{request_id}", I4.ACTOR, batch_id, g)
            passing = []
            for it in todo:
                rs = per_item[it["item_id"]]
                if rs:
                    op.put("items", it["item_id"], {**it, "status": "excluded_at_release", "reasons": rs})
                    for pid in it["payable_ids"]:
                        p = op.get("payables", pid)
                        if p["status"] == "batched":
                            op.put("payables", pid, unbatched(p))
                    op.record(derived_id("iexc", it["item_id"]), "item_excluded_at_release", I4.ACTOR, it["item_id"],
                              {"item_id": it["item_id"], "codes": R.codes(rs)}, "Item excluded at release")
                    results.append({"item_id": it["item_id"], "status": "excluded_at_release", "reasons": rs})
                else:
                    passing.append(it["item_id"])
            if passing or b["status"] == "approved":
                op.put("batches", batch_id, {**op.get("batches", batch_id), "status": "releasing",
                                             "release_started_at": b.get("release_started_at") or iso(self._now())})
                op.record(derived_id("brel", batch_id), "batch_release_started", I4.ACTOR, batch_id,
                          {"batch_id": batch_id, "items": len(passing)}, f"Release started: {len(passing)} item(s)")
            self._settle_batch(op, batch_id)
            self._commit(op)
        for iid in passing:
            results.append(self._submit_item(iid, principal))
        results += self.drive_open_items(f"rel|{request_id}", batch_id=batch_id)["items"]
        with self.lock:
            op = Op(self, f"relend|{principal}|{request_id}", I4.ACTOR, batch_id)
            self._settle_batch(op, batch_id)
            self._commit(op)
            return {"batch": self._batch_view(batch_id), "results": results, "request_id": request_id}

    def _submit_item(self, iid: str, principal: str) -> dict:
        """Record first (F4b/F4c + intent + crossing), submit to the rail with the item's idempotency key OUTSIDE the
        service lock, then record the outcome (F4d on acceptance)."""
        with self.lock:
            it = self.db["items"][iid]
            if it["status"] != "approved":
                return {"item_id": iid, "status": it["status"]}
            payee = self.db["payees"][it["payee_id"]]
            op = Op(self, f"sub|{iid}", I4.ACTOR, iid)
            pre = []
            try:
                if M.D(it["netted"]) > 0:
                    e = self._post(op, "zbc", [J.dr("2020", M.D(it["netted"]), f"payee:{it['payee_id']}"),
                                               J.cr("1200", M.D(it["netted"]), f"payee:{it['payee_id']}")], "F4b",
                                   {"kind": "batch_item", "id": iid}, f"F4b|{iid}", actor="intel_05_clawback")
                    pre.append(e["entry_id"])
                    op.record(derived_id("cln", iid), "clawback_netted", "intel_05_clawback", iid,
                              {"item_id": iid, "netted": it["netted"]}, f"Clawback netted at release: {it['netted']}")
                if M.D(it["withheld"]) > 0:
                    e = self._post(op, "zbc", [J.dr("2020", M.D(it["withheld"]), f"payee:{it['payee_id']}"),
                                               J.cr("2040", M.D(it["withheld"]))], "F4c", {"kind": "batch_item", "id": iid},
                                   f"F4c|{iid}", actor="intel_06_tax")
                    pre.append(e["entry_id"])
            except PostingRefused as exc:
                op2 = Op(self, f"subx|{iid}", I4.ACTOR, iid)
                op2.put("items", iid, {**it, "status": "excluded_at_release", "reasons": exc.reasons})
                self._payables_back(op2, it)
                op2.record(derived_id("iexc", iid, "post"), "item_excluded_at_release", I4.ACTOR, iid,
                           {"item_id": iid, "codes": R.codes(exc.reasons)}, "Item excluded: pre-release posting refused")
                self._commit(op2)
                return {"item_id": iid, "status": "excluded_at_release", "reasons": exc.reasons}
            if M.D(it["net"]) == 0:
                op.put("items", iid, {**it, "status": "netted", "pre_entry_ids": pre, "submitted_at": iso(self._now())})
                for pid in it["payable_ids"]:
                    p = op.get("payables", pid)
                    op.put("payables", pid, {**p, "status": "netted"})
                op.record(derived_id("isub", iid, "netted"), "item_submitted", I4.ACTOR, iid,
                          {"item_id": iid, "net": "0.00", "netted": it["netted"], "rail_call": False},
                          "Item settled by netting only (net 0.00): nothing sent to the rail")
                self._commit(op)
                return {"item_id": iid, "status": "netted"}
            xid = derived_id("x", iid, "submit", 1)
            op.record(xid, f"crossing_rail_{it['rail']}_requested", I4.ACTOR, iid,
                      {"port": f"rail_{it['rail']}", "action": "submit", "item_id": iid,
                       "idempotency_key": it["idempotency_key"], "net": it["net"]}, f"Submit to rail {it['rail']}")
            op.record(derived_id("isub", iid), "item_submitted", I4.ACTOR, iid,
                      {"item_id": iid, "net": it["net"], "idempotency_key": it["idempotency_key"]},
                      f"Payout item submitted to {it['rail']}: {it['net']}")
            op.put("items", iid, {**it, "status": "submitting", "pre_entry_ids": pre, "attempts": 1, "resubmits": 0,
                                  "first_submitted_at": iso(self._now()), "submitted_at": iso(self._now())})
            self._commit(op)
            ref = payee.get("rail_account_ref")
        return self._record_outcome(iid, self._rail_submit(it, ref))

    def _rail_submit(self, it: dict, ref: Optional[str]) -> RailSubmit:
        """The rail submit, its answer strictly type-checked (AEGIS N17-3): an exception or a malformed answer is a
        transport error (the idempotency key makes the retry safe), never an acceptance."""
        rail = self._rail(it["rail"])
        try:
            ans = rail.submit(it["idempotency_key"], ref, it["net"], it["item_id"])
        except Exception:  # noqa: BLE001 - an exception is a transport error: the key makes a retry safe
            return RailSubmit("transport_error")
        return self._checked_answer(None, f"rail_{it['rail']}", "submit", (it["item_id"], it["idempotency_key"]), ans,
                                    RailSubmit("transport_error"), actor=I4.ACTOR, subject=it["item_id"])

    def _record_outcome(self, iid: str, ans: RailSubmit) -> dict:
        with self.lock:
            it = self.db["items"][iid]
            if it["status"] != "submitting":
                return {"item_id": iid, "status": it["status"]}
            op = Op(self, f"out|{iid}|{it.get('attempts', 1)}|{it.get('resubmits', 0)}|{ans.outcome}", I4.ACTOR, iid)
            if ans.outcome == "accepted":
                net = M.D(it["net"])
                try:
                    e = self._post(op, "zbc", [J.dr("2020", net, f"payee:{it['payee_id']}"),
                                               J.cr("2030", net, f"item:{iid}")], "F4d", {"kind": "batch_item", "id": iid},
                                   f"F4d|{iid}")
                except PostingRefused as exc:
                    # the rail accepted: the money is moving; a refused posting is a break, never silence
                    bid = rid("brk", "f4d", iid)
                    op.put("breaks", bid, {"break_id": bid, "leg": "L3", "subject": f"item:{iid}", "difference": it["net"],
                                           "opened_at": iso(self._now()), "opened_on": self._today_la().isoformat(),
                                           "owner": "andre", "explanation_code": "unknown", "status": "open",
                                           "resolution": None, "reasons": exc.reasons})
                    op.put("items", iid, {**it, "status": "submitted", "rail_ref": ans.rail_ref})
                    self._commit(op)
                    return {"item_id": iid, "status": "submitted", "break_id": bid}
                op.put("items", iid, {**it, "status": "submitted", "rail_ref": ans.rail_ref,
                                      "accepted_at": iso(self._now()), "entry_ids": [e["entry_id"]]})
                for pid in it["payable_ids"]:
                    p = op.get("payables", pid)
                    op.put("payables", pid, {**p, "status": "released"})
                self._settle_batch(op, it["batch_id"])
                self._commit(op)
                return {"item_id": iid, "status": "submitted", "rail_ref": ans.rail_ref}
            if ans.outcome == "rejected":
                self._reverse_item_entries(op, it, "rejected")
                op.put("items", iid, {**it, "status": "failed", "failed_at": iso(self._now())})
                self._payables_back(op, it)
                payee = op.get("payees", it["payee_id"])
                self._open_hold(op, payee, "payout_failed", iid)
                op.record(derived_id("ifl", iid, "rejected"), "item_failed", I4.ACTOR, iid,
                          {"item_id": iid, "stage": "submission"}, "Rail rejected the payout item")
                self._settle_batch(op, it["batch_id"])
                self._commit(op)
                return {"item_id": iid, "status": "failed"}
            op.put("items", iid, {**it, "last_error": ans.outcome, "last_error_at": iso(self._now())})
            self._commit(op)
            return {"item_id": iid, "status": "submitting", "error": ans.outcome}

    def drive_open_items(self, op_prefix: str, batch_id: Optional[str] = None) -> dict:
        """Items stuck in ``submitting`` (a transport error or an unknown outcome): within 23 h on Stripe, retry with
        the SAME idempotency key; after 23 h, or on Trolley (idempotency UNVERIFIED), look the item up at the rail:
        found -> record it; not found -> submit exactly once more; unknown -> break ``rail_state_unknown``."""
        out = []
        with self.lock:
            stuck = [dict(i) for i in self.db["items"].values() if i["status"] == "submitting"
                     and (batch_id is None or i["batch_id"] == batch_id)]
        for it in stuck:
            iid = it["item_id"]
            rail = self._rail(it["rail"])
            ref = (self.db["payees"].get(it["payee_id"]) or {}).get("rail_account_ref")
            age = self._now() - parse_iso(it["first_submitted_at"])
            lookup_mode = it["rail"] == "trolley" or age >= timedelta(hours=I4.RETRY_WINDOW_H)
            with self.lock:
                n = it.get("attempts", 1) + 1
                op = Op(self, f"{op_prefix}|drive|{iid}|{n}", I4.ACTOR, iid)
                op.record(derived_id("x", iid, "lookup" if lookup_mode else "submit", n),
                          f"crossing_rail_{it['rail']}_requested", I4.ACTOR, iid,
                          {"port": f"rail_{it['rail']}", "action": "lookup" if lookup_mode else "submit",
                           "item_id": iid, "idempotency_key": it["idempotency_key"]},
                          f"{'Look up' if lookup_mode else 'Retry (same key)'} item at rail {it['rail']}")
                op.put("items", iid, {**self.db["items"][iid], "attempts": n})
                self._commit(op)
            if not lookup_mode:
                out.append(self._record_outcome(iid, self._rail_submit(it, ref)))
                continue
            try:
                lk = rail.lookup(it["idempotency_key"], iid)
            except Exception:  # noqa: BLE001
                lk = RailLookup(False)
            else:
                lk = self._checked_answer(None, f"rail_{it['rail']}", "lookup", (iid, it["idempotency_key"]), lk,
                                          RailLookup(False), actor=I4.ACTOR, subject=iid)
            if lk.available and lk.found:
                out.append(self._record_outcome(iid, RailSubmit("accepted", lk.rail_ref)))
            elif lk.available and not lk.found and it.get("resubmits", 0) == 0:
                with self.lock:
                    op = Op(self, f"{op_prefix}|resub|{iid}", I4.ACTOR, iid)
                    op.put("items", iid, {**self.db["items"][iid], "resubmits": 1})
                    op.record(derived_id("x", iid, "resubmit"), f"crossing_rail_{it['rail']}_requested", I4.ACTOR, iid,
                              {"port": f"rail_{it['rail']}", "action": "submit", "item_id": iid,
                               "idempotency_key": it["idempotency_key"], "resubmit": True},
                              "Item not found at the rail after lookup: submitted exactly once more")
                    self._commit(op)
                out.append(self._record_outcome(iid, self._rail_submit(it, ref)))
            else:
                with self.lock:
                    bid = rid("brk", "rsu", iid)
                    if bid not in self.db["breaks"]:
                        op = Op(self, f"{op_prefix}|rsu|{iid}", I4.ACTOR, iid)
                        op.put("breaks", bid, {"break_id": bid, "leg": "L3", "subject": f"item:{iid}",
                                               "difference": it["net"], "opened_at": iso(self._now()),
                                               "opened_on": self._today_la().isoformat(), "owner": "andre",
                                               "explanation_code": "unknown", "status": "open", "resolution": None,
                                               "kind": "rail_state_unknown"})
                        op.record(derived_id("rsu", iid), "rail_state_unknown", I4.ACTOR, iid,
                                  {"item_id": iid, "break_id": bid}, "Rail state of an item unknown: break opened, the "
                                                                     "item stays submitted-unknown")
                        self._commit(op)
                out.append({"item_id": iid, "status": "submitting", "break": "rail_state_unknown"})
        return {"items": out}

    def _settle_batch(self, op: Op, batch_id: str) -> None:
        b = op.get("batches", batch_id)
        if b is None or b["status"] not in ("releasing", "released", "partially_settled"):
            return
        sts = [op.get("items", i)["status"] for i in b["item_ids"]]
        live = [s for s in sts if s not in ("excluded_at_release", "cancelled")]
        if any(s in ("approved", "submitting") for s in sts):
            new = "releasing"
        elif not live:
            new = "failed"
        elif all(s in ("paid", "netted") for s in live):
            new = "settled"
        elif all(s == "failed" for s in live):
            new = "failed"
        elif any(s == "submitted" for s in live):
            new = "released"
        else:
            new = "partially_settled"
        if new != b["status"]:
            op.put("batches", batch_id, {**b, "status": new})

    # ================================================================== exceptions (Andre)

    def list_exceptions(self) -> dict:
        with self.lock:
            return {"exceptions": sorted(self.db["exceptions"].values(), key=lambda e: e["opened_at"])}

    def decide_exception(self, request_id: str, exception_id: str, decision: str) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"exception/{exception_id}", decision)
            if ent:
                return ent["response"]
            e = self.db["exceptions"].get(exception_id)
            if e is None:
                raise NotFound("no such exception")
            if e["status"] != "open":
                raise Conflict(f"exception is already {e['status']}")
            op = Op(self, f"excd|{request_id}", "andre", exception_id)
            new = {**e, "status": "approved" if decision == "approve" else "rejected", "decided_at": iso(self._now())}
            op.put("exceptions", exception_id, new)
            op.record(derived_id("excd", exception_id), "limit_exception_decided", "andre", exception_id,
                      {"exception_id": exception_id, "decision": decision}, f"Andre {decision}d a limit exception")
            resp = {"exception": new, "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp
