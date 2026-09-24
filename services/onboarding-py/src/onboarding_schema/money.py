"""
Money for the Onboarding department — BUILD_CONTRACTS.md section 1, as
amended by ADR 0003 section 1a.

- Python value: ``decimal.Decimal`` with exactly two decimal places.
- Wire value: a JSON *string* in the contract form
  ``^(0|[1-9][0-9]{0,14})\\.[0-9]{2}$`` — at most 15 integer digits, so every
  amount is < 10^15 dollars (largest: ``"999999999999999.99"``).

ONE rule for contract money (fix wave 3, F15): money in a request, in
configuration and from another service is accepted ONLY as that exact
string. ``"12.3"``, ``"12"``, ``" 12.30"``, ``"012.30"``, a JSON number
(``12.30``, ``0.1``, ``12``, ``1e3``), a bool and anything at or above
10^15 are rejected (a ValueError, i.e. 422 — never rounded, never a
``decimal.InvalidOperation`` 500). Verified against every vector of
``fixtures/money_vectors.json`` (tests/test_fix_wave3.py).

A ``Decimal`` is accepted only from code inside this service (a JSON body
can never produce one): finite, non-negative, at most two decimals, < 10^15.

``client_stated_amount`` is NOT contract money: it reads a figure the
client typed as a free-form intake fact (e.g. stated monthly revenue),
used only for the Risk and Anomaly comparison; it never becomes a ledger
amount or a client-facing figure.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Optional

from pydantic import BeforeValidator, PlainSerializer

CENT = Decimal("0.01")
# The contract pattern (ADR 0003 section 1a), fullmatched.
WIRE_PATTERN = re.compile(r"(0|[1-9][0-9]{0,14})\.[0-9]{2}")
MAX_MONEY = Decimal("999999999999999.99")
_MAX_TEXT = 18  # the contract's max_length
_STATED_PATTERN = re.compile(r"(0|[1-9][0-9]{0,14})(\.[0-9]{1,2})?")


def _check_range(d: Decimal) -> Decimal:
    if not d.is_finite():
        raise ValueError("money must be finite")
    if d.is_signed() and d != 0:
        raise ValueError("money cannot be negative")
    if d > MAX_MONEY:
        raise ValueError("money is above the maximum accepted amount (must be < 10^15)")
    if d.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValueError("money must have at most two decimal places (it is never rounded)")
    return d.copy_abs().quantize(CENT)  # exact: at most two places already


def parse_wire_money(value: Any) -> Decimal:
    """The contract form: a canonical two-decimal string, < 10^15."""
    if not isinstance(value, str) or len(value) > _MAX_TEXT or not WIRE_PATTERN.fullmatch(value):
        raise ValueError("money must be a JSON string in the contract form like '12.30' (two decimals, no spaces, "
                         "no leading zeros, no sign or exponent, less than 10^15)")
    return _check_range(Decimal(value))


def to_money(value: Any) -> Decimal:
    """Contract money from a request, configuration or another service: the
    contract string only (a Decimal only from inside this service)."""
    if isinstance(value, Decimal):
        try:
            return _check_range(value)
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise ValueError("money is not a valid amount") from exc
    return parse_wire_money(value)


def client_stated_amount(value: Any) -> Optional[Decimal]:
    """A dollar figure the client typed as an intake fact ("10000",
    "10000.5", 10000). None when it is not a plain non-negative number with
    at most two decimals below 10^15. Never contract money."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or len(value) > _MAX_TEXT or not _STATED_PATTERN.fullmatch(value):
        return None
    return _check_range(Decimal(value))


def money_str(d: Decimal) -> str:
    return f"{d.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _positive(value: Any) -> Decimal:
    d = to_money(value)
    if d == 0:
        raise ValueError("money must be positive (0.00 rejected)")
    return d


def _positive_wire(value: Any) -> Decimal:
    d = parse_wire_money(value)
    if d == 0:
        raise ValueError("money must be positive (0.00 rejected)")
    return d


Money = Annotated[Decimal, BeforeValidator(to_money), PlainSerializer(money_str, return_type=str)]
PositiveMoney = Annotated[Decimal, BeforeValidator(_positive), PlainSerializer(money_str, return_type=str)]
# Money that arrives from another service (Revenue Recovery findings).
WireMoney = Annotated[Decimal, BeforeValidator(parse_wire_money), PlainSerializer(money_str, return_type=str)]
PositiveWireMoney = Annotated[Decimal, BeforeValidator(_positive_wire), PlainSerializer(money_str, return_type=str)]
