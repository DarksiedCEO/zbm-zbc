"""
Money for the Onboarding department — BUILD_CONTRACTS.md section 1.

- Python value: ``decimal.Decimal`` quantized to 0.01 with ROUND_HALF_UP at
  the moment it is created.
- Wire value: a JSON *string* with exactly two decimals, e.g. ``"12.30"``.
- Accepted input: ``int``, ``str``, ``Decimal``; ``float`` only by going
  through ``str(value)`` (never ``Decimal(float)``), so ``49.99`` from a
  JSON number stays exactly 49.99. NaN / Infinity / negatives / bools are
  rejected.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

CENT = Decimal("0.01")
WIRE_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$")
_INPUT_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def to_money(value: Any) -> Decimal:
    """Convert an accepted input to a quantized, non-negative Decimal."""
    if isinstance(value, bool):
        raise ValueError("money cannot be a boolean")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(str(value))  # never Decimal(float)
    elif isinstance(value, str):
        s = value.strip()
        if not _INPUT_PATTERN.match(s):
            raise ValueError("money string must be a non-negative decimal like '12.30'")
        d = Decimal(s)
    else:
        raise ValueError("money must be a string, int or Decimal")
    try:
        if not d.is_finite():
            raise ValueError("money must be finite")
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError("money must be finite") from exc
    if d < 0:
        raise ValueError("money cannot be negative")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(d: Decimal) -> str:
    return f"{d.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _positive(value: Any) -> Decimal:
    d = to_money(value)
    if d == 0:
        raise ValueError("money must be positive (0.00 rejected)")
    return d


Money = Annotated[Decimal, BeforeValidator(to_money), PlainSerializer(money_str, return_type=str)]
PositiveMoney = Annotated[Decimal, BeforeValidator(_positive), PlainSerializer(money_str, return_type=str)]
