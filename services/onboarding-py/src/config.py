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
    # Fix wave 4 (R1): hard input caps, enforced before any scanning.
    max_body_bytes: int = 1_048_576  # whole request body (Content-Length and while streaming) -> 413
    max_request_target_bytes: int = 8192  # path + query string -> 414
    # CPU budget for the credential checks of one request body (fix wave 5,
    # NEW-2): the request thread's OWN CPU time (time.thread_time), never
    # wall-clock time, so waiting for the GIL behind other requests costs
    # nothing. Budget = scan_budget_seconds + scan_cpu_ms_per_kb * body KB.
    # Derivation: the scanners are linear; the worst measured cost on the
    # dev box was ~1.8 ms of CPU per KB (hostile "password is x" text), benign
    # bodies 1.0-1.3 ms/KB; 10 ms/KB is ~5x headroom (a 1 MiB body gets
    # ~11 s). A body that cannot be checked within it is refused (422).
    scan_budget_seconds: float = 1.0
    scan_cpu_ms_per_kb: float = 10.0
    # Scanning concurrency is a WEIGHTED budget (fix wave 6, N4; it was a
    # step at 64 KiB, so 40 concurrent 58 KB bodies bypassed it and light
    # requests waited seconds for a thread and the GIL): every request body
    # takes max(body bytes, scan_min_cost_bytes), capped at the whole budget,
    # out of scan_inflight_bytes while it is validated and scanned, so many
    # medium bodies are throttled exactly like one large one. The defaults
    # are set by measurement (tests/test_fix_wave6.py, real uvicorn, 40
    # concurrent clients): scanning is CPU-bound under one GIL, so N scans
    # at once add no throughput, and EVERY extra CPU-bound thread lengthens
    # the event loop's wait for the GIL — 40x16 KB bodies at 4 concurrent
    # scans: /health p50 300 ms, light GET p50 790 ms; at 2: 320 / 700 ms;
    # at 1: 16 / 70 ms. So the minimum cost equals the budget: one scan at
    # a time, whatever the size (a 64 KiB body is ~100 ms of CPU). Raising
    # scan_inflight_bytes above scan_min_cost_bytes admits several bodies
    # at once; on CPython that measurably slows light requests, so do it
    # only on a runtime without a GIL. At most scan_max_waiting requests
    # wait for budget, each for at most scan_wait_seconds (budget goes to
    # the oldest waiter that fits). Busy -> 503 + Retry-After ("nothing was
    # done, retry"), never a 422.
    # These four describe the LARGE lane. Fix wave 7 (NEW-5): with the
    # minimum cost equal to the budget every body waited its turn behind
    # every other, so one client's 416 KB bodies back-to-back put other
    # clients' 40-byte messages at p50 294 ms (4 uploaders: 1.6 s; 12: 6 s),
    # and 16 queued large bodies answered a tiny message 503. A body of at
    # most scan_small_body_bytes (its scan is ~1 ms) now takes a SMALL lane
    # instead: scan_small_inflight of them at a time, at most
    # scan_small_max_waiting waiting (its own queue, so a flood of large
    # bodies cannot fill it), the same scan_wait_seconds. A small body never
    # waits for the large lane; large bodies stay serialized. 0 disables
    # the small lane (every body is large). Waiters block a threadpool
    # thread each: 1+16 large and 1+8 small leave 14 of the 40 threads for
    # light requests.
    scan_inflight_bytes: int = 65_536
    scan_min_cost_bytes: int = 65_536
    scan_max_waiting: int = 16
    scan_wait_seconds: float = 30.0
    scan_small_body_bytes: int = 16_384
    scan_small_inflight: int = 1
    scan_small_max_waiting: int = 8
    # Intake facts per client are bounded (fix wave 6, wave-5 leftover: they
    # grew without a cap and the profile was rebuilt over all of them): a
    # request that would take a client past max_facts_per_client is refused
    # (409, nothing stored), and each field keeps only its latest
    # facts_history_per_field observations, so a profile rebuild is O(cap).
    max_facts_per_client: int = 2000
    facts_history_per_field: int = 20
    # A request body must arrive within this many seconds (NEW-3 sweep) -> 408.
    body_read_timeout_seconds: float = 30.0
    # Stable instance component of every derived event id (ADR 0004, "Event
    # ids"): a restarted process with the same id derives the same ids for
    # the same first operations, so its retries dedupe at the ledger.
    instance_id: str = "onboarding-1"

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
        if self.max_body_bytes < 1 or self.max_request_target_bytes < 1 or self.scan_budget_seconds <= 0 \
                or self.scan_cpu_ms_per_kb < 0:
            raise ConfigError("input caps and the scan budget must be positive")
        if self.scan_inflight_bytes < 1 or self.scan_min_cost_bytes < 1 or self.scan_max_waiting < 0 \
                or self.scan_wait_seconds <= 0 or self.body_read_timeout_seconds <= 0:
            raise ConfigError("scan admission limits and the body read timeout must be positive "
                              "(scan_max_waiting may be 0)")
        if self.scan_small_body_bytes < 0 or self.scan_small_inflight < 1 or self.scan_small_max_waiting < 0:
            raise ConfigError("scan_small_body_bytes and scan_small_max_waiting must be 0 or more and "
                              "scan_small_inflight at least 1")
        if self.max_facts_per_client < 1 or self.facts_history_per_field < 1:
            raise ConfigError("facts caps must be positive")
        if not self.instance_id or not all(ch.isascii() and (ch.isalnum() or ch in "._:-") for ch in self.instance_id) \
                or len(self.instance_id) > 64:
            raise ConfigError("ONBOARDING_INSTANCE_ID must be 1-64 characters of [A-Za-z0-9._:-]")
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
    if env.get("ONBOARDING_MAX_BODY_BYTES"):
        kwargs["max_body_bytes"] = int(env["ONBOARDING_MAX_BODY_BYTES"])
    if env.get("ONBOARDING_MAX_REQUEST_TARGET_BYTES"):
        kwargs["max_request_target_bytes"] = int(env["ONBOARDING_MAX_REQUEST_TARGET_BYTES"])
    if env.get("ONBOARDING_SCAN_BUDGET_SECONDS"):
        kwargs["scan_budget_seconds"] = float(env["ONBOARDING_SCAN_BUDGET_SECONDS"])
    for var, key, conv in (("ONBOARDING_SCAN_CPU_MS_PER_KB", "scan_cpu_ms_per_kb", float),
                           ("ONBOARDING_SCAN_INFLIGHT_BYTES", "scan_inflight_bytes", int),
                           ("ONBOARDING_SCAN_MIN_COST_BYTES", "scan_min_cost_bytes", int),
                           ("ONBOARDING_SCAN_MAX_WAITING", "scan_max_waiting", int),
                           ("ONBOARDING_SCAN_WAIT_SECONDS", "scan_wait_seconds", float),
                           ("ONBOARDING_SCAN_SMALL_BODY_BYTES", "scan_small_body_bytes", int),
                           ("ONBOARDING_SCAN_SMALL_INFLIGHT", "scan_small_inflight", int),
                           ("ONBOARDING_SCAN_SMALL_MAX_WAITING", "scan_small_max_waiting", int),
                           ("ONBOARDING_MAX_FACTS_PER_CLIENT", "max_facts_per_client", int),
                           ("ONBOARDING_FACTS_HISTORY_PER_FIELD", "facts_history_per_field", int),
                           ("ONBOARDING_BODY_READ_TIMEOUT_SECONDS", "body_read_timeout_seconds", float)):
        if env.get(var):
            kwargs[key] = conv(env[var])
    if env.get("ONBOARDING_INSTANCE_ID"):
        kwargs["instance_id"] = env["ONBOARDING_INSTANCE_ID"]
    return OnboardingConfig(**kwargs)
