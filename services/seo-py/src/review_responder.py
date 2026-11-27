"""
SEO-L7 — review response drafter (MVP spec v0.1, Local / Business Profile). One job: for one public review, draft the
business's reply and say what must happen before it is posted. Deterministic; no LLM; nothing is posted from here.

Rules:
- The reply is one of a fixed set of vetted wordings, chosen only by the review's stars and whether the business is in
  a regulated vertical. No word of the review, the reviewer's name, the service, a date or any record text ever reaches
  the reply, so a reply can never repeat a health or case detail and can never argue with what the reviewer wrote
  (the inverted default: every word list for "safe" echoes was out-paraphrased in the front-desk agents).
- In a regulated vertical (med spa, dental, personal-injury law) — and in any vertical this module does not know — the
  wordings never confirm the reviewer is a patient or client: no "visit", "patient", "client", "treatment",
  "appointment", "case", "care", "see you again" or any word that says the reviewer was served. Every such review needs
  the owner's approval before posting.
- No wording argues, apologises for a specific act, admits fault, or promises a refund, credit, discount or remedy.
  A review that may ask for money back goes to the owner (``route_to_owner``): any money word ("refund", "money back",
  "reimburse", "compensate", "credit", "chargeback", "dispute", "lawyer" ...) routes it, except in a 4- or 5-star
  review whose every sentence holding the word is one of the fixed non-request forms ("No need for a refund", "I
  almost asked for a refund but ..."). A miss in the other direction (routing a happy review) costs only an owner look.
- A suspected fake (a one-review account at most 7 days old, posted on a day at least 5 other new accounts reviewed the
  business) gets no public reply: ``reply`` is None, it is flagged for a removal request and routed to the owner.
- The owner approves before posting any review of 3 stars or fewer, any regulated or unknown vertical, any review
  routed for money, and any suspected fake. A review that cannot be read (not an object, no id, stars not a whole number 1-5, reviewer fields
  missing or not plain numbers) gets no reply and goes to the owner; it never raises. Only a non-list batch raises
  ValueError.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

REGULATED = frozenset({"med_spa", "dental", "personal_injury_law"})
KNOWN = REGULATED | frozenset({"hvac", "plumbing", "auto_dealer_service", "restaurant", "insurance_agency"})

# the vetted wordings: (regulated, band) -> text. Bands: "high" 4-5 stars, "mid" 3, "low" 1-2.
WORDINGS = {
    (False, "high"): "Thank you for the kind words. We appreciate you taking the time to share them.",
    (False, "mid"): "Thank you for the feedback. We read every review, and we would welcome the chance to hear more; "
                    "please contact us directly.",
    (False, "low"): "Thank you for taking the time to tell us. We take this feedback seriously and would like to hear "
                    "more; please contact us directly so we can look into it.",
    (True, "high"): "Thank you for taking the time to share this review. We appreciate your feedback.",
    (True, "mid"): "Thank you for your feedback. We read every review; please contact our office directly if you "
                   "would like to talk further.",
    (True, "low"): "Thank you for your feedback. We take every concern seriously; please contact our office directly "
                   "if you would like to talk further.",
}

# words a regulated reply must never hold (they confirm a relationship or a service), and words no reply may hold
# (an argument, an admission, a promise). The wordings above are checked against both at import and in the tests.
RELATIONSHIP_WORDS = re.compile(r"\b(?:visit\w*|patient\w*|client\w*|treat\w*|appointment\w*|case\w*|care|caring|"
                                r"procedure\w*|service\w*|serve\w*|served|see you|again|stay\w*|team took|your \w+ "
                                r"(?:with|at) us|experience)\b", re.IGNORECASE)
NEVER_WORDS = re.compile(r"\b(?:refund\w*|money|credit\w*|discount\w*|free|compensat\w*|reimburs\w*|make it right|"
                         r"fix (?:it|this)|promise\w*|guarantee\w*|sorry|apolog\w*|fault|mistake|actually|however|but|"
                         r"unfortunately|false|untrue|lie|lying|fake|wrong|policy|policies|terms)\b", re.IGNORECASE)

_MONEY = re.compile(r"\b(?:refund\w*|money|reimburs\w*|compensat\w*|credit\w*|chargeback\w*|charge ?back|dispute\w*|"
                    r"lawyer\w*|attorney\w*|sue|suing|lawsuit\w*|small claims|bbb|paid|pay\w*|cost\w*|charg\w*|"
                    r"reembols\w*|dinero|devoluci\w*|abogad\w*|demand\w*)\b", re.IGNORECASE)
# the only sentences holding a money word that are not a request, in a 4- or 5-star review (the pool's traps)
_NOT_A_REQUEST = re.compile(r"(?:no need for a refund,? (?:they|we|it) (?:fixed|made|was) [\w\s,'-]*|"
                            r"i almost asked for a refund,? but (?:they|it) (?:made it right|fixed it)[\w\s,'-]*)",
                            re.IGNORECASE)


def _check_wordings() -> None:
    for (regulated, _band), text in WORDINGS.items():
        if NEVER_WORDS.search(text) or (regulated and RELATIONSHIP_WORDS.search(text)):
            raise AssertionError(f"a vetted wording breaks its own rules: {text!r}")


_check_wordings()


@dataclass(frozen=True)
class ReplyDraft:
    review_id: str
    reply: Optional[str]                     # None: nothing is posted (suspected fake, unreadable)
    flag_suspected_fake: bool
    route_to_owner: bool
    owner_approval_before_posting: bool
    notes: tuple[str, ...] = ()


def _whole(x: object) -> Optional[int]:
    return x if type(x) is int else None


def _sentences(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'")
    return [s.strip() for s in re.split(r"[.!?\n;]+", folded) if s.strip()]


def _money_route(text: str, stars: int) -> bool:
    """True when the review may ask for money back or threaten a claim (the inverted default: any money word)."""
    holding = [s for s in _sentences(text) if _MONEY.search(s)]
    if not holding:
        return False
    if stars >= 4 and all(_NOT_A_REQUEST.fullmatch(s) for s in holding):
        return False
    return True


def draft(review: object) -> ReplyDraft:
    """The reply draft for one review. Never raises: anything unreadable is no reply, routed to the owner."""
    try:
        return _draft(review)
    except Exception:                  # a hostile object in one review: no reply, the owner looks
        rid = review.get("review_id") if type(review) is dict else None
        return ReplyDraft(rid if type(rid) is str and rid else "unknown", None, False, True, True, ("unreadable",))


def _draft(review: object) -> ReplyDraft:
    if type(review) is not dict:
        return ReplyDraft("unknown", None, False, True, True, ("unreadable",))
    rid = review.get("review_id")
    rid = rid if type(rid) is str and rid else None
    stars = _whole(review.get("stars"))
    text = review.get("text")
    reviewer = review.get("reviewer")
    pile = _whole(review.get("same_day_reviews_from_new_accounts"))
    count = _whole(reviewer.get("review_count")) if type(reviewer) is dict else None
    age = _whole(reviewer.get("account_age_days")) if type(reviewer) is dict else None
    if rid is None or stars is None or not 1 <= stars <= 5 or type(text) is not str or None in (pile, count, age) \
            or min(pile, count, age) < 0:
        return ReplyDraft(rid or "unknown", None, False, True, True, ("unreadable",))
    vertical = review.get("vertical")
    known = type(vertical) is str and vertical in KNOWN
    regulated = not known or vertical in REGULATED        # an unknown vertical gets the regulated wordings
    notes = [] if known else ["unknown_vertical_treated_as_regulated"]
    fake = count == 1 and age <= 7 and pile >= 5
    if fake:
        return ReplyDraft(rid, None, True, True, True, tuple(notes + ["suspected_fake_request_removal"]))
    money = _money_route(text, stars)
    if money:
        notes.append("may_ask_for_money_or_claim")
    band = "high" if stars >= 4 else "mid" if stars == 3 else "low"
    return ReplyDraft(rid, WORDINGS[(regulated, band)], False, money, stars <= 3 or regulated or money, tuple(notes))


def draft_all(reviews: object) -> list[ReplyDraft]:
    if not isinstance(reviews, list):
        raise ValueError("reviews must be a list")
    return [draft(r) for r in reviews]
