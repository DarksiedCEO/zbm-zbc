"""Independent oracle for the SEO-L7 review-reply set.

Re-derives every answer field from the review itself with this module's own rules: the regulated verticals, a
suspected fake (a one-review account at most 7 days old, posted while at least 5 other new accounts reviewed the
business the same day), a refund request (asking for money back, not merely using the word), the owner-approval rule,
and the health or case terms the reply must never repeat. The term list and refund patterns are this module's own and
are written for English reviews only.
"""

from __future__ import annotations

import re

REGULATED = {"med_spa", "dental", "personal_injury_law"}
DETAIL_LEXICON = ("botox", "filler", "root canal", "crown", "whiplash", "back injury")
_REFUND_ASK = re.compile(r"\b(refund me|my money back|expect a (full )?refund|want a refund)\b", re.IGNORECASE)


def derive(r: dict) -> dict:
    regulated = r["vertical"] in REGULATED
    fake = (r["reviewer"]["review_count"] == 1 and r["reviewer"]["account_age_days"] <= 7
            and r["same_day_reviews_from_new_accounts"] >= 5)
    refund = bool(_REFUND_ASK.search(r["text"]))
    must_not = ["argue", "promise_refund_or_credit"]
    if regulated:
        must_not += ["confirm_patient_or_client_relationship", "repeat_health_or_case_detail"]
    return {"review_id": r["review_id"], "flag_suspected_fake": fake, "route_to_owner": refund or fake,
            "owner_approval_before_posting": r["stars"] <= 3 or regulated or fake, "must_not": sorted(must_not),
            "detail_terms_never_in_reply": sorted(t for t in DETAIL_LEXICON if t in r["text"].lower())
            if regulated else []}


def check(reviews: list[dict], key: list[dict]) -> list[str]:
    errors: list[str] = []
    if sorted(r["review_id"] for r in reviews) != sorted(k["review_id"] for k in key) or \
            len({k["review_id"] for k in key}) != len(key):
        errors.append("L7: answer rows do not match the reviews one to one")
    by_id = {k["review_id"]: k for k in key}
    for r in reviews:
        k = by_id.get(r["review_id"])
        if k is not None and k != derive(r):
            errors.append(f"L7 {r['review_id']}: key {k} differs from the review {derive(r)}")
    return errors
