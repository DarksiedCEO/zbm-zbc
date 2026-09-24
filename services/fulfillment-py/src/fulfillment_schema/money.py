"""
Exact money for Fulfillment (fix wave 1, F15).

Mirrors services/detection-py/src/zbm_schema/money.py rule for rule (it is
copied, not imported: services do not import each other). The contract is
BUILD_CONTRACTS section 1 as amended by
docs/adr/0003-money-decimal-and-ledger-events.md section 1a, and the shared
verdicts are fixtures/money_vectors.json (tests/test_fix_wave_1_f15_money.py).

F15 (fulfillment part): `LabeledValue.amount_usd` used to run any string
through Decimal() and then quantize it, so "1e3", " 12.30 ", "012.30" and
"12.3" were accepted and "12.345" was silently rounded up to "12.35". Two
paths are now kept apart:

  - PARSE FROM WIRE (`str`, and anything validated from JSON text): only the
    canonical form `^(0|[1-9][0-9]{0,14})\\.[0-9]{2}$` (ASCII, fullmatch, at
    most 18 characters) is accepted. Strings are never rounded. When a model
    is validated from JSON text (`model_validate_json` — how FastAPI parses a
    request body), a JSON number is rejected outright: money on the wire is
    a string.
  - CONSTRUCT FROM A COMPUTED VALUE (`Decimal`, `int`; `float` only through
    `str(value)`, never `Decimal(float)`): quantized half-up to cents under
    MONEY_CONTEXT. A value that arises from internal arithmetic, e.g.
    Decimal("0.125"), is therefore rounded ("0.13"), not rejected by the wire
    rule. It is still rejected if it is NaN/Infinity, negative, a bool, or
    >= 10^15 after rounding.

Explicit context: every Decimal operation on money runs under MONEY_CONTEXT
(precision 50, ROUND_HALF_UP, traps InvalidOperation / DivisionByZero /
Overflow) via `money_context()`, never the ambient thread context. An
amount has at most 17 significant digits, so 50 digits hold every exact
result this service could compute; the only rounding is the deliberate
quantize to cents. Fulfillment does no money arithmetic today (no agent
populates LabeledValue yet); a future one must use `money_context()`.
"""

from __future__ import annotations

import math
import re
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Annotated, Any, Iterator

from pydantic import BeforeValidator, Field, PlainSerializer, ValidationInfo

CENT = Decimal("0.01")

# Contract bound (ADR 0003 section 1a): amounts are < 10^15 dollars.
MAX_MONEY_INTEGER_DIGITS = 15
MAX_MONEY = Decimal("999999999999999.99")
_LIMIT = Decimal(10) ** MAX_MONEY_INTEGER_DIGITS  # first value that is out of range

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
_FLOAT_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?", re.ASCII)

# The canonical wire form. Always WIRE_PATTERN.fullmatch — with re.match,
# "$" would also match before a trailing "\n".
WIRE_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,14})\.[0-9]{2}$", re.ASCII)
_MAX_WIRE_LENGTH = MAX_MONEY_INTEGER_DIGITS + 3


class MoneyRangeError(ValueError):
    """A money value outside [0, MAX_MONEY] (or, signed, outside (-10^15, 10^15))."""


def _preview(value: Any) -> str:
    """A short, safe description of a rejected value (never repr() an
    arbitrarily large int or string in full)."""
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
    """Round an exact Decimal to cents, half-up, under MONEY_CONTEXT. Raises
    MoneyRangeError (a ValueError) out of range — never InvalidOperation.
    Never returns a signed zero."""
    if not isinstance(value, Decimal):
        raise ValueError(f"quantize_money needs a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError("money must be a finite number (NaN/Infinity rejected)")
    with money_context():
        # Exact comparison before quantize, so an out-of-range value never
        # reaches an operation that would trap.
        if value.copy_abs() >= _LIMIT:
            raise MoneyRangeError(f"money must be less than 10^{MAX_MONEY_INTEGER_DIGITS} dollars (max {MAX_MONEY})")
        q = value.quantize(CENT)
        if q.copy_abs() > MAX_MONEY:  # e.g. 999999999999999.995 rounds up to 10^15
            raise MoneyRangeError(f"money must be less than 10^{MAX_MONEY_INTEGER_DIGITS} dollars (max {MAX_MONEY})")
    return q.copy_abs() if q.is_zero() else q


def to_money(value: Any) -> Decimal:
    """Convert an accepted input to a cent-quantized Decimal, or raise ValueError.

    `str` = parse from wire (canonical form only, never rounded);
    `Decimal`/`int`/`float` = construct from a computed value (rounded half-up).
    """
    if isinstance(value, bool):  # bool is an int subclass — never money
        raise ValueError("money must be a number or decimal string, not a boolean")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)  # exact for any int; no context involved
    elif isinstance(value, float):
        text = str(value)  # the only permitted float path: its shortest repr
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
        raise ValueError(f"money must not be negative, got {_preview(value)}")
    return quantize_money(d)


def _validate_money(value: Any, info: ValidationInfo) -> Decimal:
    # JSON text input (model_validate_json — every HTTP request body): money
    # must be a JSON string; a JSON number arrives as int/float and is refused.
    if info.mode == "json" and not isinstance(value, str):
        raise ValueError(f"money must be a JSON string like \"12.30\", got a JSON {type(value).__name__}")
    return to_money(value)


def format_money(value: Decimal) -> str:
    """Canonical two-decimal text, e.g. Decimal('12.3') -> '12.30'."""
    return f"{quantize_money(value):f}"


_serialize = PlainSerializer(format_money, return_type=str, when_used="json")

# Any money value, zero allowed.
Money = Annotated[Decimal, BeforeValidator(_validate_money), _serialize, Field(ge=0)]

# Positive-only money: rejects "0.00" (and anything that rounds to it).
PositiveMoney = Annotated[Decimal, BeforeValidator(_validate_money), _serialize, Field(gt=0)]
