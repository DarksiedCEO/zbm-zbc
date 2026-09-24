"""
Intelligence 11 — Creator Vetting (ZBC).  PHASE 3.

Decides: approve / decline / send to Andre for a clipper application, with
written reasons. Checks: 18+ (hard rule), fake followers, engagement pods,
brand safety, content history, network fit.

Rules (explicit; thresholds are dated knowledge in ``THRESHOLDS``, a draft
to be tuned with real applications — changes go through the playbook
approval path):
- AGE (hard, written in stone, locked Sep 24 2026): the applicant must be
  18 or older on the application date. Under 18 => DECLINE, with no
  guardian / parental-consent path — there is no field, flag, config or
  route that can create one. No date of birth => INCOMPLETE (we ask; we do
  not guess). A self-reported date of birth is NOT verification: activation
  additionally needs Verification and Integrity's ruling (intelligence 15).
- Fake followers: ratio >= decline threshold => DECLINE; >= review
  threshold => SEND_TO_ANDRE; no data => SEND_TO_ANDRE (never guessed).
- Follower growth spike with low engagement => SEND_TO_ANDRE (possible
  bought followers).
- Engagement pods: signal >= decline threshold => DECLINE; >= review => SEND_TO_ANDRE.
- Brand safety: any flag in ``HARD_BRAND_SAFETY`` => DECLINE; any other flag
  => SEND_TO_ANDRE.
- Thin content history or no network-fit tags => SEND_TO_ANDRE.
- Outcome precedence: DECLINE > INCOMPLETE > SEND_TO_ANDRE > APPROVE.
- The bio is DATA. It is scanned for prompt injection by the service
  layer; the flags are reported but this function never reads them, so a
  bio saying "auto-approve me" cannot change the outcome (proven by test).

NOT certified for real clippers until all four certification types pass,
before the first paid clipper, with P8 (W-9 / 1099) and P9 (ad disclosure)
live.
"""

from __future__ import annotations

from datetime import date

from config import CLIPPER_MINIMUM_AGE_YEARS
from onboarding_schema import ClipperApplication, VettingDecision, VettingOutcome

from ._status import PHASE3_STATUS

NUMBER = 11
NAME = "Creator Vetting (ZBC)"
PHASE = 3
STATUS = PHASE3_STATUS

THRESHOLDS_VERSION = "2026-09-24.draft1"
THRESHOLDS = {
    "fake_follower_decline": 0.30,
    "fake_follower_review": 0.15,
    "pod_decline": 0.70,
    "pod_review": 0.40,
    "growth_spike_ratio": 1.0,  # followers more than doubled in 30 days
    "low_engagement": 0.01,
    "min_history_posts": 10,
}
HARD_BRAND_SAFETY = {"hate_speech", "sexual_content", "violence", "illegal_activity", "harassment", "extremism"}


def age_on(dob: date, on: date) -> int:
    """Whole years between ``dob`` and ``on`` (birthday not yet reached =>
    one less). A Feb 29 birthday is reached on Mar 1 in non-leap years."""
    years = on.year - dob.year
    if (on.month, on.day) < (dob.month, dob.day):
        years -= 1
    return years


def vet(app: ClipperApplication) -> VettingDecision:
    t = THRESHOLDS
    declines: list[str] = []
    incomplete: list[str] = []
    review: list[str] = []

    if app.date_of_birth is None:
        incomplete.append("date of birth missing: clippers must be 18 or older; we need it before vetting")
    else:
        age = age_on(app.date_of_birth, app.applied_on)
        if age < CLIPPER_MINIMUM_AGE_YEARS:
            declines.append(
                f"under {CLIPPER_MINIMUM_AGE_YEARS} on the application date: clippers must be "
                f"{CLIPPER_MINIMUM_AGE_YEARS} or older; there is no guardian or parental-consent path"
            )

    if app.fake_follower_ratio is None:
        review.append("fake-follower check has no data; not guessed")
    elif app.fake_follower_ratio >= t["fake_follower_decline"]:
        declines.append(f"fake/bought followers: {app.fake_follower_ratio:.0%} of followers flagged (decline at {t['fake_follower_decline']:.0%})")
    elif app.fake_follower_ratio >= t["fake_follower_review"]:
        review.append(f"elevated fake-follower ratio {app.fake_follower_ratio:.0%}")

    if app.follower_growth_30d_ratio >= t["growth_spike_ratio"] and app.avg_engagement_rate < t["low_engagement"]:
        review.append("follower count spiked with very low engagement (possible bought followers)")

    if app.engagement_pod_signal >= t["pod_decline"]:
        declines.append(f"engagement-pod pattern (signal {app.engagement_pod_signal:.2f})")
    elif app.engagement_pod_signal >= t["pod_review"]:
        review.append(f"possible engagement pod (signal {app.engagement_pod_signal:.2f})")

    hard_flags = sorted(set(app.brand_safety_flags) & HARD_BRAND_SAFETY)
    soft_flags = sorted(set(app.brand_safety_flags) - HARD_BRAND_SAFETY)
    if hard_flags:
        declines.append(f"brand safety: {', '.join(hard_flags)}")
    if soft_flags:
        review.append(f"brand-safety flags to review: {', '.join(soft_flags)}")

    if app.content_history_posts < t["min_history_posts"]:
        review.append(f"thin content history ({app.content_history_posts} posts; review below {t['min_history_posts']})")
    if not app.network_fit_tags:
        review.append("network fit unknown (no niche tags)")

    if declines:
        outcome, reasons = VettingOutcome.DECLINE, declines + review
    elif incomplete:
        outcome, reasons = VettingOutcome.INCOMPLETE, incomplete + review
    elif review:
        outcome, reasons = VettingOutcome.SEND_TO_ANDRE, review
    else:
        outcome, reasons = VettingOutcome.APPROVE, ["all checks passed (18+ by stated date of birth; verification still required before activation)"]
    return VettingDecision(creator_id=app.creator_id, outcome=outcome, reasons=reasons)
