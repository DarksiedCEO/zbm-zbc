"""What this service never accepts (ADR 0015 decisions 7 and 13), checked on the raw JSON before the model parses it.

1. **Raw tax identification numbers** (Andre, Oct 6 2026: never store a raw TIN, SSN or EIN; store a reference only):
   - a key that names one, at any depth, in any spelling (``ssn``, ``SSN``, ``taxId``, ``tax-id``, ``ein``, ``itin``,
     ``taxpayer_identification_number``, ``social_security_number``, ``tin_last4`` ...): ``TAX_ID_REFUSED``;
   - a VALUE that has the shape of one, anywhere in the body (after Unicode NFKC, so fullwidth digits count): a group
     of exactly nine digits standing alone (any script's digits), written together or split by up to three spaces, dots,
     underscores or dashes between digits (``123-45-6789``, ``12-3456789``, ``123 - 45 - 6789``, ``123.45.6789``,
     ``123456789``): ``TAX_ID_REFUSED``. "Standing alone" means no digit or letter
     touches the group, so a 10-digit phone number, a hex digest or ``acct_1N...`` is not one; in free text (captions,
     briefs, DMs, templates, notes) a letter may touch it and it is still refused. Money fields are not scanned (they
     are canonical amounts). Handles and email addresses are read like ids ("gamer123456789" is a handle; "123456789"
     is refused). The inbound reply (``POST /inf/v1/replies``) is the one exemption: its text, sender address and
     sender handle are never stored raw (a SHA-256 or a keyed hash), and refusing one would drop an opt-out.
2. **Date of birth, age and other personal data this department never needs**: a key naming a date of birth, age,
   birth year, government id, payment card, bank account, IP, device or protected trait: ``FORBIDDEN_FIELD``. Minors are
   handled by the 18+ attestation flag alone (decision 8); no birth date or age is ever taken.

Errors never echo the value or the key's content beyond its path.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

TAX_KEYS = frozenset({
    "ssn", "ssns", "tin", "ein", "itin", "atin", "ptin", "tax_id", "taxid", "tax_id_number", "tax_number",
    "tax_identification_number", "taxpayer_id", "taxpayer_identification_number", "taxpayer_number",
    "social_security", "social_security_number", "social_security_no", "employer_identification_number",
    "employer_id_number", "federal_tax_id", "fein", "national_insurance_number", "nino", "sin", "tfn", "vat_number",
})
# a key is a tax key if, lower-cased with every non-alphanumeric removed, it equals one of these compacts or holds one of
# these stems
_TAX_COMPACT = frozenset(k.replace("_", "") for k in TAX_KEYS)
_TAX_STEMS = ("socialsecurity", "taxpayer", "taxid", "employeridentification", "federaltaxid")
_TAX_PARTS = frozenset({"ssn", "tin", "ein", "itin", "fein", "sin", "tfn"})

FORBIDDEN_KEYS = frozenset({
    "ip", "ip_address", "ipaddress", "ip_addr", "remote_addr", "remote_ip", "client_ip", "x_forwarded_for",
    "user_agent", "useragent", "device", "device_id", "device_fingerprint", "fingerprint",
    "dob", "date_of_birth", "birth_date", "birthdate", "birthday", "birth_year", "year_of_birth", "age", "age_years",
    "government_id", "passport", "passport_number", "drivers_license", "driver_license", "national_id",
    "card", "card_number", "credit_card", "debit_card", "pan", "cvv", "cvc", "expiry", "card_expiry",
    "account_number", "bank_account", "routing_number", "iban", "swift", "bic",
    "gender", "race", "ethnicity", "religion", "health", "medical", "sexual_orientation",
})
_FORBIDDEN_COMPACT = frozenset(k.replace("_", "") for k in FORBIDDEN_KEYS)
_FORBIDDEN_STEMS = ("dateofbirth", "birthdate", "birthday", "birthyear", "yearofbirth", "passport", "driverslicen",
                    "driverlicen", "cardnumber", "bankaccount", "routingnumber", "accountnumber")

MONEY_KEYS = frozenset({"fee", "product_value", "amount", "total", "auto_approve_max"})
ID_KEY = re.compile(r"(^|_)(id|ids|sha256|hash|hashes|handle|email)$")

_SEP = r"[\s._\-\u2010-\u2015\u2212]"     # a space, dot, underscore or any dash between digits, up to three
_NO_DIGIT_BEFORE = r"(?<![0-9])(?<![0-9]%s)(?<![0-9]%s%s)(?<![0-9]%s%s%s)" % ((_SEP,) * 6)
_NINE = r"[0-9](?:%s{0,3}[0-9]){8}(?![0-9])(?!%s{1,3}[0-9])" % (_SEP, _SEP)
# exactly nine digits, optionally separated, with no digit or letter touching the group (id and reference values)
_TIN_STANDALONE = re.compile(_NO_DIGIT_BEFORE + r"(?<![A-Za-z])" + _NINE + r"(?![A-Za-z])")
# in free text: exactly nine digits with no DIGIT touching the group (a letter may: "SSN123456789" is still refused)
_TIN_TEXT = re.compile(_NO_DIGIT_BEFORE + _NINE)


def _ascii_digits(value: str) -> str:
    """NFKC (fullwidth digits), then every other Unicode decimal digit (Arabic-Indic, Devanagari, ...) as ASCII."""
    v = unicodedata.normalize("NFKC", value)
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c for c in v)


def _compact(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", str(key)).lower())


def _parts(key: str) -> list[str]:
    k = re.sub(r"([a-z])([A-Z])", r"\1_\2", unicodedata.normalize("NFKC", str(key)))
    return [p for p in re.split(r"[^a-z0-9]+", k.lower()) if p]


def tax_key(key: str) -> bool:
    c = _compact(key)
    return c in _TAX_COMPACT or any(s in c for s in _TAX_STEMS) or any(p in _TAX_PARTS for p in _parts(key))


def forbidden_key(key: str) -> bool:
    c = _compact(key)
    return c in _FORBIDDEN_COMPACT or any(s in c for s in _FORBIDDEN_STEMS)


def tin_in(value: str, free_text: bool) -> bool:
    return bool((_TIN_TEXT if free_text else _TIN_STANDALONE).search(_ascii_digits(value)))


def problem(obj: Any, exempt: frozenset = frozenset(), _key: str = "", depth: int = 0) -> Optional[str]:
    """``TAX_ID_REFUSED`` or ``FORBIDDEN_FIELD`` for the first thing found, else None. ``exempt`` names keys whose
    VALUES are not scanned (the never-stored reply text); their keys are still checked."""
    if depth > 40:
        return "FORBIDDEN_FIELD"
    if isinstance(obj, dict):
        for k, v in obj.items():
            if tax_key(k):
                return "TAX_ID_REFUSED"
            if forbidden_key(k):
                return "FORBIDDEN_FIELD"
        for k, v in obj.items():
            if k in exempt or k in MONEY_KEYS:
                continue
            found = problem(v, exempt, str(k), depth + 1)
            if found:
                return found
        return None
    if isinstance(obj, list):
        for v in obj[:10_000]:
            found = problem(v, exempt, _key, depth + 1)
            if found:
                return found
        return None
    if isinstance(obj, str):
        free = not (ID_KEY.search(_key) or _key.endswith("_ref") or _key in ("request_id", "ref"))
        if tin_in(obj, free_text=free):
            return "TAX_ID_REFUSED"
    return None
