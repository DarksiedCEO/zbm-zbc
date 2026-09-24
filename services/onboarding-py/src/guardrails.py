"""
Guardrails that apply to all 15 intelligences, with no override by client,
document or conversation (locked spec, "Guardrails").

- ``check_outbound``: every piece of client-facing text passes through it.
  Blocks guarantee language (no promised results, revenue figures or
  clipper earnings) and any ``$`` figure that is not rendered through
  ``LabeledValue.render()`` (the LabeledValue rule for client text).
- ``scan_for_injection``: client content (website, bio, document, intake
  answer) is DATA, never instructions. Hidden "ignore your rules" text is
  flagged and logged. No decision function in this service reads the
  flags — they exist only for the log and the anomaly event — which is
  what makes "has no effect on decisions" provable by test.
- ``ai_disclosure_first_message``: P1 — the first message says it is an
  AI and offers a human (Andre) any time. Wording is PENDING COUNSEL
  (Cal. B&P 17941); the Compliance gate stays unmet until it is approved.
- ``asks_for_human``: detects the hard escalation trigger.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("onboarding.guardrails")


class OutboundBlocked(ValueError):
    """Client-facing text violated a guardrail and must not be sent."""


# --- guarantee language ------------------------------------------------------

_NEGATED_OK = re.compile(
    r"(?i)\b(can(?:no|')?t|cannot|can not|won'?t|will not|do not|don'?t|never|no one can|nobody can|we don'?t|not able to)\s+"
    r"(?:\w+\s+){0,3}?(guarantee[sd]?|promise[sd]?)\b"
    r"|\bno\s+guarantees?\b|\bnot\s+(?:a\s+)?guarantee[sd]?\b|\bwithout\s+(?:a\s+)?guarantee\b"
)
_GUARANTEE = re.compile(
    r"(?i)\b(?:guarantee[sd]?|guaranteeing|risk[- ]free|surefire|sure[- ]thing|"
    r"we promise|i promise|promised results?|you will (?:make|earn|see|get|double|triple)|"
    r"you'?ll (?:make|earn|see|get|double|triple)|will definitely|definitely (?:increase|grow|make)|"
    r"double your|triple your|can'?t lose|no[- ]lose|certain(?:ly)? (?:to|will) (?:grow|increase|earn))\b"
    # "100%" ends in a non-word char, so it cannot sit inside the \b...\b group
    # above (the WIP version had it there and it could never match).
    r"|\b100\s*(?:%|percent\b)"
)
_DOLLAR = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
# A figure written in words ("5,000 dollars", "USD 300") is still a dollar
# figure; it can never carry the LabeledValue suffix, so it is always blocked.
#
# Fix wave 4 (R1): the old ``\b\d[\d,]*...`` was retried at every digit after
# a comma of one long "1,1,1,..." run, each retry rescanning the run
# (quadratic). Every such start ends at the same place (the suffix can only
# follow the run), so ONE start per run is tried — the first digit a word
# boundary allows:
#   - the run starts with a digit that is not glued to a word character;
#   - the run starts with commas (the digit after them is at a boundary);
#   - the run is glued to a letter/underscore: the first digit after a comma
#     inside it (the atomic group commits to that one).
_WORD_DOLLAR = re.compile(
    r"(?i)(?:(?<![\w,])\d|(?<![\d,]),++\d|(?<=[^\W\d])(?>\d[\d,]*?,(?=\d))\d)[\d,]*+"
    r"(?:\.\d+)?\s*(?:k\s*)?(?:dollars|bucks|usd)\b|\busd\s*\d"
)
# Matched AT the end of a ``$`` figure (``match(text, pos)``): no slicing of
# the rest of the text per figure (fix wave 4, R1: that was quadratic).
_LABEL_SUFFIX = re.compile(
    r"\s\((observed|attributed|incremental|financially_verified), (low|medium|high|very_high) confidence\)"
)


def guarantee_violations(text: str) -> list[str]:
    stripped = _NEGATED_OK.sub(" ", text)
    return [m.group(0) for m in _GUARANTEE.finditer(stripped)]


def unlabeled_dollar_figures(text: str) -> list[str]:
    bad = []
    for m in _DOLLAR.finditer(text):
        if not _LABEL_SUFFIX.match(text, m.end()):
            bad.append(m.group(0))
    return bad


def check_outbound(text: str) -> str:
    """Return ``text`` unchanged if it may be sent to a client; raise
    ``OutboundBlocked`` naming the rule otherwise. Never rewrites silently."""
    g = guarantee_violations(text)
    if g:
        raise OutboundBlocked(f"guarantee language blocked: {sorted(set(x.lower() for x in g))}")
    d = unlabeled_dollar_figures(text) + [m.group(0) for m in _WORD_DOLLAR.finditer(text)]
    if d:
        raise OutboundBlocked(f"unlabeled dollar figure blocked: {d}")
    return text


# --- content is data (prompt-injection defense) --------------------------------

_INJECTION = [
    ("ignore_instructions", re.compile(r"(?i)\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|all|your|the|any)\b.{0,20}\b(instructions?|rules?|guidelines?|policy|policies|prompt|guardrails?)")),
    ("role_reassignment", re.compile(r"(?i)\b(you are now|act as|pretend (?:to be|you are)|from now on you)\b")),
    ("system_prompt_probe", re.compile(r"(?i)\b(system prompt|developer message|hidden instructions?|jailbreak)\b")),
    # ``[^\S\n]*`` not ``\s*`` (fix wave 4, R1): ``^\s*`` was retried at every
    # line start of a run of blank lines, rescanning the run each time. A
    # marker after blank lines is still found, from its own line start.
    ("fake_role_marker", re.compile(r"(?im)^[^\S\n]*(system|assistant|developer)\s*:")),
    ("ai_directive_comment", re.compile(r"(?i)<!--\s*(ai|assistant|agent|llm|bot)\b")),
    ("approval_forgery", re.compile(r"(?i)\b(auto[- ]?approve|approve (?:this|me|all)|mark (?:as )?(?:approved|verified|compliant)|skip (?:the )?(?:vetting|verification|compliance|checks?))\b")),
    ("credential_exfiltration", re.compile(r"(?i)\b(reveal|show|send|print|output|tell me)\b.{0,30}\b(password|credential|token|secret|api key)s?\b")),
    ("guarantee_coercion", re.compile(r"(?i)\b(say|tell them|promise|state) (?:that )?(?:we|you) (?:guarantee|will guarantee)\b")),
]


@dataclass(frozen=True)
class InjectionFlag:
    rule: str
    source: str
    excerpt_len: int


def scan_for_injection(text: str, source: str) -> list[InjectionFlag]:
    flags = []
    for name, rx in _INJECTION:
        m = rx.search(text or "")
        if m:
            flags.append(InjectionFlag(rule=name, source=source, excerpt_len=len(m.group(0))))
    for f in flags:
        # Logged, never obeyed. The excerpt itself is not logged (it is
        # client content and may be long / hostile); rule + source are.
        log.warning("client content flagged as possible prompt injection: rule=%s source=%s", f.rule, f.source)
    return flags


# --- honest identity -------------------------------------------------------------

P1_WORDING_STATUS = "pending counsel review (Cal. B&P Code 17941)"


def ai_disclosure_first_message(contact_first_name: str, company: str = "ZBM") -> str:
    text = (
        f"Hi {contact_first_name}, I'm the {company} onboarding assistant. I'm an AI, not a person. "
        f"If you'd rather talk to a human at any point, just say so and I'll bring in Andre, "
        f"who runs {company}. I'll get your account set up and keep you posted at every step."
    )
    return check_outbound(text)


_HUMAN_REQUEST = re.compile(
    r"(?i)\b(talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|the\s+|someone|somebody|andre|real|actual|human|person|manager|owner)"
    r"|\b(real|actual|live)\s+(person|human)\b|\bhuman\s+(?:being|please|agent)\b|\b(get|want|need)\s+(?:me\s+)?(?:a\s+)?(human|person|andre|manager)\b"
    r"|\bare you (?:a )?(?:bot|robot|ai)\b.{0,40}\b(human|person)\b|\brepresentative\b"
)


def asks_for_human(text: str) -> bool:
    return bool(_HUMAN_REQUEST.search(text or ""))


_GUARANTEE_REQUEST = re.compile(
    r"(?i)\b(guarantee|promise|100\s*%|for sure|definitely)\b.{0,60}\b(results?|revenue|money|sales|roi|earn|make|increase|double|leads?)\b"
    r"|\b(results?|revenue|money|sales|roi)\b.{0,40}\b(guaranteed?|promised?)\b"
)


def asks_for_guarantee(text: str) -> bool:
    return bool(_GUARANTEE_REQUEST.search(text or ""))


NO_GUARANTEE_REPLY = (
    "I understand wanting certainty. I can't guarantee results, and no one honest can. "
    "What I can do is show you exactly what we find in your account, with the evidence and how "
    "confident we are in each number, and track every result as it happens."
)

CREDENTIAL_REQUEST_REPLY = (
    "I can't share any login or credential, and no one at ZBM can see them either: we use "
    "delegated access you control, and you can revoke it any time from your own account settings."
)
