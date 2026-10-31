"""
Token rotation and hashed founder tokens (red team Oct 30-31 2026, RTA-5; fix wave A). Shared byte-for-byte by
finance-py, legal-py and compliance-py. Runbook: docs/runbooks/token-rotation.md.

Rotation without an outage. During a rotation window a service accepts the current AND the next value of:

- its own bearer token:        ``<P>_SERVICE_TOKEN_NEXT``
- each caller token:           ``<P>_CALLER_TOKENS_NEXT`` (same JSON shape as ``<P>_CALLER_TOKENS``, only the
                               callers being rotated)
- Andre's approval token:      ``<P>_ANDRE_APPROVAL_TOKEN_NEXT_SHA256``

so the callers can be redeployed with the new value in any order; then the new value becomes the current one and
the ``_NEXT`` variable is removed.

Hashed founder token. ``<P>_ANDRE_APPROVAL_TOKEN_SHA256`` (the lowercase hex SHA-256 of the token, as the ledger's
LEDGER_CALLERS file holds its callers) may replace the plaintext ``<P>_ANDRE_APPROVAL_TOKEN``: the service then
holds no copy of Andre's token, only its hash, and compares SHA-256 digests in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

_HEX64 = re.compile(r"[0-9a-f]{64}")


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


class Secret:
    """The values one credential may take now (current, plus next during a rotation), held as SHA-256 digests."""

    def __init__(self, plaintexts: Iterable[Optional[str]] = (), sha256_hex: Iterable[Optional[str]] = ()):
        self.digests = [digest(t) for t in plaintexts if t] + [bytes.fromhex(h) for h in sha256_hex if h]

    def __bool__(self) -> bool:
        return bool(self.digests)

    def matches(self, supplied: Optional[str]) -> bool:
        if not supplied or not self.digests:
            return False
        try:
            d = digest(supplied)
        except (TypeError, AttributeError):
            return False
        ok = False
        for x in self.digests:          # no early exit
            ok |= hmac.compare_digest(d, x)
        return ok

    def overlaps(self, others: Iterable[Optional[str]]) -> bool:
        other = {digest(t) for t in others if t}
        return any(d in other for d in self.digests)


@dataclass(frozen=True)
class Rotation:
    service_next: Optional[str] = field(default=None, repr=False)
    callers_next: dict = field(default_factory=dict, repr=False)
    andre_sha256: tuple = field(default=(), repr=False)


def load(env: dict, prefix: str, caller_names: Iterable[str], check_token: Callable[[str, str], None],
         others: Iterable[Optional[str]], andre: Optional[str] = None) -> Rotation:
    """Reads and checks the rotation variables. ``others``: every current token of this service that is NOT Andre's
    (service, callers, a second approver); ``andre``: Andre's plaintext token when it is still set. No next value may
    equal a current one or another next value, and Andre's hash may not be the hash of any other token."""
    names = set(caller_names)
    nxt = env.get(f"{prefix}_SERVICE_TOKEN_NEXT") or None
    if nxt is not None:
        check_token(nxt, f"{prefix}_SERVICE_TOKEN_NEXT")
    raw = env.get(f"{prefix}_CALLER_TOKENS_NEXT")
    callers: dict = {}
    if raw:
        try:
            callers = json.loads(raw)
        except ValueError:
            callers = None
        if not isinstance(callers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                    for k, v in callers.items()):
            raise RuntimeError(f'{prefix}_CALLER_TOKENS_NEXT must be a JSON object {{"name": "token", ...}}')
        for k, tok in callers.items():
            if k not in names:
                raise RuntimeError(f"{prefix}_CALLER_TOKENS_NEXT: unknown caller name")
            check_token(tok, f"{prefix}_CALLER_TOKENS_NEXT: the {k} token")
    hashes = []
    for var in (f"{prefix}_ANDRE_APPROVAL_TOKEN_SHA256", f"{prefix}_ANDRE_APPROVAL_TOKEN_NEXT_SHA256"):
        h = (env.get(var) or "").strip()
        if h:
            if not _HEX64.fullmatch(h):
                raise RuntimeError(f"{var} must be the 64-character lowercase hex SHA-256 of the token "
                                   "(printf %s \"$TOKEN\" | sha256sum)")
            hashes.append(h)
    new = [t for t in [nxt, *callers.values()] if t]
    cur = [t for t in [*others, andre] if t]
    if len(set(new)) != len(new) or any(t in cur for t in new):
        raise RuntimeError(f"{prefix}: a *_NEXT token equals a current token or another next token; every token "
                           "must be distinct")
    if any(digest(t).hex() in hashes for t in [*others, *new] if t):
        raise RuntimeError(f"{prefix}_ANDRE_APPROVAL_TOKEN*_SHA256 is the hash of a service or caller token; Andre's "
                           "token must be distinct")
    return Rotation(nxt, dict(callers), tuple(hashes))
