"""
Intelligence 5 — Age Assurance (spec §C.5, VI-11): adult | minor | inconclusive. Never stores an ID document,
number, image or the DOB.

1. Neutral DOB (``dob_field_neutral: true`` required; no default). Age < 18 → ``minor`` at once: hard block,
   no provider call, no guardian path (no such field exists in any schema).
2. One highly effective method through the provider. Self-declaration and debit cards never pass
   (AGE_METHOD_NOT_HIGHLY_EFFECTIVE, no provider call). A credit-card check passes only for a card whose holder
   must be 18 (``card_kind == "credit"``). Facial age estimation passes only when the estimate's lower bound is
   ≥ VI_FAE_BUFFER_AGE (25); below it → ``inconclusive`` (a document- or identity-based method is required).
   Provider DOB disagreeing with the declared DOB → ``inconclusive`` + a human hold. Provider unavailable →
   ``inconclusive`` with AGE_NOT_ASSURED.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from ports import AgeProviderAnswer
from reasons import item

NUMBER, NAME, ACTOR = 5, "Age Assurance", "intel_05_age_assurance"
HIGHLY_EFFECTIVE = ("open_banking", "photo_id_match", "facial_age_estimation", "mobile_operator", "credit_card",
                    "digital_identity", "email_age_estimation")
NOT_HIGHLY_EFFECTIVE = ("self_declaration", "debit_card")
METHODS = HIGHLY_EFFECTIVE + NOT_HIGHLY_EFFECTIVE


def age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def pre_check(dob: date, dob_field_neutral: bool, method: str, today: date, rules: dict) -> Optional[tuple]:
    """Decisions made WITHOUT calling the provider: (result, reasons) or None to go on."""
    if age_on(dob, today) < 18:
        return "minor", [item("AGE_MINOR", "declared date of birth is under 18 (hard block, no guardian path)",
                              (), rules)]
    if dob_field_neutral is not True:
        return "inconclusive", [item("AGE_NOT_ASSURED", "the DOB field was not neutral (defaulted or pre-filled)", (),
                                     rules)]
    if method in NOT_HIGHLY_EFFECTIVE:
        return "inconclusive", [item("AGE_METHOD_NOT_HIGHLY_EFFECTIVE", f"{method} is not a highly effective "
                                     "age-assurance method", (), rules)]
    return None


def judge(method: str, ans: AgeProviderAnswer, buffer_age: int, rules: dict) -> tuple[str, list[dict], bool, bool]:
    """(result, reasons, buffer_applied, needs_human)."""
    if ans.result == "minor":
        return "minor", [item("AGE_MINOR", "age-assurance provider result: under 18", (), rules)], False, False
    if ans.result != "adult":
        return "inconclusive", [item("AGE_NOT_ASSURED", f"age-assurance provider result: {ans.result}", (), rules)], \
            False, False
    if ans.dob_consistent is False:
        return "inconclusive", [item("AGE_NOT_ASSURED", "provider date of birth disagrees with the declared one "
                                     "(human review)", (), rules)], False, True
    if method == "credit_card" and ans.card_kind != "credit":
        return "inconclusive", [item("AGE_METHOD_NOT_HIGHLY_EFFECTIVE", "card check on a card whose holder need not "
                                     "be 18", (), rules)], False, False
    if method == "facial_age_estimation":
        low = ans.estimated_age_low
        if low is None or low < buffer_age:
            return "inconclusive", [item("AGE_NOT_ASSURED", f"facial age estimate below the {buffer_age} buffer: a "
                                         "document- or identity-based method is required", (), rules)], True, False
        return "adult", [], True, False
    return "adult", [], False, False
