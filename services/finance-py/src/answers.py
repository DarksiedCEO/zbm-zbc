"""
Strict type check of every answer an adapter or thin client gives Finance (AEGIS N17-3).

Every port answer is checked here BEFORE anything derived from it can be staged, recorded or anchored:
``Gather.call`` (every port call made through it) and the direct rail submit/lookup calls of the release worker run
``check`` on the answer. A malformed answer -- a wrong type in any field (a list where an id belongs, a float or a
string where a count belongs, a bool where a count belongs, an unhashable value, an id outside ``[A-Za-z0-9._:-]``,
a timestamp that does not parse, a money string that is not canonical, an unknown enum value, a count above the
configured cap) -- is REFUSED as a whole: the caller gets the port's fail-closed fallback answer, the refusal is
recorded on the evidence ledger (``adapter_answer_refused``: port, action, the problem's TYPE only), and nothing of
the malformed answer is applied. There is no partial apply and no coercion (pydantic strict mode; ``bool`` is never
an ``int``).

Answers with ``available`` false are replaced by a clean fallback of the same type (their other fields are never
read), so a malformed "unavailable" answer cannot carry data in either.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError,
                      model_validator)

from clock import parse_iso
from ports import (BankBalance, BankTransfer, Certification, ClawbackPage, ComplianceRuling, GLAnswer, HoldsAnswer,
                   JurisdictionAnswer, LegalAnswer, RailAccount, RailBalance, RailLookup, RailSubmit, RegisterRow,
                   SanctionsAnswer, StripeCheckout, StripeDispute, StripePayment, StripePayout, StripeSession,
                   TaxAgentAnswer, TierAnswer)

DEFAULT_MAX_VIEWS = 10 ** 12


def _ts(v: str) -> str:
    try:
        parse_iso(v)
    except (ValueError, TypeError):
        raise ValueError("not an RFC 3339 timestamp with offset") from None
    return v


IdS = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
Word = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
TS = Annotated[str, StringConstraints(min_length=1, max_length=40), AfterValidator(_ts)]
Money = Annotated[str, StringConstraints(pattern=r"^-?(0|[1-9][0-9]{0,14})\.[0-9]{2}$")]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Reason = Annotated[str, StringConstraints(max_length=2000)]
Count = Annotated[int, Field(ge=0, le=DEFAULT_MAX_VIEWS)]
Version = Annotated[int, Field(ge=0, le=10 ** 9)]
OID = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{1,39}$")]


class _S(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class CertificationM(_S):
    available: Literal[True]
    submission_id: IdS
    certification_id: Optional[IdS] = None
    status: Word
    certified_views: Optional[Count] = None
    campaign_id: Optional[IdS] = None
    clipper_id: Optional[IdS] = None
    platform: Optional[Word] = None
    create_time: Optional[TS] = None
    certified_at: Optional[TS] = None
    open_finding: bool = False
    rules_version: Optional[Version] = None
    reason: Reason = ""


class ClawbackItemM(_S):
    clawback_id: IdS
    certification_id: IdS
    views_delta: Annotated[int, Field(ge=-DEFAULT_MAX_VIEWS, le=0)]
    cause: Word
    rule_id: Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,4}-[0-9A-Z]{1,6}$")]


class ClawbackPageM(_S):
    available: Literal[True]
    items: tuple[ClawbackItemM, ...] = Field(default=(), max_length=10_000)
    next_cursor: Optional[Annotated[int, Field(ge=0, le=10 ** 12)]] = None
    reason: Reason = ""


class ComplianceRulingM(_S):
    available: Literal[True]
    ruling_id: IdS
    gate: Word
    subject_id: IdS
    allowed: bool
    evaluated_at: Optional[TS] = None
    register_version: Optional[Version] = None
    reason: Reason = ""


class HoldsAnswerM(_S):
    available: Literal[True]
    open_hold_ids: tuple[IdS, ...] = Field(default=(), max_length=10_000)
    reason: Reason = ""


class SanctionsAnswerM(_S):
    available: Literal[True]
    screen_id: Optional[IdS] = None
    result: Optional[Literal["clear", "potential_match", "match"]] = None
    list_version: Optional[IdS] = None
    list_current: bool = False
    screened_at: Optional[TS] = None
    fresh: bool = False
    reason: Reason = ""


class RegisterRowM(_S):
    available: Literal[True]
    obligation_id: OID
    effective_status: Literal["verified", "unverified", "expired", "superseded", "unknown"]
    expires_at: Optional[Annotated[str, StringConstraints(max_length=40)]] = None
    register_version: Optional[Version] = None
    reason: Reason = ""


class JurisdictionAnswerM(_S):
    available: Literal[True]
    status: Literal["operate", "conditional", "blocked", "unknown"]
    reason: Reason = ""


class TierAnswerM(_S):
    available: Literal[True]
    tier: Literal["T0", "T1", "T2", "T3"]
    reason: Reason = ""


class LegalAnswerM(_S):
    available: Literal[True]
    current: bool
    acceptance_matches: bool
    reason: Reason = ""


class RailAccountM(_S):
    available: Literal[True]
    account_ref: Optional[IdS] = None
    status: Literal["verified", "pending", "restricted", "unknown"]
    payouts_enabled: bool
    destination_fingerprint: Optional[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]] = None
    reason: Reason = ""


class RailSubmitM(_S):
    outcome: Literal["accepted", "rejected", "transport_error", "unavailable"]
    rail_ref: Optional[IdS] = None
    reason: Reason = ""


class RailLookupM(_S):
    available: Literal[True]
    found: bool
    rail_ref: Optional[IdS] = None
    status: Optional[Literal["submitted", "paid", "failed", "returned"]] = None
    reason: Reason = ""


class BalanceM(_S):
    available: Literal[True]
    balance: Money
    as_of: TS
    source_sha256: Optional[Sha] = None
    reason: Reason = ""


class BankTransferM(_S):
    outcome: Literal["accepted", "refused", "unavailable"]
    ref: Optional[IdS] = None
    reason: Reason = ""


class TaxAgentAnswerM(_S):
    available: Literal[True]
    form_kind: Optional[Literal["w9", "w8ben", "w8bene"]] = None
    form_on_file: bool = False
    form_received_at: Optional[TS] = None
    tin_match: Literal["matched", "mismatched", "pending", "not_applicable", "unavailable"] = "unavailable"
    tin_match_at: Optional[TS] = None
    w8_current: Optional[bool] = None
    services_outside_us_attested: Optional[bool] = None
    agent_ref: Optional[IdS] = None
    reason: Reason = ""


class GLAnswerM(_S):
    available: Literal[True]
    trial_balance_sha256: Optional[Sha] = None
    reason: Reason = ""


# --- Stripe incoming (ADR 0009 amendment, Oct 5 2026) ---
StripeId = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,8}_[A-Za-z0-9_]{1,120}$")]
PosMoney = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,14})\.[0-9]{2}$")]
CheckoutUrl = Annotated[str, StringConstraints(pattern=r"^https://checkout\.stripe\.com/[A-Za-z0-9/_.~%#=+-]{1,2000}$")]
PI_STATUS = Literal["requires_payment_method", "requires_confirmation", "requires_action", "processing",
                    "requires_capture", "canceled", "succeeded"]
DISPUTE_STATUS = Literal["warning_needs_response", "warning_under_review", "warning_closed", "needs_response",
                         "under_review", "won", "lost", "prevented"]


class StripeCheckoutM(_S):
    outcome: Literal["created", "rejected", "transport_error", "unavailable"]
    session_id: Optional[StripeId] = None
    url: Optional[CheckoutUrl] = None
    expires_at: Optional[TS] = None
    reason: Reason = ""


class StripeSessionM(_S):
    available: Literal[True]
    found: bool
    session_id: Optional[StripeId] = None
    status: Optional[Literal["open", "complete", "expired"]] = None
    payment_intent: Optional[StripeId] = None
    client_reference_id: Optional[IdS] = None
    livemode: bool = False
    reason: Reason = ""

    @model_validator(mode="after")
    def _found_is_complete(self):
        if self.found and any(getattr(self, f) is None for f in ('session_id', 'status')):
            raise ValueError("a found Stripe object must carry session_id, status")
        return self


class StripePaymentM(_S):
    available: Literal[True]
    found: bool
    payment_intent: Optional[StripeId] = None
    status: Optional[PI_STATUS] = None
    amount_received: Optional[PosMoney] = None
    currency: Optional[Annotated[str, StringConstraints(pattern=r"^[a-z]{3}$")]] = None
    invoice_id: Optional[IdS] = None
    method: Optional[Literal["card", "us_bank_account", "other"]] = None
    charge: Optional[StripeId] = None
    charge_status: Optional[Literal["succeeded", "pending", "failed"]] = None
    balance_txn: Optional[StripeId] = None
    gross: Optional[Money] = None
    fee: Optional[Money] = None
    failure_txn: Optional[StripeId] = None
    failure_amount: Optional[Money] = None
    failure_fee: Optional[Money] = None
    amount_refunded: Optional[PosMoney] = None
    livemode: bool = False
    reason: Reason = ""

    @model_validator(mode="after")
    def _found_is_complete(self):
        if self.found and any(getattr(self, f) is None for f in ('payment_intent', 'status', 'amount_received', 'currency')):
            raise ValueError("a found Stripe object must carry payment_intent, status, amount_received, currency")
        return self


class StripeTxnM(_S):
    txn_id: StripeId
    amount: Money
    fee: Money


class StripeDisputeM(_S):
    available: Literal[True]
    found: bool
    dispute_id: Optional[StripeId] = None
    payment_intent: Optional[StripeId] = None
    status: Optional[DISPUTE_STATUS] = None
    amount: Optional[PosMoney] = None
    txns: tuple[StripeTxnM, ...] = Field(default=(), max_length=10)
    livemode: bool = False
    reason: Reason = ""

    @model_validator(mode="after")
    def _found_is_complete(self):
        if self.found and any(getattr(self, f) is None for f in ('dispute_id', 'status', 'amount')):
            raise ValueError("a found Stripe object must carry dispute_id, status, amount")
        return self


class StripePayoutM(_S):
    available: Literal[True]
    found: bool
    payout_id: Optional[StripeId] = None
    status: Optional[Literal["paid", "pending", "in_transit", "canceled", "failed"]] = None
    amount: Optional[PosMoney] = None
    livemode: bool = False
    reason: Reason = ""

    @model_validator(mode="after")
    def _found_is_complete(self):
        if self.found and any(getattr(self, f) is None for f in ('payout_id', 'status', 'amount')):
            raise ValueError("a found Stripe object must carry payout_id, status, amount")
        return self


MODELS: dict[type, type[BaseModel]] = {
    Certification: CertificationM, ClawbackPage: ClawbackPageM, ComplianceRuling: ComplianceRulingM,
    HoldsAnswer: HoldsAnswerM, SanctionsAnswer: SanctionsAnswerM, RegisterRow: RegisterRowM,
    JurisdictionAnswer: JurisdictionAnswerM, TierAnswer: TierAnswerM, LegalAnswer: LegalAnswerM,
    RailAccount: RailAccountM, RailSubmit: RailSubmitM, RailLookup: RailLookupM, RailBalance: BalanceM,
    BankBalance: BalanceM, BankTransfer: BankTransferM, TaxAgentAnswer: TaxAgentAnswerM, GLAnswer: GLAnswerM,
    StripeCheckout: StripeCheckoutM, StripeSession: StripeSessionM, StripePayment: StripePaymentM,
    StripeDispute: StripeDisputeM, StripePayout: StripePayoutM,
}
# answers without an ``available`` switch: every field is always checked
_ALWAYS = (RailSubmit, BankTransfer, StripeCheckout)


class Malformed(Exception):
    """The answer failed the strict check; ``kind`` is the problem's type (never the answer's content)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind[:64]


def _raw(ans: Any) -> dict:
    # NOT dataclasses.asdict: it deep-copies and would coerce/raise on hostile values; read the fields as they are
    return {f.name: getattr(ans, f.name) for f in dataclasses.fields(ans)}


def check(ans: Any, fallback: Any, max_views: int = DEFAULT_MAX_VIEWS) -> Any:
    """``ans`` if it is a well-formed answer of the fallback's type; raises ``Malformed`` otherwise. An unavailable
    answer is normalized to a clean unavailable answer (fields other than ``available`` are dropped)."""
    if fallback is None or isinstance(fallback, bool):
        raise TypeError("use check_primitive for non-dataclass answers")
    typ = type(fallback)
    if type(ans) is not typ:
        raise Malformed(f"wrong_type:{type(ans).__name__}")
    model = MODELS.get(typ)
    if model is None:
        raise Malformed("no_model")
    raw = _raw(ans)
    if typ not in _ALWAYS:
        if raw.get("available") is not True:
            if raw.get("available") is not False:
                raise Malformed("available_not_bool")
            return fallback
    try:
        model.model_validate(raw)
    except ValidationError as exc:
        errs = exc.errors(include_url=False, include_input=False)
        first = errs[0] if errs else {}
        raise Malformed(f"{'.'.join(str(x) for x in first.get('loc', ()))[:40]}:{first.get('type', 'invalid')}") \
            from None
    except Exception as exc:  # noqa: BLE001 - anything odd inside the answer is a malformed answer, never a crash
        raise Malformed(f"unvalidatable:{type(exc).__name__}") from None
    if typ is Certification and raw.get("certified_views") is not None and raw["certified_views"] > max_views:
        raise Malformed("certified_views:above_cap")
    if typ is ClawbackPage:
        # the service reads the items as plain dicts of exactly the five fields
        return ClawbackPage(True, tuple({k: x[k] if isinstance(x, dict) else getattr(x, k)
                                         for k in ("clawback_id", "certification_id", "views_delta", "cause", "rule_id")}
                                        for x in raw["items"]), raw["next_cursor"])
    if typ is StripeDispute:
        # the service reads the balance transactions as plain dicts of exactly the three fields
        return dataclasses.replace(ans, txns=tuple({k: x[k] if isinstance(x, dict) else getattr(x, k)
                                                    for k in ("txn_id", "amount", "fee")} for x in raw["txns"]))
    return ans


_PRIMITIVE = {
    "identity_hmac_key": lambda v: v is None or (type(v) in (bytes, bytearray) and 16 <= len(v) <= 4096),
    "contact_ref_valid": lambda v: v is None or type(v) is bool,
    "verify_event": lambda v: type(v) is bool,
    "send_receipt": lambda v: type(v) is bool,
}


def check_primitive(action: str, ans: Any) -> Any:
    ok = _PRIMITIVE.get(action)
    if ok is None or not ok(ans):
        raise Malformed(f"wrong_type:{type(ans).__name__}")
    return ans


_WORD = re.compile(r"[^A-Za-z0-9._:-]")


def kind_text(kind: str) -> str:
    return _WORD.sub("-", kind)[:64]
