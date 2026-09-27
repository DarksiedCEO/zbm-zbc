"""
Books: rate cards (Andre-published data, §B.3), campaign commercial profiles and client billing profiles, billing
readiness (Onboarding P16), the campaign budget view (no money, for CN/Creative), invoices (intelligence 2: draft by
a caller, issue only by Andre), bank statement lines (receipt matching, F1 / F11a / unapplied cash), disputes,
refunds of unspent deposits (F6), Andre's journal corrections, the month-end close and journal reads.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import chart as C
import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, Invalid, NotFound
from intelligences import i01_journal as J
from intelligences import i02_receivables as I2
from intelligences import i07_reconciliation as I7
from ledger import derived_id
from ports import LegalAnswer
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, rid, sha, sha_text
from textguard import banned_words_in

CLOSE_TASKS = ("preclose_review", "subledgers_closed", "balance_sheet_recs", "restricted_recon_signed",
               "exception_aging", "rollforward_2020_1200")


class BooksMixin:
    # ================================================================== rate cards (§B.3)

    def _rc_versions(self, doc_id: str, op: Optional[Op] = None) -> list[dict]:
        vs = [v for k, v in self.db["rate_cards"].items() if v["doc_id"] == doc_id]
        if op:
            vs += [v for k, v in op.staged.get("rate_cards", {}).items() if v["doc_id"] == doc_id and
                   k not in self.db["rate_cards"]]
        return sorted(vs, key=lambda v: v["version"])

    def _rc_for_campaign(self, campaign_id: str) -> list[dict]:
        return [v for v in self.db["rate_cards"].values() if v["campaign_id"] == campaign_id]

    def propose_rate_card(self, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "rate-cards/proposals", body)
            if ent:
                return ent["response"]
            self.require_rules()
            now = self._now()
            doc_id = body.get("doc_id") or rid("rc", body["campaign_id"], request_id)
            existing = self._rc_versions(doc_id)
            if body.get("doc_id") and not existing:
                raise NotFound("no such rate card document")
            if existing and existing[0]["campaign_id"] != body["campaign_id"]:
                raise Invalid("a rate card document belongs to one campaign")
            open_same = [p for p in self.db["rc_proposals"].values() if p["doc_id"] == doc_id and p["status"] == "open"]
            version = (existing[-1]["version"] if existing else 0) + 1 + len(open_same)
            rates = {t: M.fmt(body["creator_rate_per_1000"][t]) for t in ("T0", "T1", "T2", "T3")}
            card = {"doc_id": doc_id, "version": version, "campaign_id": body["campaign_id"],
                    "creator_rate_per_1000": rates, "max_paid_views_per_clip": body["max_paid_views_per_clip"],
                    "effective_at": iso(parse_iso(body["effective_at"]))}
            card_sha = sha(card)
            prev = max((v for v in existing if v["status"] == "published"), key=lambda v: v["version"], default=None)
            weak = []
            if prev:
                if any(M.D(rates[t]) > M.D(prev["creator_rate_per_1000"][t]) for t in rates):
                    weak.append("creator_rate_raised")
                if card["max_paid_views_per_clip"] > prev["max_paid_views_per_clip"]:
                    weak.append("paid_view_cap_raised")
                if parse_iso(card["effective_at"]) < parse_iso(prev["effective_at"]):
                    weak.append("effective_earlier_than_previous_version")
            if parse_iso(card["effective_at"]) < now:
                weak.append("backdated_effective_at")
            pid = rid("rcp", "andre", request_id)
            p = {"proposal_id": pid, **card, "sha256": card_sha, "status": "open", "created_at": iso(now),
                 "weakening": bool(weak), "weakening_reasons": weak}
            p["content_sha256"] = sha({k: v for k, v in p.items() if k not in ("status",)})
            op = Op(self, pid, "andre", doc_id)
            op.record(derived_id("rcp", pid, p["content_sha256"]), "rules_proposal_created", "andre", doc_id,
                      {"proposal_id": pid, "doc_id": doc_id, "version": version, "sha256": card_sha,
                       "weakening": p["weakening"]}, f"Rate card {doc_id} v{version} proposed")
            op.put("rc_proposals", pid, p)
            resp = self._idem_add(op, key, h, {"proposal": p, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def decide_rate_cards(self, request_id: str, decisions: list[dict]) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "rate-cards/decisions", decisions)
            if ent:
                return ent["response"]
            self.require_rules()
            ids = [d["proposal_id"] for d in decisions]
            if len(set(ids)) != len(ids):
                raise Invalid("a proposal may appear only once per decision call")
            chosen = []
            for d in decisions:
                p = self.db["rc_proposals"].get(d["proposal_id"])
                if p is None:
                    raise NotFound(f"no rate card proposal {d['proposal_id']}")
                if p["status"] != "open":
                    raise Conflict(f"proposal {d['proposal_id']} is already {p['status']}")
                if d["content_sha256"] != p["content_sha256"]:
                    raise Conflict(f"proposal {d['proposal_id']} changed since you read it; nothing was applied")
                if d["decision"] == "approve" and p["weakening"] and d.get("acknowledge_weakening") is not True:
                    raise Invalid(f"approval raises exposure ({', '.join(p['weakening_reasons'])}); approving needs "
                                  "acknowledge_weakening: true. Nothing was applied")
                chosen.append((d, p))
            now = iso(self._now())
            op = Op(self, f"rcd|{request_id}", "andre", "rate_cards")
            published = []
            for d, p in chosen:
                op.put("rc_proposals", p["proposal_id"], {**p, "status": "approved" if d["decision"] == "approve"
                                                          else "rejected", "decided_at": now})
                if d["decision"] != "approve":
                    continue
                if f"{p['doc_id']}|{p['version']}" in self.db["rate_cards"]:
                    raise Conflict(f"rate card {p['doc_id']} v{p['version']} already exists (stale proposal)")
                card = {k: p[k] for k in ("doc_id", "version", "campaign_id", "creator_rate_per_1000",
                                          "max_paid_views_per_clip", "effective_at", "sha256")}
                card.update(status="published", approved_by="andre", approved_at=now,
                            acknowledged_weakening=d.get("acknowledge_weakening") is True)
                op.record(derived_id("rcpub", p["doc_id"], p["version"], p["sha256"]), "rate_card_published", "andre",
                          p["doc_id"], {"doc_id": p["doc_id"], "version": p["version"], "sha256": p["sha256"],
                                        "campaign_id": p["campaign_id"], "weakening": p["weakening"]},
                          f"Andre published rate card {p['doc_id']} v{p['version']}")
                op.put("rate_cards", f"{p['doc_id']}|{p['version']}", card)
                published.append({"doc_id": p["doc_id"], "version": p["version"], "sha256": p["sha256"]})
            resp = self._idem_add(op, key, h, {"decided": len(chosen), "published": published,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def rate_card_meta(self, doc_id: str, version: int) -> dict:
        with self.lock:
            v = self.db["rate_cards"].get(f"{doc_id}|{version}")
            if v is None:
                raise NotFound("no such rate card version")
            return {"doc_id": doc_id, "version": version, "published": v["status"] == "published", "sha256": v["sha256"],
                    "campaign_id": v["campaign_id"], "effective_at": v["effective_at"],
                    "rules_pinned": self.rules_pinned}

    def rate_card_document(self, doc_id: str, version: int) -> dict:
        with self.lock:
            v = self.db["rate_cards"].get(f"{doc_id}|{version}")
            if v is None:
                raise NotFound("no such rate card version")
            return dict(v)

    # ================================================================== profiles (§B.3)

    def put_profile(self, request_id: str, campaign_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"profile/{campaign_id}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            if body.get("account_title") and banned_words_in(body["account_title"]):
                raise InvalidReasons("account title refused", [R.item("BANNED_WORD", "custody wording in an account "
                                                                                      "title (escrow/trust/FBO)")])
            cards = [v for v in self._rc_versions(body["rate_card_doc_id"]) if v["status"] == "published"]
            if not cards or cards[0]["campaign_id"] != campaign_id:
                raise Refused("no published rate card of this campaign", [R.item("RATE_CARD_MISSING",
                                                                                 "publish the campaign's rate card first")])
            old = self.db["profiles"].get(campaign_id)
            if old and old["client_id"] != body["client_id"]:
                raise Invalid("a campaign belongs to one client")
            now = iso(self._now())
            prof = {"campaign_id": campaign_id, "client_id": body["client_id"],
                    "order_form": dict(body["order_form"]), "budget": M.fmt(body["budget"]),
                    "client_rate_per_1000": M.fmt(body["client_rate_per_1000"]),
                    "rate_card_doc_id": body["rate_card_doc_id"],
                    "status": old["status"] if old and old["status"] in ("funded", "paused", "closing", "closed")
                    else "approved", "approved_by": "andre", "approved_at": now,
                    "version": (old["version"] + 1) if old else 1,
                    "account_title": C.DEPOSITS_ACCOUNT_TITLE}
            op = Op(self, f"prof|{request_id}", "andre", campaign_id)
            op.record(derived_id("prof", campaign_id, prof["version"], sha(prof)), "campaign_profile_approved", "andre",
                      campaign_id, {"campaign_id": campaign_id, "version": prof["version"], "sha256": sha(prof),
                                    "budget": prof["budget"]}, f"Andre approved commercial profile v{prof['version']}")
            op.put("profiles", campaign_id, prof)
            resp = self._idem_add(op, key, h, {"profile": prof, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def put_billing_profile(self, request_id: str, client_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"billing/{client_id}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            prof = {"client_id": client_id, "entity": body["entity"], "payment_method": body["payment_method"],
                    "msa": dict(body["msa"]), "approved_by": "andre", "approved_at": iso(self._now())}
            op = Op(self, f"bprof|{request_id}", "andre", client_id)
            op.record(derived_id("bprof", client_id, sha(prof)), "campaign_profile_approved", "andre", client_id,
                      {"client_id": client_id, "entity": prof["entity"], "sha256": sha(prof)},
                      "Andre approved a client billing profile")
            op.put("client_profiles", client_id, prof)
            resp = self._idem_add(op, key, h, {"billing_profile": prof, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def _legal(self, g: Gather, ref: dict) -> LegalAnswer:
        legal = self.ports.legal
        args = (ref["doc_id"], ref["version"], ref["doc_sha256"], ref["acceptance_id"])
        ans = g.call("legal_37", "document_status", args, lambda: legal.document_status(*args), LegalAnswer(False))
        return ans if isinstance(ans, LegalAnswer) else LegalAnswer(False)

    def _legal_reasons(self, g: Gather, ref: dict, what: str) -> list[dict]:
        ans = self._legal(g, ref)
        if not ans.available:
            return [R.item("DEPENDENCY_UNAVAILABLE:legal_37", f"Legal (37) cannot confirm the {what}", rule="FIN-29")]
        out = []
        if not ans.current:
            out.append(R.item("LEGAL_NOT_CURRENT", f"the {what} version is not current at Legal"))
        if not ans.acceptance_matches:
            out.append(R.item("LEGAL_NOT_CURRENT", f"the {what} acceptance record does not match its sha256"))
        return out

    def _fundable_reasons(self, g: Gather, campaign_id: str) -> list[dict]:
        prof = self.db["profiles"].get(campaign_id)
        if prof is None:
            return [R.item("CAMPAIGN_NOT_FUNDED", "no Andre-approved commercial profile for this campaign")]
        out = self._counsel_reasons("FIN-CQ-01")
        out += self._legal_reasons(g, prof["order_form"], "order form")
        if prof["status"] in ("paused", "closing", "closed"):
            out.append(R.item("CAMPAIGN_NOT_FUNDED", f"campaign is {prof['status']}"))
        return out

    def billing_readiness(self, principal: str, client_id: str) -> dict:
        g = Gather(self, f"bready|{client_id}|{iso(self._now())}", "intel_02_receivables", client_id)
        unmet = list(self._rules_reasons())
        prof = self.db["client_profiles"].get(client_id)
        if prof is None:
            unmet.append(R.item("LEGAL_NOT_CURRENT", "no Andre-approved client billing profile (entity, payment method, "
                                                     "MSA)"))
        else:
            unmet += self._legal_reasons(g, prof["msa"], "MSA")
            if prof["payment_method"] not in I2.PAYMENT_METHODS:
                unmet.append(R.item("CARD_DISABLED", "payment method must be ACH or wire"))
            if prof["entity"] == "zbc":
                camps = [c for c, p in self.db["profiles"].items() if p["client_id"] == client_id]
                if not camps:
                    unmet.append(R.item("CAMPAIGN_NOT_FUNDED", "no ZBC campaign commercial profile for this client"))
                elif all(self._fundable_reasons(g, c) for c in camps):
                    unmet += self._fundable_reasons(g, camps[0])
        unmet += [R.item("TAX_TREATMENT_UNVERIFIED", x["message"], obligation_id="FIN-CQ-11")
                  for x in self._counsel_reasons("FIN-CQ-11")]
        unmet = R.dedupe(unmet)
        return {"client_id": client_id, "allowed": not unmet, "unmet": R.lines(unmet), "reasons": unmet,
                "detail": "billing ready" if not unmet else f"{len(unmet)} requirement(s) unmet",
                "rules_pinned": self.rules_pinned, "ledger_event_ids": g.events}

    def budget_view(self, campaign_id: str) -> dict:
        with self.lock:
            prof = self.db["profiles"].get(campaign_id)
            if prof is None:
                raise NotFound("no commercial profile for this campaign")
            remaining = self.bal("2010", f"campaign:{campaign_id}")
            budget = M.D(prof["budget"])
            rate = M.D(prof["client_rate_per_1000"])
            if prof["status"] not in ("funded",) or remaining <= 0:
                state = "exhausted"
            elif remaining * 10 < budget:
                state = "near_exhausted"
            else:
                state = "open"
            with M.money_context():
                est = int((remaining * 1000 / rate).to_integral_value(rounding="ROUND_FLOOR")) if remaining > 0 else 0
            return {"campaign_id": campaign_id, "budget_state": state, "remaining_views_estimate": est,
                    "rules_pinned": self.rules_pinned}

    # ================================================================== invoices (§B.9, §C.2)

    def draft_invoice(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "invoices", body)
            if ent:
                return ent["response"]
            self.require_rules()
            problems = I2.text_problems({"lines": [l.get("description") for l in body["lines"]],
                                         "notes": body.get("notes"), "template_vars": body.get("template_vars")})
            codes = I2.LINE_CODES[body["entity"]]
            for i, l in enumerate(body["lines"]):
                if l["line_code"] not in codes:
                    problems.append(R.item("JOURNAL_INVALID" if body["entity"] == "zbm" else "ENTITY_MIX",
                                           f"line {i + 1}: {l['line_code']} is not a {body['entity']} line code"))
            if "card" in body["payment_methods"]:
                problems.append(R.item("CARD_DISABLED", "card payments are off (D11; FIN-CQ-09)"))
            if body["entity"] == "zbc" and (body["kind"] != "campaign_deposit" or not body.get("campaign_id")):
                problems.append(R.item("ENTITY_MIX", "a ZBC invoice is a campaign deposit invoice naming its campaign"))
            if body["entity"] == "zbm" and body["kind"] == "campaign_deposit":
                problems.append(R.item("ENTITY_MIX", "campaign deposits are ZBC's, never ZBM's"))
            problems += I2.recurring_problems({"kind": body["kind"], "recurring": body.get("recurring")})
            if problems:
                raise InvalidReasons("invoice draft refused", R.dedupe(problems))
            total = I2.lines_total(body["lines"])
            now = self._now()
            iid = rid("inv", principal, request_id)
            inv = {"invoice_id": iid, "entity": body["entity"], "client_id": body["client_id"],
                   "campaign_id": body.get("campaign_id"), "kind": body["kind"],
                   "lines": [{"line_code": l["line_code"], "quantity": l["quantity"], "unit_price": M.fmt(l["unit_price"]),
                              "amount": M.fmt(I2.line_amount(l)), "description": l.get("description")}
                             for l in body["lines"]],
                   "total": M.fmt(total), "currency": "USD", "payment_methods": sorted(set(body["payment_methods"])),
                   "due_days": body["due_days"], "legal_ref": dict(body["legal_ref"]),
                   "recurring": ({k: (v.isoformat() if isinstance(v, date) else v)
                                  for k, v in body["recurring"].items()} if body.get("recurring") else None),
                   "notes_sha256": sha_text(body["notes"]) if body.get("notes") else None,
                   "template_vars": body.get("template_vars"), "drafted_by": principal, "drafted_at": iso(now),
                   "status": "draft", "tax_treatment": {"status": "verified" if self.counsel_verified("FIN-CQ-11")
                                                        else "unverified", "basis_row": "FIN-CQ-11"}}
            inv["content_sha256"] = sha({k: v for k, v in inv.items() if k not in ("status", "tax_treatment")})
            op = Op(self, f"inv|{iid}", "intel_02_receivables", iid)
            op.record(derived_id("invd", iid), "invoice_drafted", "intel_02_receivables", iid,
                      {"invoice_id": iid, "entity": inv["entity"], "kind": inv["kind"], "total": inv["total"],
                       "content_sha256": inv["content_sha256"]}, f"Invoice drafted: {inv['total']} ({inv['kind']})")
            self._injection(op, {"notes": body.get("notes"), "lines": [l.get("description") for l in body["lines"]]})
            op.put("invoices", iid, inv)
            resp = self._idem_add(op, key, h, {"invoice": inv, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def decide_invoice(self, request_id: str, invoice_id: str, body: dict) -> dict:
        key, h, ent = self._idem("andre", request_id, f"invoice/{invoice_id}", body)
        if ent:
            return ent["response"]
        inv = self.db["invoices"].get(invoice_id)
        if inv is None:
            raise NotFound("no such invoice")
        g = Gather(self, f"invdec|{request_id}", "intel_02_receivables", invoice_id)
        reasons = list(self._rules_reasons())
        if body["decision"] == "approve" and not reasons:
            reasons += self._legal_reasons(g, inv["legal_ref"], "contract (MSA/order form/SOW)")
            reasons += [R.item("TAX_TREATMENT_UNVERIFIED", "sales-tax treatment (FIN-CQ-11) is unverified",
                               obligation_id="FIN-CQ-11")] if not self.counsel_verified("FIN-CQ-11") else []
            if inv["kind"] == "campaign_deposit":
                reasons += self._fundable_reasons(g, inv["campaign_id"])
            if inv["kind"] in I2.RECURRING_KINDS:
                reasons += self._rows_reasons(g, ("US-ROSCA",), "FIN-22")
                reasons += self._counsel_reasons("FIN-CQ-10")
                reasons += I2.recurring_problems(inv)
        with self.lock:
            inv = self.db["invoices"][invoice_id]
            if inv["status"] != "draft":
                raise Conflict(f"invoice is already {inv['status']}")
            if body["content_sha256"] != inv["content_sha256"]:
                raise Conflict("invoice changed since you read it (content_sha256 mismatch)")
            if reasons:
                raise Refused("invoice cannot be issued", R.dedupe(reasons), ledger_event_ids=g.events)
            now = self._now()
            op = Op(self, f"invdec|{request_id}", "andre", invoice_id, g)
            if body["decision"] == "reject":
                new = {**inv, "status": "void", "decided_at": iso(now)}
                op.record(derived_id("invr", invoice_id), "invoice_approved", "andre", invoice_id,
                          {"invoice_id": invoice_id, "decision": "reject"}, "Andre rejected an invoice draft")
            else:
                due = (now.date() + timedelta(days=inv["due_days"])).isoformat()
                new = {**inv, "status": "issued", "approved_by": "andre", "issued_at": iso(now), "due_at": due}
                op.record(derived_id("inva", invoice_id), "invoice_approved", "andre", invoice_id,
                          {"invoice_id": invoice_id, "content_sha256": inv["content_sha256"]}, "Andre approved an invoice")
                op.record(derived_id("invi", invoice_id), "invoice_issued", "intel_02_receivables", invoice_id,
                          {"invoice_id": invoice_id, "entity": inv["entity"], "total": inv["total"]},
                          f"Invoice issued: {inv['total']} ({inv['entity']})")
                if inv["entity"] == "zbm":
                    credit = "2110" if inv["kind"] in ("retainer",) else "4110"
                    amt = M.D(inv["total"])
                    try:
                        self._post(op, "zbm", [J.dr("1100", amt, f"client:{inv['client_id']}"),
                                               J.cr(credit, amt, f"client:{inv['client_id']}" if credit == "2110" else None)],
                                   "F11", {"kind": "invoice", "id": invoice_id}, f"F11|{invoice_id}", approval_ref=request_id)
                    except PostingRefused as exc:
                        raise InvalidReasons("invoice posting refused", exc.reasons) from None
            op.put("invoices", invoice_id, new)
            resp = self._idem_add(op, key, h, {"invoice": new, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def get_invoice(self, invoice_id: str) -> dict:
        with self.lock:
            inv = self.db["invoices"].get(invoice_id)
            if inv is None:
                raise NotFound("no such invoice")
            return dict(inv)

    # ================================================================== bank statement lines (receipts)

    def bank_events(self, principal: str, request_id: str, lines: list[dict]) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "bank/events", lines)
            if ent:
                return ent["response"]
            self.require_rules()
            op = Op(self, f"bank|{request_id}", "intel_02_receivables", "bank_feed")
            results = []
            for ln in lines:
                rct = rid("rct", ln["txn_ref_sha256"])
                if op.get("receipts", rct):
                    results.append({"receipt_id": rct, "status": "duplicate"})
                    continue
                amt = ln["amount"]
                entity, acct = ln["entity"], ln["account"]
                if entity == "zbm" and acct == "1020":
                    raise Invalid("ZBM has no restricted deposits account (1020 is ZBC's)")
                inv = I2.match_receipt(amt, ln.get("reference_token"),
                                       {k: op.get("invoices", k) for k in self.db["invoices"]}, entity)
                ok_zbc = inv is not None and entity == "zbc" and acct == "1020" and inv["kind"] == "campaign_deposit"
                ok_zbm = inv is not None and entity == "zbm" and acct == "1010"
                src = {"kind": "receipt", "id": rct}
                try:
                    if ok_zbc:
                        e = self._post(op, "zbc", [J.dr("1020", amt), J.cr("2010", amt, f"campaign:{inv['campaign_id']}")],
                                       "F1", src, f"F1|{rct}")
                        op.put("invoices", inv["invoice_id"], {**inv, "status": "paid", "paid_at": iso(self._now())})
                        prof = op.get("profiles", inv["campaign_id"])
                        if prof and prof["status"] == "approved":
                            op.put("profiles", inv["campaign_id"], {**prof, "status": "funded"})
                        status, etype = "matched", "receipt_matched"
                    elif ok_zbm:
                        e = self._post(op, "zbm", [J.dr("1010", amt), J.cr("1100", amt, f"client:{inv['client_id']}")],
                                       "F11a", src, f"F11a|{rct}")
                        op.put("invoices", inv["invoice_id"], {**inv, "status": "paid", "paid_at": iso(self._now())})
                        status, etype = "matched", "receipt_matched"
                    else:
                        memo = "F1a" if (entity == "zbc" and acct == "1010") else "F1"
                        e = self._post(op, entity, [J.dr(acct, amt), J.cr("2070", amt)], memo, src, f"{memo}u|{rct}")
                        status, etype = "unapplied", "receipt_unapplied"
                        bid = rid("brk", "unapplied", rct)
                        op.put("breaks", bid, {"break_id": bid, "leg": "unapplied_cash", "subject": f"{entity}:{acct}",
                                               "difference": M.fmt(amt), "opened_at": iso(self._now()),
                                               "opened_on": self._today_la().isoformat(), "owner": "andre",
                                               "explanation_code": "misapplied_receipt", "status": "open",
                                               "receipt_id": rct, "resolution": None})
                        op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                                  {"break_id": bid, "leg": "unapplied_cash", "difference": M.fmt(amt)},
                                  f"Break opened: unapplied cash {M.fmt(amt)}")
                except PostingRefused as exc:
                    raise InvalidReasons("statement line could not be posted", exc.reasons) from None
                op.record(derived_id("rct", rct), etype, "intel_02_receivables", rct,
                          {"receipt_id": rct, "entity": entity, "account": acct, "amount": M.fmt(amt),
                           "invoice_id": inv["invoice_id"] if inv and status == "matched" else None},
                          f"Receipt {status}: {M.fmt(amt)} into {entity}/{acct}")
                op.put("receipts", rct, {"receipt_id": rct, "invoice_id": inv["invoice_id"] if status == "matched" else None,
                                         "entity": entity, "amount": M.fmt(amt), "method": "ach_or_wire",
                                         "bank_txn_ref_sha256": ln["txn_ref_sha256"], "into_account": acct,
                                         "value_date": ln["value_date"].isoformat() if isinstance(ln["value_date"], date)
                                         else str(ln["value_date"]), "matched_at": iso(self._now()),
                                         "status": status, "entry_id": e["entry_id"]})
                results.append({"receipt_id": rct, "status": status, "entry_id": e["entry_id"]})
            resp = self._idem_add(op, key, h, {"results": results, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def apply_receipt(self, request_id: str, receipt_id: str, invoice_id: str) -> dict:
        """Andre applies an unapplied receipt (in the deposits account) to an issued campaign deposit invoice."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"receipt/{receipt_id}", invoice_id)
            if ent:
                return ent["response"]
            self.require_rules()
            rc = self.db["receipts"].get(receipt_id)
            inv = self.db["invoices"].get(invoice_id)
            if rc is None or inv is None:
                raise NotFound("no such receipt or invoice")
            if rc["status"] != "unapplied" or inv["status"] != "issued" or M.D(inv["total"]) != M.D(rc["amount"]):
                raise Conflict("only an unapplied receipt of exactly an issued invoice's total can be applied")
            if not (rc["entity"] == "zbc" and rc["into_account"] == "1020" and inv["kind"] == "campaign_deposit"):
                raise Conflict("only a receipt that landed in the deposits account can be applied to a deposit invoice "
                               "(a deposit in the operating account is moved by an Andre top-up first)")
            op = Op(self, f"apply|{request_id}", "andre", receipt_id)
            amt = M.D(rc["amount"])
            try:
                e = self._post(op, "zbc", [J.dr("2070", amt), J.cr("2010", amt, f"campaign:{inv['campaign_id']}")], "F1",
                               {"kind": "receipt", "id": receipt_id}, f"F1apply|{receipt_id}", approval_ref=request_id)
            except PostingRefused as exc:
                raise InvalidReasons("application refused", exc.reasons) from None
            op.put("receipts", receipt_id, {**rc, "status": "matched", "invoice_id": invoice_id})
            op.put("invoices", invoice_id, {**inv, "status": "paid", "paid_at": iso(self._now())})
            prof = self.db["profiles"].get(inv["campaign_id"])
            if prof and prof["status"] == "approved":
                op.put("profiles", inv["campaign_id"], {**prof, "status": "funded"})
            for b in self.db["breaks"].values():
                if b.get("receipt_id") == receipt_id and b["status"] == "open":
                    op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolution": {
                        "entry_id": e["entry_id"], "approved_by": "andre", "at": iso(self._now())}})
                    op.record(derived_id("brkr", b["break_id"], e["entry_id"]), "break_resolved_by_andre", "andre",
                              b["break_id"], {"break_id": b["break_id"], "entry_id": e["entry_id"]},
                              "Andre resolved an unapplied-cash break")
            op.record(derived_id("rcta", receipt_id), "receipt_matched", "andre", receipt_id,
                      {"receipt_id": receipt_id, "invoice_id": invoice_id, "amount": M.fmt(amt)},
                      "Andre applied an unapplied receipt")
            resp = self._idem_add(op, key, h, {"receipt_id": receipt_id, "entry_id": e["entry_id"],
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    # ================================================================== disputes (§B.9, FIN-23)

    def open_dispute(self, who: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem(who, request_id, "disputes", body)
            if ent:
                return ent["response"]
            self.require_rules()
            if body["kind"] == "card_chargeback" or who == "rail_gateway":
                raise Refused("card prepayments are off, so no card chargeback can exist here (D11); nothing recorded "
                              "as a dispute", [R.item("CARD_DISABLED", "card acceptance is disabled (FIN-CQ-09)")])
            inv = self.db["invoices"].get(body["invoice_id"])
            if inv is None:
                raise NotFound("no such invoice")
            did = rid("dsp", who, request_id)
            now = iso(self._now())
            certs = sorted({p["certification_id"] for p in self.db["payables"].values()
                            if inv.get("campaign_id") and p["campaign_id"] == inv["campaign_id"]})[:200]
            prof = self.db["profiles"].get(inv.get("campaign_id") or "")
            pack = [{"kind": "signed_order_form", "ref": prof["order_form"]["doc_id"],
                     "sha256": prof["order_form"]["doc_sha256"]}] if prof else []
            pack += [{"kind": "verified_view_report", "ref": c, "sha256": sha_text(c)} for c in certs]
            pack += [{"kind": "caller_ref", "ref": r, "sha256": sha_text(r)} for r in body.get("evidence_refs") or []]
            d = {"dispute_id": did, "kind": body["kind"], "invoice_id": inv["invoice_id"], "amount": M.fmt(body["amount"]),
                 "opened_at": now, "respond_by": None, "evidence_pack": pack, "status": "open", "entry_ids": [],
                 "campaign_id": inv.get("campaign_id"), "notes_sha256": sha_text(body["notes"]) if body.get("notes") else None}
            op = Op(self, f"dsp|{did}", who, did)
            op.record(derived_id("dsp", did), "dispute_opened", who, did,
                      {"dispute_id": did, "invoice_id": inv["invoice_id"], "amount": d["amount"], "kind": d["kind"]},
                      f"Dispute opened on an invoice ({d['amount']})")
            self._injection(op, {"notes": body.get("notes"), "refs": body.get("evidence_refs")})
            op.put("disputes", did, d)
            if prof and prof["status"] == "funded":
                op.put("profiles", prof["campaign_id"], {**prof, "status": "paused", "paused_by": did})
            resp = self._idem_add(op, key, h, {"dispute": d, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def dispute_outcome(self, request_id: str, dispute_id: str, outcome: str) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"dispute/{dispute_id}", outcome)
            if ent:
                return ent["response"]
            d = self.db["disputes"].get(dispute_id)
            if d is None:
                raise NotFound("no such dispute")
            if d["status"] != "open":
                raise Conflict(f"dispute is already {d['status']}")
            op = Op(self, f"dspo|{request_id}", "andre", dispute_id)
            new = {**d, "status": outcome, "closed_at": iso(self._now())}
            op.record(derived_id("dspc", dispute_id), "dispute_closed", "andre", dispute_id,
                      {"dispute_id": dispute_id, "outcome": outcome}, f"Dispute closed: {outcome}")
            op.put("disputes", dispute_id, new)
            prof = self.db["profiles"].get(d.get("campaign_id") or "")
            others = [x for x in self.db["disputes"].values() if x["status"] == "open" and x["dispute_id"] != dispute_id
                      and x.get("campaign_id") == d.get("campaign_id")]
            if prof and prof["status"] == "paused" and not others:
                op.put("profiles", prof["campaign_id"], {**prof, "status": "funded", "paused_by": None})
            resp = self._idem_add(op, key, h, {"dispute": new, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def _open_disputes(self, campaign_id: str) -> list[dict]:
        return [d for d in self.db["disputes"].values() if d["status"] == "open" and d.get("campaign_id") == campaign_id]

    # ================================================================== refunds (F6)

    def propose_refund(self, principal: str, request_id: str, campaign_id: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, f"refunds/{campaign_id}", campaign_id)
            if ent:
                return ent["response"]
            self.require_rules()
            prof = self.db["profiles"].get(campaign_id)
            if prof is None:
                raise NotFound("no commercial profile for this campaign")
            reasons = []
            if self._open_disputes(campaign_id):
                reasons.append(R.item("DISPUTE_OPEN", "a dispute is open on this campaign: refunds are frozen"))
            reasons += self._control_block_reasons("refund")
            pending = [p for p in self.db["payables"].values() if p["campaign_id"] == campaign_id
                       and p["status"] in ("pending_checks", "over_budget_hold")]
            if pending:
                reasons.append(R.item("OVER_BUDGET", f"{len(pending)} payable(s) of this campaign still await checks "
                                                     "or Andre", rule="FIN-06"))
            amount = self.bal("2010", f"campaign:{campaign_id}")
            if amount <= 0:
                reasons.append(R.item("CAMPAIGN_NOT_FUNDED", "no unspent deposit to refund"))
            if reasons:
                raise Refused("refund refused", R.dedupe(reasons))
            fid = rid("rfd", campaign_id, request_id)
            r = {"refund_id": fid, "campaign_id": campaign_id, "client_id": prof["client_id"], "amount": M.fmt(amount),
                 "admin_fee": "0.00", "status": "proposed", "proposed_at": iso(self._now()), "proposed_by": principal}
            r["content_sha256"] = sha({k: r[k] for k in ("refund_id", "campaign_id", "client_id", "amount", "admin_fee")})
            op = Op(self, f"rfd|{fid}", "intel_02_receivables", fid)
            op.record(derived_id("rfdp", fid), "refund_proposed", "intel_02_receivables", fid,
                      {"refund_id": fid, "campaign_id": campaign_id, "amount": r["amount"]},
                      f"Refund of unspent deposit proposed: {r['amount']}")
            op.put("refunds", fid, r)
            op.put("profiles", campaign_id, {**prof, "status": "closing"})
            resp = self._idem_add(op, key, h, {"refund": r, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def decide_refund(self, request_id: str, refund_id: str, body: dict) -> dict:
        key, h, ent = self._idem("andre", request_id, f"refund/{refund_id}", body)
        if ent:
            return ent["response"]
        with self.lock:
            r = self.db["refunds"].get(refund_id)
            if r is None:
                raise NotFound("no such refund")
            if r["status"] != "proposed":
                raise Conflict(f"refund is already {r['status']}")
            if body["content_sha256"] != r["content_sha256"]:
                raise Conflict("refund changed since you read it (content_sha256 mismatch)")
            op = Op(self, f"rfdd|{request_id}", "andre", refund_id)
            if body["decision"] == "reject":
                prof = self.db["profiles"][r["campaign_id"]]
                op.put("refunds", refund_id, {**r, "status": "rejected"})
                op.put("profiles", r["campaign_id"], {**prof, "status": "funded"})
                op.record(derived_id("rfdr", refund_id), "refund_approved", "andre", refund_id,
                          {"refund_id": refund_id, "decision": "reject"}, "Andre rejected a refund")
                resp = self._idem_add(op, key, h, {"refund": {**r, "status": "rejected"}, "ledger_event_ids": op.events,
                                                   "request_id": request_id})
                self._commit(op)
                return resp
            reasons = []
            if self._open_disputes(r["campaign_id"]):
                reasons.append(R.item("DISPUTE_OPEN", "a dispute is open on this campaign: refunds are frozen"))
            reasons += self._control_block_reasons("refund")
            if self.bal("2010", f"campaign:{r['campaign_id']}") != M.D(r["amount"]):
                reasons.append(R.item("CAMPAIGN_NOT_FUNDED", "the unspent deposit changed since the proposal"))
            if reasons:
                raise Refused("refund refused", R.dedupe(reasons))
            amt = M.D(r["amount"])
            try:
                e = self._post(op, "zbc", [J.dr("2010", amt, f"campaign:{r['campaign_id']}"),
                                           J.cr("2050", amt, f"client:{r['client_id']}")], "F6",
                               {"kind": "refund", "id": refund_id}, f"F6|{refund_id}", approval_ref=request_id)
            except PostingRefused as exc:
                raise Refused("refund posting refused", exc.reasons) from None
            op.record(derived_id("rfda", refund_id), "refund_approved", "andre", refund_id,
                      {"refund_id": refund_id, "amount": r["amount"], "entry_id": e["entry_id"]},
                      f"Andre approved a refund of {r['amount']}")
            new = {**r, "status": "approved", "approved_at": iso(self._now()), "entry_ids": [e["entry_id"]]}
            op.put("refunds", refund_id, new)
            tid = rid("trx", "refund", refund_id)
            op.put("treasury_ops", tid, {"op_id": tid, "kind": "refund_payment", "entity": "zbc", "from": "1020",
                                         "to": f"client:{r['client_id']}", "amount": r["amount"], "status": "approved",
                                         "ref_id": refund_id, "approved_at": iso(self._now()), "attempts": 0})
            self._commit(op)
        paid = self._execute_transfer(tid)
        resp = {"refund": self.db["refunds"][refund_id], "payment": paid, "ledger_event_ids": op.events,
                "request_id": request_id}
        with self.lock:
            return self._idem_mem(key, h, resp)

    # ================================================================== corrections, close, reads

    def correction(self, request_id: str, entity: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"corrections/{entity}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            if entity not in C.ENTITIES:
                raise NotFound("no such entity")
            rev = body.get("reverses_entry_id")
            if rev and not body.get("lines"):
                orig = self.entries_by_id.get(rev)
                if orig is None:
                    raise NotFound("no such entry")
                lines = J.reversal_lines(orig)
            elif body.get("lines"):
                raw = [dict(l) for l in body["lines"]]
                mix = [l for l in raw if l.get("entity") not in (None, entity)]
                if mix:
                    raise InvalidReasons("correction refused", [R.item("ENTITY_MIX", "one entry, one entity: a line "
                                                                                     "names another entity")])
                lines = [{"account": l["account"], "subledger": l.get("subledger"), "debit": M.fmt(l["debit"]),
                          "credit": M.fmt(l["credit"])} for l in raw]
            else:
                raise Invalid("a correction names reverses_entry_id, lines, or both")
            op = Op(self, f"corr|{request_id}", "andre", entity)
            self._injection(op, {"notes": body.get("notes")})
            try:
                e = self._post(op, entity, lines, "correction", {"kind": "correction", "id": request_id},
                               f"corr|{request_id}", approval_ref=request_id, reverses=rev,
                               effective_date=body["effective_date"], actor="andre")
            except PostingRefused as exc:
                if exc.status == 409:
                    raise Refused("correction refused", exc.reasons) from None
                raise InvalidReasons("correction refused", exc.reasons) from None
            resp = self._idem_add(op, key, h, {"entry": e, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def close_task(self, principal: str, request_id: str, entity: str, period: str, task: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, f"close/{entity}/{period}/{task}", None)
            if ent:
                return ent["response"]
            self.require_rules()
            if entity not in C.ENTITIES or task not in CLOSE_TASKS:
                raise NotFound("no such entity or close task")
            if f"{entity}|{period}" in self.db["locks"]:
                raise Conflict("period already locked")
            if period >= self._today_la().isoformat()[:7]:
                raise Conflict("a period closes only after it has ended")
            reasons = []
            last = self._latest_recon()
            legs = {l["leg"] + ":" + l["subject"]: l for l in (last or {}).get("legs", [])}
            if task == "restricted_recon_signed":
                l1 = [l for k, l in legs.items() if k.startswith("L1")]
                l4 = [l for k, l in legs.items() if k.startswith("L4")]
                if not last or not l1 or not I7.all_matched(l1 + l4):
                    reasons.append(R.item("RECON_BREAK", "1020 not reconciled to the campaign sub-ledgers (L1/L4)"))
            if task == "rollforward_2020_1200":
                l4 = [l for k, l in legs.items() if k.startswith("L4")]
                if not last or not l4 or not I7.all_matched(l4):
                    reasons.append(R.item("RECON_BREAK", "2020/1200 roll-forward does not tie to payables (L4)"))
            if task == "exception_aging":
                old = [b for b in self.db["breaks"].values() if b["status"] == "open"
                       and I7.aging_bucket(date.fromisoformat(b["opened_on"]), self._today_la()) == ">5"]
                if old:
                    reasons.append(R.item("RECON_BREAK", f"{len(old)} break(s) older than 5 business days unexplained"))
            status = "failed" if reasons else "done"
            op = Op(self, f"close|{request_id}", "intel_01_journal", f"{entity}:{period}")
            k = f"{entity}|{period}|{task}"
            op.put("close", k, {"entity": entity, "period": period, "task": task, "status": status,
                                "at": iso(self._now()), "reasons": reasons})
            op.record(derived_id("clt", k, principal, request_id), "control_result_recorded", "intel_01_journal", k[:128],
                      {"task": task, "entity": entity, "period": period, "status": status},
                      f"Close task {task} {status}")
            resp = self._idem_add(op, key, h, {"task": task, "status": status, "reasons": reasons,
                                               "reason_lines": R.lines(reasons), "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def close_approve(self, request_id: str, entity: str, period: str) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"close/{entity}/{period}/approve", None)
            if ent:
                return ent["response"]
            self.require_rules()
            if entity not in C.ENTITIES:
                raise NotFound("no such entity")
            missing = [t for t in CLOSE_TASKS if (self.db["close"].get(f"{entity}|{period}|{t}") or {}).get("status")
                       != "done"]
            if missing:
                raise Refused("close checklist incomplete", [R.item("RECON_BREAK", f"close task not done: {t}",
                                                                    rule="FIN-25") for t in missing])
            draft = self._counsel_reasons("FIN-CQ-03", "FIN-CQ-05") + [R.item(
                "DEPENDENCY_UNAVAILABLE:gl_qbo", "GL tie-out (L5) cannot run: QBO adapter is a stand-in", rule="FIN-26")]
            op = Op(self, f"lock|{request_id}", "andre", f"{entity}:{period}")
            op.put("locks", f"{entity}|{period}", {"entity": entity, "period": period, "locked": True,
                                                   "locked_at": iso(self._now()), "approved_by": "andre",
                                                   "statements": "draft" if draft else "final"})
            op.record(derived_id("lock", entity, period), "period_locked", "andre", f"{entity}:{period}",
                      {"entity": entity, "period": period, "statements": "draft" if draft else "final"},
                      f"Andre approved the {period} close for {entity}; period locked")
            resp = self._idem_add(op, key, h, {"entity": entity, "period": period, "locked": True,
                                               "statements": "draft" if draft else "final", "draft_reasons": draft,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def journal_entries(self, entity: str, cursor: int) -> dict:
        with self.lock:
            if entity not in C.ENTITIES:
                raise NotFound("no such entity")
            es = [e for e in self.entries if e["entity"] == entity]
            page = es[cursor:cursor + 500]
            nxt = cursor + len(page)
            return {"entity": entity, "entries": page, "next_cursor": nxt if nxt < len(es) else None}

    def trial_balance(self, entity: str, as_of: Optional[str]) -> dict:
        with self.lock:
            if entity not in C.ENTITIES:
                raise NotFound("no such entity")
            b: dict = {}
            for e in self.entries:
                if e["entity"] == entity and (as_of is None or e["effective_date"] <= as_of):
                    J.apply_balances(b, e)
            return {**J.trial_balance(b, entity), "as_of": as_of}
