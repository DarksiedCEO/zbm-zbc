"""Gift, lobbying, conflict-of-interest and contingent-fee wording (ADR 0016 decision 12).

Decides: which sensitivity flags a response's CUSTOM text and its pursuit's notes raise. The text is normalised
(NFKC, accents dropped, case folded, punctuation between letters removed) before deterministic word rules run. A
flagged response can be approved only when Andre's approval names exactly the set of flags raised (no more, no less),
so he sees every one. False positives are expected and cost one acknowledgement; a missed flag is the failure this
guards against, so the rules are broad. Never: rewrites or removes text."""

from __future__ import annotations

import re
import unicodedata

NUMBER = 6
NAME = "sensitivity_flags"
DECIDES = "GIFT / LOBBYING / CONFLICT_OF_INTEREST / CONTINGENT_FEE flags on custom text"

def _rx(pattern: str) -> re.Pattern:
    """A space in a rule also matches no space: the normaliser joins ``success-fee`` into ``successfee``."""
    return re.compile(pattern.replace(" ", " ?"))


RULES = (
    ("GIFT", _rx(r"\b(gifts?|gratuit(y|ies)|meals?|dinners?|lunch(es)?|entertainment|tickets?|honorari(um|a)|"
                        r"donations?|travel (paid|covered)|complimentary|perks?|hospitality|giveaways?|swag|"
                        r"gift ?cards?|free (trip|stay|event))\b")),
    ("LOBBYING", _rx(r"\b(lobby\w*|campaign contributions?|political contributions?|pac|elected officials?|"
                            r"council ?members?|commissioners?|influence|advocacy|government relations|public affairs|"
                            r"meet(ing)? with (the )?(mayor|senator|supervisor|official))\b")),
    ("CONFLICT_OF_INTEREST", _rx(r"\b(conflicts? of interest|coi|former (government |agency |county |city |"
                                        r"state )?(employees?|officials?|staff)|revolving door|relatives?|family "
                                        r"members?|spouses?|brother|sister|cousin|related part(y|ies)|ownership "
                                        r"interest|financial interest|personal relationship|friends? (of|with))\b")),
    ("CONTINGENT_FEE", _rx(r"\b(contingen\w* fees?|success fees?|finders? fees?|kickbacks?|inducements?|"
                                  r"commissions? (on|for) (award|winning|the contract)|referral fees?|rebates?)\b")),
)


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).casefold()
    t = re.sub(r"(?<=[^\W\d_])[^\w\s]+(?=[^\W\d_])", "", t)
    return " ".join(re.sub(r"[^\w\s]+", " ", t).split())


def flags(*texts: str) -> list[str]:
    t = " \n ".join(normalise(x) for x in texts if x)
    return sorted(code for code, rx in RULES if rx.search(t))
