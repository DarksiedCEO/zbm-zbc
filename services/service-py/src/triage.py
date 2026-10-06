"""
Intelligence I1 — triage (ADR 0014 decision 12). Deterministic, one job: classify one inbound message.

Categories, in precedence order (the primary category is the first that fires; every category that fires is routed):
  privacy    data-deletion / privacy requests (never answered by the bot; Compliance + Legal)
  security   password, hacked, breach, phishing ... (Cybersecurity 22)
  contract   contract, cancel the agreement, lawyer, sue, terms, DMCA / copyright ... (Andre + Legal 37)
  money      refund, charge, invoice, dispute, chargeback, price ... (Andre + Finance 31)
  complaint  complaint words, profanity, ALL CAPS, repeated contacts (Andre)
  routine    nothing above fired AND the message is short and simple: a candidate for an approved answer
  other      nothing above fired but the message is long or carries many questions: a human reads it

Fail toward a human: one keyword is enough to escalate (no negation handling: "not a refund" escalates), and
``routine`` is only a candidate — the bot answers only if an Andre-approved article also matches unambiguously
(kb.py). Every decision lists its signal codes (never the matched text), so it is explainable and the codes are
safe to record.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

PRECEDENCE = ("privacy", "security", "contract", "money", "complaint")
ROUTINE_MAX_CHARS = 800
ROUTINE_MAX_QUESTIONS = 2
CAPS_MIN_LETTERS = 12
CAPS_RATIO_PCT = 70
REPEAT_CONTACTS_24H = 3

# Each entry is a whole-word (or whole-phrase) match on the normalised text.
LEXICON = {
    "privacy": (
        "delete my data", "delete my information", "delete my personal", "delete my account", "erase my data",
        "erase my information", "erasure", "remove my data", "remove my information", "remove my personal",
        "right to be forgotten", "forget me", "data deletion", "deletion request", "gdpr", "ccpa", "cpra",
        "do not sell", "dont sell my", "don t sell my", "personal data", "personal information", "privacy request",
        "data request", "access request", "copy of my data", "what data do you have", "opt out of sale",
    ),
    "security": (
        "password", "passwords", "hacked", "hack", "hacker", "breach", "breached", "data breach", "phishing",
        "phish", "compromised", "unauthorized access", "unauthorised access", "someone logged in",
        "logged into my account", "2fa", "two factor", "two-factor", "login code", "verification code",
        "suspicious email", "suspicious login", "account takeover", "malware", "virus", "stolen account",
    ),
    "contract": (
        "contract", "contracts", "agreement", "agreements", "cancel", "cancellation", "cancelling", "canceling",
        "terminate", "termination", "terminating", "lawyer", "lawyers", "attorney", "attorneys", "legal",
        "sue", "suing", "sued", "lawsuit", "litigation", "court", "small claims", "terms", "terms of service",
        "breach of contract", "dmca", "copyright", "copyrighted", "infringement", "infringing", "takedown",
        "trademark", "cease and desist", "liability", "indemnify", "arbitration",
    ),
    "money": (
        "refund", "refunds", "refunded", "money back", "charge", "charged", "charges", "overcharged",
        "double charged", "invoice", "invoices", "invoiced", "billing", "billed", "bill", "dispute", "disputed",
        "chargeback", "charge back", "price", "prices", "pricing", "cost", "costs", "fee", "fees", "payment",
        "payments", "paid", "pay", "credit card", "card was", "receipt", "discount", "rate", "rates", "budget",
        "deposit", "owe", "owed", "money", "dollars", "payout", "payouts",
    ),
    "complaint": (
        "complaint", "complain", "complaining", "unacceptable", "terrible", "horrible", "awful", "worst",
        "angry", "furious", "upset", "disappointed", "disappointing", "ridiculous", "useless", "incompetent",
        "unprofessional", "scam", "scammed", "fraud", "rip off", "ripoff", "never again", "report you",
        "better business bureau", "bbb", "bad review", "one star", "1 star", "fed up", "sick of", "waste of",
        "still waiting", "no one answered", "nobody answered", "no response", "ignored", "ignoring me",
    ),
}

PROFANITY = ("fuck", "fucking", "fucked", "fucker", "shit", "shitty", "bullshit", "damn", "dammit", "goddamn",
             "bitch", "bastard", "asshole", "ass", "crap", "piss", "pissed", "dick", "wtf", "stfu", "motherfucker")
# masked profanity: f**k, s***, f#@k, sh!t ...
MASKED = re.compile(r"(?<![a-z])(f[\*#@!$%]{1,3}k|f[\*#@!$%]{3,}|s[\*#@!$%]{2,}t?|sh[\*#@!$%]t|b[\*#@!$%]{3}h)(?![a-z])")
LEET = str.maketrans({"@": "a", "4": "a", "3": "e", "1": "i", "!": "i", "0": "o", "$": "s", "5": "s", "7": "t"})


def normalise(text: str) -> str:
    """Lower case, accents folded, apostrophes dropped, every other non-alphanumeric a space, spaces collapsed."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("'", "").replace("’", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return f" {' '.join(t.split())} "


def _has(norm: str, term: str) -> bool:
    return f" {normalise(term).strip()} " in norm


@dataclass(frozen=True)
class Triage:
    primary: str                            # one of PRECEDENCE, "routine" or "other"
    categories: tuple                       # every escalation category that fired, in precedence order
    signals: tuple                          # codes, e.g. "money:refund", "complaint:caps", "complaint:repeat"
    routine_candidate: bool                 # True only when primary == "routine"
    question_count: int = 0
    extra: dict = field(default_factory=dict)


def caps_shouting(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < CAPS_MIN_LETTERS:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper * 100 >= CAPS_RATIO_PCT * len(letters)


def classify(text: str, recent_inbound_24h: int = 0, reopened: int = 0) -> Triage:
    """``recent_inbound_24h``: the contact's inbound messages in the previous 24 hours (this one excluded);
    ``reopened``: how often this ticket was reopened. Both feed the complaint signal (repeated contacts)."""
    norm = normalise(text)
    signals: list[str] = []
    fired: list[str] = []
    for cat in PRECEDENCE:
        hits = [t for t in LEXICON[cat] if _has(norm, t)]
        if hits:
            fired.append(cat)
            signals += [f"{cat}:{normalise(h).strip().replace(' ', '_')}" for h in hits[:5]]
    complaint = []
    deleet = normalise(text.lower().translate(LEET))
    if any(_has(norm, w) or _has(deleet, w) for w in PROFANITY) or MASKED.search(text.lower()):
        complaint.append("complaint:profanity")
    if caps_shouting(text):
        complaint.append("complaint:caps")
    if "!!!" in text:
        complaint.append("complaint:exclamations")
    if recent_inbound_24h + 1 >= REPEAT_CONTACTS_24H:
        complaint.append("complaint:repeat_contacts")
    if reopened >= 2:
        complaint.append("complaint:reopened")
    if complaint:
        signals += complaint
        if "complaint" not in fired:
            fired.append("complaint")
    fired = [c for c in PRECEDENCE if c in fired]
    questions = text.count("?")
    if fired:
        return Triage(fired[0], tuple(fired), tuple(signals), False, questions)
    stripped = text.strip()
    if not stripped or len(stripped) > ROUTINE_MAX_CHARS or questions > ROUTINE_MAX_QUESTIONS:
        reason = "other:empty" if not stripped else ("other:long" if len(stripped) > ROUTINE_MAX_CHARS
                                                     else "other:many_questions")
        return Triage("other", (), (reason,), False, questions)
    return Triage("routine", (), ("routine:candidate",), True, questions)


# Where each category goes (ADR 0014 decision 13). "andre" is the human queue Andre works.
ROUTES = {
    "privacy": ("compliance_38", "legal_37", "andre"),
    "security": ("cybersecurity_22", "andre"),
    "contract": ("legal_37", "andre"),
    "money": ("finance_31", "andre"),
    "complaint": ("andre",),
    "other": ("andre",),
    "no_answer": ("andre",),
}
PRIORITY = {"security": "p1", "privacy": "p2", "contract": "p2", "money": "p2", "complaint": "p2", "other": "p3",
            "routine": "p3", "no_answer": "p3"}


def legal_kind(t: Triage) -> str:
    """legal-py's intake kind for this message (only used when Legal is a route)."""
    s = set(t.signals)
    if "privacy" in t.categories:
        return "privacy_request"
    if any(x in s for x in ("contract:sue", "contract:suing", "contract:sued", "contract:lawsuit", "contract:lawyer",
                            "contract:lawyers", "contract:attorney", "contract:attorneys", "contract:litigation",
                            "contract:court", "contract:small_claims", "contract:cease_and_desist")):
        return "litigation_threat"
    if any(x.startswith(("contract:dmca", "contract:copyright", "contract:infring", "contract:takedown",
                         "contract:trademark")) for x in s):
        return "ip_claim"
    return "contract_dispute"


def routes_for(t: Triage) -> tuple:
    out: list[str] = []
    for cat in (t.categories or (t.primary,)):
        for r in ROUTES.get(cat, ("andre",)):
            if r not in out:
                out.append(r)
    return tuple(out)
