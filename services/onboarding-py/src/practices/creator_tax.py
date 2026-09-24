"""
P8 — W-9 at signup and 1099 threshold tracking for ZBC clippers.

- The W-9 itself (with the tax identifier) is NOT stored by Onboarding:
  only the fact that it was received. Minimum data; the form lives with
  ZBC payouts/tax, which is not built.
- A payout account may not be activated without a W-9 on file.
- 1099 threshold is CONFIGURATION (OnboardingConfig.form_1099_thresholds_usd):
  $2,000.00 for 2026 payments; from 2027 it is inflation-indexed and must
  be confirmed with the accountant. A year with no configured threshold
  fails closed: treated as reportable, and says the threshold is not set.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Optional

from onboarding_schema import money_str


def payout_activation_allowed(w9_on_file: bool) -> tuple[bool, str]:
    if not w9_on_file:
        return False, "W-9 not on file: payout account cannot be activated (P8)"
    return True, "W-9 on file"


def form_1099_status(year: int, paid_to_date: Decimal, thresholds: Mapping[int, Decimal]) -> dict:
    threshold: Optional[Decimal] = thresholds.get(year)
    if threshold is None:
        return {
            "year": year,
            "paid_to_date_usd": money_str(paid_to_date),
            "threshold_usd": None,
            "form_1099_required": True,
            "detail": f"no 1099 threshold configured for {year} (inflation-indexed from 2027; confirm with the accountant); treated as reportable until set",
        }
    return {
        "year": year,
        "paid_to_date_usd": money_str(paid_to_date),
        "threshold_usd": money_str(threshold),
        "form_1099_required": paid_to_date >= threshold,
        "detail": "at or above the configured threshold" if paid_to_date >= threshold else "below the configured threshold",
    }
