"""
F15 (fulfillment part) — money parsing matches the shared wire contract
(BUILD_CONTRACTS section 1, amended by ADR 0003 section 1a) exactly, checked
against the shared vector file fixtures/money_vectors.json that detection-py,
orchestrator-go, the dashboard and the ledger also load.

Before this fix `LabeledValue.amount_usd` accepted "1e3", " 12.30 ",
"012.30", "12.3" and rounded "12.345" up to "12.35": it parsed any string
Decimal() understood and then quantized it.

Levels checked:
  1. `to_money` (Money: zero allowed) — the 'money' column; an accepted
     string serializes back to itself byte-for-byte;
  2. `LabeledValue.amount_usd` (PositiveMoney) — the 'positive_money' column,
     both from Python and from JSON text (model_validate_json, which is how
     a request body is parsed);
  3. every json_vector (raw JSON value) against LabeledValue from JSON text:
     a JSON number is never money.
  4. No HTTP route of this service accepts money in a request body (proved
     by walking every route's body model), so there is no HTTP-level money
     vector to run here; the test fails if one is ever added without one.

Internal construction (Decimal/int, e.g. a future lifetime-value estimate)
is a separate path and is NOT held to the wire-string rule: it is quantized
half-up to cents under an explicit context.
"""

from __future__ import annotations

import decimal
import json
import typing
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from fulfillment_schema import LabeledValue

VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "fixtures" / "money_vectors.json").read_text(encoding="utf-8")
)
STRINGS = VECTORS["string_vectors"]
JSONS = VECTORS["json_vectors"]


def _ids(vs, key):
    return [f"{i}:{v[key][:24]!r}" for i, v in enumerate(vs)]


def _lv_json(value_json: str) -> str:
    return '{"amount_usd":' + value_json + ',"confidence":"low"}'


def test_vector_file_is_the_expected_one():
    assert len(STRINGS) >= 60 and len(JSONS) >= 9


def test_contract_block_matches_the_python_implementation():
    from fulfillment_schema.money import MAX_MONEY, WIRE_PATTERN, format_money

    assert VECTORS["contract"]["pattern"] == WIRE_PATTERN.pattern
    assert VECTORS["contract"]["max"] == format_money(MAX_MONEY)


@pytest.mark.parametrize("vec", STRINGS, ids=_ids(STRINGS, "input"))
def test_money_zero_allowed_verdict(vec):
    from fulfillment_schema.money import format_money, to_money

    s = vec["input"]
    if vec["money"] == "accept":
        assert format_money(to_money(s)) == s
    else:
        with pytest.raises(ValueError):
            to_money(s)


@pytest.mark.parametrize("vec", STRINGS, ids=_ids(STRINGS, "input"))
def test_money_zero_allowed_field_verdict_from_json(vec):
    from fulfillment_schema.money import Money

    class M(BaseModel):
        v: Money

    s = vec["input"]
    body = '{"v":' + json.dumps(s) + "}"
    if vec["money"] == "accept":
        assert M.model_validate_json(body).model_dump(mode="json")["v"] == s
    else:
        with pytest.raises(ValidationError):
            M.model_validate_json(body)


@pytest.mark.parametrize("vec", STRINGS, ids=_ids(STRINGS, "input"))
def test_positive_money_verdict(vec):
    s = vec["input"]
    if vec["positive_money"] == "accept":
        lv = LabeledValue(amount_usd=s, confidence="low")
        assert lv.model_dump(mode="json")["amount_usd"] == s
        assert lv.model_dump_json() == _lv_json(json.dumps(s))
    else:
        with pytest.raises(ValidationError):
            LabeledValue(amount_usd=s, confidence="low")


@pytest.mark.parametrize("vec", STRINGS, ids=_ids(STRINGS, "input"))
def test_positive_money_verdict_from_json_text(vec):
    s = vec["input"]
    if vec["positive_money"] == "accept":
        lv = LabeledValue.model_validate_json(_lv_json(json.dumps(s)))
        assert lv.model_dump(mode="json")["amount_usd"] == s
    else:
        with pytest.raises(ValidationError):
            LabeledValue.model_validate_json(_lv_json(json.dumps(s)))


@pytest.mark.parametrize("vec", JSONS, ids=_ids(JSONS, "json"))
def test_json_value_verdict(vec):
    """A JSON number is never money on the wire: accepting 12.345 as a number
    would silently round it."""
    if vec["verdict"] == "accept":
        LabeledValue.model_validate_json(_lv_json(vec["json"]))
    else:
        with pytest.raises(ValidationError):
            LabeledValue.model_validate_json(_lv_json(vec["json"]))


# --- the audit's named reproductions, explicitly ------------------------------

@pytest.mark.parametrize("s", ["1e3", " 12.30 ", "012.30", "12.345", "12.3", "0.00"])
def test_named_f15_reproductions_are_rejected(s):
    with pytest.raises(ValidationError):
        LabeledValue(amount_usd=s, confidence="low")


# --- internal construction is a separate path -------------------------------

@pytest.mark.parametrize("given,expected", [
    (Decimal("0.125"), "0.13"),
    (Decimal("1.005"), "1.01"),
    (Decimal("2.675"), "2.68"),
    (1.005, "1.01"),  # float only via str(): 1.005 stays 1.005, then half-up
    (49.99, "49.99"),
    (12, "12.00"),
    (Decimal("999999999999999.99"), "999999999999999.99"),
])
def test_internal_values_are_quantized_half_up_not_rejected(given, expected):
    v = LabeledValue(amount_usd=given, confidence="low")
    assert v.model_dump(mode="json")["amount_usd"] == expected
    assert v.amount_usd == Decimal(expected)


@pytest.mark.parametrize("given", [
    Decimal("0.004"), 0.004, 0, Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), float("nan"),
    float("inf"), True, Decimal("999999999999999.995"), Decimal(10) ** 15, 10**40, Decimal("1e400"),
])
def test_internal_values_out_of_contract_are_rejected_with_validation_error(given):
    with pytest.raises(ValidationError):
        LabeledValue(amount_usd=given, confidence="low")


def test_money_arithmetic_ignores_the_ambient_decimal_context():
    """A caller (or library) that shrinks the thread's decimal context must not
    change how money rounds or make it raise."""
    from fulfillment_schema.money import quantize_money

    with decimal.localcontext() as ctx:
        ctx.prec = 3
        ctx.rounding = decimal.ROUND_DOWN
        ctx.traps[decimal.Inexact] = True
        assert quantize_money(Decimal("123456789012345.675")) == Decimal("123456789012345.68")
        assert LabeledValue(amount_usd=Decimal("999999999999999.985"), confidence="low").amount_usd == Decimal(
            "999999999999999.99"
        )


def test_signed_zero_never_serializes():
    from fulfillment_schema.money import quantize_money

    assert str(quantize_money(Decimal("-0.001"))) == "0.00"


# --- HTTP: no route takes money in a request body ----------------------------

def _models_in(tp, seen):
    origin = typing.get_origin(tp)
    if origin is not None:
        for a in typing.get_args(tp):
            yield from _models_in(a, seen)
        return
    if isinstance(tp, type) and issubclass(tp, BaseModel) and tp not in seen:
        seen.add(tp)
        yield tp
        for f in tp.model_fields.values():
            yield from _models_in(f.annotation, seen)


def _has_decimal(tp) -> bool:
    if tp is Decimal:
        return True
    return any(_has_decimal(a) for a in typing.get_args(tp))


def test_no_http_route_accepts_money_in_its_request_body():
    """If a route ever takes a money field, this fails: add an HTTP-level run
    of the shared vectors for it (see detection-py tests/test_money_vectors.py)."""
    from fastapi.routing import APIRoute

    from api import app

    offenders = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or r.body_field is None:
            continue
        for m in _models_in(r.body_field.field_info.annotation, set()):
            for name, f in m.model_fields.items():
                if _has_decimal(f.annotation):
                    offenders.append((r.path, m.__name__, name))
    assert offenders == []
