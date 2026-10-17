"""
Settings for Out-of-Home & DOOH (15) — read once at start (ADR 0019). Every problem refuses start (fail closed); a
missing optional piece leaves that capability visibly OFF in /ooh/v1/status, never silently weaker.

Copied from seo-py's config.py (secret-free caller tokens, the data-directory flock taken once per process before the
log is opened, NOT_BUILT switches that refuse start rather than pretend), trimmed to what this department reads.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import policy
from store import LOCK_NAME, DataDirBusy, DataDirLock, StoreCorrupt

# Who may hold a caller token (ADR 0019 decision 3).
#   dashboard      Andre's console backend (with X-Andre-Approval-Token, Andre himself; never Andre by itself)
#   ooh_agent      the agent runtime (none built in OOH-1): prepares campaigns, inventory, quotes, hold requests
#   scheduler      the integrity job
#   hub            the public site backend: a client's read-only view, always with a tenant token
#   compliance_38  evidence, export, integrity reads; may engage a kill switch
KNOWN_CALLERS = ("dashboard", "ooh_agent", "scheduler", "hub", "compliance_38")
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")
TENANT_ID = re.compile(r"[a-z][a-z0-9-]{1,39}")
CAPABILITY_SWITCHES = ("campaigns", "inventory", "prices", "funds", "purchase", "submission")
_AMOUNT = re.compile(r"(0|[1-9][0-9]{0,11})\.[0-9]{2}")

NOT_BUILT = {
    "OOH_FINANCE_URL": "the Finance (31) funds-verification client: not built (funds are verified by Andre's gate only)",
    "OOH_VENDOR_PORT_URL": "a vendor ordering port: not built (orders are submitted by Andre outside the system)",
    "OOH_ESIGN_URL": "e-signature: not built (Andre signs outside the system; the reference is recorded)",
    "OOH_INVENTORY_PROVIDER": "an inventory / DOOH supply data source: none chosen or built (OOH-2)",
}


def _flag(env, name: str) -> bool:
    raw = (env.get(name) or "").strip()
    if raw not in ("", "0", "1"):
        raise RuntimeError(f"{name} must be 0 or 1")
    return raw == "1"


def _int(env, name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]{1,9}", raw) or not lo <= int(raw) <= hi:
        raise RuntimeError(f"{name} must be a whole number from {lo} to {hi}")
    return int(raw)


def _amount(env, name: str, default: str) -> str:
    raw = (env.get(name) or "").strip() or default
    if not _AMOUNT.fullmatch(raw) or Decimal(raw) <= 0:
        raise RuntimeError(f"{name} must be a positive amount with two decimals (e.g. 10000.00)")
    return raw


def _list(env, name: str, allowed: tuple) -> tuple:
    raw = (env.get(name) or "").strip()
    if not raw:
        return ()
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    if any(x not in allowed for x in items):
        raise RuntimeError(f"{name} names an unknown switch; allowed: {', '.join(allowed)}")
    return items


def _data_dir(env, non_production: bool) -> Optional[str]:
    path = (env.get("OOH_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("OOH_DATA_DIR is required: campaigns, approvals, funds and authorizations that forget "
                               "everything on restart are not allowed (OOH_NON_PRODUCTION=1 allows an in-memory run "
                               "for tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("OOH_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("OOH_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("OOH_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("OOH_DATA_DIR is open to group or others; chmod 700 it")
    return path


_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """seo-py's: the exclusive flock on OOH_DATA_DIR, taken once per process before the log is opened."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another ooh-py process holds this data directory (flock on "
                               f"{LOCK_NAME}); refusing to start. Stop the other process first: two writers would "
                               "fork the log") from None
        except StoreCorrupt as exc:
            raise RuntimeError(f"{data_dir}: {exc}") from None
        _HELD[key] = lock
    return lock


@dataclass
class Settings:
    service_token: str
    caller_tokens: dict = field(default_factory=dict)
    tenant_tokens: dict = field(default_factory=dict)
    non_production: bool = False
    data_dir: Optional[str] = None
    data_dir_lock: Optional[object] = None
    ledger_url: Optional[str] = None
    ledger_token: Optional[str] = None
    andre_token: Optional[str] = None
    kill_global: bool = False
    killed_capabilities: tuple = ()
    max_purchase_amount: str = "10000.00"
    authority_max_days: int = 7
    inventory_max: int = 200
    bind_addr: str = "127.0.0.1"
    port: int = 8510


def _tokens(env, name: str, valid_name, what: str, service_token: str, others=()) -> dict:
    raw = (env.get(name) or "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a JSON object {{{what}: token}}") from None
    if not isinstance(tokens, dict):
        raise RuntimeError(f"{name} must be a JSON object {{{what}: token}}")
    for key, tok in tokens.items():
        if not valid_name(key):
            raise RuntimeError(f"{name} names an unknown or malformed {what}")
        if not isinstance(tok, str) or not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"{name}: every token must be 32..512 printable ASCII characters")
    values = list(tokens.values())
    if len(set(values)) != len(values) or service_token in values or set(values) & set(others):
        raise RuntimeError(f"{name}: every token must be distinct and differ from the service and caller tokens")
    return dict(tokens)


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("OOH_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("OOH_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "OOH_NON_PRODUCTION")
    callers = _tokens(env, "OOH_CALLER_TOKENS", lambda k: k in KNOWN_CALLERS, "caller", service_token)
    tenants = _tokens(env, "OOH_TENANT_TOKENS", lambda k: isinstance(k, str) and bool(TENANT_ID.fullmatch(k)),
                      "tenant", service_token, callers.values())
    s = Settings(service_token=service_token, caller_tokens=callers, tenant_tokens=tenants,
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    for name, why in NOT_BUILT.items():
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend.")
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None
    andre = (env.get("OOH_ANDRE_APPROVAL_TOKEN") or "").strip() or None
    if andre is not None and not _PRINTABLE.fullmatch(andre):
        raise RuntimeError("OOH_ANDRE_APPROVAL_TOKEN must be 32..512 printable ASCII characters")
    s.andre_token = andre
    s.kill_global = _flag(env, "OOH_KILL_GLOBAL")
    s.killed_capabilities = _list(env, "OOH_KILLED_CAPABILITIES", CAPABILITY_SWITCHES)
    s.max_purchase_amount = _amount(env, "OOH_MAX_PURCHASE_AMOUNT", policy.MAX_PURCHASE_CEILING)
    if Decimal(s.max_purchase_amount) > Decimal(policy.MAX_PURCHASE_CEILING):
        raise RuntimeError(f"OOH_MAX_PURCHASE_AMOUNT may only lower the locked ceiling of "
                           f"{policy.MAX_PURCHASE_CEILING} (founder decision); it can never raise it")
    s.authority_max_days = _int(env, "OOH_AUTHORITY_MAX_DAYS", policy.AUTHORITY_DAYS_CEILING, 1,
                                policy.AUTHORITY_DAYS_CEILING)
    s.inventory_max = _int(env, "OOH_INVENTORY_MAX_PER_CAMPAIGN", 200, 1, 2000)
    s.bind_addr = (env.get("OOH_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "OOH_PORT", 8510, 1024, 65535)
    s.data_dir_lock = hold_data_dir(s.data_dir) if s.data_dir else None    # last: nothing above can fail after it
    return s
