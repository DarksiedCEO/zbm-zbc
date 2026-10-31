"""
Client email (ADR 0026; red team OPS-H3 / RTM-12): what Finance sends a client, and the adapters that send it.

Rules every message follows (tests: test_client_mail.py):
- sender ``billing@zbestmedia.com`` only (config refuses any other MAIL_FROM); replies go to the same address;
- ONE recipient: the billing contact Andre recorded for the client (``PUT /fin/v1/clients/{id}/billing-contact``).
  Finance hands the adapter that address and nothing else; an adapter never looks one up, never adds cc or bcc, and
  refuses (``no_recipient``) when there is none;
- plain text plus a simple HTML part built from the same words: no images, no remote resources, no links, no
  tracking pixels, no scripts (Resend's open and click tracking are domain settings: the runbook keeps both off);
- built only from a fixed list of fields per kind (``FIELDS``): a hold, its reason, a sanctions marker or any other
  internal note can never reach a client, whatever the caller puts in the data;
- each message carries a stable idempotency key (the outbox id): a retry after a lost answer is not a second email
  (Resend keeps the key 24 hours; Finance's retries end well inside that window, see svc_mail).

Adapters: ``ResendClientMail`` (the real one, HTTPS to api.resend.com), ``FixtureClientMail`` (tests and local runs;
sends nothing anywhere) and ports.NotBuiltClientMail (the default: mail is OFF until RESEND_API_KEY and MAIL_FROM are
set).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from ports import MailOutcome

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_S = 20                      # well inside Finance's 15-minute stale-claim window
REPLY_TO = "billing@zbestmedia.com"
FOOTER = "Z Best Media\nBilling: billing@zbestmedia.com"

TOPICS = {"refund_request": "refund request", "cancellation": "cancellation notice",
          "billing_dispute": "question about an invoice", "payment_question": "payment question"}

# the ONLY fields a template reads, per kind (everything else in the data is ignored)
FIELDS = {
    "receipt": ("invoice_id", "what_you_bought", "items", "period", "amount_paid", "currency", "paid_on", "method"),
    "credit_note": ("credit_note_number", "invoice_id", "amount", "issued_on", "refund_amount", "refund_due_by",
                    "amount_still_due"),
    "invoice_cancelled": ("invoice_id", "amount_cancelled", "cancelled_on"),
    "refund_sent": ("credit_note_number", "invoice_id", "refund_amount", "refunded_on"),
    "acknowledgement": ("topic", "received_on", "invoice_id", "reply_by"),
}
KINDS = tuple(FIELDS)
_SAFE = re.compile(r"[^\x20-\x7E\n -ɏ–—’]")


def _clean(v, limit: int = 400) -> str:
    return _SAFE.sub("", str(v if v is not None else ""))[:limit]


def _money(v) -> str:
    return f"${_clean(v, 20)}"


def render(kind: str, data: dict) -> tuple[str, str, str]:
    """(subject, plain text, html) for one message. Unknown kind -> ValueError."""
    if kind not in FIELDS:
        raise ValueError("unknown mail kind")
    d = {k: data.get(k) for k in FIELDS[kind]}
    inv = _clean(d.get("invoice_id"), 128)
    if kind == "receipt":
        subject = f"Payment received: invoice {inv}"
        lines = [f"Thank you. We received your payment of {_money(d['amount_paid'])} on {_clean(d['paid_on'], 10)} "
                 f"by {_clean(d['method'], 60)}.", "", f"Invoice: {inv}", f"What you bought: {_clean(d['what_you_bought'])}"]
        for it in (d.get("items") or [])[:50]:
            if isinstance(it, dict):
                lines.append(f"  - {_clean(it.get('description'), 200)}: {_money(it.get('amount'))}")
        per = d.get("period") or {}
        if isinstance(per, dict) and per.get("flight_start"):
            lines.append(f"Period: {_clean(per.get('flight_start'), 10)} to {_clean(per.get('flight_end'), 10)}")
    elif kind == "credit_note":
        subject = f"Credit note {_clean(d['credit_note_number'], 40)} for invoice {inv}"
        lines = [f"We issued credit note {_clean(d['credit_note_number'], 40)} on {_clean(d['issued_on'], 10)} for "
                 f"{_money(d['amount'])} against invoice {inv}."]
        if d.get("refund_amount") and d["refund_amount"] != "0.00":
            lines.append(f"We will refund {_money(d['refund_amount'])} to your original payment method by "
                         f"{_clean(d['refund_due_by'], 10)}. There is no refund fee.")
        if d.get("amount_still_due") and d["amount_still_due"] != "0.00":
            lines.append(f"The amount still due on the invoice is now {_money(d['amount_still_due'])}.")
        elif not (d.get("refund_amount") and d["refund_amount"] != "0.00"):
            lines.append("You owe nothing more on this invoice.")
    elif kind == "invoice_cancelled":
        subject = f"Invoice {inv} cancelled"
        lines = [f"We cancelled invoice {inv} in full on {_clean(d['cancelled_on'], 10)} "
                 f"({_money(d['amount_cancelled'])}). You owe nothing on this invoice."]
    elif kind == "refund_sent":
        subject = f"Refund sent: credit note {_clean(d['credit_note_number'], 40)}"
        lines = [f"We sent your refund of {_money(d['refund_amount'])} on {_clean(d['refunded_on'], 10)} under credit "
                 f"note {_clean(d['credit_note_number'], 40)} (invoice {inv}).",
                 "Your bank may take a few days to show it."]
    else:
        topic = TOPICS.get(d.get("topic"), "request")
        subject = f"We received your {topic}"
        lines = [f"We received your {topic} on {_clean(d['received_on'], 10)}"
                 + (f" about invoice {inv}" if inv else "") + "."]
        if d.get("reply_by"):
            lines.append(f"We will reply by {_clean(d['reply_by'], 10)}.")
    lines += ["", "If you have a question, reply to this email.", "", FOOTER]
    text = "\n".join(lines)
    body = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in text.split("\n\n") if p.strip())
    page = ('<!doctype html><html><head><meta charset="utf-8"></head>'
            f'<body style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            f"{body}</body></html>")
    return subject[:200], text, page


def message(kind: str, client_id: str, to: Optional[str], data: dict, idempotency_key: str) -> dict:
    subject, text, page = render(kind, data)
    return {"kind": kind, "client_id": client_id, "to": to, "subject": subject, "text": text, "html": page,
            "idempotency_key": idempotency_key[:256], "data": data}


@dataclass
class ResendClientMail:
    """HTTPS to Resend's Send Email endpoint. Holds the key; never prints it, the recipient or the body."""
    api_key: str
    sender: str
    transport: Optional[httpx.BaseTransport] = None
    requires_contact = True             # class attribute: never sends without the contact on file

    def __repr__(self) -> str:
        return "ResendClientMail(***)"

    def send_receipt(self, client_id, receipt):        # the old port shape: Finance calls deliver()
        return False

    def deliver(self, msg: dict) -> MailOutcome:
        to = msg.get("to")
        if not to:
            return MailOutcome("no_recipient")
        body = {"from": self.sender, "to": [to], "subject": msg["subject"], "text": msg["text"], "html": msg["html"],
                "reply_to": REPLY_TO}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "Idempotency-Key": msg["idempotency_key"][:256], "User-Agent": "zbm-finance/1"}
        try:
            with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S), transport=self.transport,
                              follow_redirects=False) as c:
                r = c.post(RESEND_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return MailOutcome("retry", code=type(exc).__name__[:40])
        name = None
        try:
            doc = r.json()
            name = doc.get("name") if isinstance(doc, dict) else None
        except ValueError:
            doc = None
        if r.status_code == 200 and isinstance(doc, dict) and isinstance(doc.get("id"), str):
            return MailOutcome("sent", provider_id=doc["id"][:80])
        code = f"{r.status_code}:{str(name)[:40] if name else 'no-name'}"
        if r.status_code == 409 and name == "invalid_idempotent_request":
            return MailOutcome("permanent", code=code)
        if r.status_code in (409, 429) or r.status_code >= 500:
            return MailOutcome("retry", code=code)
        return MailOutcome("permanent", code=code)


class FixtureClientMail:
    """Sends nothing: keeps what it was asked to send (tests, local runs). ``outcomes`` scripts the answers."""

    requires_contact = True

    def __init__(self):
        self.delivered: list = []
        self.outcomes: list = []

    def send_receipt(self, client_id, receipt):
        return False

    def deliver(self, msg: dict) -> MailOutcome:
        if not msg.get("to"):
            return MailOutcome("no_recipient")
        out = self.outcomes.pop(0) if self.outcomes else MailOutcome("sent", provider_id=f"fixture-{len(self.delivered)}")
        if out.outcome == "sent":
            self.delivered.append(msg)
        return out
