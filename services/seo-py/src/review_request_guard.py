"""
SEO-L6 — review-request guard (MVP spec v0.1, Local / Business Profile). One job: decide whether one review request
may go to one customer, and if not, every reason why. Deterministic; no LLM; nothing is sent from here.

A request may go only when ALL of these hold (FTC 16 CFR Part 465, the Consumer Review Rule; Google's ban on
selectively soliciting positive reviews; TCPA for texts):
- a real customer after a real transaction (a purchase or a service visit, at most 365 days ago); otherwise
  ``no_transaction``;
- a text only with the customer's SMS consent (``no_sms_consent``); email needs no SMS consent;
- the customer has not opted out (``opted_out``);
- the request goes to everyone: any audience filter (only happy customers, high ratings or survey scores, "exclude
  complainers"), or a message that asks only for positive or 5-star reviews, is ``gating``;
- nothing is offered in exchange: a discount, a percentage or dollar amount off, a gift card, a prize or a chance to
  win, points, credit or anything "free" in return for a review is ``incentive``. "Feel free to tell us" is not an
  incentive;
- no review text is suggested or supplied ("you could say", "copy this", a quoted ready-made review):
  ``suggested_text``;
- nothing steers an unhappy customer away from posting ("contact us instead of posting"): ``review_suppression``.
A request this guard cannot read (not an object, unknown channel, missing customer) is refused (``unreadable``).
English and Spanish wording are both read.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CHANNELS = ("sms", "email")
TRANSACTION_TYPES = ("purchase", "service_visit")
MAX_TRANSACTION_AGE_DAYS = 365
REASONS = ("gating", "incentive", "no_sms_consent", "no_transaction", "opted_out", "review_suppression",
           "suggested_text", "unreadable")

_INCENTIVE = re.compile(
    r"\d+\s*%|\$\s*\d|\d+\s*(?:dollars?|bucks|usd)\b|gift\s*card|\bcoupon|\bvoucher|\bdiscount|\b\d*\s*off\b|"
    r"\bchance to win\b|\bto win\b|\bwin\b|\bprize|\bgiveaway|\braffle|\bsweepstake|\bentr(?:y|ies) (?:in|into|to)\b|"
    r"\breward|\bpoints\b|\bcredit\b|\bcashback|\bcash back|\brebate|"
    r"(?<!feel )(?<!feel\s)\bfree\b|\bon us\b|\bcomplimentary\b|\bbonus\b|"
    r"descuento|cup[oó]n|tarjeta de regalo|\bgratis\b|\bpremio|\bsorteo|\bganar\b|\bgana\b|\bpuntos\b|\bcr[eé]dito\b",
    re.IGNORECASE)
_SUGGESTED = re.compile(
    r"you could say|you can say|you might say|feel free to (?:copy|use|paste)|copy (?:this|and paste)|"
    r"paste this|here'?s (?:what|something) (?:to|you could) (?:write|say)|say something like|write something like|"
    r"for example[:,]? ['\"‘“]|e\.g\.,? ['\"‘“]|sample review|template review|"
    r"puede(?:s)? decir|podr[ií]a(?:s)? decir|copie esto|copia esto|algo como ['\"‘“]", re.IGNORECASE)
_QUOTED_REVIEW = re.compile(r"['\"‘“][^'\"’”]{12,}['\"’”]")
_SUPPRESSION = re.compile(
    r"instead of (?:posting|leaving|writing)|before (?:posting|leaving|writing) (?:a |your )?review|"
    r"(?:if|when) you (?:weren'?t|were not|are not|aren'?t) (?:happy|satisfied)[^.]*(?:contact|call|email|reach)|"
    r"(?:contact|call|email|reach out to) us (?:first|directly) (?:instead|before|rather)|rather than (?:posting|reviewing)|"
    r"en lugar de (?:publicar|dejar|escribir)|antes de (?:publicar|dejar|escribir)", re.IGNORECASE)
_POSITIVE_ONLY = re.compile(
    r"\b5[- ]?stars?\b|\bfive[- ]stars?\b|\bpositive review|\bgood review|\bgreat review|\bglowing|"
    r"\bif you (?:loved|liked|enjoyed|were happy)|\bonly if you\b|\bhappy customers? only|"
    r"\b5 estrellas|\bcinco estrellas|rese[nñ]a positiva|\bsi le gust[oó]|\bsi te gust[oó]", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    request_id: str
    decision: str            # "send" | "refuse"
    reasons: tuple           # sorted reason codes; empty on send


def _text(raw: object) -> str:
    return unicodedata.normalize("NFKC", raw) if isinstance(raw, str) else ""


def _transaction_ok(tx: object) -> bool:
    if not isinstance(tx, dict) or tx.get("type") not in TRANSACTION_TYPES:
        return False
    age = tx.get("days_ago")
    return isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= MAX_TRANSACTION_AGE_DAYS


def check(request: dict) -> Verdict:
    rid = request.get("request_id") if isinstance(request, dict) else None
    rid = rid if isinstance(rid, str) and rid else "unknown"
    if not isinstance(request, dict) or request.get("channel") not in CHANNELS \
            or not isinstance(request.get("customer"), dict):
        return Verdict(rid, "refuse", ("unreadable",))
    customer, reasons = request["customer"], set()
    if not _transaction_ok(customer.get("transaction")):
        reasons.add("no_transaction")
    if request["channel"] == "sms" and customer.get("sms_consent") is not True:
        reasons.add("no_sms_consent")
    if customer.get("opted_out") is not False:
        reasons.add("opted_out")
    message = _text(request.get("message"))
    if request.get("audience_filter") not in (None, {}, [], "") or _POSITIVE_ONLY.search(message):
        reasons.add("gating")
    if _INCENTIVE.search(message):
        reasons.add("incentive")
    if _SUGGESTED.search(message) or _QUOTED_REVIEW.search(message):
        reasons.add("suggested_text")
    if _SUPPRESSION.search(message):
        reasons.add("review_suppression")
    if not message.strip():
        reasons.add("unreadable")
    return Verdict(rid, "refuse" if reasons else "send", tuple(sorted(reasons)))
