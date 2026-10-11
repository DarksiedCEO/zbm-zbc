"""
Settings for Search & Answer Intelligence (2) — SEO / AEO / GEO / LLMO — read once at start (ADR 0017). Every
problem refuses start (fail closed); a missing optional piece leaves that capability visibly OFF in /seo/v1/status,
never silently weaker.

Copied from bizdev-py's config.py (secret-free caller tokens, the data-directory flock taken once per process before
the log is opened, NOT_BUILT switches that refuse start rather than pretend).
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Optional

from store import LOCK_NAME, DataDirBusy, DataDirLock, StoreCorrupt

# Who may hold a caller token (ADR 0017 decision 3).
#   dashboard      Andre's console backend (with X-Andre-Approval-Token, Andre himself; never Andre by itself)
#   seo_agent      the agent runtime: requests audits for own properties, manages prompt sets
#   scheduler      the jobs
#   hub            the public site backend: a client's read-only view, always with a tenant token
#   finance_31     Finance (31): reads audit status for invoices it issued
#   compliance_38  audit export, evidence and integrity reads
KNOWN_CALLERS = ("dashboard", "seo_agent", "scheduler", "hub", "finance_31", "compliance_38")
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")
TENANT_ID = re.compile(r"[a-z][a-z0-9-]{1,39}")
CAPABILITY_SWITCHES = ("fetch", "render", "ai_probe", "audit", "entity_write", "prompt_sets", "logs", "schedules")
PROVIDER_SWITCHES = ("web", "openai", "anthropic", "google", "perplexity")

# Pricing, locked by Andre (spec Oct 6). Config only: nothing in this service charges, quotes or invoices; payments
# are Finance (31)'s, through Stripe. Above the top tier: "Contact us". Self-serve is not offered until the client
# dashboard launches.
PRICING = {
    "currency": "USD",
    "managed_monthly": [{"tier": "managed_1", "amount": "2000.00"}, {"tier": "managed_2", "amount": "4500.00"},
                        {"tier": "managed_3", "amount": "7500.00"}],
    "above_top_tier": "Contact us",
    "self_serve_monthly_range": {"min": "29.00", "max": "500.00", "offered": False,
                                 "condition": "only after the client dashboard launches"},
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


def _list(env, name: str, allowed: tuple) -> tuple:
    raw = (env.get(name) or "").strip()
    if not raw:
        return ()
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    bad = [x for x in items if x not in allowed]
    if bad:
        raise RuntimeError(f"{name} names an unknown switch; allowed: {', '.join(allowed)}")
    return items


def _data_dir(env, non_production: bool) -> Optional[str]:
    path = (env.get("SEO_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("SEO_DATA_DIR is required: tenants, audits, approvals and kill switches that forget "
                               "everything on restart are not allowed (SEO_NON_PRODUCTION=1 allows an in-memory run "
                               "for tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("SEO_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("SEO_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("SEO_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("SEO_DATA_DIR is open to group or others; chmod 700 it")
    return path


_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """bizdev-py's: the exclusive flock on SEO_DATA_DIR, taken once per process before the log is opened."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another seo-py process holds this data directory (flock on "
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
    killed_providers: tuple = ()
    fetch_timeout_s: int = 10
    fetch_max_bytes: int = 2 * 1024 * 1024
    fetch_max_redirects: int = 5
    audit_max_pages: int = 10
    probe_samples: int = 5
    bot_info_url: Optional[str] = None
    log_hash_key: Optional[bytes] = None
    log_max_bytes: int = 256 * 1024 * 1024
    log_retention_days: int = 90
    bot_verify_dns: bool = False
    log_max_open_ingests: int = 2
    log_max_ingests: int = 30
    log_tenant_bytes: int = 1024 * 1024 * 1024
    bot_verify_max: int = 200
    schedule_budget_runs: int = 8
    schedule_period_days: int = 30
    invoice_verification: str = "finance"
    finance_url: Optional[str] = None
    finance_token: Optional[str] = field(default=None, repr=False)
    finance_caller_token: Optional[str] = field(default=None, repr=False)
    finance_timeout_s: int = 5
    bind_addr: str = "127.0.0.1"
    port: int = 8500


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


NOT_BUILT = {
    "SEO_RENDERER": "a headless renderer: none is wired (render stays NOT_CONNECTED; raw HTML only)",
    "SEO_OPENAI_API_KEY_FILE": "the OpenAI answer-engine adapter: not built (AI-visibility probes NOT_CONNECTED)",
    "SEO_ANTHROPIC_API_KEY_FILE": "the Anthropic answer-engine adapter: not built (probes NOT_CONNECTED)",
    "SEO_GOOGLE_API_KEY_FILE": "the Google answer-engine adapter: not built (probes NOT_CONNECTED)",
    "SEO_PERPLEXITY_API_KEY_FILE": "the Perplexity answer-engine adapter: not built (probes NOT_CONNECTED)",
    "SEO_SEARCH_CONSOLE_CREDENTIALS_FILE": "Google Search Console (first-party truth): not built",
    "SEO_BING_WEBMASTER_KEY_FILE": "Bing Webmaster Tools (first-party truth): not built",
    "SEO_PROMPT_VOLUME_PROVIDER": "a prompt-volume data source for Naomi: none chosen or built",
    "SEO_ZERO_DAY_URL": "Zero-Day: a port only in Wave 1 (NOT_CONNECTED)",
    "SEO_ORCA_PUBLISH_URL": "ORCA Publish: a port only in Wave 1 (NOT_CONNECTED)",
    "SEO_CLIENTFIX_URL": "Department 28 clientfix (fix execution): not in Wave 1",
}
INVOICE_VERIFICATION_MODES = ("finance", "trust")
_FINANCE_URL = re.compile(r"https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*/?")


NON_PRODUCTION_LOG_KEY = b"seo-py non-production log ip hash key, never in production"


def _secret_file_bytes(env, name: str, max_bytes: int = 4096) -> bytes:
    """bizdev-py's rules: a regular file owned by this user, mode 0600/0400, no symlink, no FIFO."""
    path = (env.get(name) or "").strip()
    if not path or not os.path.isabs(path):
        raise RuntimeError(f"{name} must be an absolute path to a file holding the secret")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise RuntimeError(f"{name}: the file cannot be opened (missing, unreadable, or a symlink)") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"{name}: not a regular file")
        if st.st_uid != os.geteuid():
            raise RuntimeError(f"{name}: the file is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError(f"{name}: the file is readable by group or others; chmod 600 it")
        if st.st_size > max_bytes:
            raise RuntimeError(f"{name}: the file is too large")
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    return raw.strip()


def _hash_key(raw: bytes, name: str) -> bytes:
    """A generated key, hex or base64, at least 32 decoded bytes, not trivially repetitive (bizdev-py's rule)."""
    text = raw.decode("ascii", "replace").strip()
    key = b""
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}){32,512}", text):
        key = bytes.fromhex(text)
    elif re.fullmatch(r"[A-Za-z0-9+/_-]{43,1024}={0,2}", text):
        try:
            key = base64.b64decode(text.replace("-", "+").replace("_", "/") + "=" * (-len(text) % 4), validate=True)
        except ValueError:
            key = b""
    if len(key) < 32 or len(set(key)) < 8:
        raise RuntimeError(f"{name} must hold a generated key of at least 32 bytes, hex or base64 "
                           "(openssl rand -hex 32)")
    return key


def _bot_info_url(env) -> Optional[str]:
    raw = (env.get("SEO_BOT_INFO_URL") or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"https://[a-z0-9.-]{1,253}(/[\x21-\x7e]{0,200})?", raw) or any(c in raw for c in "()<>\"' "):
        raise RuntimeError("SEO_BOT_INFO_URL must be an https URL (the page that explains the crawler)")
    return raw


def _finance(env, s: "Settings") -> None:
    """ADR 0017 W3-2. ``SEO_INVOICE_VERIFICATION``: ``finance`` (the default, the safe option: a paid run needs Finance
    (31) to confirm the invoice is paid, for this tenant's Finance client, and not used before; if Finance is not
    configured or cannot answer, the run is refused unless Andre overrides) or ``trust`` (Wave 1/2 behaviour: the
    invoice id is an unchecked input; shown in /status). ``SEO_FINANCE_URL``, ``SEO_FINANCE_TOKEN`` (Finance's service
    token) and ``SEO_FINANCE_CALLER_TOKEN`` (this service's ``seo_02`` caller token at Finance) are set together or
    not at all."""
    mode = (env.get("SEO_INVOICE_VERIFICATION") or "finance").strip()
    if mode not in INVOICE_VERIFICATION_MODES:
        raise RuntimeError("SEO_INVOICE_VERIFICATION must be finance (the default) or trust")
    s.invoice_verification = mode
    vals = [(env.get(k) or "").strip() or None for k in ("SEO_FINANCE_URL", "SEO_FINANCE_TOKEN",
                                                         "SEO_FINANCE_CALLER_TOKEN")]
    if any(vals) and not all(vals):
        raise RuntimeError("SEO_FINANCE_URL, SEO_FINANCE_TOKEN and SEO_FINANCE_CALLER_TOKEN must be set together (or "
                           "none: every paid run is then refused FINANCE_NOT_CONFIGURED unless Andre overrides)")
    s.finance_timeout_s = _int(env, "SEO_FINANCE_TIMEOUT_SECONDS", 5, 1, 30)
    if not all(vals):
        return
    url, token, caller = vals
    if not _FINANCE_URL.fullmatch(url):
        raise RuntimeError("SEO_FINANCE_URL must be an http(s) base URL without credentials, query or fragment")
    for name, tok in (("SEO_FINANCE_TOKEN", token), ("SEO_FINANCE_CALLER_TOKEN", caller)):
        if not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"{name} must be 32..512 printable ASCII characters")
    ours = {s.service_token, s.andre_token, *s.caller_tokens.values(), *s.tenant_tokens.values()}
    if token == caller or token in ours or caller in ours:
        raise RuntimeError("SEO_FINANCE_TOKEN and SEO_FINANCE_CALLER_TOKEN must differ from each other and from "
                           "every token this service accepts")
    s.finance_url, s.finance_token, s.finance_caller_token = url, token, caller


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("SEO_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("SEO_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "SEO_NON_PRODUCTION")
    callers = _tokens(env, "SEO_CALLER_TOKENS", lambda k: k in KNOWN_CALLERS, "caller", service_token)
    tenants = _tokens(env, "SEO_TENANT_TOKENS", lambda k: isinstance(k, str) and bool(TENANT_ID.fullmatch(k)),
                      "tenant", service_token, callers.values())
    s = Settings(service_token=service_token, caller_tokens=callers, tenant_tokens=tenants,
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    for name, why in NOT_BUILT.items():
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend.")
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None
    andre = (env.get("SEO_ANDRE_APPROVAL_TOKEN") or "").strip() or None
    if andre is not None and not _PRINTABLE.fullmatch(andre):
        raise RuntimeError("SEO_ANDRE_APPROVAL_TOKEN must be 32..512 printable ASCII characters")
    s.andre_token = andre
    s.kill_global = _flag(env, "SEO_KILL_GLOBAL")
    s.killed_capabilities = _list(env, "SEO_KILLED_CAPABILITIES", CAPABILITY_SWITCHES)
    s.killed_providers = _list(env, "SEO_KILLED_PROVIDERS", PROVIDER_SWITCHES)
    s.fetch_timeout_s = _int(env, "SEO_FETCH_TIMEOUT_SECONDS", 10, 1, 60)
    s.fetch_max_bytes = _int(env, "SEO_FETCH_MAX_BYTES", 2 * 1024 * 1024, 64 * 1024, 10 * 1024 * 1024)
    s.fetch_max_redirects = _int(env, "SEO_FETCH_MAX_REDIRECTS", 5, 0, 10)
    s.audit_max_pages = _int(env, "SEO_AUDIT_MAX_PAGES", 10, 1, 25)
    s.probe_samples = _int(env, "SEO_PROBE_SAMPLES", 5, 3, 50)
    s.bot_info_url = _bot_info_url(env)
    if env.get("SEO_LOG_HASH_KEY_FILE"):
        s.log_hash_key = _hash_key(_secret_file_bytes(env, "SEO_LOG_HASH_KEY_FILE"), "SEO_LOG_HASH_KEY_FILE")
    elif not non_production or s.data_dir:
        raise RuntimeError("SEO_LOG_HASH_KEY_FILE is required whenever SEO_DATA_DIR is set (and always in "
                           "production): client IPs from uploaded logs are only ever kept as keyed hashes")
    else:
        s.log_hash_key = NON_PRODUCTION_LOG_KEY
    s.log_max_bytes = _int(env, "SEO_LOG_MAX_BYTES", 256 * 1024 * 1024, 1024 * 1024, 2 * 1024 * 1024 * 1024)
    s.log_retention_days = _int(env, "SEO_LOG_RETENTION_DAYS", 90, 1, 365)
    s.log_max_open_ingests = _int(env, "SEO_LOG_MAX_OPEN_INGESTS", 2, 1, 20)
    s.log_max_ingests = _int(env, "SEO_LOG_MAX_INGESTS", 30, 1, 1000)
    s.log_tenant_bytes = _int(env, "SEO_LOG_TENANT_BYTES", 1024 * 1024 * 1024, 1024 * 1024, 100 * 1024 ** 3)
    s.bot_verify_dns = _flag(env, "SEO_BOT_VERIFY_DNS")
    s.bot_verify_max = _int(env, "SEO_BOT_VERIFY_MAX", 200, 1, 5000)
    s.schedule_budget_runs = _int(env, "SEO_SCHEDULE_BUDGET_RUNS", 8, 1, 100)
    s.schedule_period_days = _int(env, "SEO_SCHEDULE_PERIOD_DAYS", 30, 1, 365)
    _finance(env, s)
    s.bind_addr = (env.get("SEO_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "SEO_PORT", 8500, 1024, 65535)
    s.data_dir_lock = hold_data_dir(s.data_dir) if s.data_dir else None    # last: nothing above can fail after it
    return s
