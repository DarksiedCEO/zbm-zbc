"""
Intelligence 2 — View Certifier (spec §A.1, §C.2): pending | certified | not_certified; revisions; clawback
records. Never computes money and never raises a count after settlement.

The service gathers every A.1 condition as reason items (no short-circuit: every failing reason is kept);
this module turns them into a status and applies the settlement rules:
- no rule version in force → exactly one reason, RULES_NOT_IN_FORCE (VI-00);
- no platform create time yet → ADAPTER_UNAVAILABLE (pending until the fallback deadline passes);
- ``now < settle_at`` → ``pending`` / NOT_YET_SETTLED;
- no settlement snapshot fetched in ``[settle_at, settle_at + VI_SETTLE_FETCH_GRACE_H]`` → pending while that
  window is open, then SETTLEMENT_SNAPSHOT_MISSING;
- all pass → ``certified`` with ``certified_views`` = the settlement snapshot's value, verbatim.

Revisions (daily until ``revision_watch_end``): a platform value BELOW the current certified count →
``revised``, a revision entry and a clawback record with the exact negative delta (VI-05); a cumulative drop
≥ VI_STRIP_SHARE of the originally certified count opens a ``platform_stripped`` finding (VI-10). A higher
value changes nothing (views after the window are not certified).
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from typing import Optional

from reasons import dedupe, item

NUMBER, NAME, ACTOR = 2, "View Certifier", "intel_02_view_certifier"


def decide(now: datetime, rules_in_force: bool, window: Optional[dict], fallback_deadline: datetime,
           snapshot: Optional[dict], grace_open: bool, gathered: list[dict], rules: dict,
           evidence: tuple = ()) -> tuple[str, list[dict], Optional[int]]:
    if not rules_in_force:
        return ("pending" if now < fallback_deadline else "not_certified",
                [item("RULES_NOT_IN_FORCE", "no V&I rule version is in force: Andre has not approved the rules",
                      evidence, rules)], None)
    reasons = list(gathered)
    if window is None:
        reasons.append(item("ADAPTER_UNAVAILABLE", "no platform create time fetched yet", evidence, rules))
        return ("pending" if now < fallback_deadline else "not_certified"), dedupe(reasons), None
    if now < window["settle_at"]:
        reasons.append(item("NOT_YET_SETTLED", f"settles at {window['settle_at_iso']}", evidence, rules))
        return "pending", dedupe(reasons), None
    if snapshot is None:
        reasons.append(item("SETTLEMENT_SNAPSHOT_MISSING", "no views snapshot fetched in the settlement window "
                            f"({window['settle_at_iso']} + grace)", evidence, rules))
        return ("pending" if grace_open else "not_certified"), dedupe(reasons), None
    if reasons:
        return "not_certified", dedupe(reasons), None
    return "certified", [], int(snapshot["value"])


def revision(current: int, original: int, new_value: int, strip_share: Fraction) -> Optional[dict]:
    """None when nothing changes; else {old, new, delta, stripped}."""
    if new_value >= current:
        return None
    stripped = original > 0 and Fraction(original - new_value, original) >= strip_share
    return {"old_views": current, "new_views": new_value, "views_delta": new_value - current, "stripped": stripped}
