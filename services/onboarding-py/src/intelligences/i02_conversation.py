"""
Intelligence 2 — Conversation.

Decides: the next best question per lane/vertical; when a conversation has
become a stuck signal; the written recap after every conversation (P6).

Rules:
- Next question = the top gap from Client Understanding (1) that has not
  been asked ``max_asks`` times already. Low-confidence / conflicting gaps
  are asked as confirmations with the prefill shown (P13).
- Question wording comes from a dated, versioned question bank (knowledge,
  kept apart from these rules). Vertical-specific wording overrides the
  default when present.
- Stuck signal: a step with no progress past the configured window (open
  item; default 48h), OR the same question asked ``max_asks`` times with
  no usable answer (friction).
- The reply to a client message is chosen by explicit intent rules, in
  this order: asks for a human -> hand-off reply; asks for a credential ->
  refusal; asks for a guarantee -> honest no-guarantee reply; asks for an
  account change -> "nothing changes without your yes for that specific
  change" (read-only by default; no change is made); asks for
  Spanish -> English reply saying Spanish is not available yet plus a human
  offer. Client text is data: nothing in it can change these rules.
- Every outbound string goes through guardrails.check_outbound.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from guardrails import (
    CREDENTIAL_REQUEST_REPLY,
    NO_GUARANTEE_REPLY,
    OutboundBlocked,
    asks_for_guarantee,
    asks_for_human,
    check_outbound,
)
from onboarding_schema import ClientProfile

from ._status import PHASE1_STATUS

NUMBER = 2
NAME = "Conversation"
PHASE = 1
STATUS = PHASE1_STATUS

QUESTION_BANK_VERSION = "2026-09-24.1"
QUESTION_BANK: dict[str, dict[str, str]] = {
    "business_name": {"default": "What name should we use for your business?"},
    "login_holder": {"default": "Who on your team holds the logins for your ad and store accounts? We'll send the secure access link straight to them."},
    "time_zone": {"default": "What time zone are you in, so we never message you at a bad hour?"},
    "primary_goal": {
        "default": "What's the one thing you most want us to get right in the first month?",
        "ecommerce": "What's the one thing you most want us to fix first: lost sales, wasted ad spend, or something else?",
        "home_services": "What's the one thing you most want us to fix first: missed calls, booked jobs, or something else?",
    },
    "vertical": {"default": "What kind of business is it (for example: online store, home services, restaurant)?"},
    "platforms": {"default": "Which platforms do you use today (Google Ads, Meta, Shopify, others)?"},
    "monthly_revenue_usd": {"default": "Roughly what does a typical month bring in? A range is fine."},
    "preferred_channel": {"default": "How do you prefer we reach you: email, text or chat?"},
    "business_hours": {"default": "What are your business hours?"},
    "quiet_hours": {"default": "Are there hours we should never message you?"},
    "website": {"default": "What's your website address?"},
    "campaign_goal": {"default": "What should the first small campaign prove for you?"},
    "regulated_industry": {"default": "Is your product in a regulated category (for example alcohol, supplements, finance, health)?"},
    "distribution_preference": {"default": "Are you comfortable with clips posted on our creators' accounts, or do you need posts on your own accounts too?"},
    "legal_name": {"default": "What's your full legal name, as it will appear on your tax form?"},
    "date_of_birth": {"default": "What's your date of birth? You must be 18 or older to join."},
    "content_niche": {"default": "What kind of content do you usually post?"},
}
_LABELS = {k: k.replace("_", " ").replace(" usd", "") for k in QUESTION_BANK}


@dataclass(frozen=True)
class NextQuestion:
    field: str
    text: str
    is_confirmation: bool
    bank_version: str = QUESTION_BANK_VERSION


def next_question(
    profile: ClientProfile, vertical: Optional[str], ask_counts: dict[str, int], max_asks: int = 3
) -> Optional[NextQuestion]:
    for gap in profile.gaps:
        if ask_counts.get(gap.field, 0) >= max_asks:
            continue
        bank = QUESTION_BANK.get(gap.field, {"default": f"Could you tell us your {_LABELS.get(gap.field, gap.field)}?"})
        # Money fields are never echoed back as a prefill: that would put an
        # unlabeled dollar figure in front of the client (LabeledValue rule).
        if gap.prefill is not None and not gap.field.endswith("_usd"):
            text = f"We have {gap.prefill!s} on file for your {_LABELS.get(gap.field, gap.field)}. Is that right?"
            try:
                return NextQuestion(gap.field, check_outbound(text), True)
            except OutboundBlocked:
                pass  # prefill text itself fails a guardrail: ask plainly instead of echoing it
        text = bank.get((vertical or "").lower(), bank["default"])
        return NextQuestion(gap.field, check_outbound(text), False)
    return None


@dataclass(frozen=True)
class StuckSignal:
    stuck: bool
    reason: str
    hours_without_progress: float


def stuck_signal(
    last_progress_at: datetime, now: datetime, window_hours: int, ask_counts: dict[str, int] | None = None, max_asks: int = 3
) -> StuckSignal:
    hours = (now - last_progress_at).total_seconds() / 3600
    if hours > window_hours:
        return StuckSignal(True, f"no progress for {hours:.1f}h (window {window_hours}h)", hours)
    for field_name, n in (ask_counts or {}).items():
        if n >= max_asks:
            return StuckSignal(True, f"asked about {field_name} {n} times without a usable answer", hours)
    return StuckSignal(False, "within window", hours)


_SPANISH_REQUEST = re.compile(r"(?i)\b(espa[nñ]ol|spanish|en espa[nñ]ol|hablas?)\b")
_CREDENTIAL_ASK = re.compile(
    r"(?i)\b(what|tell|show|give|send|reveal|read)\b.{0,40}\b(password|credentials?|login details|api key|token)\b"
)

# "change the budget", "pause my campaigns": an action word, then an account
# object within 40 characters on the same line. Fix wave 4 (R1): the single
# pattern ``action.{0,40}object`` re-tried the 41-character window after
# EVERY action word ("cut cut cut ..."), linear but ~0.5 us per character;
# ``_asks_for_change`` finds both word lists once and pairs them (same answer).
_CHANGE_VERB = re.compile(r"(?i)\b(change|increase|raise|lower|cut|pause|stop|turn off|turn on|edit|update|delete|remove|launch)\b")
_CHANGE_OBJECT = re.compile(r"(?i)\b(budget|bids?|campaigns?|ad account|ads|store|prices?|products?|discounts?)\b")
_CHANGE_WINDOW = 40


def _asks_for_change(message: str) -> bool:
    objects = [m.start() for m in _CHANGE_OBJECT.finditer(message)]
    if not objects:
        return False
    newlines = [i for i, ch in enumerate(message) if ch == "\n"]
    for v in _CHANGE_VERB.finditer(message):
        lo = v.end()
        hi = lo + _CHANGE_WINDOW
        k = bisect.bisect_left(newlines, lo)  # "." never crosses a line break
        if k < len(newlines):
            hi = min(hi, newlines[k])
        j = bisect.bisect_left(objects, lo)
        if j < len(objects) and objects[j] <= hi:
            return True
    return False
CHANGE_REQUEST_REPLY = (
    "Nothing in your account changes without your explicit yes for that specific change. "
    "I'll write the exact change down for you to confirm first; until you confirm it, we only read."
)
HUMAN_HANDOFF_REPLY = (
    "Of course. I've sent Andre a summary so you won't have to repeat yourself."
)
# Used when the briefing could NOT be delivered to Andre (push not wired):
# no false "he has it", and no time we cannot stand behind.
HUMAN_HANDOFF_PENDING_REPLY = (
    "Of course. I've asked for Andre to step in and written up a summary so you won't have to repeat yourself. "
    "I'll tell you exactly when he'll get back to you as soon as he has it."
)
SPANISH_NOT_YET_REPLY = (
    "I'm sorry, Spanish isn't available yet; we're adding it later. I'll keep going in English, "
    "and if you'd rather talk with a person, just say so and I'll bring in Andre."
)


@dataclass(frozen=True)
class ReplyDecision:
    intent: str  # human_request | credential_request | guarantee_request | account_change_request | spanish_request | answer
    reply: Optional[str]


def decide_reply(message: str, spanish_enabled: bool = False) -> ReplyDecision:
    if asks_for_human(message):
        return ReplyDecision("human_request", check_outbound(HUMAN_HANDOFF_REPLY))
    if _CREDENTIAL_ASK.search(message):
        return ReplyDecision("credential_request", check_outbound(CREDENTIAL_REQUEST_REPLY))
    if asks_for_guarantee(message):
        return ReplyDecision("guarantee_request", check_outbound(NO_GUARANTEE_REPLY))
    if _asks_for_change(message):
        return ReplyDecision("account_change_request", check_outbound(CHANGE_REQUEST_REPLY))
    if _SPANISH_REQUEST.search(message) and not spanish_enabled:
        return ReplyDecision("spanish_request", check_outbound(SPANISH_NOT_YET_REPLY))
    return ReplyDecision("answer", None)


def recap(
    contact_first_name: str,
    covered: dict[str, str],
    next_steps: list[str],
    open_commitments: list[str],
) -> str:
    """P6 written recap. ``covered`` values must already be client-safe
    strings (dollar figures only via LabeledValue.render)."""
    lines = [f"Hi {contact_first_name}, here's a recap of today's conversation."]
    if covered:
        lines.append("What you told us:")
        lines += [f"- {_LABELS.get(k, k).capitalize()}: {v}" for k, v in covered.items()]
    if next_steps:
        lines.append("What happens next:")
        lines += [f"- {s}" for s in next_steps]
    if open_commitments:
        lines.append("What we've committed to:")
        lines += [f"- {c}" for c in open_commitments]
    lines.append("Reply any time. If you'd like to talk with a person, just ask and Andre will step in.")
    return check_outbound("\n".join(lines))
