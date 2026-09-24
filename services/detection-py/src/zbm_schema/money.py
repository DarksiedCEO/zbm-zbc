"""
Exact money for Revenue Recovery (README gap #6, fixed Sep 24 2026).

Every dollar figure in detection-py is a `decimal.Decimal` quantized to
cents with ROUND_HALF_UP at the point it is created or computed — never a
binary float. On the wire (JSON) money is a string with exactly two
decimal places, matching `^(0|[1-9][0-9]*)\\.[0-9]{2}$` (build contract
section 1, see docs/adr/0003-money-decimal-and-ledger-events.md).

Input rules (`to_money`):
  - `Decimal`, `int` and decimal strings are accepted.
  - `float` is accepted ONLY by converting through `str(value)` first —
    never `Decimal(float)` — so a fixture JSON number like 49.99 becomes
    exactly Decimal("49.99"), not 49.99000000000000198951966012828052043914794921875.
  - `bool`, NaN, +/-Infinity, exponent notation and anything else are rejected.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, PlainSerializer

CENT = Decimal("0.01")
HUNDRED = Decimal(100)

# Accepted textual input: optional sign, digits, optional fraction. No
# exponent, no whitespace, no "NaN"/"Infinity" spellings.
_DECIMAL_TEXT = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")

# The canonical wire form (contract section 1). Negative values never go on
# the wire: every serialized money field is constrained ge=0 or gt=0.
WIRE_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$")


def quantize_money(value: Decimal) -> Decimal:
    """Round an exact Decimal to cents, half-up. The one rounding rule."""
    if not value.is_finite():
        raise ValueError("money must be a finite number (NaN/Infinity rejected)")
    q = value.quantize(CENT, rounding=ROUND_HALF_UP)
    # Never produce a signed zero: Decimal("-0.001") quantizes to
    # Decimal("-0.00"), which compares >= 0 yet would serialize as "-0.00",
    # violating the wire pattern.
    return q.copy_abs() if q.is_zero() else q


def to_money(value: Any) -> Decimal:
    """Convert an accepted input to a cent-quantized Decimal, or raise."""
    if isinstance(value, bool):  # bool is an int subclass — never money
        raise ValueError("money must be a number or decimal string, not a boolean")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        # The only permitted float path: through its shortest repr string.
        text = str(value)
        if not _DECIMAL_TEXT.match(text):
            raise ValueError(f"money float {text!r} is not a finite plain decimal")
        d = Decimal(text)
    elif isinstance(value, str):
        if not _DECIMAL_TEXT.match(value):
            raise ValueError(f"money string {value!r} is not a plain decimal like '12.30'")
        try:
            d = Decimal(value)
        except InvalidOperation as e:  # pragma: no cover — regex already guards this
            raise ValueError(f"money string {value!r} is not a decimal") from e
    else:
        raise ValueError(f"money must be Decimal, int, str or float, got {type(value).__name__}")
    if d.is_signed():
        # Every money field on the wire is non-negative (contract section 1);
        # a negative input — including "-0.00" / "-0.001" — is rejected
        # outright rather than silently clamped. Signed intermediate values
        # (e.g. ContractTerm.drift_usd) go through quantize_money, not here.
        raise ValueError(f"money must not be negative, got {value!r}")
    return quantize_money(d)


def format_money(value: Decimal) -> str:
    """Canonical two-decimal text, e.g. Decimal('12.3') -> '12.30'.

    Used both for JSON serialization and for every dollar figure written
    into a client-facing explanation, so the Hallucination Agent compares
    like with like (see safety/hallucination_check.py).
    """
    return f"{quantize_money(value):f}"


def percent_of(amount: Decimal, percent: float | int | Decimal) -> Decimal:
    """`percent`% of a money amount, exact, quantized half-up to cents.

    `percent` may be a non-money float (e.g. DiscountApplication.percent_off);
    it is converted through str() so 15.0 is exactly 15 and 12.5 exactly 12.5.
    """
    if isinstance(percent, bool):
        raise ValueError("percent must be numeric")
    if isinstance(percent, float):
        pct = Decimal(str(percent))
    else:
        pct = Decimal(percent)
    if not pct.is_finite():
        raise ValueError("percent must be finite")
    return quantize_money(amount * pct / HUNDRED)


_serialize = PlainSerializer(format_money, return_type=str, when_used="json")

# Any money value, zero allowed (e.g. an actual billed amount of 0.00).
Money = Annotated[Decimal, BeforeValidator(to_money), _serialize, Field(ge=0)]

# Positive-only money: rejects "0.00" (and anything that rounds to it).
PositiveMoney = Annotated[Decimal, BeforeValidator(to_money), _serialize, Field(gt=0)]
