"""
Andre's approval token (Finance spec §C.4 checker, §C.9 SoD) — the creative-py / compliance-py / verification-py
``FounderGate`` pattern, copied.

``FIN_ANDRE_APPROVAL_TOKEN`` is checked exactly like creative-py/src/shared/founder.py, plus the rule V&I added: a
token equal to the service token OR to ANY caller token (or to the second approver's token) is treated as NOT
configured (otherwise that caller would be Andre). Not configured -> every approval refused; missing or wrong ->
refused; non-ASCII -> refused (compare_digest TypeError), never a 500.

The optional second human approver (spec R2, ``FIN_SECOND_APPROVER_TOKEN``) uses the same gate class; it is empty
by default (Andre only — compensating dual control, ADR 0009 note).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import token_rotation
from errors import FounderRefused

FOUNDER = "andre"


@dataclass(frozen=True)
class FounderGate:
    # fix wave A (RTA-5): the accepted values as SHA-256 digests (token_rotation.Secret), never the plaintext; built
    # from the plaintext token and/or FIN_ANDRE_APPROVAL_TOKEN_SHA256 / _NEXT_SHA256 (a rotation window)
    token: Optional[token_rotation.Secret]
    who: str = FOUNDER

    @classmethod
    def build(cls, founder_token: Optional[str], service_token: Optional[str],
              other_tokens: Iterable[str] = (), who: str = FOUNDER,
              founder_sha256: Iterable[str] = (), founder_next_sha256: Iterable[str] = (),
              next_until: Optional[float] = None) -> "FounderGate":
        secret = token_rotation.Secret([founder_token], founder_sha256, next_sha256_hex=founder_next_sha256,
                                       next_until=next_until)
        if not secret or secret.overlaps([service_token, *other_tokens]):
            return cls(None, who)
        return cls(secret, who)

    @property
    def configured(self) -> bool:
        return self.token is not None

    def matches(self, supplied: Optional[str]) -> bool:
        if self.token is None or not supplied:
            return False
        return self.token.matches(supplied)

    def verify(self, supplied: Optional[str]) -> None:
        if self.token is None:
            raise FounderRefused(f"{self.who} approval token is not configured on this service (or equals the service "
                                 "or a caller token); approvals are impossible until it is set (fail closed)")
        if not supplied:
            raise FounderRefused(f"{self.who} approval token missing")
        if not self.matches(supplied):
            raise FounderRefused(f"{self.who} approval token invalid")
