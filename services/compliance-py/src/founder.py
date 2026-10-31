"""
Andre's approval token (spec B.6) — the creative-py ``FounderGate`` pattern.

``COMPLIANCE_ANDRE_APPROVAL_TOKEN`` is checked exactly like
creative-py/src/shared/founder.py, plus one rule the spec adds: a token
equal to the service token OR to ANY caller token is treated as NOT
configured (otherwise that caller would be Andre). Not configured -> every
approval refused; missing or wrong -> refused; non-ASCII -> refused
(compare_digest TypeError), never a 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import token_rotation
from errors import FounderRefused

FOUNDER = "andre"


@dataclass(frozen=True)
class FounderGate:
    token: Optional[token_rotation.Secret]

    @classmethod
    def build(cls, founder_token: Optional[str], service_token: Optional[str],
              caller_tokens: Iterable[str] = (), founder_sha256: Iterable[str] = ()) -> "FounderGate":
        # fix wave A (RTA-5): the accepted values as SHA-256 digests (token_rotation.Secret), never the plaintext;
        # built from the plaintext token and/or *_ANDRE_APPROVAL_TOKEN_SHA256 / _NEXT_SHA256 (a rotation window)
        secret = token_rotation.Secret([founder_token], founder_sha256)
        if not secret or secret.overlaps([service_token, *caller_tokens]):
            return cls(None)
        return cls(secret)

    @property
    def configured(self) -> bool:
        return self.token is not None

    def verify(self, supplied: Optional[str]) -> None:
        if self.token is None:
            raise FounderRefused("Andre approval token is not configured on this service (or equals the service or "
                                 "a caller token); approvals are impossible until it is set (fail closed)")
        if not supplied:
            raise FounderRefused("Andre approval token missing")
        if not self.token.matches(supplied):
            raise FounderRefused("Andre approval token invalid")
