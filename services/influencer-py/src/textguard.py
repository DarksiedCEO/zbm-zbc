"""What this service never accepts (ADR 0015 decisions 7 and 13), checked on the raw JSON before the model parses it.

1. **Raw tax identification numbers** (Andre, Oct 6 2026: never store a raw TIN, SSN or EIN; store a reference only):
   - a key that names one, at any depth, in any spelling (``ssn``, ``SSN``, ``taxId``, ``tax-id``, ``ein``, ``itin``,
     ``taxpayer_identification_number``, ``social_security_number``, ``tin_last4`` ...): ``TAX_ID_REFUSED``;
   - a VALUE that has the shape of one, anywhere in the body (after Unicode NFKC, any script's digits as ASCII): a
     number of exactly nine digits, where up to MAX_GAP characters that are not letters or digits — spaces,
     punctuation, symbols, combining marks, invisible format characters — between two digits do not end the number
     (``123-45-6789``, ``123​45​6789``, ``123/45/6789``, ``12:3456789``, ``123    45    6789``): ``TAX_ID_REFUSED``. In
     free text (captions, briefs, DMs, templates, names, notes) the classic 3-2-4 and 2-7 shapes are refused even
     inside a longer run, and a letter may touch the number; in ids and references a number some letter touches is an
     id (``x123456789``), one no letter touches is refused (``acct_123456789``); a 10-digit phone number is never
     nine digits. ``tax_ref`` is stricter: ANY number of nine or more digits is refused (AEGIS R1-M3). Money fields are not scanned (they
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
ID_KEY = re.compile(r"(^|_)(id|ids|sha256|hash|hashes|handle|email|token)$")

MAX_GAP = 8          # non-alphanumeric characters (spaces, punctuation, symbols, invisible format characters) allowed
                     # between two digits of one number (AEGIS R1-M3)
_CLASSIC = (re.compile(r"(?<![0-9])[0-9]{3}[\W_]{1,%d}[0-9]{2}[\W_]{1,%d}[0-9]{4}(?![0-9])" % (MAX_GAP, MAX_GAP)),
            re.compile(r"(?<![0-9])[0-9]{2}[\W_]{1,%d}[0-9]{7}(?![0-9])" % MAX_GAP),
            re.compile(r"(?<![0-9])[0-9]{9}(?![0-9])"))


def _ascii_digits(value: str) -> str:
    """NFKC (fullwidth digits), then every other Unicode decimal digit (Arabic-Indic, Devanagari, ...) as ASCII."""
    v = unicodedata.normalize("NFKC", value)
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c for c in v)


def digit_groups(value: str) -> list[tuple[int, bool, bool]]:
    """Every number in ``value`` as (digit count, a letter immediately before it, a letter immediately after it).
    Digits separated by up to MAX_GAP characters that are not letters or digits — any space, punctuation, symbol,
    combining mark or invisible format character — belong to one number (``123​45​6789``, ``123/45/6789``,
    ``12:3456789``, ``123 ⁃ 45 ⁃ 6789`` are each one nine-digit number)."""
    v = _ascii_digits(value)
    out, i, n = [], 0, len(v)
    while i < n:
        if not ("0" <= v[i] <= "9"):
            i += 1
            continue
        start, count, j = i, 0, i
        last = i
        while j < n:
            if "0" <= v[j] <= "9":
                count += 1
                last = j
                j += 1
                continue
            k = j
            while k < n and k - j < MAX_GAP and not v[k].isalnum():
                k += 1
            if k < n and k - j <= MAX_GAP and k > j and "0" <= v[k] <= "9":
                j = k
                continue
            break
        before = start > 0 and v[start - 1].isalpha()
        after = last + 1 < n and v[last + 1].isalpha()
        out.append((count, before, after))
        i = last + 1
    return out


def _classic(value: str) -> bool:
    v = _ascii_digits(value)
    return any(rx.search(v) for rx in _CLASSIC)


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


STRICT_REF_KEYS = frozenset({"tax_ref"})


def tin_in(value: str, free_text: bool) -> bool:
    """Free text: any nine-digit number, or the classic 3-2-4 / 2-7 shapes. Ids and references: a nine-digit number
    that no letter touches (``acct_123456789`` and ``ev-123456789`` are refused, ``x123456789`` is an id)."""
    if _classic(value) and free_text:
        return True
    for count, before, after in digit_groups(value):
        if count == 9 and (free_text or not (before or after)):
            return True
    return False


def long_digit_run(value: str) -> bool:
    """References that point at tax data (``tax_ref``): ANY number of nine or more digits is refused (AEGIS R1-M3)."""
    return any(count >= 9 for count, _, _ in digit_groups(value)) or _classic(value)


def _seg(key) -> str:
    """A path segment for an error: the key's name, or ``*`` when the key itself could carry the value (AEGIS R2-L-c:
    name the field, never echo the value)."""
    k = str(key)
    return "*" if len(k) > 64 or tin_in(k, True) or not re.fullmatch(r"[A-Za-z0-9_.-]+", k) else k


def find(obj: Any, exempt: frozenset = frozenset(), _key: str = "", _path: str = "",
         depth: int = 0) -> Optional[tuple[str, str]]:
    """(``TAX_ID_REFUSED`` or ``FORBIDDEN_FIELD``, the field's path such as ``handles[].handle``) for the first thing
    found, else None. ``exempt`` names keys whose VALUES are not scanned; their keys are still checked."""
    if depth > 40:
        return "FORBIDDEN_FIELD", _path or "body"
    if isinstance(obj, dict):
        for k in obj:
            p = f"{_path}.{_seg(k)}" if _path else _seg(k)
            if tax_key(k) or tin_in(str(k), True):       # a key can carry the number too (R2-L-c)
                return "TAX_ID_REFUSED", p
            if forbidden_key(k):
                return "FORBIDDEN_FIELD", p
        for k, v in obj.items():
            if k in exempt or k in MONEY_KEYS:
                continue
            found = find(v, exempt, str(k), f"{_path}.{_seg(k)}" if _path else _seg(k), depth + 1)
            if found:
                return found
        return None
    if isinstance(obj, list):
        for v in obj[:10_000]:
            found = find(v, exempt, _key, f"{_path}[]", depth + 1)
            if found:
                return found
        return None
    if isinstance(obj, str):
        if _key in STRICT_REF_KEYS and long_digit_run(obj):
            return "TAX_ID_REFUSED", _path or "body"
        free = not (ID_KEY.search(_key) or _key.endswith("_ref") or _key in ("request_id", "ref", "requester_key"))
        if tin_in(obj, free_text=free):
            return "TAX_ID_REFUSED", _path or "body"
    return None


def problem(obj: Any, exempt: frozenset = frozenset()) -> Optional[str]:
    found = find(obj, exempt)
    return found[0] if found else None
