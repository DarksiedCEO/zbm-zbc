"""
ISO 3166 code lists (AEGIS N14-3). No network: the lists ship with the
service in ``data/iso3166.json`` (codes only; see ``SOURCE`` for where they
were copied from and the SHA-256 of the source files).

A code the list does not know is UNKNOWN, and an unknown code is refused
by the Jurisdiction Resolver (never guessed, never aliased): ``CA-PQ`` and
``CA-QUE`` (old Quebec abbreviations) are unknown and refused; ``CA-QC`` is
known and refused by HR-07. Names are never accepted anywhere (facts carry
ISO codes only), so there is no name normalisation to do.
"""

from __future__ import annotations

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "iso3166.json")

with open(_PATH, "rb") as _fh:
    _DATA = json.loads(_fh.read())

SOURCE: str = _DATA["source"]
SOURCE_SHA256: dict = _DATA["source_sha256"]
COUNTRIES: frozenset[str] = frozenset(_DATA["countries"])
SUBDIVISIONS: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in _DATA["subdivisions"].items()}
_ALL_SUBDIVISIONS: frozenset[str] = frozenset(c for v in SUBDIVISIONS.values() for c in v)


def is_known_country(code: object) -> bool:
    return isinstance(code, str) and code in COUNTRIES


def is_known_subdivision(code: object) -> bool:
    return isinstance(code, str) and code in _ALL_SUBDIVISIONS


def is_known(code: object) -> bool:
    """An ISO 3166-1 alpha-2 country or an ISO 3166-2 subdivision on the shipped list."""
    if not isinstance(code, str):
        return False
    return is_known_subdivision(code) if "-" in code else is_known_country(code)


def is_known_register_code(code: object) -> bool:
    """Register rows may also name ``EU`` or ``ALL`` (spec B.1)."""
    return code in ("EU", "ALL") or is_known(code)


__all__ = ["COUNTRIES", "SOURCE", "SOURCE_SHA256", "SUBDIVISIONS", "is_known", "is_known_country",
           "is_known_register_code", "is_known_subdivision"]
