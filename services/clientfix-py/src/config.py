"""
Settings for the Client Fix lane of Client Delivery & Operations (28), read once at start (ADR 0017). Every problem
refuses start (fail closed); a missing optional piece leaves that capability visibly OFF in /cfx/v1/status, never
silently weaker. Copied from bizdev-py's config.py (itself sales-py's / service-py's) and cut down to what this
department reads: it stores no email, phone or tax data, so it has no PII hash key.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Optional

from store import LOCK_NAME, DataDirBusy, DataDirLock, StoreCorrupt

# Who may hold a Client Fix caller token (ADR 0017 decision 3).
#   hub             the client portal backend: OAuth connection results (vault references only), revocations, client
#                   sessions, the client's quote acceptance and fix-plan approval (inside a client session)
#   dashboard       Andre's console (sees everything); with X-Andre-Approval-Token, Andre himself
#   clientfix_agent the coordinating agent runtime: jobs from findings, quotes
#   fire_team       the fire-team engineers' runtime (delivery-py's harness): proposes change sets, nothing else
#   orchestrator    Revenue Recovery (orchestrator-go / detection-py): findings intake
#   scheduler       the jobs (apply queue, re-detection, refunds, integrity)
#   finance_31      Finance (31): payment-confirmed events for a quote's invoice
#   compliance_38   audit export, evidence and integrity reads
KNOWN_CALLERS = ("hub", "dashboard", "clientfix_agent", "fire_team", "orchestrator", "scheduler", "finance_31",
                 "compliance_38")
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")


def _flag(env, name: str) -> bool:
    raw = (env.get(name) or "").strip()
    if raw not in ("", "0", "1"):
        raise RuntimeError(f"{name} must be 0 or 1")
    return raw == "1"


def _int(env, name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]{1,7}", raw) or not lo <= int(raw) <= hi:
        raise RuntimeError(f"{name} must be a whole number from {lo} to {hi}")
    return int(raw)


def _data_dir(env, non_production: bool) -> Optional[str]:
    path = (env.get("CFX_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("CFX_DATA_DIR is required: a fix lane that forgets payments, approvals, leases, frozen "
                               "resources and refunds on restart is not allowed (CFX_NON_PRODUCTION=1 allows an "
                               "in-memory run for tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("CFX_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("CFX_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("CFX_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("CFX_DATA_DIR is open to group or others; chmod 700 it")
    return path


_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """The exclusive flock on CFX_DATA_DIR (service-py's ``hold_data_dir``), taken once per process at start-up, before
    the log is opened, and held for the life of the process. A second process on the same directory refuses to start;
    a second service instance in this process must win the single claim."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another clientfix-py process holds this data directory (flock on "
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
    non_production: bool = False
    data_dir: Optional[str] = None
    data_dir_lock: Optional[object] = None
    ledger_url: Optional[str] = None
    ledger_token: Optional[str] = None
    andre_token: Optional[str] = None
    client_session_minutes: int = 30
    unknown_ticks_before_task: int = 6
    max_items_per_job: int = 50
    max_ops_per_item: int = 20
    brief_read_interval_s: int = 300
    malformed_tasks_max: int = 5
    bind_addr: str = "127.0.0.1"
    port: int = 8500


def _caller_tokens(env, service_token: str) -> dict:
    raw = (env.get("CFX_CALLER_TOKENS") or "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
    except ValueError:
        raise RuntimeError("CFX_CALLER_TOKENS must be a JSON object {caller: token}") from None
    if not isinstance(tokens, dict):
        raise RuntimeError("CFX_CALLER_TOKENS must be a JSON object {caller: token}")
    for name, tok in tokens.items():
        if name not in KNOWN_CALLERS:
            raise RuntimeError(f"CFX_CALLER_TOKENS names an unknown caller {name[:40]!r}")
        if not isinstance(tok, str) or not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"CFX_CALLER_TOKENS[{name}] must be 32..512 printable ASCII characters")
    values = list(tokens.values())
    if len(set(values)) != len(values) or service_token in values:
        raise RuntimeError("CFX_CALLER_TOKENS: every caller token must be distinct and differ from the service token")
    return dict(tokens)


# Every switch that would select something not built refuses start (ADR 0017 "Unlock list"). None of them is ever
# read as a value: the service does not pretend.
NOT_BUILT = {
    "CFX_ANTHROPIC_API_KEY_REF": "the fire teams' Claude (Anthropic API) key: Andre has not added it yet (engage "
                                 "answers 503 MODEL_NOT_WIRED; the key will live in the Cybersecurity (22) vault and "
                                 "be read by delivery-py's EgressChatModel, never by this service)",
    "CFX_DELIVERY_RUNTIME_URL": "the delivery-py fire-team runtime client (deer-flow behind our sandbox and guardrail "
                                "adapters): delivery-py has no change-set route yet",
    "CFX_CONNECTOR_TRANSPORT": "the HTTP transport to client platforms: it needs the vault and the OAuth app "
                               "credentials, none of which exist yet (every apply answers 503 CONNECTOR_NOT_WIRED "
                               "before anything is touched)",
    "CFX_VAULT_URL": "the Cybersecurity (22) vault client that resolves connection token references at call time",
    "CFX_SHOPIFY_APP_CLIENT_REF": "the Shopify Partner app (client id / secret as vault references): not created",
    "CFX_GOOGLE_OAUTH_CLIENT_REF": "the Google Cloud project's OAuth client (GA4 Admin, Tag Manager, Business "
                                   "Profile): not created",
    "CFX_GBP_API_ACCESS": "Google Business Profile API access: Google must approve the project first (0 QPM until "
                          "approved)",
    "CFX_YELP_API_KEY_REF": "the Yelp Fusion read key for verifying guided manual fixes: Andre has not chosen a Yelp "
                            "plan yet",
    "CFX_DETECTION_URL": "the Revenue Recovery re-detection client: orchestrator-go has no per-client re-scan route "
                         "(re-detection answers unknown, so nothing is ever counted fixed)",
    "CFX_FINANCE_URL": "the Finance (31) invoice and refund client: finance-py has no client-fix invoice or refund "
                       "intake (refunds stay queued after Andre approves them)",
    "CFX_SERVICE_AUTOMATIONS_URL": "Customer Service & Success (29-30) automation port: service-py has no route that "
                                   "configures a client's follow-up automations",
    "CFX_SALES_AUTOMATIONS_URL": "Sales (27) automation port: sales-py has no route that configures a client's "
                                 "follow-up automations",
    "CFX_CRM_PROVIDER": "third-party CRM connectors (HubSpot, GoHighLevel, ...): none is built",
    "CFX_WOOCOMMERCE": "WooCommerce: its app flow issues long-lived REST API keys, not OAuth tokens (Andre: official "
                       "OAuth app connections only); not built",
}


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("CFX_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("CFX_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "CFX_NON_PRODUCTION")
    for name, why in NOT_BUILT.items():
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend.")
    s = Settings(service_token=service_token, caller_tokens=_caller_tokens(env, service_token),
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None
    andre = (env.get("CFX_ANDRE_APPROVAL_TOKEN") or "").strip() or None
    if andre is not None and not _PRINTABLE.fullmatch(andre):
        raise RuntimeError("CFX_ANDRE_APPROVAL_TOKEN must be 32..512 printable ASCII characters")
    s.andre_token = andre
    s.client_session_minutes = _int(env, "CFX_CLIENT_SESSION_MINUTES", 30, 5, 240)
    s.unknown_ticks_before_task = _int(env, "CFX_UNKNOWN_TICKS_BEFORE_TASK", 6, 1, 1000)
    s.max_items_per_job = _int(env, "CFX_MAX_ITEMS_PER_JOB", 50, 1, 200)
    s.max_ops_per_item = _int(env, "CFX_MAX_OPS_PER_ITEM", 20, 1, 50)
    s.brief_read_interval_s = _int(env, "CFX_BRIEF_READ_INTERVAL_SECONDS", 300, 0, 86400)
    s.malformed_tasks_max = _int(env, "CFX_MALFORMED_TASKS_MAX", 5, 1, 1000)
    s.bind_addr = (env.get("CFX_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "CFX_PORT", 8500, 1024, 65535)
    # the flock last: every other refusal above leaves the directory untouched
    s.data_dir_lock = hold_data_dir(s.data_dir) if s.data_dir else None
    return s
