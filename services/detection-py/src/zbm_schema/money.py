"""
Exact money for Revenue Recovery (README gap #6, fixed Sep 24 2026).

Every dollar figure in detection-py is a `decimal.Decimal` quantized to
cents with ROUND_HALF_UP at the point it is created or computed — never a
binary float. On the wire (JSON) money is a string with exactly two
decimal places and at most 15 integer digits, matching
`^(0|[1-9][0-9]{0,14})\\.[0-9]{2}$` (build contract section 1, bounded by
docs/adr/0003-money-decimal-and-ledger-events.md section 1a).

Magnitude bound (fix wave 1, F14): every money amount is < 10^15 dollars,
i.e. at most MAX_MONEY = 999999999999999.99. Before the bound, a value like
"1" + "0"*30 + ".00" made `quantize` raise decimal.InvalidOperation (the
default context holds 28 significant digits), which pydantic does not turn
into a validation error, so the request died with 500.

Explicit context: every Decimal operation on money runs under
MONEY_CONTEXT (via `money_context()`), never the ambient thread context,
so no caller or library that changes `decimal.getcontext()` can make money
arithmetic round or raise. Why its precision (50) is enough for every
value the contract admits:
  - an amount has at most 17 significant digits (15 integer + 2 fraction);
  - a sum or difference of in-range amounts has at most 18;
  - a line product unit_price * quantity only exists for orders whose
    subtotal was already checked, in exact integer cents, to be <= MAX_MONEY
    (Order validator), so it has at most 17;
  - percent_of multiplies an amount (17 digits) by a percentage that comes
    from a float's shortest repr (at most 17 significant digits): at most 34
    digits, then an exact division by 100.
All of these fit in 50 digits exactly, so the only rounding that ever
happens is the deliberate ROUND_HALF_UP quantize to cents.

Input rules (`to_money`):
  - `str`: ONLY the canonical wire form (same verdicts as orchestrator-go,
    the dashboard and the ledger — see fixtures/money_vectors.json). No
    rounding of strings: "12.3", "12.345", "012.30", "1.00\\n" are rejected.
  - `Decimal` and `int` are accepted and quantized half-up to cents.
  - `float` is accepted ONLY by converting through `str(value)` first —
    never `Decimal(float)` — so a fixture JSON number like 49.99 becomes
    exactly Decimal("49.99"), not 49.99000000000000198951966012828052043914794921875.
  - `bool`, NaN, +/-Infinity, exponent notation, negatives and anything
    >= 10^15 (after rounding to cents) are rejected with ValueError.
  - When a model is validated from JSON text (`model_validate_json`, which
    is how the HTTP API parses every request body), a JSON number is
    rejected outright: money on the wire is a string (contract section 1),
    exactly as orchestrator-go and the ledger already enforce.
"""

from __future__ import annotations

import math
import re
from contextlib import contextmanager
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Annotated, Any, Iterator

from pydantic import BeforeValidator, Field, PlainSerializer, ValidationInfo

CENT = Decimal("0.01")
HUNDRED = Decimal(100)

# Contract bound (ADR 0003 section 1a): amounts are < 10^15 dollars.
MAX_MONEY_INTEGER_DIGITS = 15
MAX_MONEY = Decimal("999999999999999.99")
_LIMIT = Decimal(10) ** MAX_MONEY_INTEGER_DIGITS  # first value that is out of range

# The one context every money operation runs under (see module docstring
# for why 50 digits are always enough). Signals that would mean a bug —
# invalid operation, division by zero, overflow — raise instead of
# producing NaN/Infinity.
MONEY_CONTEXT = Context(
    prec=50,
    rounding=ROUND_HALF_UP,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


@contextmanager
def money_context() -> Iterator[Context]:
    """Run a block of money arithmetic under MONEY_CONTEXT."""
    with localcontext(MONEY_CONTEXT) as ctx:
        yield ctx


# Float input only: str(float) of a finite float in range, e.g. "49.99".
# fullmatch + re.ASCII: no trailing newline, no non-ASCII digits.
_FLOAT_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?", re.ASCII)

# The canonical wire form (contract section 1 + the section 1a bound).
# Negative values never go on the wire: every serialized money field is
# constrained ge=0 or gt=0. Always use WIRE_PATTERN.fullmatch — with
# re.match, "$" would also match before a trailing "\n".
WIRE_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,14})\.[0-9]{2}$", re.ASCII)
_MAX_WIRE_LENGTH = MAX_MONEY_INTEGER_DIGITS + 3


class MoneyRangeError(ValueError):
    """A money value outside [0, MAX_MONEY] (or, for signed intermediate
    values, outside (-10^15, 10^15))."""


def _preview(value: Any) -> str:
    """A short, safe description of a rejected value for error messages
    (never repr() an arbitrarily large int or string in full)."""
    if isinstance(value, str):
        return repr(value[:32]) + ("..." if len(value) > 32 else "")
    if isinstance(value, int) and not isinstance(value, bool):
        return repr(value) if abs(value) < 10**20 else "a very large integer"
    if isinstance(value, float) and math.isfinite(value) and abs(value) < 1e20:
        return repr(value)
    if isinstance(value, Decimal) and value.is_finite() and value.adjusted() < 20:
        return repr(value)
    return f"a {type(value).__name__}"


def quantize_money(value: Decimal) -> Decimal:
    """Round an exact Decimal to cents, half-up. The one rounding rule.

    Accepts signed values (e.g. ContractTerm.drift_usd) but only within
    (-10^15, 10^15) after rounding; anything else raises MoneyRangeError
    (a ValueError) — never decimal.InvalidOperation.
    """
    if not isinstance(value, Decimal):
        raise ValueError(f"quantize_money needs a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError("money must be a finite number (NaN/Infinity rejected)")
    with money_context():
        # Exact comparison (no precision involved) before quantize, so an
        # out-of-range value can never reach an operation that would trap.
        if value.copy_abs() >= _LIMIT:
            raise MoneyRangeError(
                f"money must be less than 10^{MAX_MONEY_INTEGER_DIGITS} dollars (max {MAX_MONEY})"
            )
        q = value.quantize(CENT)
        if q.copy_abs() > MAX_MONEY:  # e.g. 999999999999999.995 rounds up to 10^15
            raise MoneyRangeError(
                f"money must be less than 10^{MAX_MONEY_INTEGER_DIGITS} dollars (max {MAX_MONEY})"
            )
    # Never produce a signed zero: Decimal("-0.001") quantizes to
    # Decimal("-0.00"), which compares >= 0 yet would serialize as "-0.00",
    # violating the wire pattern.
    return q.copy_abs() if q.is_zero() else q


def to_money(value: Any) -> Decimal:
    """Convert an accepted input to a cent-quantized Decimal, or raise ValueError."""
    if isinstance(value, bool):  # bool is an int subclass — never money
        raise ValueError("money must be a number or decimal string, not a boolean")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)  # exact for any int; no context involved
    elif isinstance(value, float):
        # The only permitted float path: through its shortest repr string.
        text = str(value)
        if not _FLOAT_TEXT.fullmatch(text):
            raise ValueError(f"money float {text!r} is not a finite plain decimal in range")
        d = Decimal(text)
    elif isinstance(value, str):
        if len(value) > _MAX_WIRE_LENGTH or not WIRE_PATTERN.fullmatch(value):
            raise ValueError(
                f"money string {_preview(value)} is not a canonical two-decimal amount like '12.30' "
                f"(must match {WIRE_PATTERN.pattern}, max {MAX_MONEY})"
            )
        d = Decimal(value)
    else:
        raise ValueError(f"money must be Decimal, int, str or float, got {type(value).__name__}")
    if not d.is_finite():
        raise ValueError("money must be a finite number (NaN/Infinity rejected)")
    if d.is_signed():
        # Every money field on the wire is non-negative (contract section 1);
        # a negative input — including "-0.00" / "-0.001" — is rejected
        # outright rather than silently clamped. Signed intermediate values
        # (e.g. ContractTerm.drift_usd) go through quantize_money, not here.
        raise ValueError(f"money must not be negative, got {_preview(value)}")
    return quantize_money(d)


def _validate_money(value: Any, info: ValidationInfo) -> Decimal:
    # JSON text input (model_validate_json — every HTTP request body): money
    # must be a JSON string. A JSON number arrives here as int/float and is
    # rejected, so "12.345" sent as a number is not silently rounded.
    if info.mode == "json" and not isinstance(value, str):
        raise ValueError(
            f"money must be a JSON string like \"12.30\", got a JSON {type(value).__name__}"
        )
    return to_money(value)


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
    with money_context():
        return quantize_money(amount * pct / HUNDRED)


_serialize = PlainSerializer(format_money, return_type=str, when_used="json")

# Any money value, zero allowed (e.g. an actual billed amount of 0.00).
Money = Annotated[Decimal, BeforeValidator(_validate_money), _serialize, Field(ge=0)]

# Positive-only money: rejects "0.00" (and anything that rounds to it).
PositiveMoney = Annotated[Decimal, BeforeValidator(_validate_money), _serialize, Field(gt=0)]
