"""
Request models (Finance spec §D): strict (unknown key -> 422), bounded strings, no control characters, money as
canonical two-decimal JSON strings only (floats, ints, exponents, NaN, three decimals -> 422; spec A12).

Two body scans run before any model (api.body):
- ``forbidden_keys``: callers never send counts or money on the protocol routes (§C.3: any key matching
  views|count|metric|amount|rate -> 422);
- ``sensitive_problems`` (FIN-28, G7): a key named like a bank/card/tax/identity field, or a value shaped like an
  SSN/EIN/ITIN, a bare 9-digit routing/TIN number, a Luhn-valid card number or a mod-97-valid IBAN, anywhere in the
  body -> 422 SENSITIVE_DATA_REFUSED. Such a value is refused, never stored, never echoed.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, StrictInt, StrictStr, StringConstraints

import money as M
from textguard import has_control_chars, ip_in, iter_strings

ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
FORBIDDEN_KEY_RE = re.compile(r"(?i)(views|viewcount|metric|amount)")
_FORBIDDEN_TOKENS = {"view", "views", "count", "counts", "metric", "metrics", "amount", "amounts", "rate", "rates"}


def _forbidden_key(k: str) -> bool:
    low = k.lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", low) if t]
    return bool(FORBIDDEN_KEY_RE.search(low.replace("_", "").replace("-", ""))) or any(t in _FORBIDDEN_TOKENS for t in toks)
_SENSITIVE_KEY_TOKENS = {"tin", "ssn", "ein", "itin", "dob", "pan", "iban", "routing", "cvv", "cvc", "birth",
                         "birthdate", "passport"}
_SENSITIVE_KEY_SUBSTR = ("account_number", "accountnumber", "acct_no", "acctnum", "bank_account", "date_of_birth",
                         "social_security", "card_number", "cardnumber", "card_no", "credit_card", "debit_card",
                         "card_exp", "card_cvv", "routing_number", "swift_code", "swiftcode", "bic_code")
_SSN = re.compile(r"(?<![0-9A-Za-z])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9A-Za-z])")
_EIN = re.compile(r"(?<![0-9A-Za-z])[0-9]{2}-[0-9]{7}(?![0-9A-Za-z])")
_NINE = re.compile(r"(?<![0-9A-Za-z.])[0-9]{9}(?![0-9A-Za-z])")
_CARD = re.compile(r"(?<![0-9A-Za-z])(?:[0-9][ -]?){12,18}[0-9](?![0-9A-Za-z])")
_IBAN = re.compile(r"(?<![0-9A-Za-z])([A-Z]{2}[0-9]{2}[A-Z0-9 ]{11,34})(?![0-9A-Za-z])")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        num = "".join(str(int(c, 36)) for c in moved)
    except ValueError:
        return False
    return int(num) % 97 == 1


# a bounded run of 10-17 digits (spaces/dashes allowed between them): a bank account or phone-number shape
_ACCOUNT = re.compile(r"(?<![0-9A-Za-z.])[0-9](?:[ -]?[0-9]){9,16}(?![0-9A-Za-z])")


# AEGIS N17-10: Finance's OWN generated ids (``service.rid``: fin-<prefix>-<26 Crockford base32>; ``ledger.derived_id``:
# fin-<prefix>-<40 hex>) are never refused as look-alikes -- a Crockford id can happen to pass the IBAN mod-97 or
# Luhn check. Only a value that IS such an id in full is exempt; the same characters inside any other value are not.
OWN_ID_RE = re.compile(r"fin-[a-z][a-z0-9]{0,7}-(?:[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{40})")


def sensitive_value(s: str) -> Optional[str]:
    if SHA_RE.fullmatch(s) or M.WIRE_PATTERN.fullmatch(s) or OWN_ID_RE.fullmatch(s):
        return None
    if ip_in(s):                                            # AEGIS N17-6 (swept into Finance)
        return "ip_address_shape"
    if _ACCOUNT.search(s):
        return "bank_account_or_phone_number_shape"
    if _SSN.search(s):
        return "ssn_or_itin_shape"
    if _EIN.search(s):
        return "ein_shape"
    if _NINE.search(s):
        return "nine_digit_routing_or_tin_shape"
    for m in _CARD.finditer(s):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "card_number_shape"
    for m in _IBAN.finditer(s):
        if _iban_ok(m.group(1)):
            return "iban_shape"
    return None


def sensitive_key(k: str) -> bool:
    low = k.lower()
    if any(x in low for x in _SENSITIVE_KEY_SUBSTR):
        return True
    return bool(set(re.split(r"[^a-z0-9]+", low)) & _SENSITIVE_KEY_TOKENS)


def sensitive_problems(obj: Any, depth: int = 0, path: str = "body") -> list[str]:
    out: list[str] = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and sensitive_key(k):
                out.append(f"{path}: a key named like bank/card/tax/identity data")
            if isinstance(k, str) and sensitive_value(k):
                out.append(f"{path}: a key shaped like bank/card/tax data")
            out.extend(sensitive_problems(v, depth + 1, f"{path}.{str(k)[:32]}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5000]):
            out.extend(sensitive_problems(v, depth + 1, f"{path}[{i}]"))
    elif isinstance(obj, str):
        kind = sensitive_value(obj)
        if kind:
            out.append(f"{path}: value refused ({kind})")
    return out[:20]


def forbidden_keys(obj: Any, depth: int = 0, prefix: str = "") -> list[str]:
    out: list[str] = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            name = f"{prefix}{k}" if isinstance(k, str) else prefix
            if isinstance(k, str) and _forbidden_key(k):
                out.append(name[:80])
            out.extend(forbidden_keys(v, depth + 1, name + "."))
    elif isinstance(obj, list):
        for v in obj[:5000]:
            out.extend(forbidden_keys(v, depth + 1, prefix))
    return out


# --- field types ----------------------------------------------------------------------------------------------------

def _text(max_len: int, allow_newlines: bool = False, min_len: int = 1):
    def check(v: str) -> str:
        if has_control_chars(v, allow_newlines):
            raise ValueError("control characters are not allowed")
        return v
    return Annotated[str, Field(min_length=min_len, max_length=max_len), AfterValidator(check)]


def _id_check(v: str) -> str:
    if not ID_RE.fullmatch(v):
        raise ValueError("ids are 1-128 characters of [A-Za-z0-9._:-]")
    return v


def _sha_check(v: str) -> str:
    if not SHA_RE.fullmatch(v):
        raise ValueError("expected 64 lowercase hex characters")
    return v


def _money_in(v: Any) -> Decimal:
    try:
        return M.parse(v)
    except M.MoneyError as exc:
        raise ValueError(str(exc)) from None


def _pos(v: Decimal) -> Decimal:
    if v <= 0:
        raise ValueError("money must be greater than 0.00 here")
    return v


def _iso_check(v: str) -> str:
    from clock import parse_iso
    try:
        parse_iso(v)
    except ValueError:
        raise ValueError("expected an RFC 3339 timestamp with offset") from None
    return v


Id = Annotated[str, AfterValidator(_id_check)]
# A client id is also the client's party reference at Legal (37): ``client:<id>`` must fit legal-py's PartyRef
# (``[A-Za-z0-9._-]{1,100}``), or no contract could ever match it (AEGIS launch-hardening L5). Refused at the door.
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
ClientId = Annotated[Id, StringConstraints(pattern=CLIENT_ID_RE.pattern)]
Sha = Annotated[str, AfterValidator(_sha_check)]
Money = Annotated[Decimal, BeforeValidator(_money_in), PlainSerializer(M.fmt, return_type=str, when_used="json")]
PositiveMoney = Annotated[Decimal, BeforeValidator(_money_in), AfterValidator(_pos), PlainSerializer(M.fmt, return_type=str, when_used="json")]
Timestamp = Annotated[str, Field(max_length=40), AfterValidator(_iso_check)]
Note = _text(1000, allow_newlines=True)
Short = _text(200)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class RunRequest(Strict):
    request_id: Id


# --- rules ----------------------------------------------------------------------------------------------------------

class RuleProposalRequest(Strict):
    request_id: Id
    kind: Literal["add", "amend", "retire"]
    target_id: Optional[Annotated[str, Field(max_length=16)]] = None
    proposed_row: Optional[dict] = None


class RuleDecision(Strict):
    proposal_id: Id
    content_sha256: Sha
    decision: Literal["approve", "reject"]
    note: Optional[Note] = None
    acknowledge_weakening: Optional[bool] = None


class RuleDecisions(Strict):
    request_id: Id
    decisions: list[RuleDecision] = Field(min_length=1, max_length=200)


# --- rate cards, profiles -------------------------------------------------------------------------------------------

class TierRates(Strict):
    T0: PositiveMoney
    T1: PositiveMoney
    T2: PositiveMoney
    T3: PositiveMoney


class RateCardProposal(Strict):
    request_id: Id
    doc_id: Optional[Id] = None
    campaign_id: Id
    creator_rate_per_1000: TierRates
    max_paid_views_per_clip: int = Field(ge=1, le=10**12)
    effective_at: Timestamp


class Decision(Strict):
    request_id: Id
    content_sha256: Sha
    decision: Literal["approve", "reject"]
    note: Optional[Note] = None
    acknowledge_weakening: Optional[bool] = None


class DocRef(Strict):
    """A contract held by Legal (37). ``version`` is Legal's ``major.minor`` text (``"1.1"``); a whole number is
    accepted for records written before Legal was wired and means ``"<n>.0"`` -- Legal then confirms it or refuses."""
    doc_id: Id
    version: Union[Annotated[StrictStr, StringConstraints(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}$")],
                   Annotated[StrictInt, Field(ge=1, le=9999)]]
    doc_sha256: Sha
    acceptance_id: Id


class CommercialProfile(Strict):
    request_id: Id
    client_id: ClientId
    order_form: DocRef
    budget: PositiveMoney
    client_rate_per_1000: PositiveMoney
    rate_card_doc_id: Id
    account_title: Optional[Short] = None


class BillingProfile(Strict):
    request_id: Id
    entity: Literal["zbc", "zbm"]
    payment_method: Literal["ach", "wire"]
    msa: DocRef


# --- protocol routes ------------------------------------------------------------------------------------------------

class HandoffVerification(Strict):
    verified: bool
    reason: Optional[_text(2000, True, 0)] = None
    attestation_id: Optional[Annotated[str, Field(max_length=128)]] = None


class HandoffCompliance(Strict):
    allowed: bool
    reason: Optional[_text(2000, True, 0)] = None
    reference: Optional[Annotated[str, Field(max_length=128)]] = None


class HandoffFacts(Strict):
    """creative-py ``PayoutEligibility.as_dict()`` — recorded as a hash, never trusted (§C.3)."""
    submission_id: Id
    eligible: bool
    blockers: list[_text(2000, True, 0)] = Field(default_factory=list, max_length=50)
    clip_review_outcome: Annotated[str, Field(max_length=40)]
    verification: HandoffVerification
    compliance: HandoffCompliance
    note: Optional[_text(1000, True)] = None


class PayoutHandoff(Strict):
    request_id: Id
    submission_id: Id
    facts: HandoffFacts


class PayeeCreate(Strict):
    request_id: Id
    payee_id: Id
    kind: Literal["clipper"]
    declared_country: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = None
    declared_region: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{1,3}$")]] = None
    callback_contact_ref: Optional[Annotated[str, Field(pattern=r"^vault:[A-Za-z0-9._:-]{1,120}$")]] = None
    legal_form: Literal["individual", "entity"] = "individual"
    owner_subject_ids: list[Id] = Field(default_factory=list, max_length=10)


class OffboardingNotice(Strict):
    request_id: Id
    offboarding_id: Id


class CallbackRecord(Strict):
    request_id: Id
    change_event_id: Id
    contact_ref: Annotated[str, Field(pattern=r"^vault:[A-Za-z0-9._:-]{1,120}$")]
    outcome: Literal["confirmed", "denied"]
    notes: Optional[Note] = None
    creator_notified_ref: Optional[Id] = None


class BNotice(Strict):
    request_id: Id
    cp2100_received_on: date
    second_notice_within_3y: bool = False
    first_b_notice_sent_on: Optional[date] = None
    start_withholding: bool = False


# --- receivables ----------------------------------------------------------------------------------------------------

class InvoiceLine(Strict):
    # media_spend / media_fee are not here: Finance writes them itself from a media buy (POST /fin/v1/media-buys).
    line_code: Literal["campaign_deposit", "creative_services", "strategy_services", "production_services",
                       "retainer_fee", "subscription_fee", "revenue_recovery_services"]
    quantity: int = Field(ge=1, le=1_000_000)
    unit_price: PositiveMoney
    description: Optional[Short] = None


class Recurring(Strict):
    consent_artifact_ref: Optional[Id] = None
    cancel_medium: Optional[Literal["same_medium_and_online", "phone_only", "mail_only"]] = None
    annual_reminder_due: Optional[date] = None
    price_change_notice_days: Optional[int] = Field(default=None, ge=0, le=365)
    trial_days: Optional[int] = Field(default=None, ge=0, le=3650)
    trial_reminder_days: Optional[int] = Field(default=None, ge=0, le=365)


class InvoiceDraft(Strict):
    request_id: Id
    entity: Literal["zbc", "zbm"]
    client_id: ClientId
    campaign_id: Optional[Id] = None
    kind: Literal["campaign_deposit", "service", "retainer", "subscription"]
    lines: list[InvoiceLine] = Field(min_length=1, max_length=50)
    payment_methods: list[Literal["ach", "wire", "card"]] = Field(min_length=1, max_length=3)
    due_days: int = Field(default=15, ge=0, le=120)
    legal_ref: DocRef
    recurring: Optional[Recurring] = None
    notes: Optional[Note] = None
    template_vars: Optional[dict[Annotated[str, Field(max_length=40)], Short]] = Field(default=None, max_length=20)


# --- media buys (ADR 0009 amendment, Oct 5 2026; founder decisions M1-M8) ------------------------------------------

class MediaBuyCreate(Strict):
    """Andre records a media buy; Finance computes the fee and drafts its prepayment invoice from it."""
    request_id: Id
    client_id: ClientId
    media_type: Literal["broadcast_tv", "cable_tv", "streaming_tv", "radio", "streaming_audio", "podcast",
                        "out_of_home", "digital", "print", "other"]
    vendor_ref: Id                                   # an opaque vendor id/name slug; never bank details (FIN-28)
    description: Short                               # client-facing: what they are buying
    flight_start: date
    flight_end: date
    media_cost: PositiveMoney                        # what ZBM pays the vendor
    markup_pct: Optional[Money] = None               # None = FIN_MEDIA_DEFAULT_MARKUP_PCT (founder M4: 15.00)
    display: Literal["breakout", "blended"]          # what the client's invoice shows (M5)
    due_days: int = Field(default=0, ge=0, le=60)    # prepaid: due on receipt by default
    legal_ref: DocRef                                # the signed media agreement / insertion order


class VendorPayment(Strict):
    """Andre records a payment HE made to the media vendor (M7): Finance never initiates it."""
    request_id: Id
    amount: PositiveMoney
    paid_on: date
    method: Literal["ach", "wire", "check"]
    payment_ref_sha256: Sha                          # hash of the bank/check reference, never the reference itself


class MediaDelivery(Strict):
    request_id: Id
    delivered_on: date
    evidence_refs: list[Id] = Field(min_length=1, max_length=20)     # proof of performance / affidavit refs


class StatementLine(Strict):
    txn_ref_sha256: Sha
    entity: Literal["zbc", "zbm"]
    account: Literal["1010", "1020"]
    direction: Literal["credit"]
    amount: PositiveMoney
    value_date: date
    reference_token: Optional[Id] = None


class BankEvents(Strict):
    request_id: Id
    lines: list[StatementLine] = Field(min_length=1, max_length=200)


class RailEvent(Strict):
    event_id: Id
    type: Literal["paid", "failed", "returned", "destination_changed", "account_updated"]
    item_id: Optional[Id] = None
    account_ref: Optional[Id] = None
    new_destination_fingerprint: Optional[Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]] = None
    signature: Optional[Annotated[str, Field(max_length=256)]] = None


class StripeCheckoutRequest(Strict):
    request_id: Id


class StripeEventIn(Strict):
    """A Stripe webhook as the gateway received it: the RAW body (exactly the bytes Stripe signed, as text) and the
    ``Stripe-Signature`` header. Finance verifies it; the gateway never parses or edits the body."""
    request_id: Id
    payload: Annotated[str, Field(min_length=2, max_length=256 * 1024)]
    signature: Annotated[str, Field(min_length=1, max_length=1024)]


class RailEvents(Strict):
    request_id: Id
    events: list[RailEvent] = Field(min_length=1, max_length=100)


class DisputeOpen(Strict):
    request_id: Id
    kind: Literal["invoice_dispute", "card_chargeback"]
    invoice_id: Id
    amount: PositiveMoney
    evidence_refs: list[Id] = Field(default_factory=list, max_length=50)
    notes: Optional[Note] = None


class DisputeOutcome(Strict):
    request_id: Id
    outcome: Literal["won", "lost", "withdrawn"]
    notes: Optional[Note] = None


# --- payouts, recon, treasury, controls, close --------------------------------------------------------------------

class PayoutRun(Strict):
    request_id: Id
    rail: Literal["stripe", "trolley"] = "stripe"


class ExceptionDecision(Strict):
    request_id: Id
    decision: Literal["approve", "reject"]
    note: Optional[Note] = None


class Evidence(Strict):
    ref: Id
    sha256: Sha


class BreakResolution(Strict):
    request_id: Id
    explanation_code: Literal["timing_in_transit", "fee_unbooked", "misapplied_receipt", "rail_return", "unknown"]
    entry_id: Optional[Id] = None
    clears_by: Optional[date] = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)


class TreasurySettlement(Strict):
    """Andre settles a treasury operation whose bank outcome is unknown (AEGIS f751017 M-N1)."""
    request_id: Id
    content_sha256: Sha
    outcome: Literal["moved", "not_moved"]
    bank_ref: Optional[Id] = None
    note: Optional[Note] = None


class SweepProposal(Strict):
    request_id: Id
    amount: PositiveMoney


class FundingProposal(Strict):
    request_id: Id
    batch_id: Id


class ApplyReceipt(Strict):
    request_id: Id
    invoice_id: Id


class TopUp(Strict):
    request_id: Id
    amount: PositiveMoney
    reason_code: Literal["clawback_after_release", "over_budget", "dispute_loss", "deposit_return_shortfall",
                         "other"] = "other"
    notes: Optional[Note] = None
    shortfall_id: Optional[Id] = None


class DepositReturn(Strict):
    """A matched client deposit returned by the bank (ACH return), AEGIS N17-9. No amount: the whole receipt."""
    request_id: Id
    return_ref_sha256: Sha
    return_code: Annotated[str, Field(pattern=r"^R[0-9]{2}$")]
    value_date: date


class CorrectionLine(Strict):
    entity: Optional[Literal["zbc", "zbm"]] = None
    account: Annotated[str, Field(pattern=r"^[0-9]{4}$")]
    subledger: Optional[Annotated[str, Field(max_length=128)]] = None
    debit: Money
    credit: Money


class Correction(Strict):
    request_id: Id
    effective_date: date
    reverses_entry_id: Optional[Id] = None
    lines: Optional[list[CorrectionLine]] = Field(default=None, max_length=50)
    notes: Optional[Note] = None


class ControlResult(Strict):
    request_id: Id
    result: Literal["pass", "fail"]
    evidence_ref: Id


class TaxReadiness(Strict):
    request_id: Id
    tcc_obtained_at: Optional[date] = None
    iris_test_passed_at: Optional[date] = None
    ftb_swift_ready_at: Optional[date] = None


class ReconcileRequest(Strict):
    request_id: Id
    head_sha256: Sha
    void_lines: list[int] = Field(default_factory=list, max_length=10000)
    void_event_ids: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list, max_length=10000)


def texts(obj: Any) -> list[str]:
    return list(iter_strings(obj))
