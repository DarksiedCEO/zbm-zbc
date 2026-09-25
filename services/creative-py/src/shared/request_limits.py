"""
Worst-case JSON MEMBER count of a request model, computed from the model's
own field limits (fix wave 7, AEGIS round 6 NEW-1; ADR 0005 decision 23).

Fix wave 6 refused any JSON body with more than 4,096 members (object keys
+ array items) before the framework parsed it, on the belief that "no
legitimate request has more than a few hundred members". False: the
Moment Map model (`zbc.source_mining.SourceMaterial`) allows 2,000
segments of 4 keys — 10,003 members — and a legal 820-segment request got
422. The limit of each route is now sized from the largest body a LEGAL
request to it can have, the way detection-py sizes its body limits
(`services/detection-py/src/request_limits.py`), so the API never refuses
a body its own models accept.

`worst_case_json_members(Model)` walks the pydantic model and returns the
number of members the compact JSON of a maximal legal instance has:

  - a model: one member per field (every field present, defaults
    included) plus the members of each field's value;
  - a list: its max_length items, plus the members of each item;
  - a dict: its max_length keys, plus the members of each value;
  - a union / Optional: the largest alternative;
  - a string, number, bool, enum, date, null: 0 (a scalar is not a
    member; its key was counted by the container).

A list or dict field without a max_length makes this raise: a request
model cannot be added without one, since no finite limit could admit it.
`member_limit_for(Model)` adds HEADROOM and rounds up to a multiple of
LIMIT_STEP.
"""

from __future__ import annotations

import math
import types
import typing

import annotated_types
from pydantic import BaseModel
from pydantic.fields import FieldInfo

HEADROOM = 1.25
LIMIT_STEP = 64


class UnboundedField(TypeError):
    """A list or dict field has no max_length, so no worst case exists."""


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


def worst_case_json_members(tp, meta: list | tuple = (), where: str = "") -> int:
    meta = _flatten(meta)
    origin = typing.get_origin(tp)

    if origin is typing.Annotated:
        base, *extra = typing.get_args(tp)
        return worst_case_json_members(base, [*meta, *extra], where)

    if origin in (typing.Union, types.UnionType):
        return max(worst_case_json_members(a, meta, where) for a in typing.get_args(tp))

    if origin in (list, tuple, set, frozenset):
        args = typing.get_args(tp)
        item = args[0] if args else typing.Any
        n = _max_len(meta, where)
        return n + n * worst_case_json_members(item, (), f"{where}[]")

    if origin is dict:
        args = typing.get_args(tp)
        value = args[1] if len(args) == 2 else typing.Any
        n = _max_len(meta, where)
        return n + n * worst_case_json_members(value, (), f"{where}{{}}")

    if isinstance(tp, type) and issubclass(tp, BaseModel):
        fields = tp.model_fields
        size = len(fields)
        for name, f in fields.items():
            size += worst_case_json_members(f.annotation, f.metadata, f"{tp.__name__}.{name}")
        return size

    if tp is typing.Any or tp is dict or tp is list:
        raise UnboundedField(f"{where}: untyped container")

    return 0  # a scalar (str, int, float, bool, Decimal, date, datetime, enum, Literal, None)


def member_limit_for(model: type[BaseModel]) -> int:
    """The route's member limit: the worst case plus HEADROOM, rounded up to
    a multiple of LIMIT_STEP (at least LIMIT_STEP)."""
    return max(LIMIT_STEP, math.ceil(worst_case_json_members(model) * HEADROOM / LIMIT_STEP) * LIMIT_STEP)
