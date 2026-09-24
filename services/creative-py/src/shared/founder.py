"""
Andre's approval token.

Andre's final approval (ZBM) and his per-campaign signature (ZBC) require a
SEPARATE secret, `CREATIVE_ANDRE_APPROVAL_TOKEN`, not the service bearer
token — so a caller who merely has API access cannot sign as Andre.

Fail closed:
- token not configured            -> every founder approval refused;
- configured equal to the service token -> treated as NOT configured
  (otherwise every API caller would be Andre);
- non-ASCII supplied token        -> refused (compare_digest TypeError), never a 500.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from shared.errors import FounderApprovalRefused

FOUNDER_ACTOR = "andre"


@dataclass(frozen=True)
class FounderGate:
    token: str | None

    @classmethod
    def build(cls, founder_token: str | None, service_token: str | None) -> "FounderGate":
        if not founder_token or founder_token == service_token:
            return cls(None)
        return cls(founder_token)

    @property
    def configured(self) -> bool:
        return self.token is not None

    def verify(self, supplied: str | None) -> None:
        if self.token is None:
            raise FounderApprovalRefused(
                "Andre approval token is not configured on this service (or equals the service "
                "token); founder approvals are impossible until it is set (fail closed)"
            )
        if not supplied:
            raise FounderApprovalRefused("Andre approval token missing")
        try:
            ok = hmac.compare_digest(supplied, self.token)
        except TypeError:
            ok = False
        if not ok:
            raise FounderApprovalRefused("Andre approval token invalid")
