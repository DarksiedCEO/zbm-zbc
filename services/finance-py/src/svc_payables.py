"""
Payables (intelligence 3) and clawbacks (intelligence 5).

A payable is created ONLY from what Finance reads itself: the V&I certification (``GET
/vi/v1/submissions/{id}/certification``), the Compliance payout ruling named by Creative's handoff, the campaign's
Andre-approved profile and the Andre-published rate card effective at the clip's posting time. Creative's facts are
recorded as a hash and never trusted; a body carrying a count, an amount or a rate never reaches this module (422
at the API). Clawbacks come only from V&I's clawback feed (counts, never money) and are only ever netted against
future earnings or written off by Andre — no function here or anywhere debits a creator's external account.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, Invalid, NotFound
from intelligences import i01_journal as J
from intelligences import i03_payables as I3
from intelligences import i05_clawback as I5
from ledger import derived_id
from ports import Certification, ClawbackPage, ComplianceRuling, TierAnswer
from service import Gather, Op, PostingRefused, Refused, facts_sha256, rid, sha_text

ACCRUAL_ROWS = ("HR-12", "HR-13")


class PayablesMixin:
    def _payable_by_submission(self, sid: str) -> Optional[dict]:
        k = self.pay_by_sub.get(sid)
        return self.db["payables"].get(k) if k else None

    def _payable_by_cert(self, cid: str) -> Optional[dict]:
        k = self.pay_by_cert.get(cid)
        return self.db["payables"].get(k) if k else None

    # --- gather (outside the lock) ------------------------------------------------------------------------------------

    def _gather_accrual(self, g: Gather, sid: str, ruling_id: Optional[str]) -> dict:
        vi, cmp_, cn = self.ports.vi, self.ports.compliance, self.ports.cn
        cert = g.call("verification_integrity", "certification", (sid,), lambda: vi.certification(sid),
                      Certification(False))
        cert = cert if isinstance(cert, Certification) else Certification(False)
        ruling = ComplianceRuling(False)
        if ruling_id:
            ruling = g.call("compliance_38", "ruling", (ruling_id,), lambda: cmp_.ruling(ruling_id), ComplianceRuling(False))
            ruling = ruling if isinstance(ruling, ComplianceRuling) else ComplianceRuling(False)
        rows = self._rows_reasons(g, ACCRUAL_ROWS, "FIN-04")
        tier = None
        if cert.available and cert.campaign_id and cert.clipper_id:
            prof = self.db["profiles"].get(cert.campaign_id)
            cards = self._rc_versions(prof["rate_card_doc_id"]) if prof else []
            card = I3.pick_rate_card(cards, cert.create_time)
            if card and I3.tiered(card):
                t = g.call("clipper_network", "tier", (cert.campaign_id, cert.clipper_id),
                           lambda: cn.tier(cert.campaign_id, cert.clipper_id), TierAnswer(False))
                tier = t if isinstance(t, TierAnswer) else TierAnswer(False)
        return {"cert": cert, "ruling": ruling, "rows": rows, "tier": tier}

    # --- evaluate (inside the lock) ----------------------------------------------------------------------------------

    def _evaluate_accrual(self, sid: str, ruling_id: Optional[str], got: dict) -> tuple[list[dict], Optional[dict]]:
        """Every check runs every time; returns (reasons, computed payable fields or None)."""
        reasons = list(self._rules_reasons())
        cert: Certification = got["cert"]
        creasons = I3.cert_reasons(cert, sid)
        reasons += creasons
        reasons += I3.ruling_reasons(got["ruling"], sid, cert.certified_at, ruling_id)
        reasons += got["rows"]
        computed = None
        if not creasons:
            prof = self.db["profiles"].get(cert.campaign_id)
            card = None
            if prof is None:
                reasons.append(R.item("CAMPAIGN_NOT_FUNDED", "no commercial profile for the campaign"))
            elif prof["status"] == "paused":
                reasons.append(R.item("DISPUTE_OPEN", "campaign accruals are paused by an open dispute"))
            elif prof["status"] != "funded":
                reasons.append(R.item("CAMPAIGN_NOT_FUNDED", f"campaign is {prof['status']}, not funded"))
            if prof is not None:
                card = I3.pick_rate_card(self._rc_versions(prof["rate_card_doc_id"]), cert.create_time)
                if card is None:
                    reasons.append(R.item("RATE_CARD_MISSING", "no published rate card effective at the clip's posting time"))
            tier = "T0"
            if card is not None and I3.tiered(card):
                t = got["tier"]
                if t is None or not t.available or t.tier not in I3.TIERS:
                    reasons.append(R.item("DEPENDENCY_UNAVAILABLE:clipper_network", "tier at enrolment unavailable "
                                                                                    "(tiered rate card)", rule="FIN-05"))
                else:
                    tier = t.tier
            payee = self.db["payees"].get(cert.clipper_id)
            if payee is None or payee["status"] not in ("active", "hold", "offboarding"):
                reasons.append(R.item("PAYEE_UNKNOWN", "no active payee record at Finance for this clipper"))
            if card is not None and prof is not None and not [r for r in reasons if r["code"] == "RATE_CARD_MISSING"]:
                try:
                    amounts = I3.compute(cert.certified_views, card, tier, prof["client_rate_per_1000"])
                    computed = {"card": card, "tier": tier, **amounts, "prof": prof}
                except M.MoneyError:             # AEGIS N17-11: a refusal with a reason, never a 500
                    reasons.append(R.item("AMOUNT_OUT_OF_RANGE", "the payable is outside the money bound "
                                                                 "(rate card x certified views)"))
        return R.dedupe(reasons), computed

    def _new_payable(self, sid: str, ruling_id: str, got: dict, c: dict, status: str) -> dict:
        cert: Certification = got["cert"]
        card = c["card"]
        pid = rid("pay", cert.certification_id)
        return {"payable_id": pid, "payee_id": cert.clipper_id, "campaign_id": cert.campaign_id, "submission_id": sid,
                "certification_id": cert.certification_id, "certification_status": cert.status,
                "certified_views": cert.certified_views, "paid_views": c["paid_views"],
                "max_paid_views_per_clip": card["max_paid_views_per_clip"],
                "rate_card": {"doc_id": card["doc_id"], "version": card["version"], "sha256": card["sha256"],
                              "tier": c["tier"]},
                "rate_per_1000": c["rate_per_1000"], "amount": c["amount"], "client_rate_per_1000": c["client_rate_per_1000"],
                "revenue_amount": c["revenue_amount"], "current_amount": c["amount"], "current_revenue": c["revenue_amount"],
                "compliance_ruling_id": ruling_id, "compliance_register_version": got["ruling"].register_version,
                "vi_rules_version": cert.rules_version, "accrued_at": iso(self._now()), "status": status,
                "entry_ids": [], "adjustments": [], "batch_item_id": None, "posting_time": cert.create_time}

    def _accrue(self, op: Op, p: dict) -> dict:
        amt, rev = M.D(p["amount"]), M.D(p["revenue_amount"])
        lines = []
        if rev > 0:
            lines += [J.dr("2010", rev, f"campaign:{p['campaign_id']}"), J.cr("4010", rev)]
        if amt > 0:
            lines += [J.dr("5010", amt), J.cr("2020", amt, f"payee:{p['payee_id']}")]
        new = {**p, "status": "accrued"}
        if lines:
            e = self._post(op, "zbc", lines, "F2", {"kind": "certification", "id": p["certification_id"]},
                           f"F2F3|{p['certification_id']}", actor="intel_03_payables")
            new["entry_ids"] = [e["entry_id"]]
        op.record(derived_id("pacc", p["payable_id"]), "payable_accrued", "intel_03_payables", p["payable_id"],
                  {"payable_id": p["payable_id"], "certification_id": p["certification_id"], "amount": p["amount"],
                   "revenue_amount": p["revenue_amount"], "paid_views": p["paid_views"],
                   "rate_card_sha256": p["rate_card"]["sha256"]},
                  f"Payable accrued: {p['amount']} for {p['paid_views']} verified views")
        op.put("payables", p["payable_id"], new)
        return new

    def _identity_conflict(self, op: Op, sid: str, cert: Certification,
                           existing: Optional[dict]) -> Optional[list[dict]]:
        """AEGIS N17-1: a payable is keyed by its certification id AND bound at creation to (submission, payee,
        campaign, clip posting time). An answer that names an existing payable's certification with other bindings
        -- or a different certification for a submission that already has a payable -- is refused and recorded as
        an integrity finding. Nothing is ever rewritten."""
        if not cert.available or not cert.certification_id:
            return None
        clashes = []
        by_cert = self._payable_by_cert(cert.certification_id) or self.db["payables"].get(rid("pay", cert.certification_id))
        if by_cert is not None:
            want = {"submission_id": sid, "payee_id": cert.clipper_id, "campaign_id": cert.campaign_id,
                    "posting_time": cert.create_time}
            diff = sorted(k for k, v in want.items() if by_cert.get(k) != v)
            if diff:
                clashes.append((by_cert["payable_id"], "certification_rebound", diff))
        if existing is not None and existing["certification_id"] != cert.certification_id:
            clashes.append((existing["payable_id"], "submission_recertified_under_new_id", ["certification_id"]))
        if not clashes:
            return None
        reasons = []
        for pid, kind, fields in clashes:
            fid = rid("int", "payable_identity", pid, kind, sid, cert.certification_id)
            op.record(derived_id("pic", pid, kind, sid, cert.certification_id), "payable_identity_conflict",
                      "intel_03_payables", pid, {"payable_id": pid, "kind": kind, "fields": fields,
                                                 "submission_id": sid, "finding_id": fid,
                                                 "certification_id": cert.certification_id},
                      f"Integrity finding: V&I answer would rebind payable ({kind}); refused, nothing rewritten")
            op.add("integrity_finding", {"finding_id": fid, "payable_id": pid, "kind": kind, "fields": fields,
                                         "submission_id": sid, "certification_id": cert.certification_id,
                                         "at": iso(self._now())})
            reasons.append(R.item("PAYABLE_IDENTITY_CONFLICT", f"certification {cert.certification_id[:60]} is already "
                                  f"bound to payable {pid} ({', '.join(fields)} differ{'s' if len(fields) == 1 else ''}):"
                                  " refused and recorded as an integrity finding", [pid], rule="FIN-04"))
        return reasons

    def _try_accrue(self, op: Op, sid: str, ruling_id: Optional[str], got: dict) -> tuple[Optional[dict], list[dict]]:
        existing = self._payable_by_submission(sid)
        conflict = self._identity_conflict(op, sid, got["cert"], existing)
        if conflict:
            return None, conflict
        if existing is not None and existing["status"] not in ("over_budget_hold",):
            return existing, []
        reasons, c = self._evaluate_accrual(sid, ruling_id, got)
        if reasons or c is None:
            return None, reasons or [R.item("NOT_CERTIFIED", "payable could not be computed")]
        remaining = self.bal("2010", f"campaign:{c['prof']['campaign_id']}", op=op)
        p = self._new_payable(sid, ruling_id, got, c, "pending_checks")
        if M.D(c["revenue_amount"]) > remaining:
            if existing is None or existing["status"] != "over_budget_hold":
                hold = {**p, "status": "over_budget_hold"}
                op.record(derived_id("pobh", p["payable_id"]), "payable_over_budget_hold", "intel_03_payables",
                          p["payable_id"], {"payable_id": p["payable_id"], "revenue_amount": c["revenue_amount"],
                                            "remaining_deposit": M.fmt(max(M.ZERO, remaining))},
                          "Payable held: certification exceeds the campaign's remaining deposit (Andre decides)")
                op.put("payables", p["payable_id"], hold)
                p = hold
            return p, [R.item("OVER_BUDGET", f"revenue {c['revenue_amount']} exceeds the remaining deposit "
                                             f"{M.fmt(max(M.ZERO, remaining))}: nothing posted, Andre decides",
                              [p["payable_id"]])]
        try:
            return self._accrue(op, p), []
        except PostingRefused as exc:
            return None, exc.reasons

    # --- the Creative protocol route (§D.1) --------------------------------------------------------------------------

    def accept_handoff(self, principal: str, request_id: str, submission_id: str, facts: dict) -> dict:
        key, h, _ent = self._idem(principal, request_id, "payout-handoffs", {"s": submission_id, "f": facts})
        # N14-15b: a replay with the same body RE-EVALUATES (the handoff is idempotent per certification)
        fsha = facts_sha256(facts)
        if facts.get("submission_id") != submission_id:
            raise Invalid("facts.submission_id must equal submission_id")
        ruling_id = (facts.get("compliance") or {}).get("reference") or None
        g = Gather(self, f"hand|{principal}|{request_id}|{iso(self._now())}", "intel_03_payables", submission_id)
        got = self._gather_accrual(g, submission_id, ruling_id) if self.current else \
            {"cert": Certification(False), "ruling": ComplianceRuling(False), "rows": [], "tier": None}
        with self.lock:
            op = Op(self, f"hand|{principal}|{request_id}|{fsha}", "intel_03_payables", submission_id, g)
            hid = rid("hof", submission_id)
            op.record(derived_id("hof", principal, request_id, fsha), "handoff_received", principal, submission_id,
                      {"submission_id": submission_id, "facts_sha256": fsha, "request_id_sha256": sha_text(request_id),
                       "ruling_id": ruling_id}, "Payout handoff received from Creative (facts recorded as a hash)")
            self._injection(op, facts)
            if self.current is None:
                p, reasons = None, self._rules_reasons()
            else:
                p, reasons = self._try_accrue(op, submission_id, ruling_id, got)
            allowed = p is not None and p["status"] in ("accrued", "batched", "released", "settled", "netted",
                                                        "returned")
            status = "accrued" if allowed else ("over_budget_hold" if p is not None else "pending_checks")
            if "PAYABLE_IDENTITY_CONFLICT" in R.codes(reasons):
                status = "identity_conflict"        # never retried by the accrual job; Andre reads the finding
            op.put("handoffs", hid, {"handoff_id": hid, "submission_id": submission_id, "ruling_id": ruling_id,
                                     "facts_sha256": fsha, "principal": principal, "status": status,
                                     "received_at": iso(self._now()), "payable_id": p["payable_id"] if p else None,
                                     "reasons": reasons})
            if not allowed:
                op.record(derived_id("pref", submission_id, fsha, len(self.log)), "payable_refused", "intel_03_payables",
                          submission_id, {"submission_id": submission_id, "codes": R.codes(reasons),
                                          "rule_ids": sorted({r["rule_id"] for r in reasons})},
                          f"No payable yet: {len(reasons)} reason(s)")
            resp = {"department": "finance_31", "allowed": allowed,
                    "reason": "" if allowed else f"{len(reasons)} reasons: " + "; ".join(R.lines(reasons)),
                    "reference": p["payable_id"] if p else None, "reasons": reasons, "request_id": request_id,
                    "facts_sha256": fsha, "submission_id": submission_id, "rules_pinned": self.rules_pinned,
                    "ledger_event_ids": op.events}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def get_payable(self, payable_id: str) -> dict:
        with self.lock:
            p = self.db["payables"].get(payable_id)
            if p is None:
                raise NotFound("no such payable")
            return dict(p)

    # --- jobs --------------------------------------------------------------------------------------------------------

    def job_accrual(self, op_prefix: str) -> dict:
        pending = [h for h in self.db["handoffs"].values() if h["status"] in ("pending_checks", "over_budget_hold")]
        done, still = 0, 0
        for hnd in pending:
            g = Gather(self, f"{op_prefix}|{hnd['handoff_id']}", "intel_03_payables", hnd["submission_id"])
            got = self._gather_accrual(g, hnd["submission_id"], hnd["ruling_id"])
            with self.lock:
                op = Op(self, f"{op_prefix}|{hnd['handoff_id']}", "intel_03_payables", hnd["submission_id"], g)
                p, reasons = self._try_accrue(op, hnd["submission_id"], hnd["ruling_id"], got)
                ok = p is not None and p["status"] == "accrued"
                op.put("handoffs", hnd["handoff_id"], {**hnd, "status": "accrued" if ok else
                                                       ("over_budget_hold" if p else
                                                        "identity_conflict" if "PAYABLE_IDENTITY_CONFLICT" in
                                                        R.codes(reasons) else "pending_checks"),
                                                       "payable_id": p["payable_id"] if p else None, "reasons": reasons,
                                                       "retried_at": iso(self._now())})
                self._commit(op)
                done += ok
                still += not ok
        return {"retried": len(pending), "accrued": done, "still_pending": still}

    def job_clawback_sync(self, op_prefix: str) -> dict:
        vi = self.ports.vi
        state = self.db["job_runs"].get("clawback_cursor") or {"cursor": 0}
        cursor = state["cursor"]
        g = Gather(self, f"{op_prefix}|clawbacks", "intel_05_clawback", "clawback_feed")
        items: list[dict] = []
        available = True
        for _ in range(20):
            page = g.call("verification_integrity", "clawbacks", (cursor,), lambda c=cursor: vi.clawbacks(c),
                          ClawbackPage(False))
            if not isinstance(page, ClawbackPage) or not page.available:
                available = False
                break
            items += [dict(x) for x in page.items]
            if page.next_cursor is None or page.next_cursor <= cursor:
                break
            cursor = page.next_cursor
        applied = 0
        with self.lock:
            op = Op(self, f"{op_prefix}|clawbacks", "intel_05_clawback", "clawback_feed", g)
            for cb in items:
                cid = str(cb.get("clawback_id") or "")[:128]
                if not cid or op.get("clawbacks", cid):
                    continue
                applied += self._apply_clawback(op, cb)
            if available:
                op.put("job_runs", "clawback_cursor", {"cursor": cursor, "at": iso(self._now())})
            self._commit(op)
        return {"available": available, "received": len(items), "applied": applied}

    def _apply_clawback(self, op: Op, cb: dict) -> int:
        cid = str(cb["clawback_id"])[:128]
        delta = cb.get("views_delta")
        if isinstance(delta, bool) or not isinstance(delta, int) or delta > 0:
            op.put("clawbacks", cid, {"clawback_id": cid, "status": "ignored_invalid", "at": iso(self._now())})
            return 0
        p = self._payable_by_cert(str(cb.get("certification_id")))
        p = op.get("payables", p["payable_id"]) if p else None
        rec = {"clawback_id": cid, "certification_id": cb.get("certification_id"), "views_delta": delta,
               "cause": str(cb.get("cause"))[:40], "vi_rule_id": str(cb.get("rule_id"))[:16], "at": iso(self._now()),
               "payable_id": p["payable_id"] if p else None, "status": "no_payable", "receivable": "0.00",
               "payee_id": p["payee_id"] if p else None}
        if p is None or p["status"] in ("pending_checks",):
            op.put("clawbacks", cid, rec)
            return 0
        prior = [a["views_delta"] for a in p["adjustments"]]
        adj = I5.adjustment(p, prior + [delta], cb.get("cause") in I5.VOID_CAUSES)
        released = p["status"] in ("released", "settled", "netted")
        lines = []
        if adj["payable_delta"] > 0:
            lines += ([J.dr("1200", adj["payable_delta"], f"payee:{p['payee_id']}")] if released else
                      [J.dr("2020", adj["payable_delta"], f"payee:{p['payee_id']}")]) + [J.cr("5010", adj["payable_delta"])]
        if adj["revenue_delta"] > 0:
            lines += [J.dr("4010", adj["revenue_delta"]), J.cr("2010", adj["revenue_delta"], f"campaign:{p['campaign_id']}")]
        entry_ids = []
        if lines and p["status"] != "over_budget_hold":
            memo = "F5a" if released else "F5"
            try:
                e = self._post(op, "zbc", lines, memo, {"kind": "clawback", "id": cid}, f"{memo}|{cid}",
                               actor="intel_05_clawback")
                entry_ids = [e["entry_id"]]
            except PostingRefused as exc:
                rec.update(status="refused", reasons=exc.reasons)
                op.put("clawbacks", cid, rec)
                return 0
        a = {"clawback_id": cid, "views_delta": delta, "payable_delta": M.fmt(adj["payable_delta"]) if
             adj["payable_delta"] >= 0 else M.sfmt(adj["payable_delta"]), "revenue_delta": M.sfmt(adj["revenue_delta"]),
             "entry_ids": entry_ids}
        newp = {**p, "adjustments": p["adjustments"] + [a], "current_amount": M.fmt(adj["new_amount"]),
                "current_revenue": M.fmt(adj["new_revenue"]), "paid_views": adj["new_paid_views"],
                "adjusted_at": iso(self._now())}
        if p["status"] == "over_budget_hold":
            newp.update(amount=M.fmt(adj["new_amount"]), revenue_amount=M.fmt(adj["new_revenue"]))
        op.put("payables", p["payable_id"], newp)
        rec.update(status="receivable" if released and adj["payable_delta"] > 0 else "applied",
                   receivable=M.fmt(adj["payable_delta"]) if released and adj["payable_delta"] > 0 else "0.00",
                   entry_ids=entry_ids)
        op.put("clawbacks", cid, rec)
        op.record(derived_id("clb", cid), "clawback_applied", "intel_05_clawback", p["payable_id"],
                  {"clawback_id": cid, "payable_id": p["payable_id"], "views_delta": delta,
                   "payable_delta": a["payable_delta"], "revenue_delta": a["revenue_delta"], "released": released},
                  f"Clawback applied ({'receivable' if released else 'payable reduced'}): {a['payable_delta']}")
        op.record(derived_id("padj", p["payable_id"], cid), "payable_adjusted", "intel_05_clawback", p["payable_id"],
                  {"payable_id": p["payable_id"], "current_amount": newp["current_amount"]}, "Payable adjusted")
        return 1

    def write_off(self, request_id: str, payee_id: str) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"write-off/{payee_id}", None)
            if ent:
                return ent["response"]
            self.require_rules()
            open_amt = self.bal("1200", f"payee:{payee_id}")
            if open_amt <= 0:
                raise Conflict("no open clawback receivable for this payee")
            recs = [c for c in self.db["clawbacks"].values() if c.get("payee_id") == payee_id and c["status"] == "receivable"]
            oldest = min((parse_iso(c["at"]) for c in recs), default=self._now())
            age = self._now() - oldest
            if age < timedelta(days=self.cfg.clawback_writeoff_min_days):
                raise Refused("write-off too early", [R.item(
                    "CLAWBACK_WRITEOFF_TOO_EARLY", f"oldest open clawback is {age.days} days old; write-off needs "
                                                   f"{self.cfg.clawback_writeoff_min_days}")])
            op = Op(self, f"wo|{request_id}", "andre", payee_id)
            try:
                e = self._post(op, "zbc", [J.dr("5040", open_amt), J.cr("1200", open_amt, f"payee:{payee_id}")], "F5b",
                               {"kind": "clawback", "id": f"writeoff:{payee_id}"}, f"F5b|{payee_id}|{request_id}",
                               approval_ref=request_id, actor="andre")
            except PostingRefused as exc:
                raise Refused("write-off refused", exc.reasons) from None
            for c in recs:
                op.put("clawbacks", c["clawback_id"], {**c, "status": "written_off", "written_off_at": iso(self._now())})
            wid = f"wo:{payee_id}:{request_id}"[:128]
            op.put("clawbacks", wid, {"clawback_id": wid, "payee_id": payee_id, "status": "write_off",
                                      "amount": M.fmt(open_amt), "entry_ids": [e["entry_id"]], "at": iso(self._now())})
            op.record(derived_id("wo", payee_id, request_id), "clawback_written_off", "andre", payee_id,
                      {"payee_id": payee_id, "amount": M.fmt(open_amt), "entry_id": e["entry_id"]},
                      f"Andre wrote off an uncollectible clawback of {M.fmt(open_amt)}")
            resp = self._idem_add(op, key, h, {"payee_id": payee_id, "written_off": M.fmt(open_amt),
                                               "entry_id": e["entry_id"], "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp
