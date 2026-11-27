"""
SEO-L6 — review-request guard (MVP spec v0.1, Local / Business Profile). One job: decide whether one review request
may go to one customer, and if not, every reason why. Deterministic; no LLM; nothing is sent from here.

A request may go only when ALL of these hold (FTC 16 CFR Part 465, the Consumer Review Rule; Google's ban on
selectively soliciting positive reviews; TCPA for texts):
- the wording is an APPROVED TEMPLATE (``APPROVED_TEMPLATES``: honest asks written and reviewed by ZBM, English and
  Spanish, each with one ``{link}`` slot filled by one https link), and an email subject, when there is one, is an
  approved subject. Anything else is ``unapproved_wording``: refused until a person approves the wording and adds it
  as a template. This is the guard: a word list cannot catch every way to offer something or to ask only happy
  customers (AEGIS round 1 on bbb4e06c sent 29 of 30 reworded offers), so free wording never goes on a word list's
  say-so;
- a real customer after a real transaction (a purchase or a service visit, at most 365 days ago, not cancelled,
  refunded, voided or disputed); otherwise ``no_transaction``;
- a text only to a customer with a readable phone number (``no_phone``) and SMS consent (``no_sms_consent``); email
  needs no SMS consent;
- the customer has not opted out by any flag the guard knows (``opted_out``, ``sms_opted_out``, ``do_not_contact``,
  ``unsubscribed``, ``email_opted_out``): each must be exactly ``False`` or absent (``opted_out`` must be present);
- the request goes to everyone: any audience filter (only happy customers, high ratings or survey scores, "exclude
  complainers"), or wording that asks only for positive or 5-star reviews, is ``gating``;
- a field the guard does not know (a ``segment``, an ``nps_score``) is ``unknown_field``: it might select or describe
  the audience, so it is refused rather than ignored.
The wording is also read for the reasons a person fixing it needs: ``incentive`` (anything offered in exchange),
``suggested_text`` (review text supplied), ``review_suppression`` (steering unhappy customers away), ``gating``. These
readings explain a refusal; they never allow a send, because only an approved template sends.
A request this guard cannot read (not an object, unknown channel, missing customer) is refused (``unreadable``), with
every other reason that can still be read.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CHANNELS = ("sms", "email")
TRANSACTION_TYPES = ("purchase", "service_visit")
MAX_TRANSACTION_AGE_DAYS = 365
REASONS = ("gating", "incentive", "no_email", "no_phone", "no_sms_consent", "no_transaction", "opted_out", "review_suppression",
           "suggested_text", "unapproved_wording", "unknown_field", "unreadable")

# Approved wording (ZBM-authored; each one is tested to trip none of the readings below). The first three are also
# the pool's honest wordings: the MVP starts from them.
APPROVED_TEMPLATES = (
    "Thanks for choosing us! Would you share your honest experience? {link}",
    "How did we do? We read every review, good or bad: {link}",
    "Feel free to tell us how we did, good or bad: {link}",
    "Thanks for your visit. We'd value your honest review, whatever it is: {link}",
    "¿Cómo le fue? Leemos todas las reseñas, buenas o malas: {link}",
    "Gracias por elegirnos. ¿Nos comparte su opinión sincera? {link}",
)
APPROVED_SUBJECTS = ("How did we do?", "Your honest review", "¿Cómo le fue?")
REQUEST_FIELDS = {"request_id", "channel", "customer", "audience_filter", "message", "subject", "fixture", "pool"}
# the fixture pool's own labels are the only values these two fields may carry (anything else: ``unknown_field``)
LABEL_VALUES = {"fixture": "FIXTURE DATA - synthetic, not a real business or person", "pool": "seo-l6-review-requests"}
CUSTOMER_FIELDS = {"phone", "email", "name", "transaction", "sms_consent", "opted_out", "sms_opted_out",
                   "do_not_contact", "unsubscribed", "email_opted_out"}
OPT_OUT_FLAGS = ("sms_opted_out", "do_not_contact", "unsubscribed", "email_opted_out")
TRANSACTION_FIELDS = {"type", "days_ago", "status", "id", "amount", "refunded"}
TRANSACTION_OK_STATUSES = (None, "completed", "paid", "fulfilled", "closed")

# The {link} slot takes only a review link in one of these exact forms (AEGIS round 2: any https host and path let
# "/cash-for-reviews" or "https://." ride inside an approved template). The id is letters, digits, "_" or "-" only
# and is also read for offer, gating, suggestion and steer-away words (separators read as spaces, and as nothing).
_LINK_ID = r"[A-Za-z0-9_\-]{6,64}"
_LINK_FORMS = (rf"https://g\.page/r/({_LINK_ID})/review", rf"https://search\.google\.com/local/writereview\?placeid=({_LINK_ID})",
               r"https://reviews\.example\.com/(r)")          # the fixture pool's own link (example.com is reserved)
_LINK = re.compile("(?:" + "|".join(f"(?:{f})" for f in _LINK_FORMS) + ")")
_ID_WORDS = re.compile(r"cash|free|gift|discount|coupon|deal|prize|reward|win|paid|pay|happy|only|love|best|great|"
                       r"star|unhappy|complain|call|text|first|instead|bonus|off|grat|regalo|premio", re.IGNORECASE)
_PHONE = re.compile(r"\+1[2-9][0-9]{2}[2-9][0-9]{6}")         # ASCII digits only: [0-9], never \d
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_TEMPLATES = tuple(re.compile(re.escape(t).replace(re.escape("{link}"), _LINK.pattern)) for t in APPROVED_TEMPLATES)

_INCENTIVE = re.compile(
    r"\d+\s*(?:%|٪|﹪|percent|per cent|por ciento)|[$€£¥₹₩₽¢\U0001F4B2\U0001F4B0\U0001F4B5\U0001F381]|\d+\s*(?:dollars?|bucks|usd|eur|euros?)\b|"
    r"\b(?:usd|eur)\s*\d|\b(?:twenty|ten|five|fifty|hundred|veinte|diez|cinco|cincuenta) (?:dollars?|bucks|d[oó]lares)|"
    r"gift|\bcoupon|\bvoucher|\bdiscount|\b\d*\s*off\b|\bwin\b|\bwinner|\bto win\b|\bprize|\bgiveaway|\braffle|"
    r"\bsweepstake|\bentr(?:y|ies) (?:in|into|to)\b|\breward|\bpoints\b|\bcredits?\b|\bcashback|\bcash back|\brebate|"
    r"\bfree|\bon us\b|\bon the house\b|\bcomplimentary\b|\bbonus\b|\bperks?\b|\btreat yourself\b|\bthank-you gift|"
    r"\bwaive|\brefund|\bunlock|\b2-for-1\b|\bbogo\b|\b\d\s*x\s*\d\b|"
    r"descuento|cup[oó]n|tarjeta de regalo|\bgratis\b|\bgratuit|\bpremio|\bsorteo|\bgan(?:ar|a|e|en)\b|\bpuntos\b|"
    r"\bcr[eé]dito|\bregalo|\bobsequio|\bsin costo|\bcortes[ií]a|\brebaja|\breembols", re.IGNORECASE)
# "feel free to tell / share / let us know" is not an incentive; it is removed before the incentive reading
_FEEL_FREE = re.compile(r"\bfeel free to (?:tell|share|let|write|leave|reach|say|copy|use|paste)\b", re.IGNORECASE)
# Cyrillic and Greek letters that look Latin, read as the Latin letter for the readings ("frеe" with a Cyrillic е)
_LOOKALIKE = str.maketrans("аеорсухіјѕԁԛԝАВЕКМНОРСТХІЈЅαβεικνορτυχΑΒΕΗΙΚΜΝΟΡΤΥΧ",
                           "aeopcyxijsdqwABEKMHOPCTXIJSabeiknoptuxABEHIKMNOPTYX")
_SUGGESTED = re.compile(
    r"you could say|you can say|you might say|feel free to (?:copy|use|paste)|copy (?:this|and paste)|"
    r"paste this|here'?s (?:what|something) (?:to|you could) (?:write|say)|say something like|write something like|"
    r"for example[:,]? ['\"‘“]|e\.g\.,? ['\"‘“]|sample review|template review|\bmention (?:our|how|that|the)\b|"
    r"\buse these words\b|\btell (?:people|everyone|others) (?:we|that|how)\b|"
    r"puede(?:s)? decir|podr[ií]a(?:s)? decir|copie esto|copia esto|algo como ['\"‘“]|\bmencione\b",
    re.IGNORECASE)
_QUOTED_REVIEW = re.compile(r"(?<!\w)['\"‘“][^'\"’”]{12,}['\"’”](?!\w)")
_SUPPRESSION = re.compile(
    r"instead of (?:posting|leaving|writing)|before (?:posting|leaving) (?:a |your )?review|"
    r"(?:if|when) you (?:weren'?t|were not|are not|aren'?t) (?:happy|satisfied)[^.]*(?:contact|call|email|reach)|"
    r"(?:contact|call|email|reach out to|text|reply to) us (?:first|directly|back)|rather than (?:posting|reviewing)|"
    r"\bno need to post\b|\bnot happy\?|\bunhappy\?|\breply (?:to this message )?first\b|"
    r"en lugar de (?:publicar|dejar|escribir)|antes de (?:publicar|dejar)|\bno qued[oó] content|"
    r"resp[oó]nd(?:a|e)nos primero|\bll[aá]menos primero", re.IGNORECASE)
_POSITIVE_ONLY = re.compile(
    r"\b5\s*(?:-\s*)?(?:stars?|★)|\bfive\s*(?:-\s*)?stars?|★|⭐|\bpositive (?:review|experience|feedback|comment)|"
    r"\bgood review(?!s? or bad)|\bgreat review|\bglowing|\brave review|"
    r"\bif you(?:'re| are| were)? (?:loved|liked|enjoyed|happy|satisfied|pleased)|\bif you (?:loved|liked|enjoyed)|"
    r"\bhad a (?:great|good|wonderful|amazing) (?:experience|time|visit)|\bonly if you\b|"
    r"\b(?:happy|satisfied) customers? only|\b9 or 10\b|\bpromoters?\b|"
    r"\b5 estrellas|\bcinco estrellas|rese[nñ]a positiva|comentario positivo|\bsi le gust[oó]|\bsi te gust[oó]|"
    r"\bsi (?:qued[oó]|est[aá]) content|\bsi le encant[oó]|\bsi te encant[oó]", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    request_id: str
    decision: str            # "send" | "refuse"
    reasons: tuple           # sorted reason codes; empty on send


def _text(raw: object) -> str:
    """NFKC, invisible format characters removed, every dash and hyphen read as "-"."""
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf" and c != "­")
    return "".join("-" if unicodedata.category(c) == "Pd" else c for c in text).translate(_LOOKALIKE)


def _wording_reasons(text: str) -> set:
    reasons = set()
    if _POSITIVE_ONLY.search(text):
        reasons.add("gating")
    if _INCENTIVE.search(_FEEL_FREE.sub(" ", text)) or any(unicodedata.category(c) == "Sc" for c in text):
        reasons.add("incentive")
    if _SUGGESTED.search(text) or _QUOTED_REVIEW.search(text):
        reasons.add("suggested_text")
    if _SUPPRESSION.search(text):
        reasons.add("review_suppression")
    return reasons


def _approved(message: object, subject: object) -> bool:
    if not isinstance(message, str):
        return False
    for t in _TEMPLATES:
        m = t.fullmatch(message)
        if m:
            link_id = next(g for g in m.groups() if g is not None)
            if link_id != "r" and (_ID_WORDS.search(link_id) or _wording_reasons(re.sub(r"[_\-]", " ", link_id))):
                return False
            return subject is None or subject in APPROVED_SUBJECTS
    return False


def _transaction_ok(tx: object) -> bool:
    if not isinstance(tx, dict) or tx.get("type") not in TRANSACTION_TYPES or set(tx) - TRANSACTION_FIELDS:
        return False
    if tx.get("status") not in TRANSACTION_OK_STATUSES or tx.get("refunded") not in (None, False):
        return False
    amount = tx.get("amount")
    if "amount" in tx and (isinstance(amount, bool) or not isinstance(amount, (int, float)) or not amount > 0):
        return False                   # a zero, negative or unreadable amount is not a real purchase
    age = tx.get("days_ago")
    return isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= MAX_TRANSACTION_AGE_DAYS


def check(request: dict) -> Verdict:
    if not isinstance(request, dict):
        return Verdict("unknown", "refuse", ("unreadable",))
    rid = request.get("request_id")
    rid = rid if isinstance(rid, str) and rid else "unknown"
    reasons = set()
    channel, customer = request.get("channel"), request.get("customer")
    if channel not in CHANNELS or not isinstance(customer, dict):
        reasons.add("unreadable")
    if set(request) - REQUEST_FIELDS or any(k in request and request[k] != v for k, v in LABEL_VALUES.items()):
        reasons.add("unknown_field")
    if isinstance(customer, dict):
        if set(customer) - CUSTOMER_FIELDS:
            reasons.add("unknown_field")
        if not _transaction_ok(customer.get("transaction")):
            reasons.add("no_transaction")
        if channel == "sms":
            if not (isinstance(customer.get("phone"), str) and _PHONE.fullmatch(customer["phone"])):
                reasons.add("no_phone")
            if customer.get("sms_consent") is not True:
                reasons.add("no_sms_consent")
        # an email address, when the request carries one, must be readable; the pool's records carry none (the
        # sender looks the address up), so a missing one is not refused here (disclosed in ADR 0017)
        if channel == "email" and "email" in customer and not (
                isinstance(customer["email"], str) and customer["email"].isascii() and _EMAIL.fullmatch(customer["email"])):
            reasons.add("no_email")
        if customer.get("opted_out") is not False or \
                any(customer.get(f, False) is not False for f in OPT_OUT_FLAGS):
            reasons.add("opted_out")
    message, subject = request.get("message"), request.get("subject")
    wording = _text(message) + "\n" + _text(subject)
    if request.get("audience_filter") not in (None, {}, [], ""):
        reasons.add("gating")
    reasons |= _wording_reasons(wording)
    if not _text(message).strip():
        reasons.add("unreadable")
    elif not _approved(message, subject):
        reasons.add("unapproved_wording")
    return Verdict(rid, "refuse" if reasons else "send", tuple(sorted(reasons)))
