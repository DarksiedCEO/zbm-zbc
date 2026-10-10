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

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import lookalikes

PRECEDENCE = ("privacy", "security", "contract", "money", "complaint")
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
        "my information", "my details", "wipe my", "purge my",
        # Spanish
        "borrar mis datos", "eliminar mis datos", "borren mis datos", "eliminen mis datos", "mis datos personales",
        "datos personales", "privacidad", "eliminar mi cuenta", "borrar mi cuenta",
    ),
    "security": (
        "password", "passwords", "hacked", "hack", "hacker", "breach", "breached", "data breach", "phishing",
        "phish", "compromised", "unauthorized access", "unauthorised access", "someone logged in",
        "logged into my account", "2fa", "two factor", "two-factor", "login code", "verification code",
        "suspicious email", "suspicious login", "account takeover", "malware", "virus", "stolen account",
        "got into my account", "into my account", "accessed my account", "access to my account", "someone else",
        "someone logged", "locked out", "not me", "wasnt me", "was not me", "identity theft", "impersonat",
        "fake email", "fake text", "credentials", "login",
        # Spanish
        "contrasena", "hackeado", "hackearon", "hackeo", "me robaron", "robaron mi cuenta", "suplantacion",
        "acceso a mi cuenta", "entraron a mi cuenta",
    ),
    "contract": (
        "contract", "contracts", "agreement", "agreements", "cancel", "cancellation", "cancelling", "canceling",
        "terminate", "termination", "terminating", "lawyer", "lawyers", "attorney", "attorneys", "legal",
        "sue", "suing", "sued", "lawsuit", "litigation", "court", "small claims", "terms", "terms of service",
        "breach of contract", "dmca", "copyright", "copyrighted", "infringement", "infringing", "takedown",
        "trademark", "cease and desist", "liability", "indemnify", "arbitration",
        "end my service", "end the service", "end our service", "stop paying", "stop service", "stop the service",
        "quit", "quitting", "leave your", "leaving your", "close my account", "solicitor", "solicitors",
        "small claims", "claims court", "legal action", "my rights", "breach", "notice", "lien", "subpoena",
        # Spanish
        "contrato", "contratos", "cancelar", "cancelacion", "abogado", "abogada", "abogados", "demanda",
        "demandar", "demandare", "demandaremos", "tribunal", "juicio", "terminos", "acuerdo", "rescindir",
        "derechos de autor", "accion legal",
    ),
    "money": (
        "refund", "refunds", "refunded", "money back", "charge", "charged", "charges", "overcharged",
        "double charged", "invoice", "invoices", "invoiced", "billing", "billed", "bill", "dispute", "disputed",
        "chargeback", "charge back", "price", "prices", "pricing", "cost", "costs", "fee", "fees", "payment",
        "payments", "paid", "pay", "credit card", "card was", "receipt", "discount", "rate", "rates", "budget",
        "deposit", "owe", "owed", "money", "dollars", "payout", "payouts",
        "reimburse", "reimbursed", "reimbursement", "compensate", "compensation", "credit back", "credit me",
        "return my", "give back", "pay back", "reverse this", "reverse the", "reversal", "my bank", "the bank",
        "paypal", "stripe", "venmo", "zelle", "cash", "overdraft", "subscription", "renewal", "renew", "plan cost",
        "quote", "estimate", "how much", "usd",
        # Spanish
        "reembolso", "reembolsar", "devolucion", "devolver", "devuelvan", "mi dinero", "dinero", "cobro",
        "cobraron", "cobrar", "cargo", "cargos", "factura", "facturas", "precio", "precios", "pago", "pagos",
        "pague", "tarjeta", "cuanto cuesta", "costo", "banco",
    ),
    "complaint": (
        "complaint", "complain", "complaining", "unacceptable", "terrible", "horrible", "awful", "worst",
        "angry", "furious", "upset", "disappointed", "disappointing", "ridiculous", "useless", "incompetent",
        "unprofessional", "scam", "scammed", "fraud", "rip off", "ripoff", "never again", "report you",
        "better business bureau", "bbb", "bad review", "one star", "1 star", "fed up", "sick of", "waste of",
        "still waiting", "no one answered", "nobody answered", "no response", "ignored", "ignoring me",
        "garbage", "pathetic", "trash", "sucks", "suck", "joke", "worthless", "lousy", "rude", "liar", "liars",
        "lied", "lying", "dishonest", "shameful", "disgrace", "disgusting", "frustrated", "frustrating", "annoyed",
        "mad", "hate", "unhappy", "not happy", "dissatisfied", "poor service", "bad service", "scammers",
        "cheated", "misled", "never received", "did not receive", "didnt receive", "doesnt work", "not working",
        "broken", "wrong",
        # Spanish
        "queja", "quejas", "reclamo", "reclamacion", "pesimo", "pesima", "horrible", "terrible", "estafa",
        "estafadores", "basura", "molesto", "molesta", "enojado", "enojada", "mal servicio", "fraude",
        "no funciona", "decepcionado",
    ),
}

PROFANITY = ("fuck", "fucking", "fucked", "fucker", "shit", "shitty", "bullshit", "damn", "dammit", "goddamn",
             "bitch", "bastard", "asshole", "ass", "crap", "piss", "pissed", "dick", "wtf", "stfu", "motherfucker",
             "mierda", "carajo", "pendejo", "joder")
# masked profanity: f**k, s***, f#@k, sh!t ...
MASKED = re.compile(r"(?<![a-z])(f[\*#@!$%]{1,3}k|f[\*#@!$%]{3,}|s[\*#@!$%]{2,}t?|sh[\*#@!$%]t|b[\*#@!$%]{3}h)(?![a-z])")
LEET = str.maketrans({"@": "a", "4": "a", "3": "e", "1": "i", "!": "i", "0": "o", "$": "s", "5": "s", "7": "t"})

# AEGIS round 1 (V1-H1): Cyrillic / Greek letters that look Latin, mapped before matching ("rеfund" with a Cyrillic е)
CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "У": "Y", "І": "I", "Ј": "J", "Ѕ": "S",
    "α": "a", "ο": "o", "ρ": "p", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "τ": "t", "υ": "u", "χ": "x", "γ": "y",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X",
})
_TAG = re.compile(r"<[^<>]{0,200}>")

# Misspellings: a token of 5+ letters within one edit (insert, delete, substitute, swap) of one of these fires its
# category ("refnd", "chargebak", "lawyr"). Stems chosen far from everyday words (no "contract": "contact").
FUZZY = {
    "money": ("refund", "refunds", "chargeback", "invoice", "reimburse", "reimbursement", "overcharged"),
    "contract": ("lawyer", "attorney", "lawsuit", "solicitor", "copyright", "terminate", "cancellation",
                 "cancelled", "canceled"),
    "security": ("password", "phishing"),
    "complaint": ("complaint", "ridiculous", "pathetic", "garbage", "horrible", "terrible", "unacceptable"),
}
GREETING = re.compile(r"^\s*(hi|hello|hey|hola|buenas|good (morning|afternoon|evening))\b[\s,.!]*", re.I)
CLAUSE_JOINERS = ("and", "also", "but", "plus", "then", "however", "additionally", "otherwise", "or", "because",
                  "y", "pero", "tambien", "ademas", "o")
ROUTINE_MAX_WORDS = 25


def _strip_format(t: str) -> str:
    return "".join(c for c in t if unicodedata.category(c) != "Cf" and c != "\u00ad")


# War room WR-F001: the opt-out path folds every lookalike the repo's shared table and the Unicode confusables.txt
# skeleton list (src/lookalikes.py; this module's CONFUSABLES on top, so nothing it folded changes). Triage's own
# reading (categories, non_ascii_letters) keeps CONFUSABLES alone: a message written in another script must still
# reach a human as one, not be read as Latin gibberish.
_OPT_OUT_FOLD = lookalikes.Table({chr(k): v for k, v in CONFUSABLES.items()})


def clean(text: str, opt_out: bool = False) -> str:
    """V1-H1 order: format characters (zero-width, soft hyphen, bidi) out; HTML entities unescaped (repeatedly, a
    double-escaped entity too); tags stripped; NFKC (full-width letters); confusables mapped to Latin
    (``opt_out``: with the shared lookalike fold, WR-F001)."""
    t = _strip_format(text)
    for _ in range(3):
        u = html.unescape(t)
        if u == t:
            break
        t = u
    t = _strip_format(_TAG.sub("", t))
    t = _strip_format(unicodedata.normalize("NFKC", t))
    return _OPT_OUT_FOLD.fold(t) if opt_out else t.translate(CONFUSABLES)


def _fold(t: str) -> str:
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def non_ascii_letters(text: str) -> bool:
    """A letter outside ASCII survives cleaning and accent folding (a script we have no lexicon for): a human reads
    it."""
    return any(c.isalpha() and ord(c) > 127 for c in _fold(clean(text)))


def normalise(text: str, opt_out: bool = False) -> str:
    """clean(), accents folded, lower case, apostrophes dropped, every other non-alphanumeric a space, runs of three
    or more single letters joined ("S T O P" -> "stop"), spaces collapsed. ``opt_out``: ``clean`` with the shared
    lookalike fold (the opt-out rules in channels.py read text this way: WR-F001)."""
    t = _fold(clean(text, opt_out)).replace("'", "").replace("\u2019", "")
    tokens = re.sub(r"[^a-z0-9]+", " ", t).split()
    out: list[str] = []
    run: list[str] = []
    for tok in tokens + [""]:
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
            continue
        out += ["".join(run)] if len(run) >= 3 else run
        run = []
        if tok:
            out.append(tok)
    return f" {' '.join(out)} "


def normalise_opt_out(text: str) -> str:
    """``normalise`` with the shared lookalike fold: the reading every opt-out rule uses (WR-F001)."""
    return normalise(text, opt_out=True)


def _has(norm: str, term: str) -> bool:
    t = normalise(term).strip()
    return bool(t) and f" {t} " in norm


def _one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]
                                  and a[diff[1]] == b[diff[0]])
    if la > lb:
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _fuzzy_hits(norm: str, cat: str) -> list[str]:
    hits = []
    for tok in norm.split():
        if len(tok) < 5:
            continue
        for stem in FUZZY.get(cat, ()):
            if _one_edit(tok, stem):
                hits.append(stem)
    return hits


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


def _scan(text: str) -> tuple[list[str], list[str]]:
    """(categories fired, signals) for one text."""
    norm = normalise(text)
    signals: list[str] = []
    fired: list[str] = []
    stems = stem_hits(norm)
    for cat in PRECEDENCE:
        hits = [t for t in LEXICON[cat] if _has(norm, t)]
        hits += [f"~{s}" for s in _fuzzy_hits(norm, cat) if s not in hits]
        hits += [f"*{st}" for c, st in stems if c == cat]
        if hits:
            fired.append(cat)
            signals += [f"{cat}:" + ("near_" + h[1:] if h[0] == "~" else "stem_" + h[1:].replace(" ", "_")
                                     if h[0] == "*" else normalise(h).strip().replace(" ", "_")) for h in hits[:5]]
    cleaned = clean(text)
    if re.search(r"[$\u00a2-\u00a5\u20a0-\u20cf]", cleaned):           # a currency sign: money
        if "money" not in fired:
            fired.append("money")
        signals.append("money:currency_sign")
    complaint = []
    deleet = normalise(cleaned.lower().translate(LEET))
    if any(_has(norm, w) or _has(deleet, w) for w in PROFANITY) or MASKED.search(cleaned.lower()):
        complaint.append("complaint:profanity")
    if caps_shouting(cleaned):
        complaint.append("complaint:caps")
    if "!!!" in cleaned:
        complaint.append("complaint:exclamations")
    if complaint:
        signals += complaint
        if "complaint" not in fired:
            fired.append("complaint")
    return fired, signals


def single_intent(text: str) -> Optional[str]:
    """None when the text is one short sentence with one intent; else the reason code (V1-H1: anything with a
    second clause goes to a human)."""
    t = GREETING.sub("", clean(text), count=1).strip()
    words = normalise(t).split()
    if not words:
        return "other:empty"
    if len(words) > ROUTINE_MAX_WORDS:
        return "other:long"
    sentences = [x for x in re.split(r"[.!?;:\n\u3002\uff1f\uff01]+", t) if normalise(x).strip()]
    if len(sentences) > 1 or t.count("?") > 1:
        return "other:many_sentences"
    if "," in t or any(w in CLAUSE_JOINERS for w in words):
        return "other:second_clause"
    return None


def classify(text: str, recent_inbound_24h: int = 0, reopened: int = 0, subject: Optional[str] = None) -> Triage:
    """``recent_inbound_24h``: the contact's inbound messages in the previous 24 hours (this one excluded);
    ``reopened``: how often this ticket was reopened. Both feed the complaint signal (repeated contacts).
    ``subject`` (email): scanned by the same rules as the body (V1-H2); a routine answer needs both to be clean."""
    fired, signals = _scan(text)
    if subject:
        sf, ss = _scan(subject)
        fired += [c for c in sf if c not in fired]
        signals += [f"subject:{x}" for x in ss]
    if recent_inbound_24h + 1 >= REPEAT_CONTACTS_24H:
        signals.append("complaint:repeat_contacts")
        if "complaint" not in fired:
            fired.append("complaint")
    if reopened >= 2:
        signals.append("complaint:reopened")
        if "complaint" not in fired:
            fired.append("complaint")
    fired = [c for c in PRECEDENCE if c in fired]
    questions = text.count("?")
    if fired:
        return Triage(fired[0], tuple(fired), tuple(signals), False, questions)
    reasons = []
    if non_ascii_letters(text) or (subject and non_ascii_letters(subject)):
        reasons.append("other:non_ascii")
    r = single_intent(text)
    if r:
        reasons.append(r)
    if subject:
        rs = single_intent(subject)
        if rs and rs != "other:empty":
            reasons.append(f"subject:{rs}")
    if reasons:
        return Triage("other", (), tuple(reasons), False, questions)
    return Triage("routine", (), ("routine:candidate",), True, questions)


# ------------------------------------------------------------------------------------------------ word stems
# AEGIS round 3 (V3-M1): inflections ("refunding", "deleted", "lawyers", "hacking", "thieves") fire their category by
# STEM. These label and route (classification) and refuse an approved example question that holds one (kb save).
STEMS = {
    "money": ("refund", "reimburs", "overcharg", "chargeback", "charge", "disput", "invoic", "billing", "billed",
              "payment", "paid", "pay", "price", "cost", "fee", "money", "credit", "debit", "cash", "owe"),
    "privacy": ("delet", "eras", "wipe", "wiping", "privac", "gdpr", "ccpa", "remove my", "personal"),
    "security": ("hack", "breach", "leak", "expos", "passw", "phish", "stole", "stolen", "steal", "robbed",
                 "robber", "compromis", "unauthori", "login", "log in"),
    "contract": ("cancel", "lawyer", "attorney", "lawsuit", "litigat", "court", "contract", "terminat", "legal",
                 "takedown", "taken down", "take down", "copyright", "dmca", "solicitor", "agreement"),
    "complaint": ("complain", "scam", "thie", "fraud", "rip off", "ripoff", "garbage", "pathetic", "terrible",
                  "horrible", "awful", "worst", "useless", "angry", "furious", "disappoint", "unacceptable"),
}
EXACT_WORDS = {"contract": ("sue", "sues", "sued", "suing", "sueing")}


def stem_hits(norm: str) -> list[tuple[str, str]]:
    """(category, stem) for every token (or two-token phrase) that starts with a stem, or equals an exact word."""
    tokens = norm.split()
    grams = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    out = []
    for cat, stems in STEMS.items():
        for st in stems:
            if " " in st:
                if any(g == st or g.startswith(st) for g in grams if " " in g):
                    out.append((cat, st))
            elif any(t.startswith(st) for t in tokens):
                out.append((cat, st))
    for cat, words in EXACT_WORDS.items():
        out += [(cat, w) for w in words if w in tokens]
    return out


def denied_question(text: str) -> bool:
    """An example question Andre may not approve: any category fires on it (lexicon, stem, typo, profanity)."""
    fired, _ = _scan(text)
    return bool(fired) or bool(stem_hits(normalise(text)))


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
