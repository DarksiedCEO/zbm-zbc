"""
Outbound ports (Finance spec §D.2). Protocols and their fail-closed stand-ins ONLY: every ``NotBuilt*`` /
``NotWired*`` class answers unavailable / not allowed, never "fine" (BUILD_CONTRACTS §0, spec G3). The passing fakes
live in ``tests/fakes.py`` and are never importable from ``src/``. A port that raises is treated as unavailable by
the service and its exception text is dropped.

What no port offers, by design: a debit, pull or reversal of a creator's external account (FIN-16, test A7), a
place to put a bank account number, card number, TIN, SSN or date of birth (FIN-28), a way for a caller to supply a
view count or an amount (FIN-04).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

NOT_BUILT = "not built yet: not allowed yet"


# --- Verification and Integrity ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Certification:
    available: bool
    submission_id: Optional[str] = None
    certification_id: Optional[str] = None
    status: Optional[str] = None              # certified | revised | voided | pending | not_certified | ...
    certified_views: Optional[int] = None
    campaign_id: Optional[str] = None
    clipper_id: Optional[str] = None
    platform: Optional[str] = None
    create_time: Optional[str] = None         # window.create_time (RFC 3339)
    certified_at: Optional[str] = None
    open_finding: bool = False
    rules_version: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class ClawbackPage:
    available: bool
    items: tuple = ()                          # dicts: clawback_id, certification_id, views_delta, cause, rule_id
    next_cursor: Optional[int] = None
    reason: str = ""


class VerificationPort(Protocol):
    def certification(self, submission_id: str) -> Certification: ...

    def clawbacks(self, cursor: int) -> ClawbackPage: ...


class NotWiredVerification:
    REASON = f"Verification and Integrity is not wired to Finance: {NOT_BUILT}"

    def certification(self, submission_id):
        return Certification(False, reason=self.REASON)

    def clawbacks(self, cursor):
        return ClawbackPage(False, reason=self.REASON)


# --- Compliance (38) --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ComplianceRuling:
    available: bool
    ruling_id: Optional[str] = None
    gate: Optional[str] = None
    subject_id: Optional[str] = None
    allowed: bool = False
    evaluated_at: Optional[str] = None
    register_version: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class HoldsAnswer:
    available: bool
    open_hold_ids: tuple = ()
    reason: str = ""


@dataclass(frozen=True)
class SanctionsAnswer:
    available: bool
    screen_id: Optional[str] = None
    result: Optional[str] = None              # clear | potential_match | match
    list_version: Optional[str] = None
    list_current: bool = False
    screened_at: Optional[str] = None
    fresh: bool = False
    reason: str = ""


@dataclass(frozen=True)
class RegisterRow:
    available: bool
    obligation_id: str = ""
    effective_status: str = "unknown"         # verified | unverified | expired | superseded
    expires_at: Optional[str] = None
    register_version: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class JurisdictionAnswer:
    available: bool
    status: str = "unknown"                   # operate | conditional | blocked | unknown
    reason: str = ""


class CompliancePort(Protocol):
    def ruling(self, ruling_id: str) -> ComplianceRuling: ...

    def holds(self, subjects: tuple) -> HoldsAnswer: ...

    def sanctions_status(self, subject_id: str, role: str) -> SanctionsAnswer: ...

    def row(self, obligation_id: str) -> RegisterRow: ...

    def jurisdiction(self, country: str, region: Optional[str]) -> JurisdictionAnswer: ...


class NotWiredCompliance:
    REASON = f"Compliance (38) is not wired to Finance: {NOT_BUILT}"

    def ruling(self, ruling_id):
        return ComplianceRuling(False, reason=self.REASON)

    def holds(self, subjects):
        return HoldsAnswer(False, reason=self.REASON)

    def sanctions_status(self, subject_id, role):
        return SanctionsAnswer(False, reason=self.REASON)

    def row(self, obligation_id):
        return RegisterRow(False, obligation_id, reason=self.REASON)

    def jurisdiction(self, country, region):
        return JurisdictionAnswer(False, reason=self.REASON)


# --- Clipper Network, Legal 37 ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TierAnswer:
    available: bool
    tier: Optional[str] = None                # T0..T3
    reason: str = ""


class ClipperNetworkPort(Protocol):
    def tier(self, campaign_id: str, clipper_id: str) -> TierAnswer: ...

    def notify(self, template: str, clipper_id: str, ref: str) -> bool: ...


class NotBuiltClipperNetwork:
    REASON = f"Clipper Network has no Finance client: {NOT_BUILT}"

    def tier(self, campaign_id, clipper_id):
        return TierAnswer(False, reason=self.REASON)

    def notify(self, template, clipper_id, ref):
        return False


@dataclass(frozen=True)
class LegalAnswer:
    available: bool
    current: bool = False
    acceptance_matches: bool = False
    reason: str = ""


class LegalPort(Protocol):
    """``current``: the version is approved and in force at Legal, carries ``doc_sha256``, and belongs to ``entity``.
    ``acceptance_matches``: the acceptance is for exactly that document, version and hash, its evidence is sufficient,
    and it was given by ``party_ref`` (``client:<client_id>``). ``version`` is Legal's ``major.minor`` text."""

    def document_status(self, doc_id: str, version: str, doc_sha256: str, acceptance_id: str,
                        party_ref: Optional[str] = None, entity: Optional[str] = None) -> LegalAnswer: ...


class NotBuiltLegal37:
    REASON = f"Legal (37) is not wired to Finance (FIN_LEGAL_URL unset): {NOT_BUILT}"

    def document_status(self, doc_id, version, doc_sha256, acceptance_id, party_ref=None, entity=None):
        return LegalAnswer(False, reason=self.REASON)


# --- Rails (Stripe Connect, Trolley) -----------------------------------------------------------------------------

@dataclass(frozen=True)
class RailAccount:
    available: bool
    account_ref: Optional[str] = None          # opaque id at the rail (never account data)
    status: str = "unknown"                    # verified | pending | restricted | unknown
    payouts_enabled: bool = False
    destination_fingerprint: Optional[str] = None   # rail-supplied fingerprint; hashed before it is kept
    reason: str = ""


@dataclass(frozen=True)
class RailSubmit:
    outcome: str                                # accepted | rejected | transport_error | unavailable
    rail_ref: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class RailLookup:
    available: bool
    found: bool = False
    rail_ref: Optional[str] = None
    status: Optional[str] = None                # submitted | paid | failed | returned
    reason: str = ""


@dataclass(frozen=True)
class RailBalance:
    available: bool
    balance: Optional[str] = None               # money string
    as_of: Optional[str] = None
    source_sha256: Optional[str] = None
    reason: str = ""


class RailPort(Protocol):
    """No debit, pull or reversal method exists (FIN-16)."""

    def create_account(self, payee_id: str, country: str) -> RailAccount: ...

    def account_status(self, account_ref: str) -> RailAccount: ...

    def submit(self, idempotency_key: str, account_ref: str, amount: str, item_id: str) -> RailSubmit: ...

    def lookup(self, idempotency_key: str, item_id: str) -> RailLookup: ...

    def balance(self) -> RailBalance: ...

    def verify_event(self, body: dict, signature: Optional[str]) -> bool: ...


class NotWiredRail:
    def __init__(self, name: str):
        self.name = name
        self.REASON = f"rail {name} is not wired: {NOT_BUILT} (nothing is payable)"

    def create_account(self, payee_id, country):
        return RailAccount(False, reason=self.REASON)

    def account_status(self, account_ref):
        return RailAccount(False, reason=self.REASON)

    def submit(self, idempotency_key, account_ref, amount, item_id):
        return RailSubmit("unavailable", reason=self.REASON)

    def lookup(self, idempotency_key, item_id):
        return RailLookup(False, reason=self.REASON)

    def balance(self):
        return RailBalance(False, reason=self.REASON)

    def verify_event(self, body, signature):
        return False


# --- Bank (feed + transfers) ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BankBalance:
    available: bool
    balance: Optional[str] = None
    as_of: Optional[str] = None
    source_sha256: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class BankTransfer:
    outcome: str                                # accepted | refused | unavailable
    ref: Optional[str] = None
    reason: str = ""


class BankPort(Protocol):
    def balance(self, entity: str, account: str) -> BankBalance: ...

    def transfer(self, entity: str, from_account: str, to_account: str, amount: str, key: str) -> BankTransfer: ...


class NotWiredBank:
    REASON = f"bank feed / bank transfers are not wired (bank not chosen): {NOT_BUILT}"

    def balance(self, entity, account):
        return BankBalance(False, reason=self.REASON)

    def transfer(self, entity, from_account, to_account, amount, key):
        return BankTransfer("unavailable", reason=self.REASON)


# --- Tax agent, GL, vault, People 43, push ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaxAgentAnswer:
    available: bool
    form_kind: Optional[str] = None             # w9 | w8ben | w8bene
    form_on_file: bool = False
    form_received_at: Optional[str] = None
    tin_match: str = "unavailable"              # matched | mismatched | pending | not_applicable | unavailable
    tin_match_at: Optional[str] = None
    w8_current: Optional[bool] = None
    services_outside_us_attested: Optional[bool] = None
    agent_ref: Optional[str] = None
    reason: str = ""


class TaxAgentPort(Protocol):
    def status(self, payee_id: str) -> TaxAgentAnswer: ...


class NotWiredTaxAgent:
    REASON = f"tax agent (TIN matching) is not wired: {NOT_BUILT}"

    def status(self, payee_id):
        return TaxAgentAnswer(False, reason=self.REASON)


@dataclass(frozen=True)
class GLAnswer:
    available: bool
    trial_balance_sha256: Optional[str] = None
    reason: str = ""


class GLPort(Protocol):
    def trial_balance(self, entity: str, period: str) -> GLAnswer: ...


class NotWiredGL:
    REASON = f"GL adapter (QBO) is not wired: {NOT_BUILT}"

    def trial_balance(self, entity, period):
        return GLAnswer(False, reason=self.REASON)


class VaultPort(Protocol):
    def identity_hmac_key(self) -> Optional[bytes]: ...

    def contact_ref_valid(self, ref: str) -> Optional[bool]: ...


class NotWiredVault:
    def identity_hmac_key(self):
        return None

    def contact_ref_valid(self, ref):
        return None


class People43Port(Protocol):
    def second_approver_active(self) -> Optional[bool]: ...


class NotBuiltPeople43:
    def second_approver_active(self):
        return None


class PushPort(Protocol):
    def push(self, kind: str, briefing: dict) -> bool: ...


class NotBuiltPush:
    def push(self, kind, briefing):
        return False


class ClientMailPort(Protocol):
    """Sends a client their payment receipt (founder, Oct 5 2026: "that receipt goes out to them with the explanation
    of what they bought via email"). True only when the mail provider accepted the message for delivery.

    Contract for a real adapter (AEGIS N1/N2): delivery is at-least-once -- a crash after the provider accepted the
    message, or a call that outlives Finance's 15-minute stale-claim window, is retried. The adapter MUST pass
    ``receipt["client_receipt_id"]`` to the provider as its idempotency / de-duplication key, and MUST time out well
    inside 15 minutes, so a retry never reaches the client as a second email."""
    def send_receipt(self, client_id: str, receipt: dict) -> bool: ...


class NotBuiltClientMail:
    """No mail provider is chosen or wired: nothing is sent; the receipt stays ``pending_send``."""
    def send_receipt(self, client_id, receipt):
        return False


# --- Stripe incoming: ZBM client payments (founder M6/M9/M10, ADR 0009 amendment Oct 5 2026) -------------------------
# Money is a canonical money string here (the adapter converts Stripe's integer cents exactly). Stripe ids are opaque.
# Finance never trusts an event's body: every webhook is only a "look again" signal; the adapter re-reads the object.

@dataclass(frozen=True)
class StripeCheckout:
    outcome: str                                # created | rejected | transport_error | unavailable
    session_id: Optional[str] = None
    url: Optional[str] = None
    expires_at: Optional[str] = None            # RFC 3339
    reason: str = ""


@dataclass(frozen=True)
class StripeSession:
    available: bool
    found: bool = False
    session_id: Optional[str] = None
    status: Optional[str] = None                # open | complete | expired
    payment_intent: Optional[str] = None
    client_reference_id: Optional[str] = None
    livemode: bool = False
    reason: str = ""


@dataclass(frozen=True)
class StripePayment:
    available: bool
    found: bool = False
    payment_intent: Optional[str] = None
    status: Optional[str] = None                # the PaymentIntent status
    amount_received: Optional[str] = None
    currency: Optional[str] = None
    invoice_id: Optional[str] = None            # metadata.invoice_id (set by Finance's own checkout)
    method: Optional[str] = None                # card | us_bank_account | other
    charge: Optional[str] = None
    charge_status: Optional[str] = None         # succeeded | pending | failed
    balance_txn: Optional[str] = None
    gross: Optional[str] = None                 # the charge's balance transaction: amount
    fee: Optional[str] = None                   # ... and fee
    failure_txn: Optional[str] = None           # the charge's failure balance transaction, if it failed after success
    failure_amount: Optional[str] = None        # signed (negative)
    failure_fee: Optional[str] = None           # signed
    livemode: bool = False
    reason: str = ""


@dataclass(frozen=True)
class StripeDispute:
    available: bool
    found: bool = False
    dispute_id: Optional[str] = None
    payment_intent: Optional[str] = None
    status: Optional[str] = None
    amount: Optional[str] = None
    txns: tuple = ()                            # dicts: txn_id, amount (signed), fee (signed)
    livemode: bool = False
    reason: str = ""


@dataclass(frozen=True)
class StripePayout:
    available: bool
    found: bool = False
    payout_id: Optional[str] = None
    status: Optional[str] = None                # paid | pending | in_transit | canceled | failed
    amount: Optional[str] = None
    livemode: bool = False
    reason: str = ""


class StripeIncomingPort(Protocol):
    """ZBM's Stripe account, incoming side only. No refund, transfer or payout-creating method exists here."""

    def create_checkout(self, invoice_id: str, amount: str, methods: tuple, idempotency_key: str, expires_at: int,
                        label: str) -> StripeCheckout: ...

    def verify_event(self, payload: str, signature: str, now_epoch: int) -> bool: ...

    def session(self, session_id: str) -> StripeSession: ...

    def expire_session(self, session_id: str) -> StripeSession: ...

    def payment(self, payment_intent: str) -> StripePayment: ...

    def dispute(self, dispute_id: str) -> StripeDispute: ...

    def payout(self, payout_id: str) -> StripePayout: ...

    def balance(self) -> RailBalance: ...


class NotWiredStripeIncoming:
    REASON = f"Stripe (incoming client payments) is not wired (FIN_STRIPE_INCOMING unset): {NOT_BUILT}"

    def create_checkout(self, invoice_id, amount, methods, idempotency_key, expires_at, label):
        return StripeCheckout("unavailable", reason=self.REASON)

    def verify_event(self, payload, signature, now_epoch):
        return False

    def session(self, session_id):
        return StripeSession(False, reason=self.REASON)

    def expire_session(self, session_id):
        return StripeSession(False, reason=self.REASON)

    def payment(self, payment_intent):
        return StripePayment(False, reason=self.REASON)

    def dispute(self, dispute_id):
        return StripeDispute(False, reason=self.REASON)

    def payout(self, payout_id):
        return StripePayout(False, reason=self.REASON)

    def balance(self):
        return RailBalance(False, reason=self.REASON)


@dataclass
class Ports:
    vi: VerificationPort = field(default_factory=NotWiredVerification)
    compliance: CompliancePort = field(default_factory=NotWiredCompliance)
    cn: ClipperNetworkPort = field(default_factory=NotBuiltClipperNetwork)
    legal: LegalPort = field(default_factory=NotBuiltLegal37)
    rails: dict = field(default_factory=lambda: {"stripe": NotWiredRail("stripe"), "trolley": NotWiredRail("trolley")})
    bank: BankPort = field(default_factory=NotWiredBank)
    tax: TaxAgentPort = field(default_factory=NotWiredTaxAgent)
    gl: GLPort = field(default_factory=NotWiredGL)
    vault: VaultPort = field(default_factory=NotWiredVault)
    people: People43Port = field(default_factory=NotBuiltPeople43)
    push: PushPort = field(default_factory=NotBuiltPush)
    client_mail: ClientMailPort = field(default_factory=NotBuiltClientMail)
    stripe_in: StripeIncomingPort = field(default_factory=NotWiredStripeIncoming)


STAND_INS = (NotWiredVerification, NotWiredCompliance, NotBuiltClipperNetwork, NotBuiltLegal37, NotWiredRail,
             NotWiredBank, NotWiredTaxAgent, NotWiredGL, NotWiredVault, NotBuiltPeople43, NotBuiltPush,
             NotBuiltClientMail, NotWiredStripeIncoming)
