"""
Settings for Customer Service (30) + Client Success (29), read once at start (ADR 0014). Every problem refuses
start (fail closed); a missing optional piece leaves that capability visibly OFF in /svc/v1/status, never silently
weaker.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Optional

# Who may hold a caller token (ADR 0014 decision 2). `dashboard` is Andre's console backend: it is never Andre by
# itself; Andre's actions also carry X-Andre-Approval-Token.
KNOWN_CALLERS = ("hub", "email_gateway", "sms_gateway", "voice_gateway", "onboarding", "detection", "finance_31",
                 "legal_37", "compliance_38", "scheduler", "dashboard")
BRANDS = ("zbm", "zbc")
BRAND_NAMES = {"zbm": "Z Best Media", "zbc": "Z Best Clips"}
PRIORITIES = ("p1", "p2", "p3", "p4")
# (default, lowest, highest) minutes, Andre's starting targets (ADR 0014 decision 9)
SLA_FIRST_DEFAULT = {"p1": 60, "p2": 240, "p3": 480, "p4": 1440}
SLA_RESOLUTION_DEFAULT = {"p1": 480, "p2": 1440, "p3": 4320, "p4": 10080}
SLA_FIRST_BOUNDS = (5, 2880)
SLA_RESOLUTION_BOUNDS = (60, 20160)
NON_PRODUCTION_HMAC_KEY = b"service-py non-production digest key, tests only!"
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")
_EMAIL = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63}){1,8}")
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")


def _secret_file_bytes(env, name: str, max_bytes: int = 4096) -> bytes:
    """A regular file, owned by this user, mode 0600/0400, opened without following a symlink and without
    blocking on a FIFO (finance-py's _secret_file rules, AEGIS L5/N5). Returns its stripped bytes."""
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


KEY_NAME = "hmac.key"


def weak_key(key: bytes) -> bool:
    """V2-L1 / V3-L1: all zeros, fewer than 16 distinct bytes, any 8-byte block occurring twice, or every byte
    printable ASCII (a typed or hex-looking "key", not random bytes)."""
    blocks = [key[i:i + 8] for i in range(0, len(key) - 7)]
    return key == bytes(len(key)) or len(set(key)) < 16 or len(set(blocks)) != len(blocks) \
        or all(0x20 <= b < 0x7F for b in key)


def _generated_key(data_dir: str) -> bytes:
    """The service's own key: read from SVC_DATA_DIR/hmac.key, or created there (0600, O_EXCL) on the first start."""
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    path = os.path.join(data_dir, KEY_NAME)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = None
    if fd is not None:
        try:
            os.write(fd, base64.b64encode(os.urandom(32)))
            os.fsync(fd)
        finally:
            os.close(fd)
    raw = _secret_file_bytes({"KEY_FILE": path}, "KEY_FILE")
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError:
        key = b""
    if len(key) < 32 or weak_key(key):
        raise RuntimeError(f"{KEY_NAME} in SVC_DATA_DIR is not a valid generated key; refusing to start")
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
    path = (env.get("SVC_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("SVC_DATA_DIR is required: a support desk that forgets its tickets, consents and "
                               "approvals on restart is not one (SVC_NON_PRODUCTION=1 allows an in-memory run for "
                               "tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("SVC_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("SVC_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("SVC_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("SVC_DATA_DIR is open to group or others; chmod 700 it")
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
    support_email: dict = field(default_factory=dict)      # brand -> support identity (lower case)
    sms_number: dict = field(default_factory=dict)         # brand -> E.164 sending number
    legal_url: Optional[str] = None
    legal_token: Optional[str] = None
    legal_caller_token: Optional[str] = None
    sla_first: dict = field(default_factory=lambda: dict(SLA_FIRST_DEFAULT))
    sla_resolution: dict = field(default_factory=lambda: dict(SLA_RESOLUTION_DEFAULT))
    at_risk_threshold: int = 60
    renewal_window_days: int = 60
    hmac_key: bytes = b""                                  # keys every stored digest (bodies, consent texts)
    nonprod_outbox: Optional[str] = None                   # SVC_SMS_PROVIDER=nonprod_file (non-production only)
    bind_addr: str = "127.0.0.1"
    port: int = 8460


def _caller_tokens(env, service_token: str) -> dict:
    raw = (env.get("SVC_CALLER_TOKENS") or "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
    except ValueError:
        raise RuntimeError("SVC_CALLER_TOKENS must be a JSON object {caller: token}") from None
    if not isinstance(tokens, dict):
        raise RuntimeError("SVC_CALLER_TOKENS must be a JSON object {caller: token}")
    for name, tok in tokens.items():
        if name not in KNOWN_CALLERS:
            raise RuntimeError(f"SVC_CALLER_TOKENS names an unknown caller {name[:40]!r}")
        if not isinstance(tok, str) or not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"SVC_CALLER_TOKENS[{name}] must be 32..512 printable ASCII characters")
    values = list(tokens.values())
    if len(set(values)) != len(values) or service_token in values:
        raise RuntimeError("SVC_CALLER_TOKENS: every caller token must be distinct and differ from the service token")
    return dict(tokens)


def _thin(env, prefix: str) -> tuple:
    vals = [(env.get(f"{prefix}_{k}") or "").strip() or None for k in ("URL", "TOKEN", "CALLER_TOKEN")]
    if any(vals) and not all(vals):
        raise RuntimeError(f"{prefix}_URL, {prefix}_TOKEN and {prefix}_CALLER_TOKEN come as a set (all or none)")
    if vals[0] and not re.fullmatch(r"https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?", vals[0]):
        raise RuntimeError(f"{prefix}_URL must be a base URL (scheme, host, optional port)")
    return tuple(vals)


NOT_BUILT = {
    "SVC_VOICE_PROVIDER": "the phone (voice) provider: call plumbing is built, no voice provider is chosen or built",
    "SVC_EMAIL_PROVIDER": "the email sending provider: outbound email stays queued until one is chosen and built",
    "SVC_SMS_PROVIDER": "the SMS sending provider: outbound SMS stays queued until one is chosen and built",
    "SVC_CHAT_PROVIDER": "the chat push provider: outbound chat stays queued until one is chosen and built",
    "SVC_ALERT_PROVIDER": "Andre's alert channel: alerts are recorded, not sent, until one is chosen and built",
    "SVC_FINANCE_URL": "the Finance (31) client: payment status and money handoffs are not wired yet",
    "SVC_CYBER_URL": "the Cybersecurity (22) client: its incident route is dashboard-only today",
    "SVC_COMPLIANCE_URL": "the Compliance (38) client: privacy-request intake is not wired yet",
    "SVC_RESULTS_URL": "the results-trend client (Revenue Recovery / detection metrics) is not wired yet",
    "SVC_ONBOARDING_URL": "the Onboarding client (contract end dates) is not wired yet",
}


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("SVC_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("SVC_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "SVC_NON_PRODUCTION")
    s = Settings(service_token=service_token, caller_tokens=_caller_tokens(env, service_token),
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None
    s.andre_token = (env.get("SVC_ANDRE_APPROVAL_TOKEN") or "").strip() or None

    for brand in BRANDS:
        raw = (env.get(f"SVC_SUPPORT_EMAIL_{brand.upper()}") or "").strip().lower()
        if raw:
            if not _EMAIL.fullmatch(raw):
                raise RuntimeError(f"SVC_SUPPORT_EMAIL_{brand.upper()} must be one email address")
            s.support_email[brand] = raw
        num = (env.get(f"SVC_SMS_NUMBER_{brand.upper()}") or "").strip()
        if num:
            if not _E164.fullmatch(num):
                raise RuntimeError(f"SVC_SMS_NUMBER_{brand.upper()} must be an E.164 number (+15551234567)")
            s.sms_number[brand] = num
    if len(set(s.support_email.values())) != len(s.support_email):
        raise RuntimeError("SVC_SUPPORT_EMAIL_ZBM and SVC_SUPPORT_EMAIL_ZBC must differ: one support identity per brand")
    if len(set(s.sms_number.values())) != len(s.sms_number):
        raise RuntimeError("SVC_SMS_NUMBER_ZBM and SVC_SMS_NUMBER_ZBC must differ: one number per brand")

    s.legal_url, s.legal_token, s.legal_caller_token = _thin(env, "SVC_LEGAL")
    if (env.get("SVC_SMS_PROVIDER") or "").strip() == "nonprod_file":
        # V2-L4: the live run's file sender; never in production
        if not non_production:
            raise RuntimeError("SVC_SMS_PROVIDER=nonprod_file is allowed only with SVC_NON_PRODUCTION=1")
        outbox = (env.get("SVC_NONPROD_OUTBOX_FILE") or "").strip()
        if not os.path.isabs(outbox):
            raise RuntimeError("SVC_SMS_PROVIDER=nonprod_file needs SVC_NONPROD_OUTBOX_FILE (an absolute path)")
        s.nonprod_outbox = outbox
    elif (env.get("SVC_NONPROD_OUTBOX_FILE") or "").strip():
        raise RuntimeError("SVC_NONPROD_OUTBOX_FILE is set but SVC_SMS_PROVIDER is not nonprod_file")
    for name, why in NOT_BUILT.items():
        if name == "SVC_SMS_PROVIDER" and s.nonprod_outbox:
            continue
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend.")

    for p in PRIORITIES:
        s.sla_first[p] = _int(env, f"SVC_SLA_{p.upper()}_FIRST_RESPONSE_MINUTES", SLA_FIRST_DEFAULT[p],
                              *SLA_FIRST_BOUNDS)
        s.sla_resolution[p] = _int(env, f"SVC_SLA_{p.upper()}_RESOLUTION_MINUTES", SLA_RESOLUTION_DEFAULT[p],
                                   *SLA_RESOLUTION_BOUNDS)
        if s.sla_first[p] > s.sla_resolution[p]:
            raise RuntimeError(f"SLA {p}: the first-response target must not exceed the resolution target")
    for a, b in zip(PRIORITIES, PRIORITIES[1:]):
        if s.sla_first[a] > s.sla_first[b] or s.sla_resolution[a] > s.sla_resolution[b]:
            raise RuntimeError(f"SLA targets must not be looser for {a} than for {b}")
    # AEGIS round 1 (V1-M2): stored digests of message bodies and consent texts are HMAC-SHA-256 under this key, so
    # a short text (a phone number, "yes") cannot be confirmed by hashing guesses
    # AEGIS round 3 (V3-L1): the key is GENERATED by the service on its first start (os.urandom, 32 bytes) and kept in
    # SVC_DATA_DIR/hmac.key (0600); its fingerprint goes into the log. A key supplied by file must look random.
    if (env.get("SVC_HMAC_KEY_FILE") or "").strip():
        raw = _secret_file_bytes(env, "SVC_HMAC_KEY_FILE")
        try:
            key = base64.b64decode(raw, validate=True)
        except ValueError:
            key = b""
        if len(key) < 32:
            raise RuntimeError("SVC_HMAC_KEY_FILE must hold at least 32 random bytes, base64-encoded")
        if weak_key(key):
            raise RuntimeError("SVC_HMAC_KEY_FILE holds a weak key (all zeros, fewer than 16 distinct bytes, a repeated "
                               "block or only printable characters): leave it unset and the service generates one")
        s.hmac_key = key
    elif s.data_dir:
        s.hmac_key = _generated_key(s.data_dir)
    elif non_production:
        s.hmac_key = NON_PRODUCTION_HMAC_KEY
    else:
        raise RuntimeError("no HMAC key: set SVC_DATA_DIR (the key is generated there) or SVC_HMAC_KEY_FILE")
    s.at_risk_threshold = _int(env, "SVC_AT_RISK_THRESHOLD", 60, 1, 99)
    s.renewal_window_days = _int(env, "SVC_RENEWAL_WINDOW_DAYS", 60, 7, 180)
    s.bind_addr = (env.get("SVC_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "SVC_PORT", 8460, 1024, 65535)
    return s
