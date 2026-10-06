"""
New Business Development -> Legal (37): the agreement hand-off (ADR 0016 decision 19), modelled on service-py's
``legal_client`` (its Legal port), with the HTTP client NOT built: legal-py has no intake kind for a partner agreement,
an NDA or a white-label contract yet (its matter kinds are disputes, claims, privacy requests and questions), and
building a client against a route that does not exist would be guessing a contract. ``NBD_LEGAL_URL`` therefore
refuses start (config.NOT_BUILT) and the port is the stand-in below.

The port has two calls, both made OUTSIDE the service lock:
- ``send(handoff)``: hand one agreement to Legal for drafting / review / signature. ``delivered`` with a Legal matter
  reference is the only success; anything else is not.
- ``in_force(partner_id, kind)``: is an executed agreement of that kind in force with that partner?
The stand-in answers ``unavailable`` to both, so sending is refused ``LEGAL_UNAVAILABLE`` (nothing is recorded, the
caller retries once Legal is wired) and a partner deal cannot be marked won (no agreement can be shown in force).
A hand-off carries ids and codes only: never a contact detail, a tax reference or agreement text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

AGREEMENT_KINDS = ("referral_agreement", "alliance_agreement", "white_label_agreement", "nda")


@dataclass(frozen=True)
class AgreementHandoff:
    handoff_id: str
    kind: str                         # one of AGREEMENT_KINDS
    brand: str
    partner_id: Optional[str]
    pursuit_id: Optional[str]


@dataclass(frozen=True)
class LegalAnswer:
    status: str                       # delivered | in_force | not_in_force | refused | unavailable
    reference: Optional[str] = None


class LegalAgreements(Protocol):
    wired: bool

    def send(self, handoff: AgreementHandoff) -> LegalAnswer: ...

    def in_force(self, partner_id: str, kind: str) -> LegalAnswer: ...


class NotWiredLegal:
    wired = False

    def send(self, handoff: AgreementHandoff) -> LegalAnswer:
        return LegalAnswer("unavailable")

    def in_force(self, partner_id: str, kind: str) -> LegalAnswer:
        return LegalAnswer("unavailable")
