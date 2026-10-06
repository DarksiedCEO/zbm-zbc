"""Partner tax information: references and tokens only (ADR 0016 decision 16).

Decides: whether a tax reference is acceptable and whether a partner request body carries a raw taxpayer id. A
reference is ``vault:tax:<16..64 of A-Z a-z 0-9 _ ->`` or ``tok:<same>``, and may hold no run of nine or more
digits once ``-``, ``_`` and spaces are ignored (an SSN, ITIN or EIN written into a "reference" is still a raw id).
Any other string field of a partner body that, NFKC-normalised with every non-alphanumeric character dropped,
holds nine digits in a row, or that holds an SSN / ITIN shape (3-2-4 digits), an EIN shape (2-7 digits),
nine digits in a row (separators ``-``, ``_``, ``.``, ``/`` and spaces ignored), or a TIN / SSN / EIN / ITIN label followed by digits (or by nine digits however spaced) is refused (422 TAX_ID_RAW_REFUSED); keys
named like a tax id (``tin``, ``ssn``, ``ein``, ``tax_id`` ...) are refused anywhere in any body by the API's
forbidden-key check. Never: stores, logs or echoes the value it refused."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

NUMBER = 12
NAME = "tax_reference_guard"
DECIDES = "tax info as a vault reference or token only; raw TIN / SSN / EIN refused"

REF = re.compile(r"(vault:tax|tok):[A-Za-z0-9_-]{16,64}")
_RAW = (
    re.compile(r"(?<!\d)\d{3}[\s._/-]?\d{2}[\s._/-]?\d{4}(?!\d)"),   # SSN / ITIN, with or without separators
    re.compile(r"(?<!\d)\d{2}[\s._/-]?\d{7}(?!\d)"),                  # EIN
    re.compile(r"(?i)\b(tin|ssn|ein|itin|tax ?id|taxpayer|social security)\b\W{0,5}\d"),
)
_LABEL = re.compile(r"(?i)\b(tin|ssn|ein|itin|tax ?id\w*|taxpayer|social security|employer identification)\b")
# money values (``1234567.89`` is nine digits once the point is dropped), versions, server-issued ids and hashes the
# service checks against its own records; request_id is NOT skipped (AEGIS round 1 M2)
SKIP_KEYS = frozenset({"rate_pct", "deal_value", "amount", "value", "version", "content_sha256", "binding_sha256",
                       "item_sha256", "partner_id", "deal_id"})


PAYEE_REF = re.compile(r"fin:[A-Za-z0-9._-]{4,120}")
MAX_REF_DIGITS = 8


def ref_ok(ref: str, rx: re.Pattern = REF) -> bool:
    """The exact shape, and fewer than nine digits IN TOTAL: no arrangement of a nine-digit id fits in a reference
    (a token that happens to hold more digits is refused; the issuer re-mints it)."""
    if not isinstance(ref, str) or not rx.fullmatch(ref):
        return False
    return sum(c.isdigit() for c in ref) <= MAX_REF_DIGITS


def payee_ref_ok(ref: str) -> bool:
    return ref_ok(ref, PAYEE_REF)


_GROUPS = re.compile(r"\d+(?:[^\w]{1,3}\d+)*")
_SHAPES = ((3, 2, 4), (2, 7), (9,))


def raw_tax_id(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text)
    # AEGIS round 1 M2: every separator dropped first ("x1-23456789", "1 2 3 4 5 6 7 8 9"): nine digits in a row
    if re.search(r"\d{9}", "".join(ch for ch in t if ch.isalnum())):
        return True
    text = t
    if any(rx.search(text) for rx in _RAW):
        return True
    # digit groups joined by ANY one to three non-word characters ("123|45|6789", "12 : 3456789")
    for m in _GROUPS.finditer(text):
        if tuple(len(g) for g in re.findall(r"\d+", m.group())) in _SHAPES:
            return True
    # a tax-id label followed, within 40 characters, by nine digits however they are spaced ("taxpayer id 1 23 45 6789")
    return any(len(re.sub(r"\D", "", text[m.end():m.end() + 40])) >= 9 for m in _LABEL.finditer(text))


def body_has_raw_tax_id(obj: Any, depth: int = 0) -> bool:
    if depth > 20:
        return True
    if isinstance(obj, dict):
        return any(body_has_raw_tax_id(v, depth + 1) for k, v in obj.items() if k not in SKIP_KEYS)
    if isinstance(obj, list):
        return any(body_has_raw_tax_id(v, depth + 1) for v in obj)
    return isinstance(obj, str) and raw_tax_id(obj)
