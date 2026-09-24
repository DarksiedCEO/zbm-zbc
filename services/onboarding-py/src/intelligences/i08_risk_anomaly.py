"""
Intelligence 8 — Risk and Anomaly.

Decides: soft trigger, hard stop, or nothing — from fraud/ban signals and
from numbers that don't match what the client said. It spots the
unexpected; Contract (14) and Compliance (15) enforce the known. Kept
separate on purpose.

Rules:
- HARD STOP signals (work stops, Andre is escalated immediately):
  account_suspended, payment_fraud_confirmed, policy_violation_active.
- SOFT signals (one resolution attempt, then escalate): chargeback_rate_high,
  disapproved_ads_spike, sudden_spend_spike, unusual_login_location,
  and any signal name this module does not recognise (an unknown signal is
  never silently ignored).
- Numbers vs client claims: if the client's stated monthly revenue and the
  account-pulled figure differ by more than the tolerance (default 25% of
  the larger), that is a SOFT trigger ("numbers don't match").
- Prompt-injection flags are NOT an input here. Hostile client content is
  logged and forwarded as an anomaly event, but it cannot change a ruling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ._status import PHASE1_STATUS

NUMBER = 8
NAME = "Risk and Anomaly"
PHASE = 1
STATUS = PHASE1_STATUS

HARD_SIGNALS = {"account_suspended", "payment_fraud_confirmed", "policy_violation_active"}
SOFT_SIGNALS = {"chargeback_rate_high", "disapproved_ads_spike", "sudden_spend_spike", "unusual_login_location"}


@dataclass(frozen=True)
class RiskRuling:
    kind: str  # "nothing" | "soft_trigger" | "hard_stop"
    reasons: tuple[str, ...] = field(default_factory=tuple)


def assess(
    signals: list[str],
    client_stated_monthly_revenue: Optional[Decimal],
    observed_monthly_revenue: Optional[Decimal],
    tolerance: Decimal = Decimal("0.25"),
) -> RiskRuling:
    hard, soft = [], []
    for s in signals:
        if s in HARD_SIGNALS:
            hard.append(f"hard signal: {s}")
        elif s in SOFT_SIGNALS:
            soft.append(f"soft signal: {s}")
        else:
            soft.append(f"unrecognised signal (not ignored): {s[:64]}")
    if client_stated_monthly_revenue is not None and observed_monthly_revenue is not None:
        larger = max(client_stated_monthly_revenue, observed_monthly_revenue)
        if larger > 0:
            diff = abs(client_stated_monthly_revenue - observed_monthly_revenue) / larger
            if diff > tolerance:
                soft.append(
                    f"numbers don't match: client said {client_stated_monthly_revenue:.2f}/month, "
                    f"account shows {observed_monthly_revenue:.2f}/month ({(diff * 100):.0f}% apart, tolerance {(tolerance * 100):.0f}%)"
                )
    if hard:
        return RiskRuling("hard_stop", tuple(hard + soft))
    if soft:
        return RiskRuling("soft_trigger", tuple(soft))
    return RiskRuling("nothing", ())
