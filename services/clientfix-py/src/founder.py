"""
Andre's approval token — legal-py's ``FounderGate`` (itself creative-py / compliance-py's), unchanged in behaviour.

``CFX_ANDRE_APPROVAL_TOKEN`` is compared with ``hmac.compare_digest``; a token equal to the service token OR to ANY
caller token is treated as NOT configured (otherwise that caller would be Andre). Not configured -> every approval
refused; missing or wrong -> refused; non-ASCII -> refused (compare_digest TypeError), never a 500. Moving Andre's
approvals to Cybersecurity (22) passkeys is an unlock-list item (ADR 0017).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Iterable, Optional

from errors import FounderRefused
from reasons import R

FOUNDER = "andre"
HEADER = "X-Andre-Approval-Token"


@dataclass(frozen=True)
class FounderGate:
    token: Optional[str]

    @classmethod
    def build(cls, founder_token: Optional[str], service_token: Optional[str],
              caller_tokens: Iterable[str] = ()) -> "FounderGate":
        if not founder_token or founder_token == service_token or founder_token in set(caller_tokens):
            return cls(None)
        return cls(founder_token)

    @property
    def configured(self) -> bool:
        return self.token is not None

    def verify(self, supplied: Optional[str]) -> None:
        if self.token is None:
            raise FounderRefused(R("ANDRE_NOT_CONFIGURED"))
        if not supplied:
            raise FounderRefused(R("ANDRE_APPROVAL_REQUIRED"))
        try:
            ok = hmac.compare_digest(supplied, self.token)
        except TypeError:
            ok = False
        if not ok:
            raise FounderRefused(R("ANDRE_APPROVAL_INVALID"))
