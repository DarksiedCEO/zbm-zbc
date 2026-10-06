"""Fields Sales never accepts (ADR 0013 decision 7; legal-py's G6 list, with payment fields added): a request body
naming any of these keys, at any depth, is refused 422 before it is parsed. Records about people hold the minimum:
a name, a work email, a phone, a title and a time zone."""

from __future__ import annotations

from typing import Any

FORBIDDEN_KEYS = frozenset({
    "ip", "ip_address", "ipaddress", "ip_addr", "remote_addr", "remote_ip", "client_ip", "x_forwarded_for",
    "user_agent", "useragent", "device", "device_id", "device_fingerprint", "fingerprint",
    "dob", "date_of_birth", "birth_date", "birthdate", "birthday", "age", "ssn", "social_security_number", "tin",
    "tax_id", "government_id", "passport", "passport_number", "drivers_license", "driver_license", "national_id",
    "card", "card_number", "credit_card", "debit_card", "pan", "cvv", "cvc", "expiry", "card_expiry",
    "account_number", "bank_account", "routing_number", "iban", "swift", "bic",
    "gender", "race", "ethnicity", "religion", "health", "medical", "sexual_orientation",
})


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
