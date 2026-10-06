"""
Settings for New Business Development (12), read once at start (ADR 0016). Every problem refuses start (fail closed);
a missing optional piece leaves that capability visibly OFF in /nbd/v1/status, never silently weaker.

Copied from sales-py's config.py (secret files, the PII hash key, the outreach domain rules, NOT_BUILT switches) and
service-py's (the data-directory flock taken once per process, before the log is opened).
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import money
from store import LOCK_NAME, DataDirBusy, DataDirLock, StoreCorrupt

# Who may hold a caller token (ADR 0016 decision 3).
#   dashboard        Andre's console backend (with X-Andre-Approval-Token, Andre himself; never Andre by itself)
#   bizdev_agent     the agent runtime: drafts pursuits, responses, partner records, outreach
#   scheduler        the jobs
#   provider_events  the relay for the email provider's webhooks: bounces, complaints, inbound replies
#   hub              the public site backend: relays the one-click unsubscribe link
#   finance_31       Finance (31): client payment, refund and chargeback events; payout confirmations
#   compliance_38    audit export and integrity reads
KNOWN_CALLERS = ("dashboard", "bizdev_agent", "scheduler", "provider_events", "hub", "finance_31", "compliance_38")
BRANDS = ("zbm", "zbc")
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
DEAL_APPROVAL_CEILING = Decimal("10000.00")     # Andre, Oct 6: anything over $10,000 is his
NON_PRODUCTION_PII_KEY = b"bizdev-py non-production pii hash key, never in production"


class Secret:
    """A secret read from a file. ``repr``/``str`` never show it; ``reveal()`` is the one way to read it."""

    __slots__ = ("_v",)

    def __init__(self, value):
        self._v = value

    def reveal(self):
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


def _secret_file_bytes(env, name: str, max_bytes: int = 4096) -> bytes:
    """A regular file, owned by this user, mode 0600/0400, opened without following a symlink and without
    blocking on a FIFO (sales-py's helper, itself finance-py's _secret_file rules). Returns its stripped bytes."""
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


def pii_key(raw: bytes) -> bytes:
    """sales-py AEGIS S1-M5: a generated key as hex (``openssl rand -hex 32``) or base64 of at least 32 bytes; the
    DECODED bytes are the key and must not be trivially repetitive."""
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
        raise RuntimeError("NBD_PII_HASH_KEY_FILE must hold a generated key of at least 32 bytes, hex or base64 "
                           "(openssl rand -hex 32)")
    return key


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
    path = (env.get("NBD_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("NBD_DATA_DIR is required: pursuits, approvals, attestations, commissions and the "
                               "suppression list that forget everything on restart are not allowed "
                               "(NBD_NON_PRODUCTION=1 allows an in-memory run for tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("NBD_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("NBD_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("NBD_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("NBD_DATA_DIR is open to group or others; chmod 700 it")
    return path


_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """service-py's: the exclusive flock on NBD_DATA_DIR, taken once per process at start-up before the log is opened,
    held for the life of the process (the kernel releases it at exit). A second process on the same directory refuses
    to start; a second service instance in the same process must ``claim()`` it and is refused while one holds it."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another bizdev-py process holds this data directory (flock on "
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
    pii_key: Optional[Secret] = None
    outreach_domain: Optional[str] = None
    primary_domains: tuple = ()
    postal_address: Optional[str] = None
    from_local: str = "partners"
    daily_send_cap: int = 50
    queue_max_per_caller: int = 500
    deal_approval_threshold: Decimal = DEAL_APPROVAL_CEILING
    aggregation_window_days: int = 365
    unknown_ticks_before_task: int = 6
    payout_max_refusals: int = 3
    bind_addr: str = "127.0.0.1"
    port: int = 8490


def _caller_tokens(env, service_token: str) -> dict:
    raw = (env.get("NBD_CALLER_TOKENS") or "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
    except ValueError:
        raise RuntimeError("NBD_CALLER_TOKENS must be a JSON object {caller: token}") from None
    if not isinstance(tokens, dict):
        raise RuntimeError("NBD_CALLER_TOKENS must be a JSON object {caller: token}")
    for name, tok in tokens.items():
        if name not in KNOWN_CALLERS:
            raise RuntimeError(f"NBD_CALLER_TOKENS names an unknown caller {name[:40]!r}")
        if not isinstance(tok, str) or not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"NBD_CALLER_TOKENS[{name}] must be 32..512 printable ASCII characters")
    values = list(tokens.values())
    if len(set(values)) != len(values) or service_token in values:
        raise RuntimeError("NBD_CALLER_TOKENS: every caller token must be distinct and differ from the service token")
    return dict(tokens)


# sales-py's registrable-domain rule (AEGIS S1-M3): an unlisted two-level suffix is compared on its last two labels,
# which can only make the check stricter.
TWO_LEVEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.nz", "net.nz", "org.nz", "co.za", "co.jp", "ne.jp", "or.jp", "co.in", "net.in", "org.in", "com.br", "net.br",
    "com.mx", "com.ar", "com.co", "com.sg", "com.hk", "com.tw", "com.cn", "com.tr", "co.kr", "co.il", "com.my",
    "com.ph", "co.id", "com.pk", "com.ng", "com.eg", "com.sa", "co.th", "com.vn", "com.pe", "com.ec", "co.ve",
    "ca.gov", "ny.gov", "tx.gov", "fl.gov", "wa.gov", "or.gov", "nv.gov", "az.gov", "il.gov", "ma.gov",
    "lacounty.gov"})


def registrable(domain: str) -> str:
    labels = domain.lower().rstrip(".").split(".")
    n = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else 2
    return ".".join(labels[-n:])


def related_domains(a: str, b: str) -> bool:
    return registrable(a) == registrable(b)


def _domain_env(env, name: str) -> Optional[str]:
    v = (env.get(name) or "").strip().lower() or None
    if v is not None and not _HOST.fullmatch(v):
        raise RuntimeError(f"{name} must be a domain name (lowercase, no scheme, no path)")
    return v


def _domains(env) -> tuple[Optional[str], tuple]:
    """sales-py's rule: both brand domains pinned by name before anything can be sent; the outreach domain may share a
    registrable domain with none of them."""
    outreach = _domain_env(env, "NBD_OUTREACH_DOMAIN")
    zbm, zbc = _domain_env(env, "NBD_ZBM_DOMAIN"), _domain_env(env, "NBD_ZBC_DOMAIN")
    raw_primary = (env.get("NBD_PRIMARY_DOMAINS") or "").strip().lower()
    extra = tuple(p.strip() for p in raw_primary.split(",") if p.strip()) if raw_primary else ()
    for p in extra:
        if not _HOST.fullmatch(p):
            raise RuntimeError("NBD_PRIMARY_DOMAINS must be a comma-separated list of domain names")
    if zbm and zbc and related_domains(zbm, zbc):
        raise RuntimeError("NBD_ZBM_DOMAIN and NBD_ZBC_DOMAIN must be the two brands' different domains")
    primary = tuple(x for x in (zbm, zbc) if x) + extra
    if outreach is None:
        return None, primary
    if not (zbm and zbc):
        raise RuntimeError("NBD_OUTREACH_DOMAIN is set but NBD_ZBM_DOMAIN and NBD_ZBC_DOMAIN are not both set: the "
                           "service cannot prove the outreach domain is separate from both brands' own domains")
    for p in primary:
        if related_domains(outreach, p):
            raise RuntimeError("NBD_OUTREACH_DOMAIN must be a separate domain: it shares a registrable domain with a "
                               "brand domain (NBD_ZBM_DOMAIN, NBD_ZBC_DOMAIN or NBD_PRIMARY_DOMAINS)")
    return outreach, primary


def _postal(env, outreach: Optional[str]) -> Optional[str]:
    raw = (env.get("NBD_POSTAL_ADDRESS") or "").strip()
    if not raw:
        if outreach:
            raise RuntimeError("NBD_OUTREACH_DOMAIN is set but NBD_POSTAL_ADDRESS is not: every commercial email must "
                               "carry a valid physical postal address (CAN-SPAM)")
        return None
    if not 10 <= len(raw) <= 200 or not re.fullmatch(r"[\x20-\x7e]+", raw) or not re.search(r"[0-9]", raw):
        raise RuntimeError("NBD_POSTAL_ADDRESS must be a physical postal address (10..200 printable characters, with a "
                           "street or box number)")
    return raw


NOT_BUILT = {
    "NBD_EMAIL_PROVIDER": "the email send provider: none is chosen or built yet (outreach stays queued)",
    "NBD_SUBMISSION_PROVIDER": "bid / RFP / pitch submission: no portal or delivery adapter is built (approved "
                               "submissions stay queued)",
    "NBD_BID_SOURCE_PROVIDER": "bid-portal and RFP source fetching: not built, by founder decision fetched by no code here",
    "NBD_ONBOARDING_URL": "the Onboarding hand-off client: not built yet (won pursuits stay pending_delivery)",
    "NBD_FINANCE_URL": "the Finance (31) client (invoice drafts and partner payouts): not built yet (payouts stay "
                       "queued, hand-offs stay pending_delivery)",
    "NBD_LEGAL_URL": "the Legal (37) agreement client: not built yet (agreements are refused LEGAL_UNAVAILABLE)",
    "NBD_SALES_SUPPRESSION_URL": "the Sales (27) shared-suppression sync: not built yet (this list is shared across "
                                 "both brands, not yet with Sales)",
}


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("NBD_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("NBD_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "NBD_NON_PRODUCTION")
    s = Settings(service_token=service_token, caller_tokens=_caller_tokens(env, service_token),
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    for name, why in NOT_BUILT.items():
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend.")
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None
    andre = (env.get("NBD_ANDRE_APPROVAL_TOKEN") or "").strip() or None
    if andre is not None and not _PRINTABLE.fullmatch(andre):
        raise RuntimeError("NBD_ANDRE_APPROVAL_TOKEN must be 32..512 printable ASCII characters")
    s.andre_token = andre

    if env.get("NBD_PII_HASH_KEY_FILE"):
        s.pii_key = Secret(pii_key(_secret_file_bytes(env, "NBD_PII_HASH_KEY_FILE")))
    elif not non_production or s.data_dir:
        raise RuntimeError("NBD_PII_HASH_KEY_FILE is required whenever NBD_DATA_DIR is set (and always in production): "
                           "contact emails are matched, suppressed, recorded and exported as keyed hashes")
    else:
        s.pii_key = Secret(NON_PRODUCTION_PII_KEY)

    s.outreach_domain, s.primary_domains = _domains(env)
    s.postal_address = _postal(env, s.outreach_domain)
    local = (env.get("NBD_OUTREACH_FROM_LOCAL") or "partners").strip()
    if not re.fullmatch(r"[a-z][a-z0-9.-]{0,30}", local) or re.search(r"no-?reply|donotreply", local):
        raise RuntimeError("NBD_OUTREACH_FROM_LOCAL must be a plain mailbox name that accepts replies (lowercase, no "
                           "'noreply')")
    s.from_local = local
    s.daily_send_cap = _int(env, "NBD_DAILY_SEND_CAP", 50, 1, 200)
    s.queue_max_per_caller = _int(env, "NBD_QUEUE_MAX_PER_CALLER", 500, 1, 5000)
    raw = (env.get("NBD_DEAL_APPROVAL_THRESHOLD") or "10000.00").strip()
    try:
        threshold = money.parse(raw)
    except money.MoneyError:
        raise RuntimeError("NBD_DEAL_APPROVAL_THRESHOLD must be a two-decimal amount like 10000.00") from None
    if threshold > DEAL_APPROVAL_CEILING:
        raise RuntimeError("NBD_DEAL_APPROVAL_THRESHOLD may be at most 10000.00 (Andre's threshold; it can only be "
                           "lowered)")
    s.deal_approval_threshold = threshold
    s.aggregation_window_days = _int(env, "NBD_AGGREGATION_WINDOW_DAYS", 365, 90, 3650)
    # AEGIS round 2 N2 / L3: counted in job runs (ticks), never wall hours
    s.unknown_ticks_before_task = _int(env, "NBD_UNKNOWN_TICKS_BEFORE_TASK", 6, 1, 1000)
    s.payout_max_refusals = _int(env, "NBD_PAYOUT_MAX_REFUSALS", 3, 1, 20)
    s.bind_addr = (env.get("NBD_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "NBD_PORT", 8490, 1024, 65535)
    s.data_dir_lock = hold_data_dir(s.data_dir) if s.data_dir else None    # last: nothing above can fail after it
    return s
