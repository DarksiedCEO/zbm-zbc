"""
Environment configuration (spec §E, §H). Every rule here fails closed: a
configuration the service cannot honor refuses start-up with a plain message.

Three kinds of setting (ADR 0008 choice 1):
- identities and wiring (tokens, the identity HMAC key, the ledger, the
  thin clients, the data directory) — validated, never defaulted to "open";
- operational switches that may only NARROW what the service does
  (``CN_CHANNELS`` may drop channels, never add SMS/Reddit/X);
- values the spec lists in §H that are RULE PARAMETERS in the Andre-approved
  register (tier thresholds, caps, appeal window and SLA, S2 length, rate
  notice, quiet window, retention, the admission connection requirement).
  Founder-locked §0.1.5 says only Andre changes a rule, so the environment
  may only restate the pinned seed default for these; any other value
  refuses start-up and names the rule to propose a change to.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from store import LOCK_NAME, DataDirBusy, DataDirLock, StoreCorrupt

CALLER_NAMES = ("hub", "onboarding", "creative_production", "verification_integrity", "finance_31", "compliance_38",
                "scheduler")
TOKEN_MIN = 32
# SHA-256 of seed/cn_rules_seed.json as generated for this build (ADR 0008 "Seed"); checked at every start.
PINNED_SEED_SHA256 = "4ae4553c6f7ad8bd794cac1c01bb7a6bb9eb8174c8fb7a30d8da44e2c4aa4af4"
DEFAULT_SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "seed", "cn_rules_seed.json")
DEFAULT_CHANNELS = ("discord_server_post", "email_opt_in", "inbound_form", "referral")
_DELEGATE_NAME = re.compile(r"[a-z0-9_]{1,32}")

# §H values that are rule parameters: env name -> (rule id, dotted parameter path, parser)
RULE_BACKED = {
    "CN_TIER_T1_MIN_CERTIFIED_CLIPS": ("CN-10", "t1.min_certified_clips", int),
    "CN_TIER_T1_MIN_DAYS": ("CN-10", "t1.min_days_since_admission", int),
    "CN_TIER_T1_MAX_ACTIVE_S1": ("CN-10", "t1.max_active_s1", int),
    "CN_TIER_T2_MIN_CERTIFIED_CLIPS": ("CN-10", "t2.min_certified_clips", int),
    "CN_TIER_T2_MIN_CAMPAIGNS": ("CN-10", "t2.min_campaigns", int),
    "CN_TIER_T2_MIN_MEDIAN_VIEWS": ("CN-10", "t2.min_median_certified_views", int),
    "CN_TIER_T2_MIN_DAYS": ("CN-10", "t2.min_days_since_admission", int),
    "CN_TIER_T2_NO_UPHELD_STRIKE_DAYS": ("CN-10", "t2.no_upheld_strike_days", int),
    "CN_MAX_ENROLMENTS_T0": ("CN-10", "max_active_enrolments.T0", int),
    "CN_MAX_ENROLMENTS_T1": ("CN-10", "max_active_enrolments.T1", int),
    "CN_MAX_ENROLMENTS_T2": ("CN-10", "max_active_enrolments.T2", int),
    "CN_MAX_ENROLMENTS_T3": ("CN-10", "max_active_enrolments.T3", int),
    "CN_TIER_PLATFORM_ANCHORS": ("CN-10", "platform_anchors", lambda v: {"0": False, "1": True}[v]),
    "CN_APPEAL_WINDOW_DAYS": ("CN-18", "appeal_window_days", int),
    "CN_APPEAL_SLA_BUSINESS_DAYS": ("CN-18", "sla_business_days", int),
    "CN_S2_SUSPENSION_DAYS": ("CN-19", "table.S2.days", int),
    "CN_RATE_NOTICE_DAYS": ("CN-17", "rate_notice_days", int),
    "CN_QUIET_WINDOW": ("CN-15", "quiet_window", str),
    "CN_POST_EXIT_RETENTION_DAYS": ("CN-21", "post_exit_retention_days", int),
    "CN_AGREEMENT_RETENTION_DAYS": ("CN-21", "agreement_retention_days", int),
    "CN_ADMISSION_REQUIRES_CONNECTION": ("CN-06", "required", lambda v: {"0": False, "1": True}[v]),
}
# Ports with no adapter in this build: asking for one refuses start-up (the stand-in stays).
NOT_BUILT_WIRING = ("CN_FINANCE_URL", "CN_LEGAL_URL", "CN_PEOPLE_URL", "CN_HUB_URL", "CN_PUSH_URL")


@dataclass
class Client:
    url: str
    service_token: str
    caller_token: Optional[str] = None
    accept_unpinned: bool = False


@dataclass
class Settings:
    service_token: str
    identity_hmac_key: str
    caller_tokens: dict[str, str] = field(default_factory=dict)
    delegate_tokens: dict[str, str] = field(default_factory=dict)
    andre_token: Optional[str] = None
    seed_path: str = DEFAULT_SEED_PATH
    seed_sha256: Optional[str] = None
    allow_unpinned_seed: bool = False
    data_dir: Optional[str] = None
    data_dir_lock: Optional[object] = field(default=None, repr=False)   # store.DataDirLock (load() takes it)
    ledger_url: Optional[str] = None
    ledger_token: Optional[str] = None
    reconcile_mode: bool = False
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    postal_address: Optional[str] = None
    opt_out_url: Optional[str] = None
    rule_env: dict[str, str] = field(default_factory=dict)   # §H rule-backed values restated in the env
    vi: Optional[Client] = None
    compliance: Optional[Client] = None
    creative: Optional[Client] = None


def _printable(tok: str) -> bool:
    return all(0x21 <= ord(c) <= 0x7e for c in tok)


def _flag(env, name) -> bool:
    raw = (env.get(name) or "").strip()
    if raw not in ("", "0", "1"):
        raise RuntimeError(f"{name} must be 0 or 1")
    return raw == "1"


def _tokens(env, name, allowed_names=None) -> dict[str, str]:
    raw = env.get(name)
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except ValueError:
        out = None
    if not isinstance(out, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in out.items()):
        raise RuntimeError(f'{name} must be a JSON object {{"name": "token", ...}}')
    for n, tok in out.items():
        if allowed_names is not None and n not in allowed_names:
            raise RuntimeError(f"{name}: unknown name (allowed: {', '.join(allowed_names)})")
        if allowed_names is None and not _DELEGATE_NAME.fullmatch(n):
            raise RuntimeError(f"{name}: names are 1-32 characters of [a-z0-9_]")
        if len(tok) < TOKEN_MIN or not _printable(tok):
            raise RuntimeError(f"{name}: the {n} token must be >= {TOKEN_MIN} printable ASCII characters")
    return out


def _client(env, prefix: str, needs_caller: bool, unpinned_flag: Optional[str]) -> Optional[Client]:
    names = [f"{prefix}_URL", f"{prefix}_SERVICE_TOKEN"] + ([f"{prefix}_CALLER_TOKEN"] if needs_caller else [])
    vals = [env.get(n) or None for n in names]
    if not any(vals):
        return None
    if not all(vals):
        raise RuntimeError(f"{', '.join(names)} must be set together (a partly configured client cannot be honored; "
                           "unset all of them to keep the fail-closed stand-in)")
    url = vals[0]
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?", url):
        raise RuntimeError(f"{prefix}_URL is not a plain http(s) URL")
    return Client(url, vals[1], vals[2] if needs_caller else None,
                  _flag(env, unpinned_flag) if unpinned_flag else False)



_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """The exclusive flock on CN_DATA_DIR (bug sweep C, E-5/F-3; finance-py's ``hold_data_dir``), taken once per process
    at start-up, before the log is opened, and held for the life of the process. A second process on the same
    directory refuses to start; a second service instance in this process must win the single claim
    (store.DataDirLock)."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another clipper-network-py process holds this data directory (flock on "
                               f"{LOCK_NAME}); refusing to start. Stop the other process first: two writers would "
                               "fork the log") from None
        except StoreCorrupt as exc:
            raise RuntimeError(f"{data_dir}: {exc}") from None
        _HELD[key] = lock
    return lock

def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ) if env is None else env
    token = env.get("CN_SERVICE_TOKEN")
    if not token:
        raise RuntimeError("CN_SERVICE_TOKEN is not set. clipper-network-py refuses to start without an auth token "
                           "(fail closed, not open).")
    key = env.get("CN_IDENTITY_HMAC_KEY") or ""
    if len(key) < TOKEN_MIN or not _printable(key):
        raise RuntimeError(f"CN_IDENTITY_HMAC_KEY must be set (>= {TOKEN_MIN} printable ASCII): without it the service "
                           "cannot enforce one identity per person (CN-03) or keep contact data out of its log")
    andre = env.get("CN_ANDRE_APPROVAL_TOKEN") or None
    callers = _tokens(env, "CN_CALLER_TOKENS", CALLER_NAMES)
    delegates = _tokens(env, "CN_DELEGATE_TOKENS")
    every = [token, key] + ([andre] if andre else []) + list(callers.values()) + list(delegates.values())
    if len(set(every)) != len(every):
        raise RuntimeError("the service token, identity HMAC key, Andre token, caller tokens and delegate tokens must "
                           "all be distinct")
    for name in NOT_BUILT_WIRING:
        if env.get(name):
            raise RuntimeError(f"{name}: no adapter for this department is built in clipper-network-py; leave it unset "
                               "(the fail-closed stand-in answers 'not allowed yet')")
    provider = (env.get("CN_MESSAGE_PROVIDER") or "").strip().lower()
    if provider not in ("", "none"):
        raise RuntimeError(f"CN_MESSAGE_PROVIDER={provider!r}: no messaging adapter is built; leave it unset "
                           "(messages are recorded as not delivered)")
    raw_ch = env.get("CN_CHANNELS")
    channels = DEFAULT_CHANNELS
    if raw_ch:
        asked = tuple(sorted({c.strip() for c in raw_ch.split(",") if c.strip()}))
        extra = [c for c in asked if c not in DEFAULT_CHANNELS]
        if extra:
            raise RuntimeError(f"CN_CHANNELS may only narrow the default channels {', '.join(DEFAULT_CHANNELS)}; "
                               f"refused: {', '.join(extra)} (SMS, Reddit and X are off under CN-09 / counsel holds)")
        channels = asked
    rule_env = {n: env[n] for n in RULE_BACKED if env.get(n) not in (None, "")}
    postal = env.get("CN_POSTAL_ADDRESS") or None
    if postal is not None and (len(postal) > 200 or not all(0x20 <= ord(c) <= 0x7e for c in postal)):
        raise RuntimeError("CN_POSTAL_ADDRESS must be <= 200 printable ASCII characters")
    opt_out = env.get("CN_OPT_OUT_URL") or None
    if opt_out is not None and not re.fullmatch(r"https://[A-Za-z0-9.\-]+(/[A-Za-z0-9._~/?=&%-]*)?", opt_out):
        raise RuntimeError("CN_OPT_OUT_URL must be a plain https URL")
    unpinned = _flag(env, "CN_ALLOW_UNPINNED_SEED")
    seed_sha = env.get("CN_RULES_SEED_SHA256") or None
    if seed_sha is not None and seed_sha != PINNED_SEED_SHA256 and not unpinned:
        raise RuntimeError("CN_RULES_SEED_SHA256 differs from the pinned seed hash; refusing to start "
                           "(set CN_ALLOW_UNPINNED_SEED=1 for a NON-PRODUCTION run)")
    if unpinned and seed_sha is None:
        raise RuntimeError("CN_ALLOW_UNPINNED_SEED=1 needs CN_RULES_SEED_SHA256 (the unpinned seed's own hash, "
                           "stated explicitly)")
    seed_path = env.get("CN_RULES_SEED_PATH") or DEFAULT_SEED_PATH
    if seed_path != DEFAULT_SEED_PATH and not unpinned:
        raise RuntimeError("CN_RULES_SEED_PATH names another seed: allowed only with CN_ALLOW_UNPINNED_SEED=1")
    s = Settings(
        service_token=token, identity_hmac_key=key, caller_tokens=callers, delegate_tokens=delegates, andre_token=andre,
        seed_path=seed_path, seed_sha256=seed_sha, allow_unpinned_seed=unpinned,
        data_dir=env.get("CN_DATA_DIR") or None,
        ledger_url=env.get("LEDGER_SERVICE_URL") or None, ledger_token=env.get("LEDGER_SERVICE_TOKEN") or None,
        reconcile_mode=_flag(env, "CN_RECONCILE_MODE"), channels=channels, postal_address=postal, opt_out_url=opt_out,
        rule_env=rule_env,
        vi=_client(env, "CN_VI", True, "CN_VI_ACCEPT_UNPINNED"),
        compliance=_client(env, "CN_COMPLIANCE", True, "CN_COMPLIANCE_ACCEPT_UNPINNED"),
        creative=_client(env, "CN_CREATIVE", False, None),
    )
    # bug sweep C (E-5/F-3): the single-writer flock last, so every other refusal above leaves the directory untouched
    s.data_dir_lock = hold_data_dir(s.data_dir) if s.data_dir else None
    return s


def check_rule_env(rule_env: dict[str, str], seed_rules: dict[str, dict]) -> None:
    """§H values restated in the env must equal the pinned seed's parameter (a no-op assertion); anything
    else refuses start-up: the value is a rule parameter and only Andre changes a rule (§0.1.5)."""
    for name, raw in rule_env.items():
        rule_id, path, parse = RULE_BACKED[name]
        try:
            value = parse(raw.strip())
        except (ValueError, KeyError):
            raise RuntimeError(f"{name}={raw!r} is not a valid value") from None
        cur = seed_rules[rule_id]["parameters"]
        for part in path.split("."):
            cur = cur[part]
        if value != cur:
            raise RuntimeError(f"{name}={raw!r}: this value is a parameter of rule {rule_id} ({path} = {cur!r} in the "
                               "pinned seed); only Andre changes a rule — propose it with POST /cn/v1/rules/proposals "
                               "instead of the environment")
