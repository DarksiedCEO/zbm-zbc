"""Bid / no-bid qualification (ADR 0016 decision 8).

Decides: a recommendation (``bid``, ``no_bid`` or ``needs_andre``) and a score from seven fixed criteria, each answered
``yes``, ``no`` or ``unknown`` by the agent. Four are must-haves: a ``no`` on any of them recommends ``no_bid``; an
``unknown`` on any of them recommends ``needs_andre``. Otherwise ``bid`` needs at least BID_MIN_YES yeses of seven.
Never: decides to bid. A ``bid`` decision is always Andre's; the recommendation is shown to him and recorded with it."""

from __future__ import annotations

NUMBER = 1
NAME = "bid_qualification"
DECIDES = "bid / no_bid / needs_andre recommendation and score"

CRITERIA = ("scope_fit", "capacity", "deadline_feasible", "compliance_feasible", "relationship", "price_competitive",
            "payment_terms_acceptable")
MUST_HAVE = ("scope_fit", "capacity", "deadline_feasible", "compliance_feasible")
ANSWERS = ("yes", "no", "unknown")
BID_MIN_YES = 5


def recommend(answers: dict) -> dict:
    if set(answers) != set(CRITERIA) or any(v not in ANSWERS for v in answers.values()):
        raise ValueError("every criterion must be answered yes, no or unknown")
    score = sum(1 for c in CRITERIA if answers[c] == "yes")
    blockers = [c for c in MUST_HAVE if answers[c] == "no"]
    unknown = [c for c in MUST_HAVE if answers[c] == "unknown"]
    if blockers:
        rec = "no_bid"
    elif unknown or score < BID_MIN_YES:
        rec = "needs_andre"
    else:
        rec = "bid"
    return {"recommendation": rec, "score": score, "of": len(CRITERIA), "blockers": blockers, "unknown": unknown}
