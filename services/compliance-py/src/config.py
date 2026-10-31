"""
Environment configuration (spec §F, §I). Every rule here fails closed: a
configuration the service cannot honor refuses start-up with a plain message.
"""

from __future__ import annotations

import json
import re
import os
from dataclasses import dataclass, field
from typing import Optional

from store import LOCK_NAME, DataDirBusy, DataDirLock, StoreCorrupt

CALLER_NAMES = ("onboarding", "creative_production", "finance_31", "verification_integrity", "legal_37",
                "cybersecurity_22", "people_43", "vendor_33", "scheduler",
                # ADR 0024: readers of a client's activation status (seo-py; detection-py, Revenue Recovery)
                "seo_02", "revenue_recovery")
CALLER_TOKEN_MIN = 32
UNRESOLVED_REF_MARKERS = ("${{", "{{")
# The pinned seed hash; service.SPEC_SEED_SHA256 is the same value. Seed v3 (ADR 0027: owners and the 50 Percent Rule in
# HR-14, Oct 31, 2026); seed v2 (ADR 0024: the founder decisions of Oct 30, 2026) was bc6d371a...070f5;
# seed v1, the spec's ("SHA-256 at generation"), was 4e3821d018a42be76ede1bf501004f0a8c5590327fa98ddd9f8c3f16845f584d.
PINNED_SEED_SHA256 = "02b8d0ae6fffe6589cf34a91d0c4fd7704870936b611a237f90b8b514c6c7b2b"
DEFAULT_SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "seed",
                                 "compliance_obligations_seed.json")


@dataclass
class Settings:
    service_token: str
    caller_tokens: dict[str, str] = field(default_factory=dict)
    andre_token: Optional[str] = None
    seed_path: str = DEFAULT_SEED_PATH
    seed_sha256: Optional[str] = None
    data_dir: Optional[str] = None
    data_dir_lock: Optional[object] = field(default=None, repr=False)   # store.DataDirLock (load() takes it)
    sanctions_freshness_days: int = 1
    disclosure_max_offset_s: float = 3.0
    a11y_max_age_days: int = 30
    watcher_enabled: bool = False
    site_owner_caller: str = "creative_production"
    ledger_url: Optional[str] = None
    ledger_token: Optional[str] = None
    allow_unpinned_seed: bool = False
    watcher_max_proposals_per_cycle: int = 50
    watcher_max_proposals_per_source: int = 20
    reconcile_mode: bool = False
    # ADR 0024: the in-process scheduler (internal controls every interval, the Change Watcher daily) and Legal (37)
    scheduler_enabled: bool = False
    scheduler_interval_s: int = 21600
    legal_url: Optional[str] = None
    legal_token: Optional[str] = field(default=None, repr=False)
    legal_caller_token: Optional[str] = field(default=None, repr=False)


def _int(env, name, default, lo, hi) -> int:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except ValueError:
        v = lo - 1
    if not lo <= v <= hi:
        raise RuntimeError(f"{name} must be an integer {lo}..{hi}")
    return v


def _float(env, name, default, lo, hi) -> float:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        v = float(raw)
    except ValueError:
        v = lo - 1
    if not lo <= v <= hi:
        raise RuntimeError(f"{name} must be a number {lo}..{hi}")
    return v


def _off_only(env, name) -> None:
    raw = (env.get(name) or "").strip()
    if raw not in ("", "0"):
        raise RuntimeError(f"{name}={raw!r}: this option is not built in this service (default off); refusing to start")



_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """The exclusive flock on COMPLIANCE_DATA_DIR (bug sweep C, E-5/F-3; finance-py's ``hold_data_dir``), taken once per process
    at start-up, before the log is opened, and held for the life of the process. A second process on the same
    directory refuses to start; a second service instance in this process must win the single claim
    (store.DataDirLock)."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another compliance-py process holds this data directory (flock on "
                               f"{LOCK_NAME}); refusing to start. Stop the other process first: two writers would "
                               "fork the log") from None
        except StoreCorrupt as exc:
            raise RuntimeError(f"{data_dir}: {exc}") from None
        _HELD[key] = lock
    return lock

def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ) if env is None else env
    token = env.get("COMPLIANCE_SERVICE_TOKEN")
    if not token:
        raise RuntimeError("COMPLIANCE_SERVICE_TOKEN is not set. compliance-py refuses to start without an auth token "
                           "(fail closed, not open).")
    andre = env.get("COMPLIANCE_ANDRE_APPROVAL_TOKEN") or None
    raw = env.get("COMPLIANCE_CALLER_TOKENS")
    callers: dict[str, str] = {}
    if raw:
        try:
            callers = json.loads(raw)
        except ValueError:
            callers = None  # type: ignore[assignment]
        if not isinstance(callers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in callers.items()):
            raise RuntimeError('COMPLIANCE_CALLER_TOKENS must be a JSON object {"caller_name": "token", ...}')
        for name, tok in callers.items():
            if name not in CALLER_NAMES:
                raise RuntimeError(f"COMPLIANCE_CALLER_TOKENS: unknown caller name (allowed: {', '.join(CALLER_NAMES)})")
            if any(mark in tok for mark in UNRESOLVED_REF_MARKERS):
                # ADR 0024 (legal-py's AEGIS rule): a Railway reference left unresolved is literal text, not a token
                raise RuntimeError(f"COMPLIANCE_CALLER_TOKENS: the {name} token is an unresolved Railway reference "
                                   "(set the referenced variable in Railway, then redeploy); refusing to start")
            if len(tok) < CALLER_TOKEN_MIN or not all(0x21 <= ord(c) <= 0x7e for c in tok):
                raise RuntimeError(f"COMPLIANCE_CALLER_TOKENS: the {name} token must be >= {CALLER_TOKEN_MIN} printable ASCII characters")
            if tok == token or (andre and tok == andre):
                raise RuntimeError(f"COMPLIANCE_CALLER_TOKENS: the {name} token equals the service or Andre token")
        if len(set(callers.values())) != len(callers):
            raise RuntimeError("COMPLIANCE_CALLER_TOKENS: caller tokens must all be distinct")
    for name in ("COMPLIANCE_SANCTIONS_PROVIDER", "COMPLIANCE_A11Y_PROVIDER"):
        v = (env.get(name) or "").strip().lower()
        if v not in ("", "none"):
            raise RuntimeError(f"{name}={v!r}: no provider adapter is built in this service; leave it unset "
                               "(the fail-closed stand-in answers 'unavailable')")
    _off_only(env, "COMPLIANCE_AUTO_REVERIFY_UNCHANGED")
    _off_only(env, "COMPLIANCE_WAYBACK_CAPTURE")
    site_owner = env.get("COMPLIANCE_SITE_OWNER_CALLER") or "creative_production"
    if site_owner not in CALLER_NAMES:
        raise RuntimeError("COMPLIANCE_SITE_OWNER_CALLER must be one of the caller names")
    watcher = (env.get("COMPLIANCE_WATCHER_ENABLED") or "").strip()
    if watcher not in ("", "0", "1"):
        raise RuntimeError("COMPLIANCE_WATCHER_ENABLED must be 0 or 1")
    # AEGIS N14-13: the seed hash is PINNED to the spec's. COMPLIANCE_SEED_PATH / COMPLIANCE_SEED_SHA256 can
    # point at another seed only with COMPLIANCE_ALLOW_UNPINNED_SEED=1, and then the service calls itself
    # non-production (seed_pinned: false in /health and in every ruling).
    unpinned = (env.get("COMPLIANCE_ALLOW_UNPINNED_SEED") or "").strip()
    if unpinned not in ("", "0", "1"):
        raise RuntimeError("COMPLIANCE_ALLOW_UNPINNED_SEED must be 0 or 1")
    seed_sha = env.get("COMPLIANCE_SEED_SHA256") or None
    if seed_sha is not None and seed_sha != PINNED_SEED_SHA256 and unpinned != "1":
        raise RuntimeError("COMPLIANCE_SEED_SHA256 differs from the pinned seed hash; refusing to start "
                           "(set COMPLIANCE_ALLOW_UNPINNED_SEED=1 for a NON-PRODUCTION run)")
    # AEGIS N15-1: start a log with only voidable problems against the ledger, to let Andre reconcile it
    reconcile = (env.get("COMPLIANCE_RECONCILE_MODE") or "").strip()
    if reconcile not in ("", "0", "1"):
        raise RuntimeError("COMPLIANCE_RECONCILE_MODE must be 0 or 1")
    if unpinned == "1" and seed_sha is None:
        raise RuntimeError("COMPLIANCE_ALLOW_UNPINNED_SEED=1 needs COMPLIANCE_SEED_SHA256 (the unpinned seed's own "
                           "hash, stated explicitly)")
    sched = (env.get("COMPLIANCE_SCHEDULER_ENABLED") or "").strip()
    if sched not in ("", "0", "1"):
        raise RuntimeError("COMPLIANCE_SCHEDULER_ENABLED must be 0 or 1")
    legal = [(env.get(n) or "").strip() or None for n in ("COMPLIANCE_LEGAL_URL", "COMPLIANCE_LEGAL_TOKEN",
                                                          "COMPLIANCE_LEGAL_CALLER_TOKEN")]
    if any(v and any(mark in v for mark in UNRESOLVED_REF_MARKERS) for v in legal[1:]):
        raise RuntimeError("COMPLIANCE_LEGAL_TOKEN / COMPLIANCE_LEGAL_CALLER_TOKEN holds an unresolved Railway "
                           "reference, not a token; refusing to start")
    if any(legal) and not all(legal):
        raise RuntimeError("COMPLIANCE_LEGAL_URL, COMPLIANCE_LEGAL_TOKEN and COMPLIANCE_LEGAL_CALLER_TOKEN are set "
                           "together or not at all (unset = Legal (37) not wired: client activation refused)")
    if legal[0] and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?|http://[a-z0-9-]+\.railway\.internal(:[0-9]{1,5})?|"
                                     r"http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?", legal[0].rstrip("/")):
        raise RuntimeError("COMPLIANCE_LEGAL_URL must be https://host[:port], http://<service>.railway.internal[:port] "
                           "or a loopback http URL")
    s = Settings(
        service_token=token, caller_tokens=callers, andre_token=andre,
        seed_path=env.get("COMPLIANCE_SEED_PATH") or DEFAULT_SEED_PATH,
        seed_sha256=seed_sha,
        data_dir=env.get("COMPLIANCE_DATA_DIR") or None,
        sanctions_freshness_days=_int(env, "COMPLIANCE_SANCTIONS_FRESHNESS_DAYS", 1, 1, 30),
        disclosure_max_offset_s=_float(env, "COMPLIANCE_DISCLOSURE_MAX_OFFSET_S", 3.0, 0.0, 60.0),
        a11y_max_age_days=_int(env, "COMPLIANCE_A11Y_MAX_AGE_DAYS", 30, 1, 365),
        watcher_enabled=watcher == "1", site_owner_caller=site_owner,
        ledger_url=env.get("LEDGER_SERVICE_URL") or None, ledger_token=env.get("LEDGER_SERVICE_TOKEN") or None,
        allow_unpinned_seed=unpinned == "1",
        watcher_max_proposals_per_cycle=_int(env, "COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_CYCLE", 50, 1, 10_000),
        watcher_max_proposals_per_source=_int(env, "COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_SOURCE", 20, 1, 10_000),
        reconcile_mode=reconcile == "1",
        scheduler_enabled=sched == "1",
        scheduler_interval_s=_int(env, "COMPLIANCE_SCHEDULER_INTERVAL_SECONDS", 21600, 300, 86400),
        legal_url=legal[0].rstrip("/") if legal[0] else None, legal_token=legal[1], legal_caller_token=legal[2],
    )
    # bug sweep C (E-5/F-3): the single-writer flock last, so every other refusal above leaves the directory untouched
    s.data_dir_lock = hold_data_dir(s.data_dir) if s.data_dir else None
    return s
