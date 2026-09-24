"""
Worst-case JSON size of a request model, computed from the model's own
field limits (fix wave 1, Sep 24 2026 — LOW-C; ADR 0001 "Request limits").

The body limit of each route is sized from the largest body a LEGAL request
to it can have, so the API never answers 413 to a batch it advertises as
acceptable. `worst_case_json_bytes(Model)` walks the pydantic model and
returns an upper bound on the size of its compact JSON encoding with every
field at its limit:

  - string of at most N characters: 2 + 6N bytes. Six bytes is the most one
    character takes in minimal JSON escaping (a control character is
    \\u00XX; any other character is at most 4 bytes of UTF-8) and in Go's
    encoding/json (the orchestrator's encoder; it also writes <, >, & and
    U+2028/2029 as \\uXXXX). So a batch of nothing but escaped characters
    still fits.
  - enum: its longest value. bool: 5 ("false"). null: 4.
  - integer: the digits of its larger bound (every int field has one).
  - float: 24 (the longest shortest-repr of a finite double).
  - money: 20 (a JSON string of at most 18 characters, see zbm_schema.money).
  - datetime: 37 (RFC 3339 with 9 fractional digits and an offset — Go's
    RFC3339Nano — in quotes).
  - list: its max_length items and the commas. Optional: the larger of null
    and the value. Model: braces, every field (present, even if it has a
    default), its quoted name, colon and commas.

Not counted, because no finite limit could admit them: insignificant JSON
whitespace, zero-padded numbers and over-long fractional seconds (all are
legal JSON and can make any document arbitrarily large). A client that
escapes astral-plane characters as surrogate pairs (12 bytes; Python's
json.dumps default) exceeds the bound only for strings made of them; the
headroom below absorbs a good share of that.

A string, list or int field without a limit makes this raise: a request
model cannot be added without one.
"""

from __future__ import annotations

import enum
import json
import math
import types
import typing
from datetime import datetime
from decimal import Decimal

import annotated_types
from pydantic import AwareDatetime, BaseModel
from pydantic.fields import FieldInfo

from zbm_schema.money import _MAX_WIRE_LENGTH

ESCAPED_CHAR_BYTES = 6
DATETIME_JSON_BYTES = 2 + len("2026-09-24T00:00:00.123456789+05:30")
FLOAT_JSON_BYTES = 24
MONEY_JSON_BYTES = 2 + _MAX_WIRE_LENGTH

MIB = 1024 * 1024
HEADROOM = 1.25


class UnboundedField(TypeError):
    """A field has no limit, so no worst case exists."""


def _flatten(meta) -> list:
    out: list = []
    for m in meta:
        if isinstance(m, FieldInfo):
            out.extend(_flatten(m.metadata))
        elif isinstance(m, annotated_types.GroupedMetadata):
            out.extend(_flatten(list(m)))
        else:
            out.append(m)
    return out


def _max_len(meta: list, where: str) -> int:
    lens = [m.max_length for m in meta if isinstance(m, annotated_types.MaxLen)]
    if not lens:
        raise UnboundedField(f"{where}: no max_length")
    return min(lens)


def _int_digits(meta: list, where: str) -> int:
    hi = lo = None
    for m in meta:
        if isinstance(m, annotated_types.Le):
            hi = m.le
        elif isinstance(m, annotated_types.Lt):
            hi = m.lt - 1
        elif isinstance(m, annotated_types.Ge):
            lo = m.ge
        elif isinstance(m, annotated_types.Gt):
            lo = m.gt + 1
    if hi is None or lo is None:
        raise UnboundedField(f"{where}: int without both bounds")
    return max(len(str(hi)), len(str(lo)))


def worst_case_json_bytes(tp, meta: list | tuple = (), where: str = "") -> int:
    meta = _flatten(meta)
    origin = typing.get_origin(tp)

    if origin is typing.Annotated:
        base, *extra = typing.get_args(tp)
        return worst_case_json_bytes(base, [*meta, *extra], where)

    if origin in (typing.Union, types.UnionType):
        return max(worst_case_json_bytes(a, meta, where) for a in typing.get_args(tp))

    if tp is type(None):
        return 4

    if origin is list:
        (item,) = typing.get_args(tp)
        n = _max_len(meta, where)
        return 2 + n * worst_case_json_bytes(item, (), f"{where}[]") + max(n - 1, 0)

    if isinstance(tp, type) and issubclass(tp, BaseModel):
        fields = tp.model_fields
        size = 2 + max(len(fields) - 1, 0)
        for name, f in fields.items():
            size += len(json.dumps(name)) + 1
            size += worst_case_json_bytes(f.annotation, f.metadata, f"{tp.__name__}.{name}")
        return size

    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return max(len(json.dumps(m.value)) for m in tp)

    if tp is bool:
        return 5
    if tp is str:
        return 2 + ESCAPED_CHAR_BYTES * _max_len(meta, where)
    if tp is int:
        return _int_digits(meta, where)
    if tp is float:
        return FLOAT_JSON_BYTES
    if tp is Decimal:
        return MONEY_JSON_BYTES
    if tp is datetime or tp is AwareDatetime:
        return DATETIME_JSON_BYTES

    raise UnboundedField(f"{where}: no worst-case size rule for {tp!r}")


def body_limit_for(model: type[BaseModel]) -> int:
    """The route's body limit: the worst case plus HEADROOM, rounded up to
    a whole MiB."""
    return math.ceil(worst_case_json_bytes(model) * HEADROOM / MIB) * MIB
