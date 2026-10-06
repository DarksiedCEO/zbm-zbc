"""Outreach email templates (copied from sales-py's i08 template guard as fixed in its AEGIS rounds 1-2): honesty
checks, the content hash Andre's approval binds, and rendering with the required footer (CAN-SPAM 15 U.S.C. 7704;
ADR 0015 decision 10).

Decides: whether a subject is deceptive (a fake ``Re:``/``Fwd:``, alarm words, shouting, billing or account-security
bait, prize claims), whether a template uses only the allowed merge fields, the content SHA-256, and the exact
message. Every email gets the brand name, the physical postal address and a one-click unsubscribe link
(``List-Unsubscribe`` + ``List-Unsubscribe-Post``, RFC 8058) appended by this module — a template cannot leave them
out. The one merge field is ``{{first_name}}``, rendered only from the influencer's ``verified_first_name`` that a
person set at Andre's console (sales-py S2-H1): nothing a creator or a public profile typed ever renders. Never: sends
anything or picks the recipient."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

NUMBER = 5
NAME = "template_guard"
DECIDES = "template honesty, content hash and the rendered message"

BRAND_DISPLAY = {"zbm": "Z Best Media", "zbc": "Z Best Clips"}
MERGE_FIELDS = ("first_name",)
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]{0,40}?)\s*\}\}")
DECEPTIVE = (
    ("FAKE_THREAD", re.compile(r"^\s*(re|fw|fwd|aw|tr)\s*[:\]]", re.I)),
    ("ALARM", re.compile(r"\b(urgent|action required|final notice|last chance|immediate(ly)?|act now|"
                         r"expires? (today|tonight))\b", re.I)),
    ("ACCOUNT_BAIT", re.compile(r"\b(account (suspended|locked|closed|on hold)|verify your|password|security alert|"
                                r"unusual (activity|sign[- ]?in))\b", re.I)),
    ("BILLING_BAIT", re.compile(r"\b(invoice|payment (due|failed|overdue)|your (order|receipt|refund|subscription)|"
                                r"refund|past due)\b", re.I)),
    ("PRIZE_CLAIM", re.compile(r"\b(you('ve| have)? won|winner|congratulations|free money|100% free|"
                               r"guarantee(d)?|risk[- ]free|no cost)\b", re.I)),
    ("SHOUTING", re.compile(r"!{2,}|\${2,}")),
)


def deceptive_subject(subject: str) -> Optional[str]:
    for code, rx in DECEPTIVE:
        if rx.search(subject):
            return code
    letters = [c for c in subject if c.isalpha()]
    if len(letters) >= 6 and sum(c.isupper() for c in letters) * 2 > len(letters):
        return "SHOUTING"
    return None


def unknown_placeholders(*texts: str) -> list[str]:
    bad = []
    for t in texts:
        for name in _PLACEHOLDER.findall(t):
            if name not in MERGE_FIELDS:
                bad.append(name[:40])
    if any(("{{" in t and not _PLACEHOLDER.search(t)) for t in texts):
        bad.append("unbalanced")
    return bad


def content_sha256(brand: str, subject: Optional[str], body: str) -> str:
    doc = {"brand": brand, "channel": "email", "subject": subject, "body": body}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _clean(v: Optional[str]) -> str:
    v = "".join(c for c in (v or "") if c.isprintable())
    return v.replace("{", "").replace("}", "")[:60]


def _merge(text: str, fields: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: _clean(fields.get(m.group(1).strip())), text)


# sales-py AEGIS S2-H1: merge values are never taken from what outside parties typed. ``first_name`` renders only the
# influencer's ``verified_first_name``, set by a person at the console (a typed ledger event); with no verified value
# the template is refused for that influencer (or uses a generic greeting). The rules below are the second layer.
# AEGIS S1-H1: a merge value comes from a form anyone can fill in, so it is never trusted as copy. Only ASCII letters,
# digits, space and . ' & - ; no scheme, no "www.", no "@"; at most 40 characters. A template that uses a field whose
# value breaks this is refused for that contact (MERGE_FIELD_REFUSED); the RENDERED subject is checked for deception
# and the rendered text may carry no URL or domain the approved template does not carry.
MERGE_VALUE = re.compile(r"[A-Za-z0-9 .'&-]{0,40}")
_URL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://\S+|www\.\S+|\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                  r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]{2,24}\b)")


def merge_value_ok(v: str) -> bool:
    low = v.lower()
    return bool(MERGE_VALUE.fullmatch(v)) and "://" not in low and "www." not in low and "@" not in low


def urls(text: str) -> set[str]:
    return {u.lower().rstrip(".,;:!?)") for u in _URL.findall(text or "")}


def merge_problem(tpl: dict, fields: dict) -> Optional[str]:
    """None when every used merge value is safe and the merged copy is still honest; otherwise a refusal code."""
    subject, body = tpl.get("subject"), tpl["body"]
    for name in {n.strip() for n in _PLACEHOLDER.findall((subject or "") + "\n" + body)}:
        value = fields.get(name)
        if not value or not merge_value_ok(value):       # S2-H1: no human-verified value -> refused
            return "MERGE_FIELD_REFUSED"
    merged_subject = _merge(subject, fields) if subject is not None else None
    if merged_subject is not None and deceptive_subject(merged_subject):
        return "SUBJECT_DECEPTIVE"
    merged = (merged_subject or "") + "\n" + _merge(body, fields)
    if urls(merged) - urls((subject or "") + "\n" + body):
        return "URL_NOT_APPROVED"
    return None


def render_email(tpl: dict, fields: dict, from_local: str, outreach_domain: str, postal_address: str,
                 unsubscribe_url: str) -> dict:
    brand = BRAND_DISPLAY[tpl["brand"]]
    footer = (f"\n\n--\n{brand}\n{postal_address}\n"
              f"You are receiving this because we would like to work with you as a creator. "
              f"To stop all messages from {brand} and its sister brand, unsubscribe: {unsubscribe_url}\n")
    sender = f"{brand} <{from_local}@{outreach_domain}>"
    return {"from": sender, "reply_to": f"{from_local}@{outreach_domain}",
            "subject": _merge(tpl["subject"], fields), "body": _merge(tpl["body"], fields) + footer,
            "headers": {"List-Unsubscribe": f"<{unsubscribe_url}>",
                        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}


def rendered_sha256(msg: dict) -> str:
    return hashlib.sha256(json.dumps(msg, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
