"""
Money for the Onboarding department — BUILD_CONTRACTS.md section 1.

- Python value: ``decimal.Decimal`` with exactly two decimal places.
- Wire value: a JSON *string* with exactly two decimals, e.g. ``"12.30"``.

Inbound money is NEVER silently rounded (fix wave 1, F14/F15):

- ``parse_wire_money`` — money arriving from ANOTHER SERVICE (Revenue
  Recovery findings): only the canonical contract string
  ``^(0|[1-9][0-9]*)\\.[0-9]{2}$`` is accepted. No whitespace, no leading
  zeros, no sign, no exponent, no third decimal, no JSON number.
- ``to_money`` — money arriving in a request or configuration: a plain
  decimal string with at most two decimals and no leading zeros
  (``"12.3"``/``"12"`` are exact and accepted), an ``int``, a ``Decimal``
  with at most two decimals, or a ``float`` only through ``str(value)`` and
  only if that string has at most two decimals (``49.99`` yes,
  ``0.1 + 0.2`` no). Anything that would need rounding is rejected.
- Both reject values above ``MAX_MONEY`` (a ValueError, i.e. 422 — never a
  ``decimal.InvalidOperation`` 500) and NaN/Infinity/negatives/bools.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

CENT = Decimal("0.01")
WIRE_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.[0-9]{2}")
_INPUT_PATTERN = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]{1,2})?")
# One trillion dollars minus a cent: far above any real onboarding figure,
# far below Decimal's 28-digit context (so quantize can never overflow).
MAX_MONEY = Decimal("999999999999.99")
_MAX_TEXT = 32  # longer text is refused before Decimal ever sees it


def _check_range(d: Decimal) -> Decimal:
    if not d.is_finite():
        raise ValueError("money must be finite")
    if d.is_signed() and d != 0:
        raise ValueError("money cannot be negative")
    if d > MAX_MONEY:
        raise ValueError("money is above the maximum accepted amount")
    if d.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValueError("money must have at most two decimal places (it is never rounded)")
    return d.copy_abs().quantize(CENT)  # exact: at most two places already


def parse_wire_money(value: Any) -> Decimal:
    """Strict contract form, for money from other services."""
    if not isinstance(value, str) or len(value) > _MAX_TEXT or not WIRE_PATTERN.fullmatch(value):
        raise ValueError("money from another service must be a canonical two-decimal string like '12.30'")
    return _check_range(Decimal(value))


def to_money(value: Any) -> Decimal:
    """Convert an accepted request/config input to a Decimal, without rounding."""
    if isinstance(value, bool):
        raise ValueError("money cannot be a boolean")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        if abs(value) > 10**15:
            raise ValueError("money is above the maximum accepted amount")
        d = Decimal(value)
    elif isinstance(value, float):
        s = str(value)  # never Decimal(float)
        if not _INPUT_PATTERN.fullmatch(s):
            raise ValueError("money must be a plain decimal with at most two decimal places")
        d = Decimal(s)
    elif isinstance(value, str):
        if len(value) > _MAX_TEXT or not _INPUT_PATTERN.fullmatch(value):
            raise ValueError("money string must be a non-negative decimal like '12.30' (no spaces, no leading zeros, "
                             "at most two decimals)")
        d = Decimal(value)
    else:
        raise ValueError("money must be a string, int or Decimal")
    try:
        return _check_range(d)
    except InvalidOperation as exc:  # pragma: no cover - defensive; the range check comes first
        raise ValueError("money is not a valid amount") from exc


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
