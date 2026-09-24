"""
Ports to departments and infrastructure that DO NOT EXIST YET.

House rule (BUILD_CONTRACTS.md section 0): a missing department is reached
only through an interface whose default stand-in answers "not allowed
yet" — never "fine". Each ``NotBuilt*`` class below is that default. The
``Fake*`` classes are test doubles that tests opt into explicitly; the API
never uses them unless constructed with them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from onboarding_schema import ContractTerms

NOT_ALLOWED_YET = "not allowed yet"


@dataclass(frozen=True)
class Ruling:
    allowed: bool
    unmet: tuple[str, ...] = ()
    detail: str = ""


# --- Compliance department (38) -----------------------------------------------


class ComplianceDepartment(Protocol):
    def rule(self, subject_id: str, lane: str, facts: dict) -> Ruling: ...


class NotBuiltComplianceDepartment:
    def rule(self, subject_id, lane, facts) -> Ruling:
        return Ruling(
            allowed=False,
            unmet=("compliance_department_38_ruling: Compliance department (38) is not built — not allowed yet",),
            detail=NOT_ALLOWED_YET,
        )


class FakeComplianceDepartment:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed

    def rule(self, subject_id, lane, facts) -> Ruling:
        return Ruling(allowed=self.allowed, unmet=() if self.allowed else ("compliance_department_38_ruling: blocked",))


# --- Contract storage (location undecided) ---------------------------------------


class ContractStorage(Protocol):
    def get(self, client_id: str) -> Optional[ContractTerms]: ...

    def put(self, terms: ContractTerms) -> None: ...


class ContractStorageUnavailable(RuntimeError):
    pass


class NotDecidedContractStorage:
    def get(self, client_id):
        return None

    def put(self, terms):
        raise ContractStorageUnavailable(
            "contract storage location is not decided — refusing to store contract terms (not allowed yet)"
        )


class InMemoryContractStorage:
    def __init__(self) -> None:
        self._terms: dict[str, ContractTerms] = {}

    def get(self, client_id):
        return self._terms.get(client_id)

    def put(self, terms):
        self._terms[terms.client_id] = terms


# --- Verification and Integrity (age verification for clippers) --------------------


class VerificationDepartment(Protocol):
    def age_verified_18_plus(self, creator_id: str) -> Ruling: ...


class NotBuiltVerificationDepartment:
    def age_verified_18_plus(self, creator_id):
        return Ruling(False, ("age_verified_18_plus: Verification and Integrity is not built — not allowed yet",), NOT_ALLOWED_YET)


class FakeVerificationDepartment:
    def __init__(self, verified: bool = True):
        self.verified = verified

    def age_verified_18_plus(self, creator_id):
        return Ruling(self.verified, () if self.verified else ("age_verified_18_plus: not verified",))


# --- Billing (P16) ------------------------------------------------------------------


class BillingDepartment(Protocol):
    def billing_ready(self, client_id: str) -> Ruling: ...


class NotBuiltBillingDepartment:
    def billing_ready(self, client_id):
        return Ruling(False, ("billing_setup_p16: Billing department is not built — not allowed yet",), NOT_ALLOWED_YET)


class FakeBillingDepartment:
    def billing_ready(self, client_id):
        return Ruling(True)


# --- ZBC payouts / tax ----------------------------------------------------------------


class PayoutsDepartment(Protocol):
    def activate_payout_account(self, creator_id: str) -> Ruling: ...


class NotBuiltPayoutsDepartment:
    def activate_payout_account(self, creator_id):
        return Ruling(False, ("payout_account: ZBC payouts/tax is not built — not allowed yet",), NOT_ALLOWED_YET)


class FakePayoutsDepartment:
    def __init__(self) -> None:
        self.activated: list[str] = []

    def activate_payout_account(self, creator_id):
        self.activated.append(creator_id)
        return Ruling(True)


# --- Activation handoff (RR / Digital Advertising / Fulfillment) ------------------------


class HandoffTarget(Protocol):
    def accept(self, client_file: dict) -> Ruling: ...


class NotWiredHandoff:
    """RR, Digital Advertising and Fulfillment have no intake route for an
    onboarding client file yet. Handoff is not accepted until one exists
    AND names an owner."""

    def accept(self, client_file):
        return Ruling(False, ("handoff: receiving department has no intake route — not accepted, no owner named",), NOT_ALLOWED_YET)


class FakeHandoff:
    def __init__(self, owner: str = "rr_owner_test"):
        self.owner = owner
        self.received: list[dict] = []

    def accept(self, client_file):
        self.received.append(client_file)
        return Ruling(True, (), f"accepted; owner={self.owner}")


# --- Andre's phone (push) ---------------------------------------------------------------


class PushNotifier(Protocol):
    def push(self, escalation_id: str, briefing: dict) -> tuple[bool, str]: ...


class NotWiredPushNotifier:
    def push(self, escalation_id, briefing):
        return False, "push channel to Andre's phone is not wired — briefing NOT delivered"


class FakePushNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def push(self, escalation_id, briefing):
        self.sent.append((escalation_id, briefing))
        return True, "delivered (fake)"


@dataclass
class Departments:
    compliance: ComplianceDepartment = field(default_factory=NotBuiltComplianceDepartment)
    contracts: ContractStorage = field(default_factory=NotDecidedContractStorage)
    verification: VerificationDepartment = field(default_factory=NotBuiltVerificationDepartment)
    billing: BillingDepartment = field(default_factory=NotBuiltBillingDepartment)
    payouts: PayoutsDepartment = field(default_factory=NotBuiltPayoutsDepartment)
    handoff: HandoffTarget = field(default_factory=NotWiredHandoff)
    notifier: PushNotifier = field(default_factory=NotWiredPushNotifier)
