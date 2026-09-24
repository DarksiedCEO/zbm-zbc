"""Money wire format (contract section 1) and schema-level rules."""

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from onboarding_schema import AccessGrantIn, LabeledValue, Money, PositiveMoney, money_str, to_money
from onboarding_schema.requests import StartClientRequest


class M(BaseModel):
    m: Money
    p: PositiveMoney = Decimal("1.00")


# Fix wave 3 (F15): request money is ONLY the contract string (ADR 0003 1a);
# this test previously accepted "12.3", 12, 49.99 and 12.0 — those are now
# rejected (see test_money_rejects_bad_input and test_fix_wave3's vectors).
@pytest.mark.parametrize("inp,out", [("12.30", "12.30"), (Decimal("0.5"), "0.50"), ("0.00", "0.00"), ("1.01", "1.01"),
                                     ("999999999999999.99", "999999999999999.99")])
def test_money_is_exact_and_serializes_as_two_decimal_string(inp, out):
    assert M(m=inp).model_dump(mode="json")["m"] == out


# Fix wave 1 (F14/F15): inputs that would need ROUNDING are rejected, never
# silently quantized (this test previously asserted 0.005 -> 0.01, 0.004 ->
# 0.00, 1.005 -> 1.01 and 0.1 + 0.2 -> 0.30).
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1.00", "1e3", "abc", True, None, float("nan"), float("inf"),
                                 Decimal("0.005"), "0.004", "1.005", 0.1 + 0.2, " 12.30", "012.30", "9" * 40,
                                 Decimal("1E+30"), 10**30, "12.3", "12", 12, 49.99, 12.0, 0.1, "1000000000000000.00"])
def test_money_rejects_bad_input(bad):
    with pytest.raises(ValidationError):
        M(m=bad)


def test_positive_money_rejects_zero():
    with pytest.raises(ValidationError):
        M(m="1.00", p="0.00")
    with pytest.raises(ValidationError):
        M(m="1.00", p="0.004")  # rounds to 0.00


def test_labeled_value_needs_both_labels_and_renders_with_them():
    with pytest.raises(ValidationError):
        LabeledValue(amount_usd="5.00", classification="observed")
    v = LabeledValue(amount_usd="5.00", classification="observed", confidence="high")
    assert v.render() == "$5.00 (observed, high confidence)"
    assert v.model_dump(mode="json")["amount_usd"] == "5.00"


def test_no_model_can_carry_a_secret_field():
    with pytest.raises(ValidationError):
        AccessGrantIn(platform="meta", account_id="a", account_type="business", granted_role="analyst", access_token="x")
    for model in (AccessGrantIn, StartClientRequest):
        for name in model.model_fields:
            assert not any(s in name for s in ("password", "token", "secret", "credential")), name


def test_inbound_datetimes_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        AccessGrantIn(platform="meta", account_id="a", account_type="business", granted_role="analyst",
                      account_last_activity_at="2026-09-01T00:00:00")


def test_credential_looking_ids_are_refused():
    with pytest.raises(ValidationError):
        StartClientRequest(client_id="FAKEsecret9Zq7Hunter2Xy81Lp", business_name="b", time_zone="UTC",
                           signer={"name": "a", "email": "a@b.co"})


def test_money_str_and_to_money():
    assert money_str(to_money("7.00")) == "7.00"
    with pytest.raises(ValueError):
        to_money("7")  # fix wave 3 (F15): previously accepted; not the contract form
