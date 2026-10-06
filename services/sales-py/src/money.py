"""
Exact money for Sales, copied from services/finance-py/src/money.py (BUILD_CONTRACTS §1, ADR 0003 §1a), its
payout helpers replaced by the quote helpers (``times``, ``percent_of``, ``parse_pct``).

Every amount is a ``decimal.Decimal`` quantized to cents with ROUND_HALF_UP at the point it is created or computed,
under one explicit context (``MONEY_CONTEXT``, never the ambient one). On the wire money is a JSON STRING matching
``^(0|[1-9][0-9]{0,14})\\.[0-9]{2}$`` (the §1 pattern with the ADR 0003 §1a 15-integer-digit bound, the same verdicts
as ``fixtures/money_vectors.json``). Copied in spirit from detection-py ``zbm_schema/money.py``; stricter on input:

- a caller-supplied money value must be a JSON string in canonical form. Floats, ints, exponents, NaN, Infinity,
  negatives, three decimals and padded forms are refused (spec A12: "floats in any money field -> 422"). The
  BUILD_CONTRACTS float-through-``str`` path is not offered on this service's wire at all (spec overrides);
- internal arithmetic: ``times(price, quantity)`` and ``percent_of(cost, markup)`` are exact, then ONE half-up
  quantize per line; a proposal total is the sum of its line amounts minus its discount.

Nothing here ever builds a Decimal from a float.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Any, Iterator

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_MONEY = Decimal("999999999999999.99")
_LIMIT = Decimal(10) ** 15
WIRE_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,14})\.[0-9]{2}$", re.ASCII)
_MAX_WIRE_LENGTH = 18
MONEY_CONTEXT = Context(prec=60, rounding=ROUND_HALF_UP, traps=[InvalidOperation, DivisionByZero, Overflow])


class MoneyError(ValueError):
    pass


@contextmanager
def money_context() -> Iterator[Context]:
    with localcontext(MONEY_CONTEXT) as ctx:
        yield ctx


def q(value: Decimal) -> Decimal:
    """The one rounding rule: cents, half-up. Signed values allowed (differences), bounded to (-10^15, 10^15)."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MoneyError("money must be a finite Decimal")
    with money_context():
        if value.copy_abs() >= _LIMIT:
            raise MoneyError("money out of range")
        r = value.quantize(CENT)
    if r.copy_abs() > MAX_MONEY:
        raise MoneyError("money out of range")
    return r.copy_abs() if r.is_zero() else r


def parse(value: Any, positive: bool = False) -> Decimal:
    """A caller-supplied money value: ONLY the canonical wire string (see module doc)."""
    if not isinstance(value, str):
        raise MoneyError(f"money must be a JSON string like \"12.30\", got a JSON {type(value).__name__}")
    if len(value) > _MAX_WIRE_LENGTH or not WIRE_PATTERN.fullmatch(value):
        raise MoneyError("money must be a canonical two-decimal string like \"12.30\" (max 999999999999999.99)")
    d = Decimal(value)
    if positive and d <= 0:
        raise MoneyError("money must be greater than 0.00 here")
    return d


def D(value: Any) -> Decimal:
    """An INTERNAL money value (a stored record field or a Decimal): canonical string or Decimal, never a float."""
    if isinstance(value, Decimal):
        return q(value)
    if isinstance(value, str):
        neg = value.startswith("-")
        body = value[1:] if neg else value
        if not WIRE_PATTERN.fullmatch(body):
            raise MoneyError(f"stored money {value[:20]!r} is not canonical")
        d = Decimal(body)
        return -d if neg and d else d
    raise MoneyError(f"money must be a Decimal or a canonical string, got {type(value).__name__}")


def fmt(value: Decimal) -> str:
    """Canonical two-decimal text (non-negative values only go on the wire)."""
    v = q(value)
    if v < 0:
        raise MoneyError("a negative amount never goes on the wire")
    return f"{v:f}"


def sfmt(value: Decimal) -> str:
    """Signed canonical text for DIFFERENCES inside records (e.g. a recon leg difference "-0.01")."""
    v = q(value)
    return f"{v:f}"


PCT_PATTERN = re.compile(r"^(0|[1-9][0-9]?|100)\.[0-9]{2}$", re.ASCII)


def parse_pct(value: Any) -> Decimal:
    """A percentage on the wire: a canonical two-decimal string from "0.00" to "100.00" (a markup), never a float."""
    if not isinstance(value, str) or len(value) > 6 or not PCT_PATTERN.fullmatch(value):
        raise MoneyError("a percentage must be a canonical two-decimal string like \"15.00\" (0.00 .. 100.00)")
    d = Decimal(value)
    if d > Decimal(100):
        raise MoneyError("a percentage is at most 100.00")
    return d


def percent_of(amount: Decimal, percent: Decimal) -> Decimal:
    """``percent``% of an amount, exact, then ONE half-up quantize (a media-buy markup)."""
    with money_context():
        return q(D(amount) * percent / Decimal(100))


def times(amount: Decimal, quantity: int) -> Decimal:
    """A unit price times a whole quantity, exact, then quantized."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise MoneyError("quantity must be a non-negative integer")
    with money_context():
        return q(D(amount) * Decimal(quantity))


def total(values) -> Decimal:
    with money_context():
        s = Decimal(0)
        for v in values:
            s += D(v)
        return q(s)
