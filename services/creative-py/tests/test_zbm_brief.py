"""ZBM brief model: 15 fields, key-message rule, numeric success, deliverable specs."""

import pytest
from pydantic import ValidationError

from samples import zbm_requirements
from zbm.brief import BRIEF_FIELD_NAMES, BriefFields, SuccessMetric, key_message_violations


def test_exactly_the_fifteen_locked_fields():
    assert set(BRIEF_FIELD_NAMES) == {
        "objective", "audience", "key_message", "deliverables", "mandatories", "approvers", "distribution",
        "deadline", "disclosure_requirements", "hook", "success_in_numbers", "insight", "tone_of_voice",
        "rights_and_permissions", "transformation_plan"}


@pytest.mark.parametrize("msg", [
    "Acme oat milk froths like dairy.",
    "Our shoes last 2.5 times longer!",
    "Why pay more for the same coffee?",
    "Fresh bread, delivered by seven.",  # one comma allowed
])
def test_key_message_accepts_one_sentence_one_idea(msg):
    assert key_message_violations(msg) == []


@pytest.mark.parametrize("msg,code", [
    ("", "K0"),
    ("Acme oat milk froths like dairy", "K1"),
    ("It froths. It tastes great.", "K2"),
    ("It froths\nlike dairy.", "K2"),
    ("It froths and it tastes great.", "K3"),
    ("Cheap but good.", "K3"),
    ("Froths, as well as dairy does.", "K3"),
    ("It froths; it pours.", "K4"),
    ("Two benefits: froth.", "K4"),
    ("Froth & pour.", "K4"),
    ("Fast, cheap, good milk.", "K5"),
    ("Froths.", "K6"),
    ("This oat milk froths like dairy milk does in every single kitchen we have tested it in so far this year.", "K6"),
])
def test_key_message_rejections_are_deterministic(msg, code):
    assert any(v.startswith(code) for v in key_message_violations(msg)), key_message_violations(msg)


def test_key_message_rule_is_whole_word_not_substring():
    # "brand" contains "and", "orange" contains "or", "butter" contains "but"
    assert key_message_violations("Orange butter makes brand toast.") == []


@pytest.mark.parametrize("target", [500, 12.5, "250", "0.35"])
def test_success_target_numeric(target):
    SuccessMetric(metric="m", comparator=">=", target=target, unit="u", measured_by="x")


@pytest.mark.parametrize("target", ["lots", "500 signups", True, float("nan"), float("inf"), None])
def test_success_target_must_be_a_number(target):
    with pytest.raises(ValidationError):
        SuccessMetric(metric="m", comparator=">=", target=target, unit="u", measured_by="x")


def test_float_target_goes_through_str_not_binary():
    from decimal import Decimal

    assert SuccessMetric(metric="m", comparator=">=", target=49.99, unit="u", measured_by="x").target == Decimal("49.99")


def test_brief_rejects_unknown_fields_and_missing_fields():
    data = {k: v for k, v in zbm_requirements().items() if k not in ("client_id", "insight_candidates")}
    data["insight"] = "x"
    BriefFields.model_validate(data)
    with pytest.raises(ValidationError):
        BriefFields.model_validate({**data, "budget": "1000.00"})
    with pytest.raises(ValidationError):
        BriefFields.model_validate({k: v for k, v in data.items() if k != "hook"})


@pytest.mark.parametrize("bad", [{"aspect_ratio": "tall"}, {"format": "MP4!"}, {"count": 0}, {"length_seconds": 0}])
def test_deliverable_exact_specs_validated(bad):
    from zbm.brief import Deliverable

    base = zbm_requirements()["deliverables"][0]
    with pytest.raises(ValidationError):
        Deliverable.model_validate({**base, **bad})
