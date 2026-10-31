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
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

_HEX64 = re.compile(r"[0-9a-f]{64}")


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


class Secret:
    """The values one credential may take now, held as SHA-256 digests: the current ones always, the ``next`` ones
    only until ``next_until`` (epoch seconds; AEGIS wave A F-11: a rotation window has an explicit end)."""

    def __init__(self, plaintexts: Iterable[Optional[str]] = (), sha256_hex: Iterable[Optional[str]] = (),
                 next_plaintexts: Iterable[Optional[str]] = (), next_sha256_hex: Iterable[Optional[str]] = (),
                 next_until: Optional[float] = None, clock: Callable[[], float] = time.time):
        self.digests = [digest(t) for t in plaintexts if t] + [bytes.fromhex(h) for h in sha256_hex if h]
        self.next_digests = [digest(t) for t in next_plaintexts if t] + [bytes.fromhex(h) for h in next_sha256_hex if h]
        self.next_until = next_until
        self.clock = clock

    def __bool__(self) -> bool:
        return bool(self.digests or self.next_digests)

    def _accepted(self) -> list:
        nxt = self.next_digests if (self.next_until is not None and self.clock() < self.next_until) else []
        return self.digests + nxt

    def matches(self, supplied: Optional[str]) -> bool:
        accepted = self._accepted()
        if not supplied or not accepted:
            return False
        try:
            d = digest(supplied)
        except (TypeError, AttributeError):
            return False
        ok = False
        for x in accepted:              # no early exit
            ok |= hmac.compare_digest(d, x)
        return ok

    def overlaps(self, others: Iterable[Optional[str]]) -> bool:
        other = {digest(t) for t in others if t}
        return any(d in other for d in self.digests + self.next_digests)


MAX_WINDOW_S = 14 * 86400        # a rotation window ends at most 14 days after the service starts


@dataclass(frozen=True)
class Rotation:
    service_next: Optional[str] = field(default=None, repr=False)
    callers_next: dict = field(default_factory=dict, repr=False)
    andre_sha256: tuple = field(default=(), repr=False)          # the current hash (when set)
    andre_next_sha256: tuple = field(default=(), repr=False)     # the next hash, during a window
    ends_at: Optional[float] = None                              # epoch seconds; None = no window open

    @property
    def in_progress(self) -> bool:
        return bool(self.service_next or self.callers_next or self.andre_next_sha256)


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
    cur_hash: tuple = ()
    next_hash: tuple = ()
    for var in (f"{prefix}_ANDRE_APPROVAL_TOKEN_SHA256", f"{prefix}_ANDRE_APPROVAL_TOKEN_NEXT_SHA256"):
        h = (env.get(var) or "").strip()
        if h:
            if not _HEX64.fullmatch(h):
                raise RuntimeError(f"{var} must be the 64-character lowercase hex SHA-256 of the token "
                                   "(printf %s \"$TOKEN\" | sha256sum)")
            hashes.append(h)
            if var.endswith("_NEXT_SHA256"):
                next_hash = (h,)
            else:
                cur_hash = (h,)
    # AEGIS wave A F-11: once Andre's token is held as a hash, a different plaintext may not keep working beside it
    if andre and cur_hash and digest(andre).hex() != cur_hash[0]:
        raise RuntimeError(f"{prefix}_ANDRE_APPROVAL_TOKEN and {prefix}_ANDRE_APPROVAL_TOKEN_SHA256 name different "
                           f"tokens; delete {prefix}_ANDRE_APPROVAL_TOKEN (the hash replaces it)")
    if next_hash and not (cur_hash or andre):
        raise RuntimeError(f"{prefix}_ANDRE_APPROVAL_TOKEN_NEXT_SHA256 needs a current Andre token or hash")
    new = [t for t in [nxt, *callers.values()] if t]
    cur = [t for t in [*others, andre] if t]
    if len(set(new)) != len(new) or any(t in cur for t in new):
        raise RuntimeError(f"{prefix}: a *_NEXT token equals a current token or another next token; every token "
                           "must be distinct")
    if any(digest(t).hex() in hashes for t in [*others, *new] if t):
        raise RuntimeError(f"{prefix}_ANDRE_APPROVAL_TOKEN*_SHA256 is the hash of a service or caller token; Andre's "
                           "token must be distinct")
    ends_at = None
    if nxt or callers or next_hash:
        raw_end = (env.get(f"{prefix}_ROTATION_ENDS_AT") or "").strip()
        try:
            from datetime import datetime
            ends_at = datetime.fromisoformat(raw_end.replace("Z", "+00:00")).timestamp() if raw_end else None
            if raw_end and datetime.fromisoformat(raw_end.replace("Z", "+00:00")).tzinfo is None:
                ends_at = None
        except ValueError:
            ends_at = None
        if ends_at is None:
            raise RuntimeError(f"a *_NEXT token is set: {prefix}_ROTATION_ENDS_AT must say when the window ends "
                               "(ISO 8601 with a time zone, e.g. 2026-11-07T00:00:00Z); the next values are refused "
                               "after it (docs/runbooks/token-rotation.md)")
        if not (time.time() < ends_at <= time.time() + MAX_WINDOW_S):
            raise RuntimeError(f"{prefix}_ROTATION_ENDS_AT must be in the future and at most 14 days ahead")
    return Rotation(nxt, dict(callers), cur_hash, next_hash, ends_at)
