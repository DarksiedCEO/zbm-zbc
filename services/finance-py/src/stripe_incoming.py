"""
Stripe incoming adapter: ZBM's own Stripe account, client payments only (founder M6/M9/M10; ADR 0009 amendment
"Stripe incoming", Oct 5 2026). It implements ``ports.StripeIncomingPort`` and nothing else: there is no refund,
transfer, payout-creating or account-changing call here.

Facts this relies on (docs.stripe.com, read Oct 5 2026):
- requests are form-encoded, ``Authorization: Bearer <secret key>``; the API version is pinned per request with the
  ``Stripe-Version`` header (``API_VERSION`` below), so a Dashboard default-version change cannot reshape answers;
- POSTs carry an ``Idempotency-Key``, so a retried create returns the first session instead of a second one;
- Checkout Session ``allowed_payment_method_types`` FILTERS the eligible methods (it can only narrow): a method not
  listed is never offered; ``expires_at`` is 30 minutes to 24 hours out; ``client_reference_id`` is at most 200 chars;
- webhook signatures: header ``Stripe-Signature: t=<unix>,v1=<hex>[,v1=...]``; the signed payload is
  ``"{t}.{raw body}"`` under HMAC-SHA256 with the endpoint's ``whsec_`` secret; only ``v1`` counts; several ``v1``
  values appear while a secret is being rolled; the default tolerance is 5 minutes;
- PaymentIntent ``amount_received`` / ``status`` / ``latest_charge`` (expandable); Charge ``balance_transaction`` and
  ``failure_balance_transaction`` (expandable) and ``payment_method_details.type``; Balance Transaction ``amount`` /
  ``fee`` / ``net`` in cents (``net = amount - fee``); Dispute ``balance_transactions`` (zero, one or two: funds
  withdrawn, funds reinstated) and ``status``; Payout ``status`` / ``amount``.

Every call has a hard wall-clock budget (the thin-client pattern of ``clients.py``), a 1 MiB answer cap, no
redirects, and maps anything unexpected to "unavailable" (never a pass). Nothing here was exercised against
api.stripe.com from the build sandbox (it is not reachable there): the tests drive this module through a simulated
Stripe transport, and a run with Stripe TEST keys is a go-live precondition (ADR 0009).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import urlencode

import httpx

from clients import MAX_RESPONSE_BYTES, _run_bounded
from ports import RailBalance, StripeCheckout, StripeDispute, StripePayment, StripePayout, StripeSession

API_BASE = "https://api.stripe.com"
API_VERSION = "2026-09-30.endive"
TIMEOUT_S = 20                                    # seconds, whole (no float literals outside the network modules)
SIGNATURE_TOLERANCE_S = 300
MAX_SIGNATURE_HEADER = 1024
_SID = re.compile(r"[a-z]{2,8}_[A-Za-z0-9_]{1,120}")
_FIN_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
CHECKOUT_METHODS = ("us_bank_account", "card")


# --- money ------------------------------------------------------------------------------------------------------------

def to_cents(amount: str) -> int:
    """'1234.50' -> 123450, exactly; anything that is not a canonical positive money string raises ValueError."""
    if not isinstance(amount, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,14})\.[0-9]{2}", amount):
        raise ValueError("not a canonical money string")
    d = Decimal(amount)
    return int(d * 100)


def from_cents(n: int) -> str:
    """Signed integer cents -> canonical money string ('-15.00')."""
    if type(n) is not int:
        raise ValueError("cents must be an int")
    sign = "-" if n < 0 else ""
    n = abs(n)
    return f"{sign}{n // 100}.{n % 100:02d}"


# --- webhook signature --------------------------------------------------------------------------------------------------

def verify_signature(payload: bytes, header: Optional[str], secret: str, now_epoch: int,
                     tolerance: int = SIGNATURE_TOLERANCE_S) -> bool:
    """Stripe's v1 scheme. False on anything malformed, stale (or too far in the future), or not matching."""
    if not isinstance(header, str) or not header or len(header) > MAX_SIGNATURE_HEADER or not secret:
        return False
    ts: Optional[int] = None
    sigs: list[str] = []
    for part in header.split(","):
        k, sep, v = part.strip().partition("=")
        if not sep:
            continue
        if k == "t":
            if not v.isdigit() or len(v) > 12 or ts is not None:
                return False
            ts = int(v)
        elif k == "v1" and re.fullmatch(r"[0-9a-f]{64}", v):
            sigs.append(v)
    if ts is None or not sigs or abs(now_epoch - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), str(ts).encode("ascii") + b"." + payload, hashlib.sha256).hexdigest()
    ok = False
    for s in sigs:                                   # constant-time per candidate; every candidate is compared
        ok = hmac.compare_digest(expected, s) or ok
    return ok


# --- form encoding (Stripe's nested bracket syntax) ---------------------------------------------------------------------

def form_pairs(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += form_pairs(v, key)
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out += form_pairs(item, f"{key}[{i}]")
                else:
                    out.append((f"{key}[{i}]", str(item)))
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        elif v is not None:
            out.append((key, str(v)))
    return out


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class StripeIncoming:
    """The real adapter. ``transport`` is an ``httpx`` transport (tests pass a simulated Stripe)."""

    def __init__(self, secret_key: str, webhook_secret: str, livemode: bool, success_url: str, cancel_url: str,
                 transport=None, budget_s: int = TIMEOUT_S, clock=None):
        self._key = secret_key
        self._whsec = webhook_secret
        self.livemode = livemode
        self.success_url, self.cancel_url = success_url, cancel_url
        self._transport = transport
        self._budget = budget_s
        self._clock = clock

    def __repr__(self) -> str:                        # never print a secret
        return f"StripeIncoming(livemode={self.livemode})"

    # --- HTTP ---------------------------------------------------------------------------------------------------------

    def _call(self, method: str, path: str, form: Optional[list] = None, query: Optional[list] = None,
              idem: Optional[str] = None):
        """('ok', status, json) | ('http', status, json-or-None) | ('error', kind, None)."""
        headers = {"Authorization": f"Bearer {self._key}", "Stripe-Version": API_VERSION,
                   "Accept-Encoding": "identity"}
        content = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            content = urlencode(form).encode("ascii")
        if idem:
            headers["Idempotency-Key"] = idem
        url = API_BASE + path + (("?" + urlencode(query)) if query else "")

        def run(cancel: threading.Event):
            try:
                with httpx.Client(timeout=httpx.Timeout(self._budget), transport=self._transport,
                                  follow_redirects=False) as c:
                    with c.stream(method, url, headers=headers, content=content) as resp:
                        enc = resp.headers.get("content-encoding", "identity").strip().lower()
                        if enc not in ("", "identity"):
                            return "error", "encoded answer", None
                        buf = bytearray()
                        for chunk in resp.iter_bytes():
                            if cancel.is_set():
                                return "error", "deadline", None
                            buf += chunk
                            if len(buf) > MAX_RESPONSE_BYTES:
                                return "error", "too large", None
                        try:
                            doc = json.loads(bytes(buf)) if buf else None
                        except ValueError:
                            doc = None
                        return ("ok" if resp.status_code == 200 else "http"), resp.status_code, doc
            except httpx.HTTPError as exc:
                return "error", type(exc).__name__, None

        r = _run_bounded(run, self._budget)
        return r if r is not None else ("error", "deadline", None)

    def _get(self, path: str, query: Optional[list] = None, want_id: Optional[str] = None,
             want_object: Optional[str] = None):
        """An answer counts only if it is the object asked for (its ``id`` and ``object``); anything else -- an error
        body behind a 200, a different object -- is 'unavailable', so the event is retried rather than mis-booked."""
        kind, status, doc = self._call("GET", path, query=query)
        if kind == "ok" and isinstance(doc, dict):
            if (want_id is not None and doc.get("id") != want_id) or \
                    (want_object is not None and doc.get("object") != want_object):
                return "unavailable", None
            return "ok", doc
        if kind == "http" and status == 404:
            return "not_found", None
        return "unavailable", None

    # --- the port -------------------------------------------------------------------------------------------------------

    def create_checkout(self, invoice_id: str, amount: str, methods: tuple, idempotency_key: str, expires_at: int,
                        label: str) -> StripeCheckout:
        if not _FIN_ID.fullmatch(invoice_id or "") or not methods or any(m not in CHECKOUT_METHODS for m in methods):
            return StripeCheckout("rejected", reason="bad checkout request")
        try:
            cents = to_cents(amount)
        except ValueError:
            return StripeCheckout("rejected", reason="bad amount")
        meta = {"invoice_id": invoice_id, "entity": "zbm"}
        form = form_pairs({
            "mode": "payment", "client_reference_id": invoice_id, "success_url": self.success_url,
            "cancel_url": self.cancel_url, "expires_at": int(expires_at),
            "allowed_payment_method_types": list(methods),
            "line_items": [{"quantity": 1, "price_data": {"currency": "usd", "unit_amount": cents,
                                                           "product_data": {"name": label[:120]}}}],
            "metadata": meta, "payment_intent_data": {"metadata": meta},
        })
        kind, status, doc = self._call("POST", "/v1/checkout/sessions", form=form, idem=idempotency_key)
        if kind == "error" or (kind == "http" and (status == 429 or status >= 500)):
            return StripeCheckout("transport_error", reason=f"stripe unreachable or busy ({status})")
        if kind != "ok" or not isinstance(doc, dict):
            code = (doc or {}).get("error", {}).get("code") if isinstance(doc, dict) else None
            return StripeCheckout("rejected", reason=f"stripe refused the session ({status}, "
                                                     f"{str(code)[:40] if code else 'no code'})")
        sid, url, exp = doc.get("id"), doc.get("url"), doc.get("expires_at")
        if not (isinstance(sid, str) and _SID.fullmatch(sid) and isinstance(url, str) and type(exp) is int
                and doc.get("livemode") is self.livemode and doc.get("amount_total") == cents
                and doc.get("client_reference_id") == invoice_id and doc.get("currency") == "usd"):
            return StripeCheckout("rejected", reason="stripe's session does not match what was asked")
        return StripeCheckout("created", session_id=sid, url=url, expires_at=_iso(exp))

    def verify_event(self, payload: str, signature: str, now_epoch: int) -> bool:
        if not isinstance(payload, str):
            return False
        return verify_signature(payload.encode("utf-8"), signature, self._whsec, int(now_epoch))

    def session(self, session_id: str) -> StripeSession:
        if not _SID.fullmatch(session_id or ""):
            return StripeSession(True, found=False)
        kind, doc = self._get(f"/v1/checkout/sessions/{session_id}", want_id=session_id,
                              want_object="checkout.session")
        if kind == "not_found":
            return StripeSession(True, found=False)
        if kind != "ok":
            return StripeSession(False, reason="stripe unavailable")
        return self._session_answer(doc)

    def expire_session(self, session_id: str) -> StripeSession:
        """Close a Checkout page so nobody can pay on it. Stripe refuses to expire a page that is no longer open; the
        page is then read back, so the answer always says what the page IS now (expired, or complete = paid)."""
        if not _SID.fullmatch(session_id or ""):
            return StripeSession(True, found=False)
        kind, status, doc = self._call("POST", f"/v1/checkout/sessions/{session_id}/expire", form=[],
                                       idem=f"fin-exp-{session_id}")
        if kind == "ok" and isinstance(doc, dict) and doc.get("id") == session_id \
                and doc.get("object") == "checkout.session":
            return self._session_answer(doc)
        if kind == "http" and status in (400, 404):
            return self.session(session_id)
        return StripeSession(False, reason="stripe unavailable")

    def _session_answer(self, doc: dict) -> StripeSession:
        pi = doc.get("payment_intent")
        pi = pi.get("id") if isinstance(pi, dict) else pi
        return StripeSession(True, found=True, session_id=doc.get("id"), status=doc.get("status"),
                             payment_intent=pi, client_reference_id=doc.get("client_reference_id"),
                             livemode=doc.get("livemode") is True)

    def payment(self, payment_intent: str) -> StripePayment:
        if not _SID.fullmatch(payment_intent or ""):
            return StripePayment(True, found=False)
        kind, doc = self._get(f"/v1/payment_intents/{payment_intent}",
                              [("expand[]", "latest_charge.balance_transaction"),
                               ("expand[]", "latest_charge.failure_balance_transaction")],
                              want_id=payment_intent, want_object="payment_intent")
        if kind == "not_found":
            return StripePayment(True, found=False)
        if kind != "ok":
            return StripePayment(False, reason="stripe unavailable")
        try:
            ch = doc.get("latest_charge") if isinstance(doc.get("latest_charge"), dict) else {}
            bt = ch.get("balance_transaction") if isinstance(ch.get("balance_transaction"), dict) else {}
            ft = ch.get("failure_balance_transaction") if isinstance(ch.get("failure_balance_transaction"), dict) \
                else {}
            ptype = (ch.get("payment_method_details") or {}).get("type")
            method = ptype if ptype in ("card", "us_bank_account") else ("other" if ptype else None)
            meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            inv = meta.get("invoice_id")
            usd_bt = bt and bt.get("currency") == "usd"
            return StripePayment(
                True, found=True, payment_intent=doc.get("id"), status=doc.get("status"),
                amount_received=from_cents(doc["amount_received"]), currency=doc.get("currency"),
                invoice_id=inv if isinstance(inv, str) and _FIN_ID.fullmatch(inv) else None, method=method,
                charge=ch.get("id"), charge_status=ch.get("status"),
                balance_txn=bt.get("id") if usd_bt else None,
                gross=from_cents(bt["amount"]) if usd_bt else None, fee=from_cents(bt["fee"]) if usd_bt else None,
                failure_txn=ft.get("id") if ft else None,
                failure_amount=from_cents(ft["amount"]) if ft else None,
                failure_fee=from_cents(ft["fee"]) if ft else None,
                livemode=doc.get("livemode") is True)
        except (KeyError, TypeError, ValueError, AttributeError):
            return StripePayment(False, reason="stripe answer not understood")

    def dispute(self, dispute_id: str) -> StripeDispute:
        if not _SID.fullmatch(dispute_id or ""):
            return StripeDispute(True, found=False)
        kind, doc = self._get(f"/v1/disputes/{dispute_id}", want_id=dispute_id, want_object="dispute")
        if kind == "not_found":
            return StripeDispute(True, found=False)
        if kind != "ok":
            return StripeDispute(False, reason="stripe unavailable")
        try:
            pi = doc.get("payment_intent")
            pi = pi.get("id") if isinstance(pi, dict) else pi
            txns = []
            for t in doc.get("balance_transactions") or []:
                if t.get("currency") != "usd":
                    return StripeDispute(False, reason="a dispute balance transaction is not in USD")
                txns.append({"txn_id": t["id"], "amount": from_cents(t["amount"]), "fee": from_cents(t["fee"])})
            return StripeDispute(True, found=True, dispute_id=doc.get("id"), payment_intent=pi,
                                 status=doc.get("status"), amount=from_cents(doc["amount"]), txns=tuple(txns),
                                 livemode=doc.get("livemode") is True)
        except (KeyError, TypeError, ValueError, AttributeError):
            return StripeDispute(False, reason="stripe answer not understood")

    def payout(self, payout_id: str) -> StripePayout:
        if not _SID.fullmatch(payout_id or ""):
            return StripePayout(True, found=False)
        kind, doc = self._get(f"/v1/payouts/{payout_id}", want_id=payout_id, want_object="payout")
        if kind == "not_found":
            return StripePayout(True, found=False)
        if kind != "ok":
            return StripePayout(False, reason="stripe unavailable")
        try:
            if doc.get("currency") != "usd":
                return StripePayout(False, reason="payout is not in USD")
            return StripePayout(True, found=True, payout_id=doc.get("id"), status=doc.get("status"),
                                amount=from_cents(doc["amount"]), livemode=doc.get("livemode") is True)
        except (KeyError, TypeError, ValueError):
            return StripePayout(False, reason="stripe answer not understood")

    def balance(self) -> RailBalance:
        kind, status, doc = self._call("GET", "/v1/balance")
        if kind != "ok" or not isinstance(doc, dict):
            return RailBalance(False, reason="stripe unavailable")
        try:
            total = 0
            for part in ("available", "pending"):
                for b in doc.get(part) or []:
                    if b.get("currency") == "usd":
                        if type(b["amount"]) is not int:
                            raise ValueError
                        total += b["amount"]
            now = self._clock.now() if self._clock else datetime.now(timezone.utc)
            return RailBalance(True, balance=from_cents(total), as_of=now.isoformat(),
                               source_sha256=hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest())
        except (KeyError, TypeError, ValueError, AttributeError):
            return RailBalance(False, reason="stripe answer not understood")
