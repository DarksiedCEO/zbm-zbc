"""
Payees (§B.6), tax (§B.7, intelligence 6), the protocol reads other departments call (§D.1: tax-status, rail-status,
payout-identity, open-items, offboarding-notices, payee activation), destination-change callbacks (FIN-14) and rail
webhooks (§C.4: paid / failed / returned / destination_changed).

Bank details live at the rail only (FIN-28): a payee record holds the rail's opaque account reference and the SHA-256
of the rail's destination fingerprint — never account data. The callback contact is a vault reference, never a phone
number; a number supplied with a change request is never used (FBI IC3: "Use secondary channels").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import timedelta
from typing import Optional

import chart as C
import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, NotFound, Unavailable
from intelligences import i01_journal as J
from intelligences import i06_tax as I6
from ledger import derived_id
from ports import RailAccount, TaxAgentAnswer
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, facts_sha256, rid, sha, sha_text, unbatched

HELD = "held_submitting"

# Wave 25 (scout B Low): the payout-country list is a pinned seed like fin_rules_seed.json — a changed file refuses
# the import (and so the start) instead of silently changing who can be paid; read through a closed handle.
PINNED_STRIPE_REACH_SHA256 = "74f79a159e1ce878c22c225f3d78d646fed3c876844f079946a836e78214a842"
STRIPE_REACH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "seed", "stripe_reach.json")


def load_stripe_reach(path: str = STRIPE_REACH_PATH) -> frozenset:
    with open(path, "rb") as fh:
        raw = fh.read()
    got = hashlib.sha256(raw).hexdigest()
    if got != PINNED_STRIPE_REACH_SHA256:
        raise RuntimeError(f"seed/stripe_reach.json SHA-256 {got} is not the pinned {PINNED_STRIPE_REACH_SHA256}; "
                           "refusing to start (payout-country eligibility must be the reviewed file)")
    return frozenset(json.loads(raw)["stripe"])


STRIPE_REACH = load_stripe_reach()


def fp_sha(fingerprint: Optional[str]) -> Optional[str]:
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest() if fingerprint else None


class PayeesMixin:
    # --- rail choice ------------------------------------------------------------------------------------------------

    def rail_for(self, country: Optional[str]) -> Optional[str]:
        if not country:
            return None
        if country in STRIPE_REACH:
            return "stripe" if "stripe" in self.cfg.rails else None
        return "trolley" if "trolley" in self.cfg.rails else None

    def _rail(self, name: Optional[str]):
        return self.ports.rails.get(name) if name else None

    def _rail_account(self, g: Gather, payee: dict, create: bool) -> RailAccount:
        rail = self._rail(payee.get("rail"))
        if rail is None:
            return RailAccount(False, reason="no rail reaches this payee")
        if create or not payee.get("rail_account_ref"):
            ans = g.call(f"rail_{payee['rail']}", "create_account", (payee["payee_id"], payee["declared_country"]),
                         lambda: rail.create_account(payee["payee_id"], payee["declared_country"]), RailAccount(False))
        else:
            ref = payee["rail_account_ref"]
            ans = g.call(f"rail_{payee['rail']}", "account_status", (ref,), lambda: rail.account_status(ref),
                         RailAccount(False))
        return ans if isinstance(ans, RailAccount) else RailAccount(False)

    # --- activation (Onboarding activate_payout_account; Clipper Network) --------------------------------------------

    def create_payee(self, principal: str, request_id: str, body: dict) -> dict:
        key, h, ent = self._idem(principal, request_id, "payees", body)
        if ent:
            return ent["response"]
        sent = {k: v for k, v in body.items() if k != "request_id"}
        fsha = facts_sha256(sent)
        pid = body["payee_id"]
        existing = self.db["payees"].get(pid)
        country = (existing or {}).get("declared_country") or body.get("declared_country")
        rail_name = (existing or {}).get("rail") or self.rail_for(country)
        draft = existing or {"payee_id": pid, "entity": "zbc", "kind": "clipper", "status": "pending",
                             "declared_country": body.get("declared_country"),
                             "declared_region": body.get("declared_region"), "rail": rail_name,
                             "rail_account_ref": None, "destination_fingerprint_sha256": None,
                             "destination_verified_at": None, "hold": None,
                             "classification": {"doc_id": None, "version": None, "acceptance_id": None,
                                                "basis": "unverified"},
                             "callback_contact_ref": body.get("callback_contact_ref"),
                             "legal_form": body.get("legal_form") or "individual",
                             "owner_subject_ids": list(body.get("owner_subject_ids") or []), "created_at": iso(self._now()),
                             "created_by": principal, "first_paid_at": None}
        g = Gather(self, f"payee|{principal}|{request_id}", "intel_09_controls", pid)
        acct = RailAccount(False)
        if self.current and rail_name and country:
            acct = self._rail_account(g, draft, create=existing is None or not existing.get("rail_account_ref"))
        with self.lock:
            unmet = list(self._rules_reasons())
            if not country:
                unmet.append(R.item("RAIL_NOT_READY", "declared country unknown: no rail can be chosen"))
            elif not rail_name:
                unmet.append(R.item("RAIL_NOT_READY", f"no enabled rail reaches {country}"))
            elif not acct.available:
                unmet.append(R.item("DEPENDENCY_UNAVAILABLE:rail_" + rail_name, f"rail {rail_name} unavailable: no "
                                    "payout account can be created (stand-in)", rule="FIN-30"))
            else:
                if acct.status != "verified" or not acct.payouts_enabled:
                    unmet.append(R.item("RAIL_NOT_READY", f"rail account is {acct.status}, payouts "
                                                          f"{'enabled' if acct.payouts_enabled else 'disabled'}",
                                        obligation_id="US-STRIPE-KYC"))
            if not self.current:
                resp = {"payee_id": pid, "allowed": False, "unmet": R.lines(unmet), "reasons": unmet,
                        "detail": "no Finance rule version in force", "request_id": request_id, "facts_sha256": fsha,
                        "rules_pinned": self.rules_pinned}
                return self._idem_mem(key, h, resp)
            new = dict(self.db["payees"].get(pid) or draft)
            if acct.available:
                new.update(rail_account_ref=acct.account_ref or new.get("rail_account_ref"))
                if acct.destination_fingerprint and not new.get("destination_fingerprint_sha256"):
                    new.update(destination_fingerprint_sha256=fp_sha(acct.destination_fingerprint),
                               destination_verified_at=iso(self._now()))
            if new["status"] == "pending" and not unmet:
                new["status"] = "active"
            op = Op(self, f"payee|{principal}|{request_id}", "intel_09_controls", pid, g)
            op.record(derived_id("pye", pid, principal, request_id, new["status"]),
                      "payee_created", principal, pid,
                      {"payee_id": pid, "status": new["status"], "rail": new["rail"],
                       "country": new["declared_country"], "contact_ref_on_file": bool(new.get("callback_contact_ref"))},
                      f"Payee {new['status']} (rail {new['rail']})")
            op.put("payees", pid, new)
            resp = {"payee_id": pid, "allowed": new["status"] == "active" and not unmet, "unmet": R.lines(unmet),
                    "reasons": unmet, "detail": f"payee {new['status']}", "request_id": request_id,
                    "facts_sha256": fsha, "rules_pinned": self.rules_pinned, "ledger_event_ids": op.events}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def payee_view(self, pid: str) -> dict:
        with self.lock:
            p = self.db["payees"].get(pid)
            if p is None:
                raise NotFound("no such payee")
            out = {k: v for k, v in p.items() if k not in ("callback_contact_ref", "rail_account_ref")}
            out["hold"] = self._hold_view(p)
            out["clawback_open"] = M.fmt(max(M.ZERO, self.bal("1200", f"payee:{pid}")))
            out["payable_balance"] = M.fmt(max(M.ZERO, self.bal("2020", f"payee:{pid}")))
            return out

    @staticmethod
    def _hold_view(p: dict) -> Optional[dict]:
        hd = p.get("hold")
        if not hd:
            return None
        return {k: hd.get(k) for k in ("reason", "since", "change_event_id", "callback_id", "cooling_off_until")}

    def _hold_active(self, p: dict) -> tuple[bool, str]:
        hd = p.get("hold")
        if not hd:
            return False, ""
        if hd.get("callback_id") and hd.get("callback_outcome") == "confirmed":
            until = parse_iso(hd["cooling_off_until"])
            if self._now() >= until:
                return False, ""
            return True, f"destination re-verified by callback; cooling-off until {hd['cooling_off_until']}"
        return True, f"payee on hold ({hd['reason']}): no confirmed callback to the contact on file"

    def _open_hold(self, op: Op, p: dict, reason: str, event_id: str, pending_fp: Optional[str] = None) -> dict:
        new = {**p, "status": "hold" if p["status"] in ("active", "hold") else p["status"],
               "hold": {"reason": reason, "since": iso(self._now()), "change_event_id": event_id, "callback_id": None,
                        "callback_outcome": None, "cooling_off_until": None, "pending_fingerprint_sha256": pending_fp,
                        "prior_status": p["status"] if p["status"] != "hold" else (p.get("hold") or {}).get(
                            "prior_status", "active")}}
        op.put("payees", p["payee_id"], new)
        op.record(derived_id("phold", p["payee_id"], event_id), "payee_hold_opened", "intel_09_controls", p["payee_id"],
                  {"payee_id": p["payee_id"], "reason": reason, "change_event_id": event_id},
                  f"Payee hold opened: {reason}")
        return new

    # --- protocol reads -----------------------------------------------------------------------------------------------

    def _tax_answer(self, g: Gather, pid: str) -> TaxAgentAnswer:
        tax = self.ports.tax
        ans = g.call("tax_agent", "status", (pid,), lambda: tax.status(pid), TaxAgentAnswer(False))
        return ans if isinstance(ans, TaxAgentAnswer) else TaxAgentAnswer(False)

    def tax_status(self, principal: str, pid: str) -> dict:
        g = Gather(self, f"taxq|{principal}|{pid}|{iso(self._now())}", "intel_06_tax", pid)
        ans = self._tax_answer(g, pid) if pid in self.db["payees"] else TaxAgentAnswer(False, reason="unknown payee")
        rec = self.db["tax"].get(pid) or {}
        return {"payee_id": pid, "available": ans.available, "form_kind": ans.form_kind if ans.available else None,
                "form_on_file": bool(ans.available and ans.form_on_file),
                "tin_match": ans.tin_match if ans.available else "unavailable",
                "w8_current": ans.w8_current if ans.available else None,
                "services_outside_us_attested": ans.services_outside_us_attested if ans.available else None,
                "backup_withholding": bool((rec.get("backup_withholding") or {}).get("flag")),
                "as_of": iso(self._now()), "reason": "" if ans.available else "tax agent unavailable (stand-in)",
                "rules_pinned": self.rules_pinned}

    def rail_status(self, principal: str, pid: str) -> dict:
        p = self.db["payees"].get(pid)
        if p is None:
            return {"payee_id": pid, "available": False, "status": "unknown", "payouts_enabled": False, "rail": None,
                    "reason": "unknown payee", "rules_pinned": self.rules_pinned}
        g = Gather(self, f"railq|{principal}|{pid}|{iso(self._now())}", "intel_04_payout_run", pid)
        acct = self._rail_account(g, p, create=False) if p.get("rail_account_ref") else RailAccount(False)
        return {"payee_id": pid, "available": acct.available, "status": acct.status if acct.available else "unknown",
                "payouts_enabled": bool(acct.available and acct.payouts_enabled), "rail": p.get("rail"),
                "reason": "" if acct.available else "rail unavailable (stand-in)", "rules_pinned": self.rules_pinned}

    def payout_identity(self, principal: str, pid: str) -> dict:
        p = self.db["payees"].get(pid)
        if p is None or not p.get("destination_fingerprint_sha256") or not p.get("rail"):
            return {"payee_id": pid, "available": False, "identity_hmac": None, "rules_pinned": self.rules_pinned}
        g = Gather(self, f"pid|{principal}|{pid}|{iso(self._now())}", "intel_09_controls", pid)
        vault = self.ports.vault
        key = g.call("vault", "identity_hmac_key", (), lambda: vault.identity_hmac_key(), None)
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            return {"payee_id": pid, "available": False, "identity_hmac": None, "rules_pinned": self.rules_pinned}
        mac = hmac.new(bytes(key), f"{p['rail']}|{p['destination_fingerprint_sha256']}".encode("utf-8"),
                       hashlib.sha256).hexdigest()
        return {"payee_id": pid, "available": True, "identity_hmac": mac, "rules_pinned": self.rules_pinned}

    def open_items(self, principal: str, pid: str) -> dict:
        g = Gather(self, f"oi|{principal}|{pid}|{iso(self._now())}", "intel_05_clawback", pid)
        ans = self._tax_answer(g, pid)
        with self.lock:
            unpaid = [p for p in self.db["payables"].values() if p["payee_id"] == pid and p["status"] in
                      ("pending_checks", "accrued", "returned", "over_budget_hold", "batched")]
            transit = [i for i in self.db["items"].values() if i["payee_id"] == pid and i["status"] in
                       ("submitting", "submitted")]
            claw = self.bal("1200", f"payee:{pid}") > 0
            counts = {"payables_unpaid": len(unpaid), "in_transit": len(transit), "clawback_open": int(claw),
                      "disputes": 0, "tax_forms_pending": 0 if (ans.available and ans.form_on_file) else 1}
            if not ans.available:
                state = "unknown"
            elif any(v for k, v in counts.items() if k != "tax_forms_pending"):
                state = "open"
            else:
                state = "none"
            return {"payee_id": pid, "state": state, "counts": counts, "rules_pinned": self.rules_pinned}

    def offboarding_notice(self, principal: str, request_id: str, pid: str, offboarding_id: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, f"offboarding/{pid}", offboarding_id)
            if ent:
                return ent["response"]
            fsha = facts_sha256({"offboarding_id": offboarding_id})
            p = self.db["payees"].get(pid)
            if p is None or not self.current:
                return self._idem_mem(key, h, {"ok": False, "reference": None, "request_id": request_id,
                                               "facts_sha256": fsha, "payee_id": pid,
                                               "reason": "unknown payee" if p is None else "no rule version in force"})
            op = Op(self, f"offb|{request_id}", principal, pid)
            new = {**p, "status": "offboarding" if p["status"] != "hold" else "hold", "offboarding_id": offboarding_id,
                   "offboarding_since": iso(self._now())}
            if p.get("hold"):
                new["hold"] = {**p["hold"], "prior_status": "offboarding"}
            op.put("payees", pid, new)
            ref = rid("off", pid, offboarding_id)
            op.put("offboarding", ref, {"reference": ref, "payee_id": pid, "offboarding_id": offboarding_id,
                                        "at": iso(self._now())})
            op.record(derived_id("offb", pid, offboarding_id), "payee_offboarding_noticed", principal, pid, {"payee_id": pid, "status": "offboarding", "offboarding_id": offboarding_id},
                      "Payee offboarding: the final run pays any net of at least the final minimum")
            resp = {"ok": True, "reference": ref, "request_id": request_id, "facts_sha256": fsha, "payee_id": pid}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    # --- callbacks (FIN-14) -------------------------------------------------------------------------------------------

    def record_callback(self, request_id: str, pid: str, body: dict) -> dict:
        key, h, ent = self._idem("andre", request_id, f"callbacks/{pid}", body)
        if ent:
            return ent["response"]
        p = self.db["payees"].get(pid)
        if p is None:
            raise NotFound("no such payee")
        if not p.get("callback_contact_ref"):
            raise Refused("no callback channel on file", [R.item("PAYEE_HOLD", "no callback contact on file for this "
                                                                               "payee: the hold cannot be lifted")])
        if not hmac.compare_digest(body["contact_ref"].encode(), p["callback_contact_ref"].encode()):
            self.founder_refused("callbacks", "callback contact ref differs from the one on file")
            raise InvalidReasons("callback refused", [R.item("CALLBACK_CONTACT_MISMATCH", "the callback must use the "
                                                             "contact on file, never one supplied with the change")])
        g = Gather(self, f"cb|{request_id}", "andre", pid)
        vault = self.ports.vault
        ok = g.call("vault", "contact_ref_valid", (sha_text(p["callback_contact_ref"]),),
                    lambda: vault.contact_ref_valid(p["callback_contact_ref"]), None)
        with self.lock:
            self.require_rules()
            p = self.db["payees"][pid]
            hd = p.get("hold")
            if not hd or hd.get("change_event_id") != body["change_event_id"]:
                raise Conflict("no open hold for that change event on this payee")
            if ok is not True:
                raise Refused("callback impossible", [R.item("DEPENDENCY_UNAVAILABLE:vault", "the vault cannot confirm "
                                                             "the contact on file (stand-in): callbacks are impossible, "
                                                             "the payee stays on hold", rule="FIN-14")],
                              ledger_event_ids=g.events)
            now = self._now()
            cbid = rid("cb", pid, request_id)
            op = Op(self, f"cb|{request_id}", "andre", pid, g)
            self._injection(op, {"notes": body.get("notes")})
            cb = {"callback_id": cbid, "payee_id": pid, "change_event_id": body["change_event_id"],
                  "new_destination_fingerprint_sha256": hd.get("pending_fingerprint_sha256"),
                  "contact_ref_sha256": sha_text(body["contact_ref"]), "performed_by": "andre",
                  "performed_at": iso(now), "outcome": body["outcome"],
                  "notes_sha256": sha_text(body["notes"]) if body.get("notes") else None,
                  "creator_notified_ref": body.get("creator_notified_ref")}
            op.put("callbacks", cbid, cb)
            op.record(derived_id("cb", cbid), "callback_recorded", "andre", pid,
                      {"callback_id": cbid, "payee_id": pid, "outcome": body["outcome"],
                       "change_event_id": body["change_event_id"]}, f"Andre recorded a callback: {body['outcome']}")
            new_hold = {**hd, "callback_id": cbid, "callback_outcome": body["outcome"]}
            new = {**p}
            if body["outcome"] == "confirmed":
                new_hold["cooling_off_until"] = iso(now + timedelta(hours=self.cfg.cooling_off_h))
                if hd.get("pending_fingerprint_sha256"):
                    new.update(destination_fingerprint_sha256=hd["pending_fingerprint_sha256"],
                               destination_verified_at=iso(now))
            new["hold"] = new_hold
            op.put("payees", pid, new)
            resp = {"callback": cb, "hold": self._hold_view(new), "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def _lift_expired_holds(self, op: Op) -> None:
        for p in list(self.db["payees"].values()):
            if p.get("hold") and not self._hold_active(p)[0]:
                prior = p["hold"].get("prior_status") or "active"
                op.put("payees", p["payee_id"], {**p, "hold": None, "status": prior})
                op.record(derived_id("prel", p["payee_id"], p["hold"]["change_event_id"]), "payee_hold_released",
                          "intel_09_controls", p["payee_id"],
                          {"payee_id": p["payee_id"], "callback_id": p["hold"]["callback_id"]},
                          "Payee hold released: callback confirmed and cooling-off over")

    # --- tax (Andre inputs) -------------------------------------------------------------------------------------------

    def b_notice(self, request_id: str, pid: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"b-notices/{pid}", {k: str(v) for k, v in body.items()})
            if ent:
                return ent["response"]
            self.require_rules()
            if pid not in self.db["payees"]:
                raise NotFound("no such payee")
            rec = dict(self.db["tax"].get(pid) or {"payee_id": pid})
            timers = I6.b_notice_timers(body["cp2100_received_on"])
            timers["second_notice_within_3y"] = bool(body.get("second_notice_within_3y"))
            if body.get("first_b_notice_sent_on"):
                timers["first_b_notice_sent_on"] = body["first_b_notice_sent_on"].isoformat()
            rec["b_notice"] = timers
            op = Op(self, f"bn|{request_id}", "andre", pid)
            op.record(derived_id("bn", pid, request_id), "b_notice_recorded", "andre", pid,
                      {"payee_id": pid, **{k: v for k, v in timers.items()}}, "CP2100 B-notice intake recorded")
            if body.get("start_withholding"):
                rec["backup_withholding"] = {"flag": True, "reason": "b_notice", "since": iso(self._now())}
                op.record(derived_id("bwf", pid, request_id), "backup_withholding_flagged", "andre", pid,
                          {"payee_id": pid, "rate_percent": I6.BACKUP_PCT}, "Backup withholding (24%) flagged")
            op.put("tax", pid, rec)
            resp = self._idem_add(op, key, h, {"payee_id": pid, "tax": rec, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def tax_readiness(self, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "tax/readiness", {k: str(v) for k, v in body.items()})
            if ent:
                return ent["response"]
            cur = dict(self.db["tax_readiness"].get("filing") or {})
            for k in ("tcc_obtained_at", "iris_test_passed_at", "ftb_swift_ready_at"):
                if body.get(k):
                    cur[k] = body[k].isoformat()
            op = Op(self, f"txr|{request_id}", "andre", "tax_readiness")
            op.record(derived_id("txr", request_id), "control_result_recorded", "andre", "FC-08",
                      {"control": "FC-08", **cur}, "IRIS / TCC / FTB readiness facts recorded")
            op.put("tax_readiness", "filing", cur)
            resp = self._idem_add(op, key, h, {"readiness": cur, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def ytd(self, pid: str, year: int) -> dict:
        items = [i for i in self.db["items"].values() if i["payee_id"] == pid and i["status"] == "paid"
                 and i.get("paid_at") and I6.tax_year(parse_iso(i["paid_at"])) == year]
        reportable = M.total(M.q(M.D(i["gross"]) - M.D(i["netted"])) for i in items)
        withheld = M.total(i["withheld"] for i in items)
        return {"tax_year": year, "reportable_paid": M.fmt(reportable), "withheld": M.fmt(withheld), "items": len(items)}

    def form_1099(self, principal: str, year: int) -> dict:
        g = Gather(self, f"1099|{year}|{iso(self._now())}", "intel_06_tax", f"1099:{year}")
        rows = self._rows_reasons(g, ("US-IRS-1099NEC",), "FIN-24")
        with self.lock:
            threshold = self.cfg.threshold_1099.get(year)
            out = []
            for pid in sorted(self.db["payees"]):
                y = self.ytd(pid, year)
                if y["items"] == 0:
                    continue
                if rows or threshold is None:
                    required = None
                    status = "rule_not_in_force" if rows else "threshold_not_configured"
                else:
                    required = M.D(y["reportable_paid"]) >= threshold
                    status = "required" if required else "tracking"
                out.append({"payee_id": pid, "tax_year": year, "box1_nonemployee_comp": y["reportable_paid"],
                            "box4_withheld": y["withheld"], "form_1099_required": required, "status": status,
                            "channel": "iris", "filed_ref": None})
            return {"tax_year": year, "threshold": M.fmt(threshold) if threshold else None, "records": out,
                    "reasons": rows, "reason_lines": R.lines(rows), "ledger_event_ids": g.events}

    # --- rail webhooks (via rail_gateway) ---------------------------------------------------------------------------------

    def rail_events(self, principal: str, request_id: str, rail_name: str, events: list[dict]) -> dict:
        if rail_name not in self.cfg.rails:
            raise NotFound("no such rail")
        key, h, ent = self._idem(principal, request_id, f"rails/{rail_name}", events)
        if ent:
            return ent["response"]
        rail = self._rail(rail_name)
        g = Gather(self, f"rev|{rail_name}|{request_id}", "intel_04_payout_run", f"rail:{rail_name}")
        verified = {}
        for ev in events:
            body = {k: v for k, v in ev.items() if k != "signature"}
            verified[ev["event_id"]] = g.call(f"rail_{rail_name}", "verify_event", (ev["event_id"], sha(body)),
                                              lambda b=body, s=ev.get("signature"): rail.verify_event(b, s), False) is True
        with self.lock:
            self.require_rules()
            bad = [e for e, ok in verified.items() if not ok]
            if bad:
                raise Refused("rail event signature not verified", [R.item(
                    "DEPENDENCY_UNAVAILABLE:rail_" + rail_name, f"{len(bad)} event(s) whose signature the rail adapter "
                    "could not verify were refused", rule="FIN-08")], ledger_event_ids=g.events)
            op = Op(self, f"rev|{rail_name}|{request_id}", "intel_04_payout_run", f"rail:{rail_name}", g)
            results = []
            for ev in events:
                k = f"{rail_name}|{ev['event_id']}"
                prior = op.get("rail_events", k)
                if prior and not prior.get("held"):
                    results.append({"event_id": ev["event_id"], "status": "duplicate"})
                    continue
                status = self._rail_event(op, rail_name, ev)
                # sweep B-F2: a paid/failed event for an item still ``submitting`` (its acceptance not booked yet) is
                # HELD, never acked-and-ignored: a redelivery of the same event re-applies it, and so does Finance
                # itself as soon as the item's acceptance is booked (``replay_held_rail_events``)
                op.put("rail_events", k, {"event_id": ev["event_id"], "rail": rail_name, "type": ev["type"],
                                          "item_id": ev.get("item_id"), "status": status,
                                          "held": status == HELD, "at": (prior or {}).get("at") or iso(self._now())})
                results.append({"event_id": ev["event_id"], "status": status})
            resp = {"results": results, "ledger_event_ids": op.events, "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op)
            return resp

    def _reverse_item_entries(self, op: Op, item: dict, why: str) -> None:
        for eid in item.get("pre_entry_ids") or []:
            orig = self.entries_by_id.get(eid)
            if orig is None:
                continue
            self._post(op, "zbc", J.reversal_lines(orig), "correction", {"kind": "batch_item", "id": item["item_id"]},
                       f"rev|{eid}|{why}", reverses=eid, actor="intel_04_payout_run")

    def _payables_back(self, op: Op, item: dict, returned: bool = False) -> None:
        """Payables of a failed / rejected / returned item are owed again. A payable that was PAID and came back
        (F4g) becomes ``returned`` -- runnable again, never ``accrued`` again (AEGIS N17-1)."""
        for pid in item["payable_ids"]:
            p = op.get("payables", pid)
            if p:
                op.put("payables", pid, {**unbatched(p), "status": "returned"} if returned else unbatched(p))

    def _rail_event(self, op: Op, rail_name: str, ev: dict) -> str:
        t = ev["type"]
        acct = C.RAIL_ACCOUNT[rail_name]
        if t in ("paid", "failed", "returned"):
            item = op.get("items", ev.get("item_id") or "")
            if item is None:
                return "unknown_item"
            if t in ("paid", "failed") and item["status"] == "submitting":
                op.record(derived_id("rhold", rail_name, ev["event_id"]), "rail_event_held", "intel_04_payout_run",
                          item["item_id"], {"item_id": item["item_id"], "event_id": ev["event_id"], "type": t},
                          f"Rail {t} event held: the item's acceptance is not booked yet (applied once it is)")
                return HELD
            net = M.D(item["net"])
            payee = op.get("payees", item["payee_id"])
            try:
                if t == "paid":
                    if item["status"] != "submitted":
                        return f"ignored_{item['status']}"
                    self._post(op, "zbc", [J.dr("2030", net, f"item:{item['item_id']}"), J.cr(acct, net)], "F4e",
                               {"kind": "batch_item", "id": item["item_id"]}, f"F4e|{item['item_id']}",
                               actor="intel_04_payout_run")
                    op.put("items", item["item_id"], {**item, "status": "paid", "paid_at": iso(self._now())})
                    for pid in item["payable_ids"]:
                        p = op.get("payables", pid)
                        op.put("payables", pid, {**p, "status": "settled"})
                    if payee and not payee.get("first_paid_at"):
                        op.put("payees", payee["payee_id"], {**payee, "first_paid_at": iso(self._now())})
                    op.record(derived_id("ipd", item["item_id"]), "item_paid", "intel_04_payout_run", item["item_id"],
                              {"item_id": item["item_id"], "net": item["net"]}, f"Payout item paid: {item['net']}")
                    self._settle_batch(op, item["batch_id"])
                    return "paid"
                if t == "failed":
                    if item["status"] != "submitted":
                        return f"ignored_{item['status']}"
                    self._post(op, "zbc", [J.dr("2030", net, f"item:{item['item_id']}"),
                                           J.cr("2020", net, f"payee:{item['payee_id']}")], "F4f",
                               {"kind": "batch_item", "id": item["item_id"]}, f"F4f|{item['item_id']}",
                               actor="intel_04_payout_run")
                    self._reverse_item_entries(op, item, "failed")
                    op.put("items", item["item_id"], {**item, "status": "failed", "failed_at": iso(self._now())})
                    self._payables_back(op, item)
                    if payee:
                        self._open_hold(op, payee, "payout_failed", ev["event_id"])
                    op.record(derived_id("ifl", item["item_id"]), "item_failed", "intel_04_payout_run", item["item_id"],
                              {"item_id": item["item_id"], "net": item["net"]}, "Payout item failed at the rail")
                    self._settle_batch(op, item["batch_id"])
                    return "failed"
                if item["status"] != "paid":
                    return f"ignored_{item['status']}"
                self._post(op, "zbc", [J.dr(acct, net), J.cr("2020", net, f"payee:{item['payee_id']}")], "F4g",
                           {"kind": "batch_item", "id": item["item_id"]}, f"F4g|{item['item_id']}",
                           actor="intel_04_payout_run")
                self._reverse_item_entries(op, item, "returned")
                op.put("items", item["item_id"], {**item, "status": "returned", "returned_at": iso(self._now())})
                self._payables_back(op, item, returned=True)
                if payee:
                    self._open_hold(op, payee, "returned", ev["event_id"])
                bid = rid("brk", "return", item["item_id"])
                op.put("breaks", bid, {"break_id": bid, "leg": "L3", "subject": f"rail:{rail_name}:return",
                                       "difference": item["net"], "opened_at": iso(self._now()),
                                       "opened_on": self._today_la().isoformat(), "owner": "andre",
                                       "explanation_code": "rail_return", "status": "open", "resolution": None,
                                       "item_id": item["item_id"]})
                op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                          {"break_id": bid, "leg": "L3", "difference": item["net"]},
                          "Break opened: returned payout until the rail balance shows the funds")
                op.record(derived_id("irt", item["item_id"]), "item_returned", "intel_04_payout_run", item["item_id"],
                          {"item_id": item["item_id"], "net": item["net"]}, "Payout item returned after payment")
                self._settle_batch(op, item["batch_id"])
                return "returned"
            except PostingRefused as exc:
                raise InvalidReasons("rail event could not be posted", exc.reasons) from None
        if t == "destination_changed":
            payee = next((p for p in self.db["payees"].values() if p.get("rail") == rail_name
                          and p.get("rail_account_ref") and p["rail_account_ref"] == ev.get("account_ref")), None)
            if payee is None:
                return "unknown_account"
            payee = op.get("payees", payee["payee_id"])
            self._open_hold(op, payee, "destination_changed", ev["event_id"], fp_sha(ev.get("new_destination_fingerprint")))
            op.record(derived_id("dcd", payee["payee_id"], ev["event_id"]), "destination_change_detected",
                      "intel_09_controls", payee["payee_id"], {"payee_id": payee["payee_id"], "event_id": ev["event_id"]},
                      "Payout destination changed at the rail: payee on hold until an Andre callback")
            cn = self.ports.cn
            try:
                cn.notify("payout_destination_changed", payee["payee_id"], ev["event_id"])
            except Exception:  # noqa: BLE001 - notice is best effort (stand-in); the hold stands regardless
                pass
            return "hold_opened"
        return "recorded"

    def replay_held_rail_events(self, op_prefix: str, item_id: Optional[str] = None) -> int:
        """Apply the rail events held for items no longer ``submitting`` (sweep B-F2): each in its own operation (a
        refused posting leaves only that event held). Called once an item's acceptance is booked and by the rail-sync
        job. Returns how many were applied; an unavailable ledger stops the pass (the rest stay held)."""
        n = 0
        with self.lock:
            held = sorted((k, dict(r)) for k, r in self.db["rail_events"].items() if r.get("held")
                          and (item_id is None or r.get("item_id") == item_id)
                          and (self.db["items"].get(r.get("item_id") or "") or {}).get("status") != "submitting")
            for k, r in held:
                op = Op(self, f"{op_prefix}|held|{k}|{len(self.log)}", "intel_04_payout_run", f"rail:{r['rail']}")
                try:
                    status = self._rail_event(op, r["rail"], {"event_id": r["event_id"], "type": r["type"],
                                                              "item_id": r["item_id"]})
                except InvalidReasons:
                    continue
                op.put("rail_events", k, {**r, "status": status, "held": status == HELD,
                                          "released_from_hold_at": iso(self._now())})
                self._commit(op)
                n += 1
        return n

    # --- jobs ----------------------------------------------------------------------------------------------------------

    def job_tax_sync(self, op_prefix: str) -> dict:
        g = Gather(self, f"{op_prefix}|tax", "intel_06_tax", "tax_sync")
        answers = {pid: self._tax_answer(g, pid) for pid in sorted(self.db["payees"])}
        with self.lock:
            op = Op(self, f"{op_prefix}|tax", "intel_06_tax", "tax_sync", g)
            n = 0
            for pid, a in answers.items():
                rec = dict(op.get("tax", pid) or {"payee_id": pid})
                rec["agent"] = {"available": a.available, "form_kind": a.form_kind, "form_on_file": a.form_on_file,
                                "tin_match": a.tin_match, "w8_current": a.w8_current,
                                "services_outside_us_attested": a.services_outside_us_attested,
                                "agent_ref": a.agent_ref, "synced_at": iso(self._now())}
                op.put("tax", pid, rec)
                n += a.available
            op.record(derived_id("txs", op_prefix), "tax_status_synced", "intel_06_tax", "tax_sync",
                      {"payees": len(answers), "available": n}, f"Tax status synced for {len(answers)} payee(s)")
            self._commit(op)
        return {"payees": len(answers), "available": n}

    def job_rail_sync(self, op_prefix: str) -> dict:
        pending = [p for p in self.db["payees"].values() if p["status"] == "pending" and p.get("rail")
                   and p.get("declared_country")]
        g = Gather(self, f"{op_prefix}|rails", "intel_04_payout_run", "rail_sync")
        answers = {p["payee_id"]: self._rail_account(g, p, create=not p.get("rail_account_ref")) for p in pending}
        activated = 0
        with self.lock:
            op = Op(self, f"{op_prefix}|rails", "intel_04_payout_run", "rail_sync", g)
            for pid, a in answers.items():
                p = self.db["payees"][pid]
                if a.available and a.status == "verified" and a.payouts_enabled:
                    new = {**p, "status": "active", "rail_account_ref": a.account_ref or p.get("rail_account_ref")}
                    if a.destination_fingerprint and not p.get("destination_fingerprint_sha256"):
                        new.update(destination_fingerprint_sha256=fp_sha(a.destination_fingerprint),
                                   destination_verified_at=iso(self._now()))
                    op.put("payees", pid, new)
                    op.record(derived_id("pye", pid, "active"), "payee_created", "intel_04_payout_run", pid,
                              {"payee_id": pid, "status": "active"}, "Payee activated after rail verification")
                    activated += 1
            self._commit(op)
        drove = self.drive_open_items(op_prefix)
        try:
            held = self.replay_held_rail_events(op_prefix)
        except Unavailable:
            held = 0
        transfers = self.retry_transfers()
        return {"pending_payees": len(pending), "activated": activated, **drove, "held_events_applied": held,
                "transfers": transfers}
