"""
Intelligence 7 — Duplicate Identity (spec §C.7, VI-12, VI-13): findings + evidence; an automatic hold only on
EXACT matches, on the NEWER identity; the decision is human. Never bans anyone, never decides soft matches.

Inputs are used raw in memory only and stored as HMAC-SHA256 with the identity key from the vault
(VI_IDENTITY_HMAC_KEY): normalized email (NFKC, lowercase; no dot/plus folding — spec choice), the payout
identity HMAC from Finance 31 (stand-in → the check is ``incomplete``), platform account ids from
connections. Device fingerprint and IP /24 are OFF (VI_DEVICE_SIGNALS_ENABLED=1 refuses to start: not built).
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from typing import Optional

NUMBER, NAME, ACTOR = 7, "Duplicate Identity", "intel_07_duplicate_identity"


def normalize_email(email: str) -> str:
    """NFKC, case-folded, trailing dots of the domain removed (``example.com.`` is ``example.com``, AEGIS N16-12).
    Spec §C.7 choice kept: no dot or plus-tag folding of the local part (``a.b+x@`` stays distinct from ``ab@``) —
    those are different mailboxes at many providers; a same-person match on them is left to the payout identity."""
    e = unicodedata.normalize("NFKC", email).strip().casefold()
    local, at, domain = e.rpartition("@")
    return f"{local}{at}{domain.rstrip('.')}" if at else e


def hmac_hex(key: bytes, kind: str, value: str) -> str:
    return hmac.new(key, f"{kind}\x00{value}".encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def matches(values: dict[str, Optional[str]], owners: dict[tuple[str, str], str], clipper_id: str) -> list[tuple]:
    """``values``: kind -> hmac for this clipper; ``owners``: (kind, hmac) -> first clipper id holding it.
    Returns [(kind, hmac, other_clipper_id)] for exact matches with ANOTHER clipper."""
    out = []
    for kind, h in sorted(values.items()):
        if h is None:
            continue
        other = owners.get((kind, h))
        if other is not None and other != clipper_id:
            out.append((kind, h, other))
    return out
