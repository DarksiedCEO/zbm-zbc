"""
Settings for Lead Generation & Opportunity Intelligence (26) + Sales (27), read once at start (ADR 0013). Every
problem refuses start (fail closed); a missing optional piece leaves that capability visibly OFF in
/sales/v1/status, never silently weaker.
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

# Who may hold a Sales caller token (ADR 0013 decision 2).
#   hub              the public site backend: site forms, the ZBC campaign inquiry form, the unsubscribe page, consent
#                    captured on a form
#   onboarding       inquiries that reach Onboarding first
#   detection        free Revenue Recovery scan results
#   dashboard        Andre's console backend (referrals and partners; with X-Andre-Approval-Token, Andre himself)
#   scheduler        the jobs
#   sales_agent      the agent runtime that works the pipeline: outreach, proposals, stages
#   provider_events  the relay for the send providers' webhooks: bounces, complaints, inbound replies
#   compliance_38    audit export and integrity reads
KNOWN_CALLERS = ("hub", "onboarding", "detection", "dashboard", "scheduler", "sales_agent", "provider_events",
                 "compliance_38")
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
AUTO_APPROVE_CEILING = Decimal("10000.00")
DEFAULT_WARMUP = (20, 30, 40, 60, 80, 100, 150, 200)


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
    """AEGIS S1-M5: the key file holds a generated key as hex (``openssl rand -hex 32``) or base64 (``openssl rand
    -base64 32``) of at least 32 bytes; the DECODED bytes are the key and must not be trivially repetitive."""
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
        raise RuntimeError("SALES_PII_HASH_KEY_FILE must hold a generated key of at least 32 bytes, hex or base64 "
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
    path = (env.get("SALES_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("SALES_DATA_DIR is required: a pipeline, consent registry and suppression list that "
                               "forget everything on restart are not allowed (SALES_NON_PRODUCTION=1 allows an "
                               "in-memory run for tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("SALES_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("SALES_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("SALES_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("SALES_DATA_DIR is open to group or others; chmod 700 it")
    return path


@dataclass
class Settings:
    service_token: str
    caller_tokens: dict = field(default_factory=dict)
    non_production: bool = False
    data_dir: Optional[str] = None
    ledger_url: Optional[str] = None
    ledger_token: Optional[str] = None
    andre_token: Optional[str] = None
    pii_key: Optional[Secret] = None
    outreach_domain: Optional[str] = None
    primary_domains: tuple = ()
    postal_address: Optional[str] = None
    from_local: str = "hello"
    warmup: tuple = DEFAULT_WARMUP
    daily_send_cap: int = 200
    auto_approve_max: Decimal = AUTO_APPROVE_CEILING
    stale_lead_days: int = 30
    bind_addr: str = "127.0.0.1"
    port: int = 8450


def _caller_tokens(env, service_token: str) -> dict:
    raw = (env.get("SALES_CALLER_TOKENS") or "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
    except ValueError:
        raise RuntimeError("SALES_CALLER_TOKENS must be a JSON object {caller: token}") from None
    if not isinstance(tokens, dict):
        raise RuntimeError("SALES_CALLER_TOKENS must be a JSON object {caller: token}")
    for name, tok in tokens.items():
        if name not in KNOWN_CALLERS:
            raise RuntimeError(f"SALES_CALLER_TOKENS names an unknown caller {name[:40]!r}")
        if not isinstance(tok, str) or not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"SALES_CALLER_TOKENS[{name}] must be 32..512 printable ASCII characters")
    values = list(tokens.values())
    if len(set(values)) != len(values) or service_token in values:
        raise RuntimeError("SALES_CALLER_TOKENS: every caller token must be distinct and differ from the service token")
    return dict(tokens)


# Two-level public suffixes that are common for our clients and for domain registrations (AEGIS S1-M3). A full
# Public Suffix List is not vendored: an unlisted two-level suffix is compared on its last two labels, which can
# only make the check stricter (two unrelated example.co.xx domains look related and are refused).
TWO_LEVEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.nz", "net.nz", "org.nz", "co.za", "co.jp", "ne.jp", "or.jp", "co.in", "net.in", "org.in", "com.br", "net.br",
    "com.mx", "com.ar", "com.co", "com.sg", "com.hk", "com.tw", "com.cn", "com.tr", "co.kr", "co.il", "com.my",
    "com.ph", "co.id", "com.pk", "com.ng", "com.eg", "com.sa", "co.th", "com.vn", "com.pe", "com.ec", "co.ve"})


def registrable(domain: str) -> str:
    """The registrable domain: the last two labels, or the last three under a known two-level suffix."""
    labels = domain.lower().rstrip(".").split(".")
    n = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else 2
    return ".".join(labels[-n:])


def related_domains(a: str, b: str) -> bool:
    """True when two domains share a registrable domain (``go.zbestmedia.com`` and ``www.zbestmedia.com`` do): an
    outreach domain must share no reputation with a brand's own domain."""
    return registrable(a) == registrable(b)


def _domains(env) -> tuple[Optional[str], tuple]:
    outreach = (env.get("SALES_OUTREACH_DOMAIN") or "").strip().lower() or None
    raw_primary = (env.get("SALES_PRIMARY_DOMAINS") or "").strip().lower()
    primary = tuple(p.strip() for p in raw_primary.split(",") if p.strip()) if raw_primary else ()
    for p in primary:
        if not _HOST.fullmatch(p):
            raise RuntimeError("SALES_PRIMARY_DOMAINS must be a comma-separated list of domain names")
    if outreach is None:
        return None, primary
    if not _HOST.fullmatch(outreach):
        raise RuntimeError("SALES_OUTREACH_DOMAIN must be a domain name (lowercase, no scheme, no path)")
    if not primary:
        raise RuntimeError("SALES_OUTREACH_DOMAIN is set but SALES_PRIMARY_DOMAINS is not: the service cannot prove "
                           "the outreach domain is separate from the brands' own domains, so it refuses to start")
    if len({registrable(p) for p in primary}) < 2:
        raise RuntimeError("SALES_PRIMARY_DOMAINS must list both brands' own domains (ZBM and ZBC: at least two "
                           "different registrable domains), so neither can be used as the outreach domain")
    for p in primary:
        if related_domains(outreach, p):
            raise RuntimeError("SALES_OUTREACH_DOMAIN must be a separate domain: it shares a registrable domain with "
                               "one of SALES_PRIMARY_DOMAINS (cold email from a brand's own domain puts the brands' own "
                               "mail at risk)")
    return outreach, primary


def _warmup(env) -> tuple:
    raw = (env.get("SALES_WARMUP_SCHEDULE") or "").strip()
    if not raw:
        return DEFAULT_WARMUP
    parts = raw.split(",")
    if not 1 <= len(parts) <= 60 or not all(re.fullmatch(r"[0-9]{1,4}", p.strip()) for p in parts):
        raise RuntimeError("SALES_WARMUP_SCHEDULE must be 1..60 comma-separated whole numbers (sends per day)")
    days = tuple(int(p) for p in parts)
    if days[0] < 1 or days[0] > 50:
        raise RuntimeError("SALES_WARMUP_SCHEDULE must start low: day 1 is 1..50 sends")
    if any(b < a for a, b in zip(days, days[1:])) or max(days) > 500:
        raise RuntimeError("SALES_WARMUP_SCHEDULE must never decrease and never exceed 500 a day")
    if any(b > 2 * a for a, b in zip(days, days[1:])):
        raise RuntimeError("SALES_WARMUP_SCHEDULE may at most double from one day to the next")
    return days


def _postal(env, outreach: Optional[str]) -> Optional[str]:
    raw = (env.get("SALES_POSTAL_ADDRESS") or "").strip()
    if not raw:
        if outreach:
            raise RuntimeError("SALES_OUTREACH_DOMAIN is set but SALES_POSTAL_ADDRESS is not: every commercial email "
                               "must carry a valid physical postal address (CAN-SPAM)")
        return None
    if not 10 <= len(raw) <= 200 or not re.fullmatch(r"[\x20-\x7e]+", raw) or not re.search(r"[0-9]", raw):
        raise RuntimeError("SALES_POSTAL_ADDRESS must be a physical postal address (10..200 printable characters, "
                           "with a street or box number)")
    return raw


NOT_BUILT = {
    "SALES_EMAIL_PROVIDER": "the email send provider: none is chosen or built yet (sends stay queued)",
    "SALES_SMS_PROVIDER": "the SMS provider: none is chosen or built yet",
    "SALES_VOICE_PROVIDER": "the voice provider: none is chosen or built yet",
    "SALES_PUBLIC_DATA_PROVIDER": "public-data lead sourcing: no source is chosen or built yet",
    "SALES_PAID_LEAD_PROVIDER": "paid lead provider: none is chosen or built yet",
    "SALES_ONBOARDING_URL": "the Onboarding hand-off client: not built yet (hand-offs stay pending_delivery)",
    "SALES_FINANCE_URL": "the Finance (31) invoice-draft client: not built yet (hand-offs stay pending_delivery)",
    "SALES_LEGAL_URL": "the Legal (37) contract check client: not built yet (proposals cannot be sent)",
}


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("SALES_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("SALES_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "SALES_NON_PRODUCTION")
    s = Settings(service_token=service_token, caller_tokens=_caller_tokens(env, service_token),
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None
    andre = (env.get("SALES_ANDRE_APPROVAL_TOKEN") or "").strip() or None
    if andre is not None and not _PRINTABLE.fullmatch(andre):
        raise RuntimeError("SALES_ANDRE_APPROVAL_TOKEN must be 32..512 printable ASCII characters")
    s.andre_token = andre

    if env.get("SALES_PII_HASH_KEY_FILE"):
        s.pii_key = Secret(pii_key(_secret_file_bytes(env, "SALES_PII_HASH_KEY_FILE")))
    elif not non_production or s.data_dir:
        # AEGIS S1-L3: a durable log is never written under the fixed test key, non-production or not
        raise RuntimeError("SALES_PII_HASH_KEY_FILE is required whenever SALES_DATA_DIR is set (and always in "
                           "production): emails and phones are matched, suppressed and exported as keyed hashes")
    else:
        s.pii_key = Secret(b"sales-py non-production pii hash key, never in production")

    s.outreach_domain, s.primary_domains = _domains(env)
    s.postal_address = _postal(env, s.outreach_domain)
    local = (env.get("SALES_OUTREACH_FROM_LOCAL") or "hello").strip()
    if not re.fullmatch(r"[a-z][a-z0-9.-]{0,30}", local) or re.search(r"no-?reply|donotreply", local):
        raise RuntimeError("SALES_OUTREACH_FROM_LOCAL must be a plain mailbox name that accepts replies "
                           "(lowercase, no 'noreply')")
    s.from_local = local
    s.warmup = _warmup(env)
    s.daily_send_cap = _int(env, "SALES_DAILY_SEND_CAP", 200, 1, 500)
    raw_max = (env.get("SALES_AUTO_APPROVE_MAX") or "10000.00").strip()
    try:
        auto_max = money.parse(raw_max)
    except money.MoneyError:
        raise RuntimeError("SALES_AUTO_APPROVE_MAX must be a two-decimal amount like 10000.00") from None
    if auto_max > AUTO_APPROVE_CEILING:
        raise RuntimeError("SALES_AUTO_APPROVE_MAX may be at most 10000.00 (Andre's ceiling for proposals agents "
                           "send without him)")
    s.auto_approve_max = auto_max
    s.stale_lead_days = _int(env, "SALES_STALE_LEAD_DAYS", 30, 7, 365)
    for name, why in NOT_BUILT.items():
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend.")
    s.bind_addr = (env.get("SALES_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "SALES_PORT", 8450, 1024, 65535)
    return s
