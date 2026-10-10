"""
Intelligence 7 — Duplicate Identity (spec §C.7, VI-12, VI-13): findings + evidence; an automatic hold only on
EXACT matches, on the NEWER identity; the decision is human. Never bans anyone, never decides soft matches.

Inputs are used raw in memory only and stored as HMAC-SHA256 with the identity key from the vault port
(``ports.TokenVault.identity_hmac_key``; no environment variable carries it — wave 25: this named
VI_IDENTITY_HMAC_KEY, which nothing reads): normalized email (NFKC, lowercase; no dot/plus folding — spec choice), the payout
identity HMAC from Finance 31 (stand-in → the check is ``incomplete``), the canonical mailbox (``mailbox_base``, the
minor lock only: bug sweep C), platform account ids from
connections. Device fingerprint and IP /24 are OFF (VI_DEVICE_SIGNALS_ENABLED=1 refuses to start: not built).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Optional

import lookalikes

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


# AEGIS L-3: lookalike letters folded to the Latin letter they imitate (the near-identical Cyrillic / Greek entries
# of Unicode confusables.txt; creative-py's shared table, the subset that can appear in an address), diacritics dropped
_LOOKALIKE = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "γ": "y", "ɡ": "g", "ı": "i", "ɑ": "a"})


def _fold_lookalikes(text: str) -> str:
    """The fold ``mailbox_base_v0`` used (frozen: see ``mailbox_base_v0``)."""
    t = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", t).translate(_LOOKALIKE)


# War room WR-F005: the minor lock's fold is the shared lookalike fold (src/lookalikes.py: invisible characters out,
# NFKC, the final sigma before casefolding, casefold, this service's table over the repo's shared hand table and the
# Unicode confusables.txt skeleton). _LOOKALIKE stays the top layer, so every address the old fold already folded
# keeps its canonical mailbox (and its stored email_base HMAC).
_TABLE = lookalikes.Table({chr(k): v for k, v in _LOOKALIKE.items()})


def _fold_address(email: str) -> str:
    """The old order (NFKC, casefold, diacritics, table) with invisible characters removed first and the final
    sigma read before casefolding."""
    t = _TABLE.casefold(unicodedata.normalize("NFKC", lookalikes.strip_invisible(email)).strip())
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    return _TABLE.map(unicodedata.normalize("NFC", t))


# Providers that ignore dots in the local part and treat googlemail.com as gmail.com (their own documentation).
_DOT_BLIND = {"gmail.com": "gmail.com", "googlemail.com": "gmail.com"}


def mailbox_base(email: str) -> str:
    """The mailbox an address delivers to, for the MINOR LOCK only (bug sweep C): the address with invisible
    characters removed and lookalikes folded (WR-F005: ``kiԁ.ηame@``, and ``kid.name@`` with a soft hyphen or a
    zero-width space in it, are ``kid.name@``), the ``+tag`` (and everything after it) dropped from the local part, and, for Gmail, the dots dropped and
    googlemail.com read as gmail.com. ``kid+1@`` and ``k.i.d@gmail`` are then the same identity as ``kid@`` for the
    under-18 lock. Duplicate-identity FINDINGS keep the exact address (spec §C.7 choice: a plus-tag is a different
    mailbox at some providers), so this never opens a finding on its own; it only makes the minor lock harder to
    step around."""
    e = _fold_address(email)
    local, at, domain = e.rpartition("@")
    return _base(f"{local}{at}{domain.rstrip('.')}" if at else e)


def mailbox_base_v0(email: str) -> str:
    """FROZEN: ``mailbox_base`` as it was before the war room fix (WR-F005). V&I stores only HMACs, so an
    ``email_base`` recorded before the fix cannot be recomputed; an identity check also records the HMAC of this
    value (kind ``email_base_v0``, HMAC'd as ``email_base``) when it differs from the new one, so an older minor
    record still matches every variant it matched before. Never change this function."""
    return _base(_fold_lookalikes(normalize_email(email)))


def _base(e: str) -> str:
    local, at, domain = e.rpartition("@")
    if not at:
        return e
    # AEGIS L-3: "+" and "-" both introduce a sub-address at major providers (Gmail/Outlook "+", Yahoo/Fastmail "-")
    local = re.split(r"[+-]", local, maxsplit=1)[0]
    if domain in _DOT_BLIND:
        domain = _DOT_BLIND[domain]
        local = local.replace(".", "")
    return f"{local}@{domain}"
