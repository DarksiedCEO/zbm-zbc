"""
Settings for Cybersecurity (22), read once at start (ADR 0012). Every problem refuses start (fail closed); a
missing optional piece leaves that capability visibly OFF in /health, never silently weaker.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Optional

import webauthn

# Departments that may hold a Cybersecurity caller token (ADR 0012 decision 2). `dashboard` is Andre's console
# backend: it relays passkey ceremonies and Andre-approved requests, and is never Andre by itself.
KNOWN_CALLERS = ("finance_31", "verification_integrity", "legal_37", "delivery_28", "onboarding", "compliance_38",
                 "clipper_network", "creative_production", "fulfillment", "detection", "orchestrator",
                 "stripe_gateway", "scheduler", "dashboard")
MIN_TOKEN = 32
_PRINTABLE = re.compile(r"[\x21-\x7e]{32,512}")


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
    path = (env.get("SEC_DATA_DIR") or "").strip()
    if not path:
        if not non_production:
            raise RuntimeError("SEC_DATA_DIR is required: a vault that forgets everything on restart is not a vault "
                               "(SEC_NON_PRODUCTION=1 allows an in-memory run for tests only)")
        return None
    if not os.path.isabs(path):
        raise RuntimeError("SEC_DATA_DIR must be an absolute path")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("SEC_DATA_DIR must be a directory (not a symlink or a file)")
        if st.st_uid != os.geteuid():
            raise RuntimeError("SEC_DATA_DIR is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise RuntimeError("SEC_DATA_DIR is open to group or others; chmod 700 it")
    return path


@dataclass
class Settings:
    service_token: str
    caller_tokens: dict = field(default_factory=dict)
    non_production: bool = False
    data_dir: Optional[str] = None
    ledger_url: Optional[str] = None
    ledger_token: Optional[str] = None
    kms: str = "none"
    local_master_key: Optional[Secret] = None
    rp_id: str = ""
    origins: tuple = ()
    enroll_token: Optional[Secret] = None
    passkey_recovery: bool = False
    compliance_url: Optional[str] = None
    compliance_token: Optional[str] = None
    compliance_caller_token: Optional[str] = None
    release_rate_per_min: int = 60
    token_ttl_s: int = 600
    bind_addr: str = "127.0.0.1"
    port: int = 8440

    @property
    def relying(self) -> webauthn.Relying:
        return webauthn.Relying(self.rp_id, self.origins)


def _caller_tokens(env, service_token: str) -> dict:
    raw = (env.get("SEC_CALLER_TOKENS") or "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
    except ValueError:
        raise RuntimeError("SEC_CALLER_TOKENS must be a JSON object {caller: token}") from None
    if not isinstance(tokens, dict):
        raise RuntimeError("SEC_CALLER_TOKENS must be a JSON object {caller: token}")
    for name, tok in tokens.items():
        if name not in KNOWN_CALLERS:
            raise RuntimeError(f"SEC_CALLER_TOKENS names an unknown caller {name[:40]!r}")
        if not isinstance(tok, str) or not _PRINTABLE.fullmatch(tok):
            raise RuntimeError(f"SEC_CALLER_TOKENS[{name}] must be 32..512 printable ASCII characters")
    values = list(tokens.values())
    if len(set(values)) != len(values) or service_token in values:
        raise RuntimeError("SEC_CALLER_TOKENS: every caller token must be distinct and differ from the service token")
    return dict(tokens)


def _thin(env, prefix: str) -> tuple:
    vals = [(env.get(f"{prefix}_{k}") or "").strip() or None for k in ("URL", "TOKEN", "CALLER_TOKEN")]
    if any(vals) and not all(vals):
        raise RuntimeError(f"{prefix}_URL, {prefix}_TOKEN and {prefix}_CALLER_TOKEN come as a set (all or none)")
    if vals[0] and not re.fullmatch(r"https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?", vals[0]):
        raise RuntimeError(f"{prefix}_URL must be a base URL (scheme, host, optional port)")
    return tuple(vals)


NOT_BUILT = {
    "SEC_ALERT_SMS": "text-message alerts: no SMS provider is chosen or built yet",
    "SEC_ALERT_EMAIL": "email alerts: no email provider is chosen or built yet",
    "SEC_ALERT_PUSH": "push alerts: no push provider is chosen or built yet",
}


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    service_token = (env.get("SEC_SERVICE_TOKEN") or "").strip()
    if not _PRINTABLE.fullmatch(service_token):
        raise RuntimeError("SEC_SERVICE_TOKEN must be set (32..512 printable ASCII characters): fail closed")
    non_production = _flag(env, "SEC_NON_PRODUCTION")
    s = Settings(service_token=service_token, caller_tokens=_caller_tokens(env, service_token),
                 non_production=non_production, data_dir=_data_dir(env, non_production))
    s.ledger_url = (env.get("LEDGER_SERVICE_URL") or "").strip() or None
    s.ledger_token = (env.get("LEDGER_SERVICE_TOKEN") or "").strip() or None

    kms = (env.get("SEC_KMS") or "none").strip()
    if kms in ("aws", "gcp"):
        raise RuntimeError(f"SEC_KMS={kms}: that key-service adapter is not built yet (hosting is not chosen; "
                           "ADR 0012 unlock list). This service refuses to start rather than run without it.")
    if kms not in ("none", "local_file"):
        raise RuntimeError("SEC_KMS must be none or local_file (aws and gcp are not built yet)")
    if kms == "local_file":
        if not non_production:
            raise RuntimeError("SEC_KMS=local_file keeps the master key in this process's memory: allowed only with "
                               "SEC_NON_PRODUCTION=1")
        raw = _secret_file_bytes(env, "SEC_LOCAL_MASTER_KEY_FILE")
        try:
            key = base64.b64decode(raw, validate=True)
        except ValueError:
            key = b""
        if len(key) != 32:
            raise RuntimeError("SEC_LOCAL_MASTER_KEY_FILE must hold exactly 32 random bytes, base64-encoded")
        s.local_master_key = Secret(key)
    elif env.get("SEC_LOCAL_MASTER_KEY_FILE"):
        raise RuntimeError("SEC_LOCAL_MASTER_KEY_FILE is set but SEC_KMS is not local_file")
    s.kms = kms

    rp_id = (env.get("SEC_WEBAUTHN_RP_ID") or "").strip()
    origins_raw = (env.get("SEC_WEBAUTHN_ORIGINS") or "").strip()
    if bool(rp_id) != bool(origins_raw):
        raise RuntimeError("SEC_WEBAUTHN_RP_ID and SEC_WEBAUTHN_ORIGINS come as a pair (both or neither)")
    if rp_id:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*", rp_id) \
                or len(rp_id) > 253:
            raise RuntimeError("SEC_WEBAUTHN_RP_ID must be a lowercase host name (the site Andre approves on)")
        try:
            origins = webauthn.allowed_origins(origins_raw)
        except ValueError as exc:
            raise RuntimeError(f"SEC_WEBAUTHN_ORIGINS: {exc}") from None
        if any(o.startswith("http://") for o in origins) and not non_production:
            raise RuntimeError("SEC_WEBAUTHN_ORIGINS: plain http (localhost) only with SEC_NON_PRODUCTION=1")
        if not webauthn.origins_match_rp(origins, rp_id):
            raise RuntimeError("SEC_WEBAUTHN_ORIGINS: every origin's host must be SEC_WEBAUTHN_RP_ID or under it")
        s.rp_id, s.origins = rp_id, origins

    if env.get("SEC_ANDRE_ENROLL_TOKEN_FILE"):
        raw = _secret_file_bytes(env, "SEC_ANDRE_ENROLL_TOKEN_FILE")
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            value = ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value):
            raise RuntimeError("SEC_ANDRE_ENROLL_TOKEN_FILE must hold 32..256 characters of [A-Za-z0-9_-]")
        if value == service_token or value in s.caller_tokens.values():
            raise RuntimeError("SEC_ANDRE_ENROLL_TOKEN_FILE must not hold the service token or a caller token")
        s.enroll_token = Secret(value)
    s.passkey_recovery = _flag(env, "SEC_PASSKEY_RECOVERY")
    if s.passkey_recovery and s.enroll_token is None:
        raise RuntimeError("SEC_PASSKEY_RECOVERY=1 needs a fresh SEC_ANDRE_ENROLL_TOKEN_FILE")

    s.compliance_url, s.compliance_token, s.compliance_caller_token = _thin(env, "SEC_COMPLIANCE")
    for name, why in NOT_BUILT.items():
        if (env.get(name) or "").strip() not in ("", "none", "0"):
            raise RuntimeError(f"{name}: {why}. Unset it; this service refuses to start rather than pretend to alert.")
    s.release_rate_per_min = _int(env, "SEC_RELEASE_RATE_PER_MIN", 60, 1, 600)
    s.token_ttl_s = _int(env, "SEC_TOKEN_TTL_SECONDS", 600, 60, 900)
    s.bind_addr = (env.get("SEC_BIND_ADDR") or "127.0.0.1").strip()
    s.port = _int(env, "SEC_PORT", 8440, 1024, 65535)
    return s
