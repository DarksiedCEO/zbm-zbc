"""
P20 — recommend score, asked ONLY right after the first real win.

- ``may_ask``: true only when a first real win has been delivered (a
  provable win, recorded by the service) and the score has not been asked
  yet. Asking at any other moment is refused.
- ``route``: 9–10 => referral path; 7–8 => thank you, nothing more;
  <= 6 => SOFT escalation (the agent makes one resolution attempt — asks
  what would make it better — and escalates to Andre if unresolved).
"""

from __future__ import annotations

from typing import Optional

from guardrails import check_outbound

ASK_TEXT = "Now that your first result is in: how likely are you to recommend ZBM to a friend, from 0 to 10?"
REFERRAL_TEXT = (
    "Thank you, that means a lot. If someone you know could use the same help, reply with their name "
    "and we'll reach out, or share our contact with them. No pressure either way."
)
THANKS_TEXT = "Thank you. If anything would make this a 10 for you, just tell us."
RESOLUTION_ATTEMPT_TEXT = (
    "Thank you for being honest. What's the one thing that would make this better for you? "
    "If you'd rather talk to Andre directly, just say so."
)


def may_ask(first_win_delivered: bool, already_asked: bool) -> tuple[bool, str]:
    if not first_win_delivered:
        return False, "the recommend score is asked only right after the first real win; no win delivered yet"
    if already_asked:
        return False, "already asked"
    return True, "first real win delivered"


def route(score: int) -> tuple[str, Optional[str]]:
    if not 0 <= score <= 10:
        raise ValueError("score must be 0-10")
    if score >= 9:
        return "referral_path", check_outbound(REFERRAL_TEXT)
    if score >= 7:
        return "thanks", check_outbound(THANKS_TEXT)
    return "soft_escalation", check_outbound(RESOLUTION_ATTEMPT_TEXT)
