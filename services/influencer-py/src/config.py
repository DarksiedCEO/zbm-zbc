"""
Settings for Influencer & Partnership Marketing (11), read once at start (ADR 0015). Every problem refuses start
(fail closed); a missing optional piece leaves that capability visibly OFF in /inf/v1/status, never silently weaker.
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

# Who may hold an Influencer caller token (ADR 0015 decision 2).
#   hub               the public site backend: the creator application form (with the 18+ attestation), the creator
#                     portal (tax profile, content drafts), the one-click unsubscribe page
#   dashboard         Andre's console backend (manual research prospects, verified first names); with
#                     X-Andre-Approval-Token, Andre himself
#   influencer_agent  the agent runtime: discovery imports, outreach, DM drafts, campaigns, briefs, deals, content,
#                     payout requests
#   scheduler         the jobs
#   provider_events   the relay for the send providers' and platforms' webhooks: bounces, complaints, replies, DMs
#   compliance_38     audit export, integrity and material-connection reads
KNOWN_CALLERS = ("hub", "dashboard", "influencer_agent", "scheduler", "provider_events", "compliance_38")
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
AUTO_APPROVE_CEILING = Decimal("5000.00")          # Andre, Oct 6 2026: any deal over $5,000 total goes to him


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
    blocking on a FIFO (security-py's helper, itself finance-py's _secret_file rules). Returns its stripped bytes."""
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
        raise RuntimeError("INF_PII_HASH_KEY_FILE must hold a generated key of at least 32 bytes, hex or base64 "
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
    path = (env.get("INF_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("INF_DATA_DIR is required: a suppression list, deal book and material-connection record "
                               "that forget everything on restart are not allowed (INF_NON_PRODUCTION=1 allows an "
                               "in-memory run for tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("INF_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("INF_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("INF_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("INF_DATA_DIR is open to group or others; chmod 700 it")
    return path


_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """The exclusive flock on INF_DATA_DIR (service-py's ``hold_data_dir``), taken once per process at start-up, before
    the log is opened, and held for the life of the process (the kernel releases it at exit). A second process on the
    same directory refuses to start; a second service instance in this process must win the single claim."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another influencer-py process holds this data directory (flock on "
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
    from_local: str = "creators"
    daily_send_cap: int = 50
    auto_approve_max: Decimal = AUTO_APPROVE_CEILING
    queue_max_per_caller: int = 500
    confirmation_daily_cap: int = 200
    confirmation_queue_max: int = 2000
    confirmation_new_address_percent: int = 25
    andre_review_daily_cap: int = 20
    unresolved_hold_days: int = 30
    bind_addr: str = "127.0.0.1"
    port: int = 8480


def _caller_tokens(env, service_token: str) -> dict:
    raw = (env.get("INF_CALLER_TOKENS") or "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
    except ValueError:
        raise RuntimeError("INF_CALLER_TOKENS must be a JSON object {caller: token}") from None
    if not isinstance(tokens, dict):
        raise RuntimeError("INF_CALLER_TOKENS must be a JSON object {caller: token}")
    for name, tok in tokens.items():
        if name not in KNOWN_CALLERS:
            raise RuntimeError(f"INF_CALLER_TOKENS names an unknown caller {name[:40]!r}")
        if not isinstance(tok, str) or not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"INF_CALLER_TOKENS[{name}] must be 32..512 printable ASCII characters")
    values = list(tokens.values())
    if len(set(values)) != len(values) or service_token in values:
        raise RuntimeError("INF_CALLER_TOKENS: every caller token must be distinct and differ from the service token")
    return dict(tokens)


# sales-py AEGIS S1-M3: registrable domains are compared, not suffixes. A full Public Suffix List is not vendored: an
# unlisted two-level suffix is compared on its last two labels, which can only make the check stricter.
TWO_LEVEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.nz", "net.nz", "org.nz", "co.za", "co.jp", "ne.jp", "or.jp", "co.in", "net.in", "org.in", "com.br", "net.br",
    "com.mx", "com.ar", "com.co", "com.sg", "com.hk", "com.tw", "com.cn", "com.tr", "co.kr", "co.il", "com.my",
    "com.ph", "co.id", "com.pk", "com.ng", "com.eg", "com.sa", "co.th", "com.vn", "com.pe", "com.ec", "co.ve"})


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
    """The two brand domains are pinned by name (INF_ZBM_DOMAIN, INF_ZBC_DOMAIN), both required before anything can
    be sent; INF_PRIMARY_DOMAINS may add more brand-owned domains. The outreach domain shares a registrable domain
    with none of them (sales-py S1-M3 / S2-L2)."""
    outreach = _domain_env(env, "INF_OUTREACH_DOMAIN")
    zbm, zbc = _domain_env(env, "INF_ZBM_DOMAIN"), _domain_env(env, "INF_ZBC_DOMAIN")
    raw_primary = (env.get("INF_PRIMARY_DOMAINS") or "").strip().lower()
    extra = tuple(p.strip() for p in raw_primary.split(",") if p.strip()) if raw_primary else ()
    for p in extra:
        if not _HOST.fullmatch(p):
            raise RuntimeError("INF_PRIMARY_DOMAINS must be a comma-separated list of domain names")
    if zbm and zbc and related_domains(zbm, zbc):
        raise RuntimeError("INF_ZBM_DOMAIN and INF_ZBC_DOMAIN must be the two brands' different domains")
    primary = tuple(x for x in (zbm, zbc) if x) + extra
    if outreach is None:
        return None, primary
    if not (zbm and zbc):
        raise RuntimeError("INF_OUTREACH_DOMAIN is set but INF_ZBM_DOMAIN and INF_ZBC_DOMAIN are not both set: the "
                           "service cannot prove the outreach domain is separate from both brands' own domains, so it "
                           "refuses to start")
    for p in primary:
        if related_domains(outreach, p):
            raise RuntimeError("INF_OUTREACH_DOMAIN must be a separate domain: it shares a registrable domain with a "
                               "brand domain (INF_ZBM_DOMAIN, INF_ZBC_DOMAIN or INF_PRIMARY_DOMAINS)")
    return outreach, primary


def _postal(env, outreach: Optional[str]) -> Optional[str]:
    raw = (env.get("INF_POSTAL_ADDRESS") or "").strip()
    if not raw:
        if outreach:
            raise RuntimeError("INF_OUTREACH_DOMAIN is set but INF_POSTAL_ADDRESS is not: every commercial email must "
                               "carry a valid physical postal address (CAN-SPAM)")
        return None
    if not 10 <= len(raw) <= 200 or not re.fullmatch(r"[\x20-\x7e]+", raw) or not re.search(r"[0-9]", raw):
        raise RuntimeError("INF_POSTAL_ADDRESS must be a physical postal address (10..200 printable characters, with a "
                           "street or box number)")
    return raw


NOT_BUILT = {
    "INF_EMAIL_PROVIDER": "the email send provider: none is chosen or built yet (outreach email stays queued)",
    "INF_DM_PROVIDER": "a platform DM provider (Instagram, TikTok, X, YouTube): none exists yet (approved DMs stay "
                       "queued)",
    "INF_PUBLIC_PROFILE_PROVIDER": "public-profile discovery: no source is chosen or built yet (no scraping code "
                                   "exists here)",
    "INF_PAID_DATABASE_PROVIDER": "the paid influencer-database provider: none is chosen or built yet",
    "INF_FINANCE_URL": "the Finance (31) payee and payout client: not built yet (no payee is verified, nothing is "
                       "paid)",
    "INF_LEGAL_URL": "the Legal (37) contract client: not built yet (no contract can be sent)",
    "INF_DEAL_AGGREGATE_WINDOW_DAYS": "a time window for the $5,000 per-person deal total: not built (the total is "
                                      "lifetime, AEGIS R1-H1)",
}


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("INF_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("INF_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "INF_NON_PRODUCTION")
    for name, why in NOT_BUILT.items():
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend.")
    s = Settings(service_token=service_token, caller_tokens=_caller_tokens(env, service_token),
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None
    andre = (env.get("INF_ANDRE_APPROVAL_TOKEN") or "").strip() or None
    if andre is not None and not _PRINTABLE.fullmatch(andre):
        raise RuntimeError("INF_ANDRE_APPROVAL_TOKEN must be 32..512 printable ASCII characters")
    s.andre_token = andre

    if env.get("INF_PII_HASH_KEY_FILE"):
        s.pii_key = Secret(pii_key(_secret_file_bytes(env, "INF_PII_HASH_KEY_FILE")))
    elif not non_production or s.data_dir:
        # sales-py S1-L3: a durable log is never written under the fixed test key, non-production or not
        raise RuntimeError("INF_PII_HASH_KEY_FILE is required whenever INF_DATA_DIR is set (and always in production): "
                           "emails and handles are matched, suppressed and exported as keyed hashes")
    else:
        s.pii_key = Secret(b"influencer-py non-production pii hash key, never in production")

    s.outreach_domain, s.primary_domains = _domains(env)
    s.postal_address = _postal(env, s.outreach_domain)
    local = (env.get("INF_OUTREACH_FROM_LOCAL") or "creators").strip()
    if not re.fullmatch(r"[a-z][a-z0-9.-]{0,30}", local) or re.search(r"no-?reply|donotreply", local):
        raise RuntimeError("INF_OUTREACH_FROM_LOCAL must be a plain mailbox name that accepts replies (lowercase, no "
                           "'noreply')")
    s.from_local = local
    s.daily_send_cap = _int(env, "INF_DAILY_SEND_CAP", 50, 1, 200)
    s.queue_max_per_caller = _int(env, "INF_QUEUE_MAX_PER_CALLER", 500, 1, 5000)
    # AEGIS R2-N1: confirmation mails have their own queue and daily cap, apart from outreach
    s.confirmation_daily_cap = _int(env, "INF_CONFIRMATION_DAILY_CAP", 200, 1, 1000)
    s.confirmation_queue_max = _int(env, "INF_CONFIRMATION_QUEUE_MAX", 2000, 1, 20000)
    # AEGIS round 3: the share of the confirmation cap reserved for new addresses (L3), new items a day in Andre's
    # review queue (L2, the rest go to his digest), and the days an unresolved, target-less reply hold lasts (L5)
    s.confirmation_new_address_percent = _int(env, "INF_CONFIRMATION_NEW_ADDRESS_PERCENT", 25, 0, 90)
    s.andre_review_daily_cap = _int(env, "INF_ANDRE_REVIEW_DAILY_CAP", 20, 1, 1000)
    s.unresolved_hold_days = _int(env, "INF_UNRESOLVED_HOLD_DAYS", 30, 1, 365)
    raw_max = (env.get("INF_AUTO_APPROVE_MAX") or "5000.00").strip()
    try:
        auto_max = money.parse(raw_max)
    except money.MoneyError:
        raise RuntimeError("INF_AUTO_APPROVE_MAX must be a two-decimal amount like 5000.00") from None
    if auto_max > AUTO_APPROVE_CEILING:
        raise RuntimeError("INF_AUTO_APPROVE_MAX may be at most 5000.00 (Andre's ceiling: any influencer deal over "
                           "$5,000 total goes to him)")
    s.auto_approve_max = auto_max
    s.bind_addr = (env.get("INF_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "INF_PORT", 8480, 1024, 65535)
    # the flock last: every other refusal above leaves the directory untouched
    s.data_dir_lock = hold_data_dir(s.data_dir) if s.data_dir else None
    return s
