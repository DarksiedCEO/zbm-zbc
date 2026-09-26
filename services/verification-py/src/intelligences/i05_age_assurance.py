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
3. AEGIS N16-8: only an explicit ``dob_consistent: true`` passes (a provider that does not say whether its DOB
   matches the declared one → ``inconclusive``); a provider result whose ``checked_at`` is older than
   VI_AGE_ATTESTATION_VALIDITY_DAYS (365), in the future, or unreadable → ``inconclusive``. The attestation is
   valid for that many days from the provider's check time; after it a re-check is required.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


FUTURE_SKEW = timedelta(minutes=5)


def provider_time(checked_at, now: datetime) -> Optional[datetime]:
    """The provider's check time (UTC), ``now`` when it gave none, or None when it is unreadable / in the future."""
    if checked_at is None:
        return now
    if not isinstance(checked_at, str) or len(checked_at) > 40:
        return None
    try:
        t = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    t = t.astimezone(timezone.utc)
    return None if t > now + FUTURE_SKEW else t


def judge(method: str, ans: AgeProviderAnswer, buffer_age: int, rules: dict, now: Optional[datetime] = None,
          validity_days: int = 365) -> tuple[str, list[dict], bool, bool]:
    """(result, reasons, buffer_applied, needs_human)."""
    if ans.result == "minor":
        return "minor", [item("AGE_MINOR", "age-assurance provider result: under 18", (), rules)], False, False
    if ans.result != "adult":
        return "inconclusive", [item("AGE_NOT_ASSURED", f"age-assurance provider result: {ans.result}", (), rules)], \
            False, False
    if ans.dob_consistent is False:
        return "inconclusive", [item("AGE_NOT_ASSURED", "provider date of birth disagrees with the declared one "
                                     "(human review)", (), rules)], False, True
    if ans.dob_consistent is not True:
        return "inconclusive", [item("AGE_NOT_ASSURED", "provider did not confirm that its date of birth matches the "
                                     "declared one", (), rules)], False, False
    now = now or datetime.now(timezone.utc)
    checked = provider_time(ans.checked_at, now)
    if checked is None:
        return "inconclusive", [item("AGE_NOT_ASSURED", "provider check time unreadable or in the future", (), rules)], \
            False, False
    if now - checked >= timedelta(days=validity_days):
        return "inconclusive", [item("AGE_NOT_ASSURED", f"provider result older than {validity_days} days (stale): a "
                                     "fresh check is required", (), rules)], False, False
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
