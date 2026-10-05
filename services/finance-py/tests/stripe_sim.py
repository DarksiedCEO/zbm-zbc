"""A simulated Stripe API for the tests (api.stripe.com is not reachable from the build sandbox).

It answers the calls ``stripe_incoming.StripeIncoming`` makes, in the shapes documented at docs.stripe.com (read Oct 5
2026): form-encoded POST with an Idempotency-Key, Bearer auth, a pinned Stripe-Version; Checkout Sessions, Payment
Intents with ``latest_charge`` expanded (balance and failure balance transactions inside), Disputes with their
balance transactions, Payouts and the Balance. It also signs webhook events with the endpoint secret exactly as
Stripe's v1 scheme does, and records every request so tests can check what Finance sent.

This is a model of Stripe, not Stripe: a run with Stripe TEST keys remains a go-live precondition (ADR 0009).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional
from urllib.parse import parse_qsl, urlparse

import httpx

from stripe_incoming import API_VERSION

TEST_KEY = "sk_test_51FinanceSimulatedKey000000000000"
TEST_WHSEC = "whsec_FinanceSimulatedWebhookSecret0000"


class SimStripe:
    def __init__(self, clock, key: str = TEST_KEY, whsec: str = TEST_WHSEC, livemode: bool = False):
        self.clock, self.key, self.whsec, self.livemode = clock, key, whsec, livemode
        self._n = 0                         # a plain counter (the live run pickles the simulator)
        self.sessions: dict = {}
        self.pis: dict = {}
        self.charges: dict = {}
        self.txns: dict = {}
        self.disputes: dict = {}
        self.payouts: dict = {}
        self.idem: dict = {}
        self.requests: list = []
        self.fail_next: list = []          # status codes to answer the next requests with
        self.extra_balance = 0             # cents moved by things Finance does not see (e.g. a monthly fee)

    def next_n(self) -> int:
        self._n += 1
        return self._n

    # --- HTTP ---------------------------------------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _json(self, status: int, doc) -> httpx.Response:
        return httpx.Response(status, json=doc)

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode())) if request.content else {}
        url = urlparse(str(request.url))
        query = parse_qsl(url.query)
        self.requests.append({"method": request.method, "path": url.path, "headers": dict(request.headers),
                              "form": form, "query": query})
        if self.fail_next:
            code = self.fail_next.pop(0)
            return self._json(code, {"error": {"type": "api_error", "code": "simulated"}})
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return self._json(401, {"error": {"type": "invalid_request_error", "code": "api_key_invalid"}})
        if request.headers.get("stripe-version") != API_VERSION:
            return self._json(400, {"error": {"type": "invalid_request_error", "code": "version"}})
        p = url.path
        if request.method == "POST" and p == "/v1/checkout/sessions":
            return self._create_session(request, form)
        if request.method == "GET":
            parts = p.strip("/").split("/")
            if p == "/v1/balance":
                return self._json(200, self.balance_doc())
            if len(parts) == 4 and parts[1] == "checkout" and parts[2] == "sessions":
                s = self.sessions.get(parts[3])
                return self._json(200, s) if s else self._json(404, {"error": {"code": "resource_missing"}})
            if len(parts) == 3 and parts[1] == "payment_intents":
                return self._pi_doc(parts[2], [v for k, v in query if k == "expand[]"])
            if len(parts) == 3 and parts[1] == "disputes":
                d = self.disputes.get(parts[2])
                if not d:
                    return self._json(404, {"error": {"code": "resource_missing"}})
                return self._json(200, {**d, "balance_transactions": [self.txns[t] for t in d["_txns"]]})
            if len(parts) == 3 and parts[1] == "payouts":
                po = self.payouts.get(parts[2])
                return self._json(200, po) if po else self._json(404, {"error": {"code": "resource_missing"}})
        return self._json(404, {"error": {"code": "resource_missing"}})

    def _create_session(self, request: httpx.Request, form: dict) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        if key in self.idem:
            prev_form, resp = self.idem[key]
            if prev_form != form:
                return self._json(400, {"error": {"type": "idempotency_error"}})
            return self._json(200, resp)
        methods = [v for k, v in sorted(form.items()) if k.startswith("allowed_payment_method_types[")]
        sid = f"cs_test_{self.next_n():04d}abc"
        s = {"id": sid, "object": "checkout.session", "mode": form.get("mode"),
             "amount_total": int(form["line_items[0][price_data][unit_amount]"]), "currency": "usd",
             "client_reference_id": form.get("client_reference_id"), "expires_at": int(form["expires_at"]),
             "livemode": self.livemode, "status": "open", "payment_status": "unpaid", "payment_intent": None,
             "url": f"https://checkout.stripe.com/c/pay/{sid}#fidkdWxOYHwnPyd1blpxYHZxWjA0",
             "allowed_payment_method_types": methods,
             "metadata": {k[9:-1]: v for k, v in form.items() if k.startswith("metadata[")},
             "_pi_metadata": {k[len("payment_intent_data[metadata]["):-1]: v for k, v in form.items()
                              if k.startswith("payment_intent_data[metadata][")}}
        self.sessions[sid] = s
        self.idem[key] = (form, {k: v for k, v in s.items() if not k.startswith("_")})
        return self._json(200, {k: v for k, v in s.items() if not k.startswith("_")})

    def _pi_doc(self, pi_id: str, expand: list) -> httpx.Response:
        pi = self.pis.get(pi_id)
        if not pi:
            return self._json(404, {"error": {"code": "resource_missing"}})
        doc = dict(pi)
        ch = self.charges.get(pi["latest_charge"]) if pi.get("latest_charge") else None
        if ch is not None and "latest_charge.balance_transaction" in expand:
            ch = dict(ch)
            if ch.get("balance_transaction"):
                ch["balance_transaction"] = self.txns[ch["balance_transaction"]]
            if ch.get("failure_balance_transaction") and "latest_charge.failure_balance_transaction" in expand:
                ch["failure_balance_transaction"] = self.txns[ch["failure_balance_transaction"]]
            doc["latest_charge"] = ch
        return self._json(200, doc)

    # --- the account's state ------------------------------------------------------------------------------------------
    def _txn(self, amount: int, fee: int, source: str, typ: str) -> str:
        tid = f"txn_{self.next_n():04d}sim"
        self.txns[tid] = {"id": tid, "object": "balance_transaction", "amount": amount, "fee": fee,
                          "net": amount - fee, "currency": "usd", "source": source, "type": typ,
                          "status": "pending", "created": int(self.clock.now().timestamp())}
        return tid

    def balance_cents(self) -> int:
        out = sum(t["net"] for t in self.txns.values()) + self.extra_balance
        out -= sum(po["amount"] for po in self.payouts.values() if po["status"] not in ("failed", "canceled"))
        return out

    def balance_doc(self) -> dict:
        return {"object": "balance", "livemode": self.livemode,
                "available": [{"amount": 0, "currency": "usd"}],
                "pending": [{"amount": self.balance_cents(), "currency": "usd"}]}

    # --- what clients and banks do --------------------------------------------------------------------------------------
    def pay(self, session_id: str, method: str = "us_bank_account", fee: Optional[int] = None,
            amount: Optional[int] = None, settle: bool = True, metadata: Optional[dict] = None) -> str:
        """The client pays the hosted page. ACH starts ``processing``; ``settle`` makes it succeed."""
        s = self.sessions[session_id]
        pi_id = f"pi_{self.next_n():04d}sim"
        amt = s["amount_total"] if amount is None else amount
        self.pis[pi_id] = {"id": pi_id, "object": "payment_intent", "amount": amt, "amount_received": 0,
                           "currency": "usd", "status": "processing", "livemode": self.livemode,
                           "metadata": dict(s["_pi_metadata"] if metadata is None else metadata),
                           "latest_charge": None, "payment_method_types": [method]}
        ch_id = f"ch_{self.next_n():04d}sim"
        self.charges[ch_id] = {"id": ch_id, "object": "charge", "amount": amt, "currency": "usd",
                               "status": "pending", "payment_intent": pi_id, "balance_transaction": None,
                               "failure_balance_transaction": None, "livemode": self.livemode,
                               "payment_method_details": {"type": method}}
        self.pis[pi_id]["latest_charge"] = ch_id
        s.update(status="complete", payment_intent=pi_id)
        if settle:
            self.succeed(pi_id, fee)
        return pi_id

    def succeed(self, pi_id: str, fee: Optional[int] = None) -> None:
        pi = self.pis[pi_id]
        ch = self.charges[pi["latest_charge"]]
        if fee is None:
            # Stripe's published US prices: ACH Direct Debit 0.8% capped at $5; cards 2.9% + 30c (integer cents)
            fee = min(500, pi["amount"] * 8 // 1000) if ch["payment_method_details"]["type"] == "us_bank_account" \
                else pi["amount"] * 29 // 1000 + 30
        ch.update(status="succeeded", balance_transaction=self._txn(pi["amount"], fee, ch["id"], "charge"))
        pi.update(status="succeeded", amount_received=pi["amount"])
        self.sessions_for(pi_id, payment_status="paid")

    def sessions_for(self, pi_id: str, **upd) -> None:
        for s in self.sessions.values():
            if s.get("payment_intent") == pi_id:
                s.update(**upd)

    def fail_after_success(self, pi_id: str, fee_back: int = 0) -> None:
        pi = self.pis[pi_id]
        ch = self.charges[pi["latest_charge"]]
        ch.update(status="failed", failure_balance_transaction=self._txn(-pi["amount"], -fee_back, ch["id"],
                                                                          "payment_failure_refund"))
        pi.update(status="requires_payment_method")

    def dispute(self, pi_id: str, fee: int = 1500, amount: Optional[int] = None) -> str:
        pi = self.pis[pi_id]
        amt = pi["amount"] if amount is None else amount
        du = f"du_{self.next_n():04d}sim"
        self.disputes[du] = {"id": du, "object": "dispute", "amount": amt, "currency": "usd", "status": "needs_response",
                             "payment_intent": pi_id, "charge": pi["latest_charge"], "livemode": self.livemode,
                             "_txns": [self._txn(-amt, fee, du, "adjustment")]}
        return du

    def close_dispute(self, du: str, won: bool, fee_back: int = 0) -> None:
        d = self.disputes[du]
        if won:
            d["_txns"].append(self._txn(d["amount"], -fee_back, du, "adjustment"))
        d["status"] = "won" if won else "lost"

    def payout(self, amount: int, status: str = "pending") -> str:
        po = f"po_{self.next_n():04d}sim"
        self.payouts[po] = {"id": po, "object": "payout", "amount": amount, "currency": "usd", "status": status,
                            "livemode": self.livemode}
        return po

    # --- webhooks --------------------------------------------------------------------------------------------------------
    def event(self, typ: str, obj: dict, livemode: Optional[bool] = None, ts: Optional[int] = None,
              secret: Optional[str] = None) -> tuple[str, str]:
        """(raw body, Stripe-Signature header) for an event, signed like Stripe does."""
        ev = {"id": f"evt_{self.next_n():04d}sim", "object": "event", "type": typ, "api_version": API_VERSION,
              "livemode": self.livemode if livemode is None else livemode, "created": int(self.clock.now().timestamp()),
              "data": {"object": obj}}
        body = json.dumps(ev)
        return body, self.sign(body, ts, secret)

    def sign(self, body: str, ts: Optional[int] = None, secret: Optional[str] = None) -> str:
        t = int(self.clock.now().timestamp()) if ts is None else ts
        sig = hmac.new((secret or self.whsec).encode(), f"{t}.".encode() + body.encode(), hashlib.sha256).hexdigest()
        return f"t={t},v1={sig}"
