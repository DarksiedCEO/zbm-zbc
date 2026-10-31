"""
Environment configuration for the client acceptance page (ADR 0028). Every rule fails closed: a configuration the
service cannot honor refuses start-up with a plain message naming the variable (never its value).

The service holds three secrets and nothing else: Legal's service token, its own ``client_portal`` caller token at
Legal (which reaches only Legal's two portal routes), and the evidence key (HMAC of the client's IP address and user
agent, the form signature and the retry key; never sent anywhere).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

TOKEN_MIN = 32
KEY_MIN = 32
UNRESOLVED_REF_MARKERS = ("${{", "{{")
_PRINTABLE = re.compile(r"^[\x21-\x7e]+$")


@dataclass(frozen=True)
class Settings:
    legal_url: str
    legal_service_token: str = field(repr=False)
    legal_caller_token: str = field(repr=False)
    evidence_key: bytes = field(repr=False)
    public_origin: str
    trusted_proxy_hops: int = 1
    bind_addr: str = "127.0.0.1"
    port: int = 8480
    legal_timeout_s: float = 5.0
    ip_per_minute: int = 30
    token_per_hour: int = 40
    not_found_per_minute: int = 120
    form_max_age_s: int = 4 * 3600
    display_tz: str = "America/Los_Angeles"

    @property
    def key_id(self) -> str:
        """Names the evidence key on every acceptance (rotation): the first 16 hex characters of its SHA-256."""
        return hashlib.sha256(self.evidence_key).hexdigest()[:16]


def _secret(env: dict, name: str, minimum: int) -> str:
    value = env.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set. client-portal refuses to start without it (fail closed).")
    if any(m in value for m in UNRESOLVED_REF_MARKERS):
        raise RuntimeError(f"{name} holds an unresolved Railway reference (a '${{{{...}}}}' placeholder), not a value")
    if len(value) < minimum or not _PRINTABLE.fullmatch(value):
        raise RuntimeError(f"{name} must be >= {minimum} printable ASCII characters")
    return value


def _int(env: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    if not re.fullmatch(r"[0-9]{1,9}", raw.strip()) or not lo <= int(raw) <= hi:
        raise RuntimeError(f"{name} must be an integer {lo}..{hi}")
    return int(raw)


def _origin(env: dict) -> str:
    raw = (env.get("PORTAL_PUBLIC_ORIGIN") or "").strip()
    if not raw:
        raise RuntimeError("PORTAL_PUBLIC_ORIGIN is not set (the page's own https://host; Origin checks need it)")
    parts = urlsplit(raw)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if (parts.scheme != "https" and not (parts.scheme == "http" and local)) or not parts.hostname \
            or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        raise RuntimeError("PORTAL_PUBLIC_ORIGIN must be an origin like https://accept.example.com (http only for "
                           "localhost), with no path, query or credentials")
    return f"{parts.scheme}://{parts.netloc}".lower()


def _legal_url(env: dict) -> str:
    raw = (env.get("PORTAL_LEGAL_URL") or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment \
            or parts.username or parts.password:
        raise RuntimeError("PORTAL_LEGAL_URL must be Legal's base URL, e.g. http://legal.railway.internal:8420")
    if parts.scheme == "http" and not (parts.hostname.endswith(".railway.internal")
                                       or parts.hostname in ("127.0.0.1", "localhost", "::1")):
        raise RuntimeError("PORTAL_LEGAL_URL: plain http only on Railway's private network (*.railway.internal) or "
                           "localhost")
    return raw.rstrip("/")


def load(env: Optional[dict] = None) -> Settings:
    env = dict(os.environ) if env is None else env
    service = _secret(env, "PORTAL_LEGAL_SERVICE_TOKEN", TOKEN_MIN)
    caller = _secret(env, "PORTAL_LEGAL_CALLER_TOKEN", TOKEN_MIN)
    key = _secret(env, "PORTAL_EVIDENCE_KEY", KEY_MIN)
    if len({service, caller, key}) != 3:
        raise RuntimeError("PORTAL_LEGAL_SERVICE_TOKEN, PORTAL_LEGAL_CALLER_TOKEN and PORTAL_EVIDENCE_KEY must differ")
    tz = (env.get("PORTAL_DISPLAY_TZ") or "America/Los_Angeles").strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - any failure refuses (no silent UTC fallback)
        raise RuntimeError(f"PORTAL_DISPLAY_TZ={tz!r} is not a known IANA time zone on this host") from None
    bind = (env.get("PORTAL_BIND_ADDR") or "127.0.0.1").strip()
    return Settings(
        legal_url=_legal_url(env), legal_service_token=service, legal_caller_token=caller,
        evidence_key=key.encode("ascii"), public_origin=_origin(env),
        trusted_proxy_hops=_int(env, "PORTAL_TRUSTED_PROXY_HOPS", 1, 0, 3),
        bind_addr=bind, port=_int(env, "PORTAL_PORT", 8480, 1, 65535),
        legal_timeout_s=float(_int(env, "PORTAL_LEGAL_TIMEOUT_SECONDS", 5, 1, 30)),
        ip_per_minute=_int(env, "PORTAL_RATE_IP_PER_MINUTE", 30, 1, 10_000),
        token_per_hour=_int(env, "PORTAL_RATE_TOKEN_PER_HOUR", 40, 1, 10_000),
        not_found_per_minute=_int(env, "PORTAL_RATE_NOT_FOUND_PER_MINUTE", 120, 1, 100_000),
        form_max_age_s=_int(env, "PORTAL_FORM_MAX_AGE_SECONDS", 4 * 3600, 300, 24 * 3600),
        display_tz=tz,
    )
