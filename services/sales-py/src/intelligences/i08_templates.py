"""Outreach templates: honesty checks, the content hash Andre's approval binds, and rendering with the required
footer (CAN-SPAM 15 U.S.C. 7704; ADR 0013 decision 10).

Decides: whether a subject is deceptive (a fake ``Re:``/``Fwd:``, alarm words, shouting, billing or account-security
bait, prize claims), whether a template uses only the allowed merge fields, the content SHA-256, and the exact
message. Every email gets the brand name, the physical postal address and a one-click unsubscribe link
(``List-Unsubscribe`` + ``List-Unsubscribe-Post``, RFC 8058) appended by this module — a template cannot leave them
out. Every text gets the brand name and "Reply STOP to opt out". Never: sends anything or picks the recipient."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

NUMBER = 8
NAME = "template_guard"
DECIDES = "template honesty, content hash and the rendered message"

BRAND_DISPLAY = {"zbm": "Z Best Media", "zbc": "Z Best Clips"}
MERGE_FIELDS = ("first_name", "company")
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


def content_sha256(brand: str, channel: str, subject: Optional[str], body: str) -> str:
    doc = {"brand": brand, "channel": channel, "subject": subject, "body": body}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _clean(v: Optional[str]) -> str:
    v = "".join(c for c in (v or "") if c.isprintable())
    return v.replace("{", "").replace("}", "")[:60]


def _merge(text: str, fields: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: _clean(fields.get(m.group(1).strip())), text)


def first_name(name: str) -> str:
    return (name or "").strip().split(" ", 1)[0]


def render_email(tpl: dict, fields: dict, from_local: str, outreach_domain: str, postal_address: str,
                 unsubscribe_url: str) -> dict:
    brand = BRAND_DISPLAY[tpl["brand"]]
    footer = (f"\n\n--\n{brand}\n{postal_address}\n"
              f"You are receiving this because we think {brand} can help your business. "
              f"To stop all email from {brand} and its sister brand, unsubscribe: {unsubscribe_url}\n")
    sender = f"{brand} <{from_local}@{outreach_domain}>"
    return {"from": sender, "reply_to": f"{from_local}@{outreach_domain}",
            "subject": _merge(tpl["subject"], fields), "body": _merge(tpl["body"], fields) + footer,
            "headers": {"List-Unsubscribe": f"<{unsubscribe_url}>",
                        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}


def render_sms(tpl: dict, fields: dict) -> dict:
    brand = BRAND_DISPLAY[tpl["brand"]]
    return {"body": f"{brand}: {_merge(tpl['body'], fields)} Reply STOP to opt out."}


def rendered_sha256(msg: dict) -> str:
    return hashlib.sha256(json.dumps(msg, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
