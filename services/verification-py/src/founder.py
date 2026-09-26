"""
Andre's approval token (V&I spec B.6) — the creative-py / compliance-py ``FounderGate`` pattern.

``VI_ANDRE_APPROVAL_TOKEN`` is checked exactly like
creative-py/src/shared/founder.py, plus one rule the spec adds: a token
equal to the service token OR to ANY caller token is treated as NOT
configured (otherwise that caller would be Andre). Not configured -> every
approval refused; missing or wrong -> refused; non-ASCII -> refused
(compare_digest TypeError), never a 500.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Iterable, Optional

from errors import FounderRefused

FOUNDER = "andre"


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
            raise FounderRefused("Andre approval token is not configured on this service (or equals the service or "
                                 "a caller token); approvals are impossible until it is set (fail closed)")
        if not supplied:
            raise FounderRefused("Andre approval token missing")
        try:
            ok = hmac.compare_digest(supplied, self.token)
        except TypeError:
            ok = False
        if not ok:
            raise FounderRefused("Andre approval token invalid")
