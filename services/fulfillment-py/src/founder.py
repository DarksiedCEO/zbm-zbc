"""
Andre's approval token for fulfillment-py (AEGIS re-review of bug sweep D) -- finance-py's / creative-py's
``FounderGate``, ported minimally. Stdlib only.

``FULFILLMENT_ANDRE_APPROVAL_TOKEN`` gates the write-back reconcile (a ruling that can cause a second write to a
client's system of record, or record a write as done): the service token alone -- which every calling department
holds -- is never enough. A token equal to the service token is treated as NOT configured (otherwise every caller
would be Andre). Not configured -> every approval refused; missing or wrong -> refused; non-ASCII -> refused
(``compare_digest`` TypeError), never a 500.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional

HEADER = "X-Andre-Approval-Token"


class FounderRefused(Exception):
    pass


@dataclass(frozen=True)
class FounderGate:
    token: Optional[str]

    @classmethod
    def build(cls, founder_token: Optional[str], service_token: Optional[str]) -> "FounderGate":
        if not founder_token or founder_token == service_token:
            return cls(None)
        return cls(founder_token)

    @property
    def configured(self) -> bool:
        return self.token is not None

    def verify(self, supplied: Optional[str]) -> None:
        if self.token is None:
            raise FounderRefused("Andre's approval token is not configured on this service (or equals the service "
                                 "token); approvals are impossible until it is set (fail closed)")
        if not supplied:
            raise FounderRefused("Andre's approval token missing")
        try:
            ok = hmac.compare_digest(supplied, self.token)
        except TypeError:
            ok = False
        if not ok:
            raise FounderRefused("Andre's approval token invalid")
