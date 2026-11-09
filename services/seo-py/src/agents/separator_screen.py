"""
A second personal-data screen for a stored excerpt (ADR 0049, review round 10). The base screen
(``agents.prompt_guard.personal_data``, used by ``agents.callum.excerpt``; not changed here) reads digits separated by
whitespace and ``( ) . + / , _ -`` (and dashes). A phone, card or SSN written with any other separator (``•`` ``·``
``∙`` ``・`` ``~`` ``|`` ``:`` ``*`` ``#`` …) slipped past it.

Rule: every character whose Unicode general category is punctuation (P*), symbol (S*) or separator (Z*) — derived
from ``unicodedata``, not a list — and which the base screen does not already read as a separator, is read as a
space; so is ``x`` / ``X`` between two digits. Then the BASE screen runs again on that reading, so the base's own
allowances (round counts, year ranges, versions) apply unchanged, and its 3-character separator bound with them.
The excerpt is withheld when either reading is personal data. Linear in the excerpt (which is bounded).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from agents.prompt_guard import personal_data

BASE_SEPARATORS = frozenset("().+/,_-")
_X_BETWEEN = re.compile(r"(?<=\d)[xX](?=\d)")


def reading(text: str) -> str:
    """The text with every P*/S*/Z* character the base does not read as a separator read as a space."""
    out = []
    for c in text or "":
        cat = unicodedata.category(c)
        if c not in BASE_SEPARATORS and not c.isspace() and cat[0] in "PSZ" and cat not in ("Pd", "Pc"):
            out.append(" ")
        else:
            out.append(c)
    return _X_BETWEEN.sub(" ", "".join(out))


def personal(text: str) -> Optional[str]:
    """The base screen's kind on the text as written, else on the separator reading, else None."""
    return personal_data(text) or personal_data(reading(text))


def screen(ex: dict) -> dict:
    """Withhold a stored excerpt (``callum.excerpt`` shape) whose text the separator reading finds personal."""
    body = (ex.get("excerpt") or {}).get("text") if isinstance(ex.get("excerpt"), dict) else None
    if body is None:
        return ex
    kind = personal(body)
    if kind is None:
        return ex
    return {**{k: v for k, v in ex.items() if k != "secret_redacted"}, "excerpt": None,
            "withheld": f"personal_data:{kind}"}
