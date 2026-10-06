"""
Founder decision 4 (ADR 0017): official app connections only; this service never stores a client password or a raw
token. Two layers, both applied to EVERY request body before it is parsed (api.body):

1. Keys. A key naming a password, a secret, a raw token or personal data (date of birth, government or tax id, card
   or bank data, IP, device) is refused 422 at any depth. Business listing fields (a store's public phone number) are
   NOT personal data here and are allowed.
2. Values. A string shaped like a credential is refused 422 ``SECRET_REFUSED`` wherever it appears — a Shopify access
   token (``shpat_`` / ``shppa_`` / ``shpca_`` / ``shpss_``), a Google OAuth access token (``ya29.``) or refresh token
   (``1//``), an Anthropic key (``sk-ant-``), any ``sk-``/``sk_live_``/``rk_live_`` key, a JWT, a PEM private key, a
   ``Bearer`` header value, or a URL carrying user:password. A connection carries ONLY a vault reference
   (``vault:<owner>.<name>``, security-py's REF shape) to the token the hub stored in the Cybersecurity (22) vault.

This is defence in depth, not the guarantee: the guarantee is structural (no model has a field that can hold a token;
the transport, not this service, resolves vault references at call time).
"""

from __future__ import annotations

import re
from typing import Any, Optional

FORBIDDEN_KEYS = frozenset({
    # credentials
    "password", "passwd", "pass", "pwd", "passcode", "pin", "secret", "client_secret", "api_secret", "api_key",
    "apikey", "access_token", "refresh_token", "id_token", "token", "auth_token", "bearer", "authorization",
    "private_key", "consumer_key", "consumer_secret", "credentials", "credential", "otp", "totp", "mfa_code",
    "session_cookie", "cookie", "shopify_access_token", "x_shopify_access_token",
    # personal data this department never takes
    "ip", "ip_address", "ipaddress", "remote_addr", "client_ip", "x_forwarded_for", "user_agent", "useragent",
    "device", "device_id", "device_fingerprint", "dob", "date_of_birth", "birth_date", "birthdate", "ssn", "tin",
    "ein", "itin", "tax_id", "taxid", "taxpayer_id", "social_security_number", "government_id", "passport",
    "drivers_license", "driver_license", "national_id", "card", "card_number", "credit_card", "pan", "cvv", "cvc",
    "account_number", "bank_account", "routing_number", "iban", "swift", "bic",
})

_SECRET_VALUE = re.compile(
    r"(?:\bshp(?:at|pa|ca|ss)_[0-9a-fA-F]{8,}"          # Shopify tokens and shared secrets
    r"|\bya29\.[0-9A-Za-z_\-]{8,}"                       # Google OAuth access token
    r"|(?:^|[\s\"'])1//[0-9A-Za-z_\-]{8,}"               # Google OAuth refresh token
    r"|\bsk-ant-[0-9A-Za-z_\-]{8,}"                      # Anthropic API key
    r"|\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{8,}"        # Stripe-style keys
    r"|\bsk-[0-9A-Za-z]{20,}"                            # generic sk- keys
    r"|\beyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}"   # a JWT
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bBearer\s+[0-9A-Za-z._\-]{16,}"
    r"|[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@)",       # user:password@ in a URL
)

VAULT_REF = re.compile(r"^vault:[a-z0-9_]{1,40}\.[A-Za-z0-9_][A-Za-z0-9._-]{0,79}$")


def forbidden_keys(obj: Any, path: str = "", depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if str(k).lower().replace("-", "_") in FORBIDDEN_KEYS:
                out.append(p[:120])
            out += forbidden_keys(v, p, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:10_000]:
            out += forbidden_keys(v, path, depth + 1)
    return out


def secret_value(obj: Any, path: str = "", depth: int = 0) -> Optional[str]:
    """The path of the first credential-shaped string in ``obj`` (keys included), or None."""
    if depth > 40:
        return None
    if isinstance(obj, str):
        return path or "(body)" if _SECRET_VALUE.search(obj) else None
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(k, str) and _SECRET_VALUE.search(k):
                return p[:120]
            hit = secret_value(v, p, depth + 1)
            if hit:
                return hit[:120]
    elif isinstance(obj, list):
        for v in obj[:10_000]:
            hit = secret_value(v, path, depth + 1)
            if hit:
                return hit
    return None
