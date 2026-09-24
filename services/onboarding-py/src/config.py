"""
Onboarding configuration. Every OPEN item from the locked spec lives here
as configuration with a fail-closed default — never as an invented answer
buried in code (ADR 0004, "Open items").

| Open item                  | Default here                              | Why this default is the safe one |
|----------------------------|-------------------------------------------|----------------------------------|
| Deal-size threshold        | None (UNSET)                              | Unset => every deal escalates, and says so |
| Stuck window               | 48h (suggested, not confirmed)            | Spec's suggested value |
| Intake channel             | None (UNDECIDED)                          | API returns text; no channel is wired |
| Commitment cutoff          | 12:00 America/Los_Angeles                 | Locked for now, revisit later |
| P1 wording counsel-approved| False                                     | Compliance gate stays unmet until counsel signs off |
| P23 clause counsel-approved| False                                     | Same |
| 1099 thresholds            | {2026: 2000.00}                           | Any other year => "not configured, ask accountant" |
| Spanish                    | False, and True is refused at startup     | Wave 2/3; no reviewed Spanish content exists |
| Soft-resolution window     | 24h (ADR 0004 choice, not in the spec)    | One attempt, then escalate if still unresolved |
| Proving-campaign budget cap| 500.00 (ADR 0004 draft, not confirmed)    | Small live campaign before scaling |
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from typing import Mapping, Optional

from onboarding_schema.money import to_money

# 18+ is "written in stone" (locked Sep 24) — deliberately a constant, not
# configuration, so no deployment setting can lower it.
CLIPPER_MINIMUM_AGE_YEARS = 18


class ConfigError(RuntimeError):
    pass


def _parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"expected HH:MM, got {value!r}") from exc


@dataclass(frozen=True)
class OnboardingConfig:
    deal_size_threshold_usd: Optional[Decimal] = None
    stuck_window_hours: int = 48
    # After the ONE resolution attempt on a soft trigger, how long to wait
    # for it to work before escalating (ADR 0004 choice; not in the spec).
    soft_resolution_window_hours: int = 24
    intake_channel: Optional[str] = None
    commitment_cutoff_local: time = time(12, 0)
    commitment_tz: str = "America/Los_Angeles"
    today_due_local: time = time(17, 0)  # "today" commitments are due by this local time (ADR choice)
    first_thing_local: time = time(9, 0)  # "first thing tomorrow" means this local time (ADR choice)
    andre_nudge_lead_hours: int = 3
    client_warn_lead_hours: int = 1
    # Fix wave 2: an undelivered nudge to Andre is retried on later ticks,
    # at most this many attempts per nudge (ADR 0004 choice).
    andre_nudge_max_attempts: int = 3
    # An escalation briefing that did not reach Andre is retried on tick,
    # like the Promise Keeper nudge: initial push + retries, this many in all.
    escalation_push_max_attempts: int = 3
    default_quiet_start: time = time(21, 0)
    default_quiet_end: time = time(8, 0)
    stale_account_days: int = 90
    platform_fact_shelf_life_days: int = 90
    revenue_mismatch_tolerance: Decimal = Decimal("0.25")
    p1_wording_counsel_approved: bool = False
    p23_clause_counsel_approved: bool = False
    spanish_enabled: bool = False
    form_1099_thresholds_usd: Mapping[int, Decimal] = field(
        default_factory=lambda: {2026: Decimal("2000.00")}
    )
    proving_campaign_budget_cap_usd: Decimal = Decimal("500.00")
    gaps_short_list_size: int = 5

    def __post_init__(self) -> None:
        if self.spanish_enabled:
            raise ConfigError(
                "Spanish (P24) is wave 2/3, NOT launch. No reviewed Spanish content "
                "exists and legal text is never machine-translated on its own. "
                "Refusing to start with ONBOARDING_SPANISH_ENABLED on."
            )
        if self.stuck_window_hours <= 0 or self.soft_resolution_window_hours <= 0:
            raise ConfigError("stuck and soft-resolution windows must be positive")
        if self.andre_nudge_max_attempts < 1:
            raise ConfigError("andre_nudge_max_attempts must be at least 1")
        if self.escalation_push_max_attempts < 1:
            raise ConfigError("escalation_push_max_attempts must be at least 1")
        from zoneinfo import ZoneInfo  # validates the cutoff zone at startup

        try:
            ZoneInfo(self.commitment_tz)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError("ONBOARDING_COMMITMENT_TZ is not a valid IANA time zone") from exc

    def threshold_1099(self, year: int) -> Optional[Decimal]:
        return self.form_1099_thresholds_usd.get(year)


def _bool(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(env: Mapping[str, str] | None = None) -> OnboardingConfig:
    env = os.environ if env is None else env
    kwargs: dict = {}
    if env.get("ONBOARDING_DEAL_SIZE_THRESHOLD_USD"):
        kwargs["deal_size_threshold_usd"] = to_money(env["ONBOARDING_DEAL_SIZE_THRESHOLD_USD"])
    if env.get("ONBOARDING_STUCK_WINDOW_HOURS"):
        kwargs["stuck_window_hours"] = int(env["ONBOARDING_STUCK_WINDOW_HOURS"])
    if env.get("ONBOARDING_SOFT_RESOLUTION_WINDOW_HOURS"):
        kwargs["soft_resolution_window_hours"] = int(env["ONBOARDING_SOFT_RESOLUTION_WINDOW_HOURS"])
    if env.get("ONBOARDING_INTAKE_CHANNEL"):
        kwargs["intake_channel"] = env["ONBOARDING_INTAKE_CHANNEL"]
    if env.get("ONBOARDING_COMMITMENT_CUTOFF"):
        kwargs["commitment_cutoff_local"] = _parse_hhmm(env["ONBOARDING_COMMITMENT_CUTOFF"])
    if env.get("ONBOARDING_COMMITMENT_TZ"):
        kwargs["commitment_tz"] = env["ONBOARDING_COMMITMENT_TZ"]
    if env.get("ONBOARDING_1099_THRESHOLDS"):
        raw = json.loads(env["ONBOARDING_1099_THRESHOLDS"])
        kwargs["form_1099_thresholds_usd"] = {int(k): to_money(v) for k, v in raw.items()}
    kwargs["p1_wording_counsel_approved"] = _bool(env.get("ONBOARDING_P1_WORDING_COUNSEL_APPROVED"))
    kwargs["p23_clause_counsel_approved"] = _bool(env.get("ONBOARDING_P23_CLAUSE_COUNSEL_APPROVED"))
    kwargs["spanish_enabled"] = _bool(env.get("ONBOARDING_SPANISH_ENABLED"))
    return OnboardingConfig(**kwargs)
