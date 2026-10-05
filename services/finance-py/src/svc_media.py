"""
Media billing (ADR 0009 amendment, Oct 5 2026; founder decisions M1-M8, docs/golive/MEDIA_BILLING_SPEC.md).

Z Best Media buys TV, radio, out-of-home and other media for clients as PRINCIPAL (M1): the client pays ZBM, ZBM pays
the vendor. Every buy is prepaid (M2) and the vendor is paid only from money that has cleared (M3). The fee is a
markup on the vendor cost, 15% by default and set per buy (M4); the client's invoice shows the cost and the fee, or
one blended price (M5), while the books always keep both. Money comes in by ACH or wire only (M6/M9: a card can be
disputed after the vendor is paid). Andre pays vendors himself; Finance records it (M7).

Flows (ZBM only; posted nowhere else, i01 refuses the media accounts to every other flow):

  F12   invoice issued      Dr 1100 Accounts receivable[client]        Cr 2120 Media prepayments[buy]   total
  F11a  prepayment cleared  Dr 1010 Cash - Operating                   Cr 1100 Accounts receivable[client]
  F12v  vendor paid         Dr 1150 Prepaid media[buy]                 Cr 1010 Cash - Operating         vendor cost
  F12r  media delivered     Dr 2120 Media prepayments[buy]  total      Cr 4120 Media revenue            total
                            Dr 5110 Media cost              cost       Cr 1150 Prepaid media[buy]       cost
  F12c  cancelled unpaid    Dr 2120 Media prepayments[buy]             Cr 1100 Accounts receivable[client]
  F12x  prepayment returned Dr 1100 Accounts receivable[client]        Cr 1010 Cash - Operating   (a bank fact)

Revenue and cost are recognised together when the media has run (F12r, like ADR 0009 choice 4): a buy can never
show revenue without its cost. WHEN revenue is recognised for a flight that spans months, and whether California
sales tax applies, are the tax questions of counsel row FIN-CQ-11; no invoice issues until Andre verifies it (M8).

Client receipts: every matched payment (ZBC and ZBM) produces a receipt that says what the client bought; the
scheduler sends it through the client-mail port (stand-in: nothing is sent, the receipt stays ``pending_send``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import money as M
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, NotFound
from intelligences import i01_journal as J
from intelligences import i02_receivables as I2
from intelligences import i06_tax as I6
from ledger import derived_id
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, rid, sha

MEDIA_LABELS = {"broadcast_tv": "Broadcast TV", "cable_tv": "Cable TV", "streaming_tv": "Streaming TV",
                "radio": "Radio", "streaming_audio": "Streaming audio", "podcast": "Podcast",
                "out_of_home": "Out-of-home", "digital": "Digital media", "print": "Print", "other": "Media"}
LINE_LABELS = {"campaign_deposit": "Campaign deposit", "creative_services": "Creative services",
               "strategy_services": "Strategy services", "production_services": "Production services",
               "retainer_fee": "Retainer", "subscription_fee": "Subscription",
               "revenue_recovery_services": "Revenue Recovery services", "media_spend": "Media",
               "media_fee": "Media placement and management"}
OPEN_FOR_VENDOR = ("prepaid", "vendor_partially_paid")


def _d(v) -> str:
    return v.isoformat() if isinstance(v, date) else str(v)


class MediaMixin:
    # ================================================================== media buys

    def create_media_buy(self, request_id: str, body: dict) -> dict:
        """Andre records a buy (M1, M4, M5); Finance computes the fee and drafts the prepayment invoice from it.
        The invoice is issued only through Andre's normal invoice decision (content hash, Legal, FIN-CQ-11)."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "media-buys", {k: _d(v) for k, v in body.items()})
            if ent:
                return ent["response"]
            self.require_rules()
            problems = I2.text_problems({"description": body["description"], "vendor_ref": body["vendor_ref"]})
            if body["flight_end"] < body["flight_start"]:
                problems.append(R.item("JOURNAL_INVALID", "flight_end is before flight_start"))
            if body["flight_start"] < self._today_la():
                problems.append(R.item("COLLECT_BEFORE_PAY", "the flight starts before today: media is prepaid, so a "
                                                             "buy is recorded before it runs (founder M2)"))
            pct = body.get("markup_pct")
            pct = self.cfg.media_default_markup_pct if pct is None else M.D(pct)
            if pct < 0 or pct > self.cfg.media_max_markup_pct:
                problems.append(R.item("AMOUNT_OUT_OF_RANGE", f"markup must be 0.00..{M.fmt(self.cfg.media_max_markup_pct)} "
                                                              "percent (FIN_MEDIA_MAX_MARKUP_PCT)"))
            if problems:
                raise InvalidReasons("media buy refused", R.dedupe(problems))
            cost = M.D(body["media_cost"])
            fee = I2.media_fee(cost, pct)
            total = M.total([cost, fee])
            now = self._now()
            buy_id = rid("mb", "andre", request_id)
            inv_id = rid("inv", "andre", "media", request_id)
            buy = {"buy_id": buy_id, "entity": "zbm", "client_id": body["client_id"], "media_type": body["media_type"],
                   "vendor_ref": body["vendor_ref"], "description": body["description"],
                   "flight_start": _d(body["flight_start"]), "flight_end": _d(body["flight_end"]),
                   "media_cost": M.fmt(cost), "markup_pct": M.fmt(pct), "fee": M.fmt(fee), "total": M.fmt(total),
                   "display": body["display"], "invoice_id": inv_id, "legal_ref": dict(body["legal_ref"]),
                   "status": "awaiting_payment", "prepayment": None, "vendor_paid": "0.00", "vendor_payments": [],
                   "delivered": None, "created_by": "andre", "created_at": iso(now)}
            inv = {"invoice_id": inv_id, "entity": "zbm", "client_id": body["client_id"], "campaign_id": None,
                   "kind": I2.MEDIA_KIND, "media_buy_id": buy_id,
                   "lines": I2.media_lines(buy), "client_lines": I2.client_view(buy), "display": body["display"],
                   "total": M.fmt(total), "currency": "USD", "payment_methods": list(I2.PAYMENT_METHODS),
                   "due_days": body["due_days"], "legal_ref": dict(body["legal_ref"]), "recurring": None,
                   "notes_sha256": None, "template_vars": None, "drafted_by": "andre", "drafted_at": iso(now),
                   "status": "draft", "tax_treatment": {"status": "verified" if self.counsel_verified("FIN-CQ-11")
                                                        else "unverified", "basis_row": "FIN-CQ-11"}}
            inv["content_sha256"] = sha({k: v for k, v in inv.items() if k not in ("status", "tax_treatment")})
            op = Op(self, f"mb|{buy_id}", "andre", buy_id)
            op.record(derived_id("mbc", buy_id), "media_buy_created", "andre", buy_id,
                      {"buy_id": buy_id, "client_id": buy["client_id"], "media_type": buy["media_type"],
                       "media_cost": buy["media_cost"], "markup_pct": buy["markup_pct"], "fee": buy["fee"],
                       "total": buy["total"], "display": buy["display"]},
                      f"Media buy recorded: {buy['total']} ({buy['media_type']}, markup {buy['markup_pct']}%)")
            op.record(derived_id("invd", inv_id), "invoice_drafted", I2.ACTOR, inv_id,
                      {"invoice_id": inv_id, "entity": "zbm", "kind": inv["kind"], "total": inv["total"],
                       "content_sha256": inv["content_sha256"]}, f"Invoice drafted: {inv['total']} ({inv['kind']})")
            self._injection(op, {"description": body["description"], "vendor_ref": body["vendor_ref"]})
            op.put("media_buys", buy_id, buy)
            op.put("invoices", inv_id, inv)
            resp = self._idem_add(op, key, h, {"media_buy": buy, "invoice": inv, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def get_media_buy(self, buy_id: str) -> dict:
        with self.lock:
            buy = self.db["media_buys"].get(buy_id)
            if buy is None:
                raise NotFound("no such media buy")
            return dict(buy)

    def _media_clears_on(self, buy: dict) -> Optional[date]:
        """The first day a vendor payment may be recorded (M3): the prepayment's value date plus the hold."""
        pre = buy.get("prepayment")
        if not pre:
            return None
        return I6.add_business_days(date.fromisoformat(pre["value_date"]), self.cfg.media_release_hold_bd)

    def record_vendor_payment(self, request_id: str, buy_id: str, body: dict) -> dict:
        """Andre records a payment he made to the vendor (M7). Refused unless the client's prepayment for THIS buy
        has cleared and the hold has passed (M3), and never above the vendor cost (M1)."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"media-buys/{buy_id}/vendor-payments",
                                     {k: _d(v) for k, v in body.items()})
            if ent:
                return ent["response"]
            self.require_rules()
            buy = self.db["media_buys"].get(buy_id)
            if buy is None:
                raise NotFound("no such media buy")
            if buy["status"] not in OPEN_FOR_VENDOR:
                raise Refused("the vendor cannot be paid on this buy", [R.item(
                    "COLLECT_BEFORE_PAY", f"media buy is {buy['status']}: the vendor is paid only from a cleared "
                                          "prepayment (founder M3)")])
            today = self._today_la()
            clears = self._media_clears_on(buy)
            reasons = []
            if buy.get("payment_disputed"):
                reasons.append(R.item("COLLECT_BEFORE_PAY", "the client disputed the prepayment at Stripe; the vendor "
                                                            "is not paid while that dispute is open (founder M3)"))
            if clears is None or today < clears:
                reasons.append(R.item("COLLECT_BEFORE_PAY", f"the client's prepayment clears on {_d(clears)} "
                                                            f"({self.cfg.media_release_hold_bd} business days after it "
                                                            "landed); record the vendor payment after that"))
            if body["paid_on"] > today:
                reasons.append(R.item("JOURNAL_INVALID", "paid_on is in the future"))
            elif clears is not None and body["paid_on"] < clears:
                reasons.append(R.item("COLLECT_BEFORE_PAY", f"paid_on {_d(body['paid_on'])} is before the client's money "
                                                            f"cleared ({_d(clears)}): the vendor is paid only from "
                                                            "collected money (founder M3)"))
            amt = M.D(body["amount"])
            left = M.q(M.D(buy["media_cost"]) - M.D(buy["vendor_paid"]))
            if amt > left:
                reasons.append(R.item("AMOUNT_OUT_OF_RANGE", f"{M.fmt(amt)} is more than the {M.fmt(left)} still owed to "
                                                             "the vendor on this buy"))
            if any(p["payment_ref_sha256"] == body["payment_ref_sha256"] for p in buy["vendor_payments"]):
                raise Conflict("this vendor payment reference is already recorded on this buy")
            if reasons:
                raise Refused("vendor payment refused", R.dedupe(reasons))
            op = Op(self, f"mbv|{request_id}", "andre", buy_id)
            n = len(buy["vendor_payments"]) + 1
            try:
                e = self._post(op, "zbm", [J.dr("1150", amt, f"buy:{buy_id}"), J.cr("1010", amt)], "F12v",
                               {"kind": "media_buy", "id": buy_id}, f"F12v|{buy_id}|{body['payment_ref_sha256']}",
                               approval_ref=request_id, effective_date=body["paid_on"])
            except PostingRefused as exc:
                raise InvalidReasons("vendor payment posting refused", exc.reasons) from None
            paid = M.total([M.D(buy["vendor_paid"]), amt])
            pay = {"n": n, "amount": M.fmt(amt), "paid_on": _d(body["paid_on"]), "method": body["method"],
                   "payment_ref_sha256": body["payment_ref_sha256"], "entry_id": e["entry_id"],
                   "recorded_at": iso(self._now())}
            new = {**buy, "vendor_paid": M.fmt(paid), "vendor_payments": [*buy["vendor_payments"], pay],
                   "status": "vendor_paid" if paid == M.D(buy["media_cost"]) else "vendor_partially_paid"}
            op.record(derived_id("mbv", buy_id, body["payment_ref_sha256"]), "media_vendor_payment_recorded", "andre",
                      buy_id, {"buy_id": buy_id, "amount": M.fmt(amt), "vendor_paid": M.fmt(paid),
                               "entry_id": e["entry_id"]},
                      f"Vendor payment recorded: {M.fmt(amt)} (paid {M.fmt(paid)} of {buy['media_cost']})")
            op.put("media_buys", buy_id, new)
            resp = self._idem_add(op, key, h, {"media_buy": new, "entry_id": e["entry_id"],
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def record_media_delivery(self, request_id: str, buy_id: str, body: dict) -> dict:
        """Andre confirms the media ran (proof of performance referenced). Revenue and cost are recognised together
        (F12r). Refused until the vendor is paid in full and the flight has ended."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"media-buys/{buy_id}/delivery",
                                     {k: _d(v) if not isinstance(v, list) else v for k, v in body.items()})
            if ent:
                return ent["response"]
            self.require_rules()
            buy = self.db["media_buys"].get(buy_id)
            if buy is None:
                raise NotFound("no such media buy")
            reasons = []
            if buy["status"] != "vendor_paid":
                reasons.append(R.item("COLLECT_BEFORE_PAY", f"media buy is {buy['status']}: delivery is recorded once the "
                                                            "client has paid and the vendor is paid in full"))
            if body["delivered_on"] > self._today_la():
                reasons.append(R.item("JOURNAL_INVALID", "delivered_on is in the future"))
            floor = max([(buy.get("prepayment") or {}).get("value_date") or ""] +
                        [p["paid_on"] for p in buy["vendor_payments"]])
            if floor and _d(body["delivered_on"]) < floor:
                reasons.append(R.item("JOURNAL_INVALID", f"delivered_on is before the prepayment or a vendor payment "
                                                         f"({floor}): delivery is dated after the money moved"))
            if _d(body["delivered_on"]) < buy["flight_end"]:
                reasons.append(R.item("JOURNAL_INVALID", f"the flight runs until {buy['flight_end']}: revenue is "
                                                         "recognised once it has run (FIN-CQ-11 decides any other "
                                                         "timing)"))
            if reasons:
                raise Refused("media delivery refused", R.dedupe(reasons))
            total, cost = M.D(buy["total"]), M.D(buy["media_cost"])
            op = Op(self, f"mbd|{request_id}", "andre", buy_id)
            try:
                e = self._post(op, "zbm", [J.dr("2120", total, f"buy:{buy_id}"), J.cr("4120", total),
                                           J.dr("5110", cost), J.cr("1150", cost, f"buy:{buy_id}")], "F12r",
                               {"kind": "media_buy", "id": buy_id}, f"F12r|{buy_id}", approval_ref=request_id,
                               effective_date=body["delivered_on"])
            except PostingRefused as exc:
                raise InvalidReasons("delivery posting refused", exc.reasons) from None
            new = {**buy, "status": "delivered", "delivered": {"on": _d(body["delivered_on"]),
                                                               "evidence_refs": list(body["evidence_refs"]),
                                                               "entry_id": e["entry_id"], "at": iso(self._now())}}
            op.record(derived_id("mbd", buy_id), "media_buy_delivered", "andre", buy_id,
                      {"buy_id": buy_id, "revenue": M.fmt(total), "cost": M.fmt(cost), "entry_id": e["entry_id"],
                       "evidence_sha256": sha(list(body["evidence_refs"]))},
                      f"Media delivered: revenue {M.fmt(total)}, cost {M.fmt(cost)}")
            op.put("media_buys", buy_id, new)
            resp = self._idem_add(op, key, h, {"media_buy": new, "entry_id": e["entry_id"],
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def cancel_media_buy(self, request_id: str, buy_id: str) -> dict:
        """Andre cancels a buy the client has NOT paid: the invoice is voided (an issued one is reversed, F12c).
        A paid buy is refused: refunding a media prepayment is not built."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"media-buys/{buy_id}/cancel", None)
            if ent:
                return ent["response"]
            self.require_rules()
            buy = self.db["media_buys"].get(buy_id)
            if buy is None:
                raise NotFound("no such media buy")
            if buy["status"] != "awaiting_payment":
                raise Conflict(f"media buy is {buy['status']}: only an unpaid buy can be cancelled (refunding a media "
                               "prepayment is not built)")
            inv = self.db["invoices"][buy["invoice_id"]]
            op = Op(self, f"mbx|{request_id}", "andre", buy_id)
            entry_id = None
            if inv["status"] == "issued":
                amt = M.D(inv["total"])
                try:
                    e = self._post(op, "zbm", [J.dr("2120", amt, f"buy:{buy_id}"),
                                               J.cr("1100", amt, f"client:{inv['client_id']}")], "F12c",
                                   {"kind": "media_buy", "id": buy_id}, f"F12c|{buy_id}", approval_ref=request_id)
                except PostingRefused as exc:
                    raise InvalidReasons("cancellation posting refused", exc.reasons) from None
                entry_id = e["entry_id"]
            op.put("invoices", inv["invoice_id"], {**inv, "status": "void", "decided_at": iso(self._now())})
            new = {**buy, "status": "cancelled", "cancelled_at": iso(self._now())}
            op.record(derived_id("mbx", buy_id), "media_buy_cancelled", "andre", buy_id,
                      {"buy_id": buy_id, "invoice_id": inv["invoice_id"], "entry_id": entry_id},
                      "Media buy cancelled before payment")
            op.put("media_buys", buy_id, new)
            resp = self._idem_add(op, key, h, {"media_buy": new, "entry_id": entry_id, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    # --- hooks called from the receivables code (svc_books) -----------------------------------------------------------

    def _media_prepaid(self, op: Op, inv: dict, receipt_id: str, value_date: str, amount: str) -> None:
        """The client's prepayment matched. A first payment makes the buy ``prepaid``. A payment AFTER a bank
        return (AEGIS H1) restores the buy to where its vendor payments left it, with the hold restarting from the
        new money; the ``media_exposure`` break stays for Andre, who resolves it citing this receipt's entry."""
        buy = op.get("media_buys", inv["media_buy_id"])
        if buy is None or not (buy["status"] in ("awaiting_payment", "payment_returned") or buy.get("payment_returned")):
            return
        pre = {"receipt_id": receipt_id, "value_date": value_date, "amount": amount, "matched_at": iso(self._now())}
        if buy["status"] == "delivered":
            status = "delivered"
        elif buy["status"] == "awaiting_payment" or M.D(buy["vendor_paid"]) == 0:
            status = "prepaid"
        else:
            status = "vendor_paid" if M.D(buy["vendor_paid"]) == M.D(buy["media_cost"]) else "vendor_partially_paid"
        op.put("media_buys", buy["buy_id"], {**buy, "status": status, "prepayment": pre, "payment_returned": False})

    def _media_return(self, op: Op, rc: dict, inv: dict, body: dict) -> dict:
        """The bank returned a ZBM media prepayment (F12x, a FACT flow). The client owes again. If the vendor was
        already paid from that money, ZBM is exposed: the buy is marked and a break opens for Andre."""
        amt = M.D(rc["amount"])
        try:
            e = self._post(op, "zbm", [J.dr("1100", amt, f"client:{inv['client_id']}"), J.cr("1010", amt)], "F12x",
                           {"kind": "receipt_return", "id": rc["receipt_id"]}, f"F12x|{rc['receipt_id']}",
                           actor=I2.ACTOR, fact=True)
        except PostingRefused as exc:
            raise InvalidReasons("return posting refused", exc.reasons) from None
        op.put("receipts", rc["receipt_id"], {**rc, "status": "returned", "returned": {
            "return_code": body["return_code"], "value_date": _d(body["value_date"]),
            "return_ref_sha256": body["return_ref_sha256"], "entry_id": e["entry_id"]}})
        op.put("invoices", inv["invoice_id"], {**inv, "status": "issued", "paid_at": None})
        self._withdraw_client_receipt(op, rc["receipt_id"])
        exposed = self._media_unpaid(op, inv, rc["receipt_id"], "the bank returned")
        op.record(derived_id("rctx", rc["receipt_id"]), "deposit_returned", I2.ACTOR, rc["receipt_id"],
                  {"receipt_id": rc["receipt_id"], "amount": M.fmt(amt), "entity": "zbm", "entry_id": e["entry_id"],
                   "vendor_exposure": M.fmt(exposed)},
                  f"Media prepayment returned by the bank: {M.fmt(amt)}")
        return {"receipt_id": rc["receipt_id"], "entry_id": e["entry_id"], "vendor_exposure": M.fmt(exposed)}

    def _media_unpaid(self, op: Op, inv: dict, receipt_id: str, cause: str) -> Decimal:
        """The client's prepayment for this buy is gone (a bank return, a Stripe payment that failed after it had
        succeeded, a lost Stripe dispute). Returns the vendor money already out: if any, ZBM is exposed, the buy is
        marked and a ``media_exposure`` break opens for Andre; if none, the buy waits for payment again."""
        buy = op.get("media_buys", inv["media_buy_id"])
        exposed = M.D(buy["vendor_paid"]) if buy else M.ZERO
        if buy is not None:
            if exposed > 0:
                # a delivered buy keeps its status (its books are final); the exposure is flagged and a break opens
                op.put("media_buys", buy["buy_id"], {**buy, "payment_returned": True, "payment_disputed": False}
                       if buy["status"] == "delivered"
                       else {**buy, "status": "payment_returned", "payment_returned": True, "payment_disputed": False})
                bid = rid("brk", "media_exposure", buy["buy_id"], receipt_id)
                op.put("breaks", bid, {"break_id": bid, "leg": "media_exposure", "subject": f"zbm:buy:{buy['buy_id']}",
                                       "difference": M.fmt(exposed), "opened_at": iso(self._now()),
                                       "opened_on": self._today_la().isoformat(), "owner": "andre",
                                       "explanation_code": "unknown", "status": "open", "receipt_id": receipt_id,
                                       "resolution": None})
                op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                          {"break_id": bid, "leg": "media_exposure", "difference": M.fmt(exposed)},
                          f"Break opened: vendor paid {M.fmt(exposed)} from a prepayment {cause}"[:200])
            else:
                op.put("media_buys", buy["buy_id"], {**buy, "status": "awaiting_payment", "prepayment": None,
                                                     "payment_disputed": False})
        return exposed

    def _expected_media_sub(self) -> dict[str, dict[str, Decimal]]:
        """Reconciliation L4 for ZBM media: what 2120 and 1150 should hold per buy, from the buy records alone."""
        out: dict[str, dict[str, Decimal]] = {"2120": {}, "1150": {}}
        for b in self.db["media_buys"].values():
            inv = self.db["invoices"].get(b["invoice_id"]) or {}
            if b["status"] not in ("delivered", "cancelled") and inv.get("status") in ("issued", "paid"):
                out["2120"][f"buy:{b['buy_id']}"] = M.D(b["total"])
            if b["status"] != "delivered" and M.D(b["vendor_paid"]) > 0:
                out["1150"][f"buy:{b['buy_id']}"] = M.D(b["vendor_paid"])
        return out

    # ================================================================== client receipts

    def _client_receipt(self, op: Op, receipt_id: str, inv: dict, amount: str, value_date: str,
                        method: str = "ACH or wire") -> dict:
        """What the client bought, in words, for the email receipt (founder, Oct 5 2026)."""
        crid = rid("crc", receipt_id)
        if inv.get("kind") == I2.MEDIA_KIND:
            buy = op.get("media_buys", inv["media_buy_id"]) or {}
            items = [dict(x) for x in inv.get("client_lines") or []]
            what = f"{MEDIA_LABELS.get(buy.get('media_type'), 'Media')}: {buy.get('description', '')}".strip()
            period = {"flight_start": buy.get("flight_start"), "flight_end": buy.get("flight_end")}
        else:
            items = [{"description": l.get("description") or LINE_LABELS.get(l["line_code"], l["line_code"]),
                      "amount": l["amount"]} for l in inv["lines"]]
            what = "; ".join(x["description"] for x in items)[:400]
            period = None
        rec = {"client_receipt_id": crid, "receipt_id": receipt_id, "entity": inv["entity"],
               "client_id": inv["client_id"], "invoice_id": inv["invoice_id"], "invoice_kind": inv["kind"],
               "what_you_bought": what, "items": items, "period": period, "amount_paid": amount, "currency": "USD",
               "paid_on": value_date, "method": method, "status": "pending_send", "attempts": 0,
               "created_at": iso(self._now()), "sent_at": None}
        op.put("client_receipts", crid, rec)
        op.record(derived_id("crc", crid), "client_receipt_created", I2.ACTOR, crid,
                  {"client_receipt_id": crid, "receipt_id": receipt_id, "invoice_id": inv["invoice_id"],
                   "amount": amount, "entity": inv["entity"]}, f"Client receipt prepared: {amount}")
        return rec

    def _withdraw_client_receipt(self, op: Op, receipt_id: str) -> None:
        """The bank returned the payment (AEGIS M1): its receipt is never sent; one already sent is marked so."""
        for crid, rec in self.db["client_receipts"].items():
            if rec["receipt_id"] != receipt_id or rec["status"] in ("withdrawn", "withdrawn_after_send"):
                continue
            status = "withdrawn_after_send" if rec["status"] == "sent" else "withdrawn"
            op.put("client_receipts", crid, {**rec, "status": status, "withdrawn_at": iso(self._now())})
            op.record(derived_id("crcw", crid), "client_receipt_withdrawn", I2.ACTOR, crid,
                      {"client_receipt_id": crid, "receipt_id": receipt_id, "status": status},
                      "Client receipt withdrawn: the bank returned the payment")

    def get_client_receipt(self, crid: str) -> dict:
        with self.lock:
            rec = self.db["client_receipts"].get(crid)
            if rec is None:
                raise NotFound("no such client receipt")
            return dict(rec)

    SEND_CLAIM_STALE_MIN = 15
    SEND_BACKOFF_MIN = 15

    def _send_refusal(self, rec: dict) -> Optional[str]:
        """Why this receipt must not be sent now (None = send it)."""
        if rec["status"] == "sent":
            return "already sent"
        if rec["status"] in ("withdrawn", "withdrawn_after_send"):
            return "withdrawn: the bank returned this payment"
        rc = self.db["receipts"].get(rec["receipt_id"]) or {}
        if rc.get("status") != "matched":
            return "the payment is not a matched receipt any more"
        now = self._now()
        if rec["status"] == "sending" and rec.get("claimed_at") and \
                (now - parse_iso(rec["claimed_at"])).total_seconds() < self.SEND_CLAIM_STALE_MIN * 60:
            return "a send is already in progress"
        last = rec.get("last_attempt_at")
        if rec["status"] == "pending_send" and last and \
                (now - parse_iso(last)).total_seconds() < self.SEND_BACKOFF_MIN * 60:
            return f"backoff: the last attempt failed less than {self.SEND_BACKOFF_MIN} minutes ago"
        return None

    def send_client_receipt(self, principal: str, request_id: str, crid: str) -> dict:
        """The scheduler asks the client-mail port to send one receipt. The port call happens outside the lock and
        is recorded first; a refusal or the stand-in leaves the receipt ``pending_send`` (nothing claims a send)."""
        key, h, ent = self._idem(principal, request_id, f"client-receipts/{crid}/send", None)
        if ent:
            return ent["response"]
        self.require_rules()
        with self.lock:
            rec = self.db["client_receipts"].get(crid)
            if rec is None:
                raise NotFound("no such client receipt")
            refusal = self._send_refusal(rec)
            if refusal:
                return {"client_receipt": dict(rec), "sent": False, "detail": refusal, "request_id": request_id}
            view = {k: rec[k] for k in ("client_receipt_id", "entity", "invoice_id", "what_you_bought", "items",
                                        "period", "amount_paid", "currency", "paid_on", "method")}
            # AEGIS M2: claim the send (recorded and committed) BEFORE the port call, so a concurrent sender sees
            # ``sending`` and never emails the client a second time
            claim = Op(self, f"crcc|{principal}|{request_id}", I2.ACTOR, crid)
            claim.record(derived_id("crcc", crid, request_id), "client_receipt_send_claimed", I2.ACTOR, crid,
                         {"client_receipt_id": crid, "attempt": rec["attempts"] + 1}, "Client receipt send started")
            claim.put("client_receipts", crid, {**rec, "status": "sending", "claimed_at": iso(self._now()),
                                                "claimed_by": request_id})
            self._commit(claim)
        mail = self.ports.client_mail
        g = Gather(self, f"crcs|{principal}|{request_id}", I2.ACTOR, crid)
        sent = g.call("client_mail", "send_receipt", (rec["client_id"], crid),
                      lambda: mail.send_receipt(rec["client_id"], view), False)
        with self.lock:
            rec = self.db["client_receipts"][crid]
            now = iso(self._now())
            withdrawn = rec["status"] in ("withdrawn", "withdrawn_after_send")
            mine = rec["status"] == "sending" and rec.get("claimed_by") == request_id
            if sent is True:
                status = "withdrawn_after_send" if withdrawn else "sent"
            elif mine:
                status = "pending_send"
            else:
                # AEGIS N1: a late failure never undoes another request's outcome (a stale claim taken over and
                # sent, or a withdrawal) -- it is recorded as an attempt only
                status = rec["status"]
            keep_claim = not mine and rec["status"] == "sending" and sent is not True
            new = {**rec, "attempts": rec["attempts"] + 1, "last_attempt_at": now, "status": status,
                   **({} if keep_claim else {"claimed_at": None, "claimed_by": None}),
                   **({"sent_at": now} if sent is True else {})}
            op = Op(self, f"crcs|{principal}|{request_id}", I2.ACTOR, crid, g)
            op.record(derived_id("crcs", crid, request_id), "client_receipt_sent" if sent is True
                      else "client_receipt_send_failed", I2.ACTOR, crid,
                      {"client_receipt_id": crid, "sent": sent is True, "attempt": new["attempts"]},
                      "Client receipt sent" if sent is True else "Client receipt not sent (mail unavailable)")
            op.put("client_receipts", crid, new)
            resp = self._idem_add(op, key, h, {"client_receipt": new, "sent": sent is True,
                                               "detail": "sent" if sent is True else
                                               "not sent: the client-mail adapter is not built or refused it",
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp
