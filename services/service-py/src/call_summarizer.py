"""
RR-F3 — call summarizer (MVP spec v0.1, front-desk team). One job: turn one call transcript into a short, correct
summary for the business owner: who called (name and callback number), what they need (one category from a fixed
list, never their own words), how urgent it is, and the next action. Deterministic; no LLM.

Rules (spec v0.1, the front-desk shared rules, the Nov 26 spec-freeze rulings):
- Nothing is added that the caller did not say. Facts come only from the CALLER's turns that speech recognition
  trusts (confidence >= 0.6); the receptionist's own confirmation line is checked against them, never used as the
  source. A turn below 0.6 that has words is flagged and never used as a fact. A silent turn carries nothing and is
  not flagged (Nov 26 ruling): silence means re-ask, never guess.
- A number the caller rejects ("not 602 555 0101") is never used. When the caller gives two different numbers, the
  later one wins only when a correction cue sits between them ("no wait", "sorry", "actually", "I mean", "perdón",
  "mejor", "quiero decir"); two numbers with no cue ("my cell is ... and my office is ...") are ambiguous: no callback
  number is reported and the owner sees ``ambiguous_number``.
- A name is reported only when the caller gave it AND the receptionist confirmed it: the caller says it after a
  lead-in ("I'm", "my name is", "put it under", "soy", "me llamo" ...; "this is", "it's", "habla" only before a number
  or a calling phrase) or as "First Last" opening a sentence before a number, and a trusted receptionist turn repeats
  it ("Let me confirm: Ana Reyes ..."). A pair nobody confirmed (a landlord, a business, a mother, a city) is never a
  name (AEGIS F3 round 3: word lists alone kept missing them). Never a pair holding a command, place, business or
  health word. Exactly one confirmed name, or none; two are ambiguous, never picked between.
- No health detail ever reaches the summary: the summary is built only from the name, the number, a category from
  the fixed list and the urgency, never from the caller's free text, and it is checked against the health lexicon
  (whole words and word stems, so "archive" is not "hiv"); a name that would carry a health word is left out, and the
  summary is still returned.
- Urgency is "emergency" only when a trusted receptionist turn gave the 911 instruction in one of its fixed forms
  ("Please hang up and dial 911 right now.", "Cuelgue y marque al 911 ahora mismo." ...); a disclaimer, a negation, a
  question or a low-confidence turn is not the instruction. The receptionist decides an emergency on the call; the
  summary reports what it did. Separately, and on purpose too broadly, any caller wording that might describe one
  (fire, smoke, gas, a smell, sparks, a weapon, blood, breathing, fainting, a seizure ... in English or Spanish) sets
  ``possible_emergency``: the text starts "CHECK NOW" and the owner is told to call back now. That net over-flags
  ("gun safe", "fire my plumber") by design: a word list cannot tell an emergency apart (AEGIS F3 round 3), so the
  summary never calls a call routine-and-safe on its own say-so; it only flags. Otherwise "routine".
- A transcript that is not an object, or has no id or no turn list, is refused (ValueError). A confidence that is not
  a finite number from 0 to 1 (NaN, infinity, text) counts as low: the turn is flagged, never used.
- The need is one label: the pack's own category (its booking category for a booking, ``treatment_question`` for a
  health matter, ``pricing_question`` for a priced service, ``pricing`` for a price the pack does not hold) or one of
  the front desk's fixed labels (emergency, complaint, existing_customer, reschedule, hours, vendor, wrong_number,
  spam_call, long_silence). When nothing matches, ``unclear`` (never a guess).
"""

from __future__ import annotations

import math
import re
import unicodedata
from fractions import Fraction
from dataclasses import dataclass, field
from typing import Optional

_APOSTROPHES = dict.fromkeys(map(ord, "\u2018\u2019\u201b\u0060\u00b4\u02b9\u02bb\u02bc\u02bd\u02be\u02c8\u02ca\u02cb"
                                      "\u2032\u2035\u05f3\ua78c\uff07\uff40\u0384\u055a\u1fbd\u1fbf\u1fef\u1ffd"), "'")
LOW_CONFIDENCE = Fraction(3, 5)      # 0.6, kept exact: this service has no float literals (guardrail test)
# Pack categories that may be spoken in the summary text as written (every category the shipped packs use); any other
# pack category is spoken as "appointment request" (AEGIS F3 round 3: a pack could name a procedure).
KNOWN_PACK_LABELS = frozenset({"billing", "book_appointment", "book_consult", "catering", "claim", "complaint", "drain",
                               "existing_case_update", "hours", "insurance_question", "legal_question", "maintenance",
                               "new_case", "new_quote", "policy_change", "pricing", "quote", "repair", "reschedule",
                               "reservation", "sales_inquiry", "service_appointment", "status_check", "booking"})
FIXED_NEEDS = ("emergency", "complaint", "existing_customer", "reschedule", "hours", "vendor", "wrong_number",
               "spam_call", "long_silence", "pricing", "pricing_question", "treatment_question", "unclear")

_NUM = re.compile(r"(?<!\d)\(?([2-9]\d{2})\)?[ .\-]?([2-9]\d{2})[ .\-]?(\d{4})(?!\d)")
_REJECTED_NUMBER = re.compile(
    r"(?:\bnot|\bno es el|\bno al|\bisn'?t|\b(?:don'?t|do not|never)\b[^.!?\d]{0,40}\b(?:call|use|text|dial|try)|"
    r"\bno (?:me )?(?:llame|llames|llamen|use|uses|marque|marques)(?:n)?\b[^.!?\d]{0,20}|\bnunca (?:al|llame)|"
    r"\bya no uso(?: el)?|\bold number(?: was| is)?|\bmi (?:viejo|antiguo) n[uú]mero(?: es| era)?)"
    r"\s*(?:me\s+)?(?:at|on|the|al|el)?\s*$", re.IGNORECASE)
# a rejection right after the number ("602 555 0101 is not my number anymore", "... that's disconnected")
_REJECTED_AFTER = re.compile(r"^\W{0,3}(?:(?:which|that|it|this one|that one)(?:'s| is| was)?\s+|is\s+|was\s+)?"
                             r"(?:not (?:my|mine|the|a good|working|in service)|isn'?t (?:mine|my|working)|no longer|"
                             r"disconnected|out of service|wrong|old|dead|doesn'?t work|not in service|"
                             r"(?:has been|got|was|is) disconnected|(?:is|was) my old|don'?t use (?:it|that)|"
                             r"do not use (?:it|that)|never use (?:it|that)|"
                             r"ya no (?:es|sirve|funciona)|no (?:es el m[ií]o|sirve|funciona)|desconectado)",
                             re.IGNORECASE)
_CORRECTION_CUE = (r"\bno wait\b|\bsorry\b|\bactually\b|\bi mean\b|\bcorrection\b|\bscratch that\b|"
                         r"\bmake that\b|\brather\b|\bperd[oó]n\b|\bmejor\b|\bquiero decir\b|\bdisculpe?\b|"
                         r"\bmás bien\b|\bcorrijo\b")
_CORRECTION = re.compile(_CORRECTION_CUE, re.IGNORECASE)
# a correction cue followed by digits that do not form a whole number
_PARTIAL_CORRECTION = re.compile("(?:" + _CORRECTION_CUE + r")[\s,:\-]{0,5}\d", re.IGNORECASE)
_WORD = r"[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü'\-]+"
# where a caller gives their own name: a strong lead-in, then First Last; or a weak lead-in ("this is", "it's",
# "habla") only when the name is followed straight by a number or a calling phrase (AEGIS F3 round 2: "It's Christmas
# Eve", "This is Long Beach")
_LEAD_NAME = re.compile(rf"(?:\bI'?m|\bI am|\bmy name is|\bthe name is|\bput it under|\bunder the name(?: of)?|"
                        rf"\bme llamo|\bsoy|\bmi nombre es|\ba nombre de|\bp[oó]ngalo a nombre de)\s+({_WORD} {_WORD})"
                        rf"(?![\w'])", re.IGNORECASE)
_WEAK_LEAD_NAME = re.compile(rf"(?:\bit'?s|\bit is|\bthis is|\bhabla)\s+({_WORD} {_WORD}),?\s+(?=\(?[2-9]\d{{2}}|"
                             rf"call me|calling|here\b|speaking|ll[aá]m(?:e|en)me|my number|mi n[uú]mero)",
                             re.IGNORECASE)
# or First Last opening a sentence and followed straight by a phone number ("Ana Reyes, 602 555 0101")
_NAME_THEN_NUMBER = re.compile(rf"(?:^|[.!?¿¡]\s+)({_WORD} {_WORD}),?\s+\(?[2-9]\d{{2}}")
_NOT_NAME_WORD = {
    # greetings and sentence words
    "hi", "hello", "hola", "okay", "ok", "yes", "thanks", "thank", "gracias", "please", "sorry", "wait", "oiga",
    "bueno", "muy", "me", "mi", "my", "this", "it", "is", "es", "hay", "there", "someone", "alguien", "dear", "the",
    "in", "on", "at", "from", "calling", "here", "just", "not", "no", "very", "so", "really", "a", "an",
    # commands a caller could plant
    "approve", "approved", "refund", "refunds", "wire", "funds", "transfer", "ignore", "ignora", "owner", "must",
    "now", "send", "pay", "cancel", "delete", "give", "dame", "grant", "allow", "authorize", "urgent", "urgente",
    "emergency", "discount", "free", "press", "call", "system", "admin", "instructions", "override",
    # places and businesses
    "los", "las", "san", "santa", "new", "north", "south", "east", "west", "street", "st", "avenue", "ave", "road",
    "rd", "drive", "lane", "boulevard", "plumbing", "dental", "clinic", "center", "centre", "hospital", "pharmacy",
    "farmacia", "services", "service", "company", "inc", "llc", "spa", "auto", "law", "office", "group", "bank",
    "beach", "city", "valley", "springs", "park", "county", "lake", "hills", "heights", "village", "vegas", "angeles",
    "diego", "francisco", "jose", "phoenix", "tucson", "portland", "seattle", "reno", "bakery", "cafe", "café", "salon",
    "roofing", "repair", "repairs", "electric", "heating", "cooling", "hvac", "landscaping", "cleaning", "studio",
    "store", "shop", "market", "restaurant", "grill", "kitchen", "motors", "realty", "insurance", "medical",
    "christmas", "eve", "easter", "thanksgiving", "halloween", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december", "cell", "phone", "number", "home", "work", "mobile", "usted", "español", "espanol",
    "english", "spanish", "inglés", "ingles", "señor", "señora", "senor", "senora", "mister", "mr", "mrs", "ms", "miss",
    "doctor", "dr", "sir", "madam", "ma'am", "god", "jesus", "lord", "happy", "good", "great", "merry", "buenos",
    "buenas", "feliz",
}
_GREETINGS = frozenset({"hello", "hi", "hey", "hola", "bueno", "alo", "aló", "yes", "si", "sí", "okay", "ok"})


def _is_greeting(text: str) -> bool:
    """Only greeting words (a word scan, linear in the text: the old nested pattern backtracked for minutes)."""
    words = re.findall(r"\w+", text.lower())
    return bool(words) and all(w in _GREETINGS for w in words)
# An emergency is read only in context: a fire burning, smoke filling, someone holding a weapon, someone not breathing.
# Bare "fire", "smoke", "knife", "gun", "shooting" are not enough (AEGIS F3 round 2: "furnace won't fire up", "sharpen
# my knife", "gun safe", "shooting a video", "smoke-free", "fire my plumber").
_EMERGENCY = re.compile(
    r"\bnot breathing|\bisn'?t breathing|\bstopped breathing|\bbarely breathing|\btrouble breathing|\bcan'?t breathe|"
    r"\bcannot breathe|\bcollapsed|\bunconscious|\bunresponsive|\bpassed out|\bfainted|\bwon'?t wake up|"
    r"\bheart attack|\bchest pain|\bchoking|\boverdos|\b(?:having|is having|she.s having|he.s having) a (?:seizure|stroke)|"
    r"\b(?:seizure|stroke) right now|\bbleeding (?:badly|a lot|heavily|everywhere|out)|\bis bleeding\b|"
    r"\bsmell(?:s|ing)? (?:like |of )?(?:natural |propane )?gas\b|\bgas (?:is )?leak|\bleaking gas|\bgas smell|"
    r"\bon fire\b|\bin flames|\bcaught fire|\bis burning\b|\bare burning\b|\bburning (?:down|up)\b|"
    r"\bthere'?s a fire\b|\b(?:house|kitchen|electrical|garage|car|grease) fire\b|\bfire (?:is |has )?(?:spread|"
    r"started|broke out|everywhere)|\bsmoke (?:is )?(?:coming|pouring|filling|everywhere|billowing)|"
    r"\bfull of smoke|\b(?:see|smell) smoke\b|\bsmoke (?:in|from) (?:the|my|our)\b|"
    r"\b(?:with|has|holding|waving|pulled|pointing) a (?:knife|gun|pistol|weapon|firearm)|\bpistol\b|\barmed\b|"
    r"\bshots? fired|\bsomeone (?:is )?shooting|\bbeing shot|\bgot shot|\bgunshot|\bthreatening (?:people|me|us|"
    r"them|someone|everyone|to (?:kill|hurt|shoot|stab))|"
    r"\bdesmay|\bno est[aá] respirando|\bno respira|\bdej[oó] de respirar|\bsin respirar|\bataque (?:al|card)|"
    r"\bhuele [^.!?]{0,60}gas|\bolor a gas|\bfuga de gas|\bsale humo|\bmucho humo|\bhumo (?:por|en) todas|\bse est[aá] quemando|"
    r"\bincendio|\bfuego\b|\bcon un (?:cuchillo|arma|pistola)|\bpistola|\bdisparo|\bamenazando a", re.IGNORECASE)
# A deliberately broad net: any word that might be part of describing an emergency. It only ever ADDS a flag.
_EMERGENCY_WORDS = re.compile(
    r"fire|\b(?:flame|burn|smok|spark|gas\b|odou?r|smell|fume|carbon monoxide|co alarm|co detector|explo|knife|gun|"
    r"fell|fall|not moving|won'?t move|can'?t move|swallow|pill|poison|drank|bleach|drain cleaner|overdos|"
    r"break(?:ing)? in|broke in|broken into|intruder|burglar|kill|suicid|die|dying|dead|stab|turning blue|blue lips|"
    r"swell|swollen|allergic reaction|anaphyla|epipen|labor|contraction|water broke|head injur|hit (?:his|her|my) head|"
    r"shock|kick|abuse|entrando|ahog|assault|rape|missing child|kidnap|"
    r"ca[ií]d|se cay[oó]|me ca[ií]|no se mueve|trag[oó]|pastilla|veneno|cloro|robo|ladr[oó]n|entraron|mat(?:ar|arme)|"
    r"suicid|morir|muri|apu[ñn]al|labios morados|hinch|parto|contracciones|rompi[oó] (?:la )?fuente|golpe|"
    r"weapon|armed|shoot|shot|blood|bleed|breath|chok|unconscious|not respond|unresponsive|faint|collaps|seizure|"
    r"stroke|heart|chest|overdos|dizzy|hurt|injur|emergenc|911|9-1-1|ambulance|police|threat|attack|electrocut|"
    r"drown|fuego|llama[s]?\b|humo|incendi|quem|chispa|olor|huele|sangr|respir|desmay|cuchillo|pistola|arma\b|"
    r"disparo|herid|ayuda|emergencia|ambulancia|polic|amenaz|no responde|inconscien|ataque|explot|mareado)",
    re.IGNORECASE)
# the 911 instruction, in its fixed forms only (a whole sentence)
_SAID_911 = re.compile(
    r"(?:please,?\s+)?(?:hang up(?:\s+now)?,?\s+and\s+)?(?:dial|call)\s+(?:9-1-1|911)"
    r"(?:\s+(?:right now|now|immediately|right away))?(?:,?\s+please)?[.!]?|"
    r"(?:por favor,?\s+)?(?:cuelgue(?:\s+ahora)?\s+y\s+)?(?:marque|llame)(?:\s+al)?\s+(?:9-1-1|911)"
    r"(?:\s+(?:ahora mismo|ahora|de inmediato|ya))?(?:,?\s+por favor)?[.!]?", re.IGNORECASE)
_HEALTH = re.compile(
    r"\b(?:pregnan|embaraz|blood thinner|anticoagul|diabet|seizure|convulsi|c[aá]ncer|hiv\b|vih\b|aids\b|sida\b|"
    r"medicat|medicament|surger|cirug|allerg|al[eé]rgi|crohn|sclerosis|esclerosis|disease|enfermedad|"
    r"(?:i was|i've been|i am|i'm|been|me) diagnos|diagnosis\b|me diagnosticaron|"
    r"chemo|quimio|asthma|asma\b|epilep|arthritis|artritis|lupus|hepatitis|depression|depressed|deprimid|anxiety|ansiedad|bipolar|"
    r"autis|dementia|demencia|alzheimer|parkinson|insulin|tumou?r|leukemia|leucemia|hypertension|hipertensi|"
    r"heart condition|condici[oó]n card|kidney|ri[ñn][oó]n|dialys|di[aá]lisis|stroke|derrame cerebral|hernia|fibromialg|"
    r"palsy|par[aá]lisis|syndrome|s[ií]ndrome|tourette|expecting (?:a baby|twins|triplets)|due date|trimester|"
    r"miscarri|aborto|ivf\b|fertility|fertilidad|hiv\b|transplant|trasplante|sclero|pacemaker|marcapasos|"
    r"chronic|cr[oó]nic|terminal|hospice|psychiatr|psiqui|schizo|esquizo|ptsd|adhd|tdah|ocd\b|"
    r"fibromyalg|migraine|migra[ñn]a|cholesterol|colesterol|thyroid|tiroides|herpes|std\b|sti\b|ets\b)",
    re.IGNORECASE)
# Health words that also have ordinary meanings ("depression in the lawn", "due date on the invoice", "two-stroke",
# "Dutch elm disease"): a health NEED only right after a personal word (I, my, me, tengo ...). They still keep a name
# or label out of the text wherever they appear (_HEALTH).
_HEALTH_PLAIN = re.compile(r"\b(?:chronic|cr[oó]nic|terminal|transplant|trasplante|kidney|ri[ñn][oó]n|due date|"
                           r"stroke|derrame|disease|enfermedad|depression|depressed|anxiety|ansiedad|tumou?r|hernia|"
                           r"aids|std|sti|ets|aborto|fertility|fertilidad|trimester|hospice|autism|autistic)",
                           re.IGNORECASE)
_PERSONAL = re.compile(r"\b(?:i|i'm|i am|i have|i've|i had|i've had|my|me|mi|tengo|tuve|tomo|estoy|soy|he has|she has|"
                       r"he had|she had|he's|she's)\s+(?:a\s+|an\s+)?$", re.IGNORECASE)


def _health_need(text: str) -> bool:
    for m in _HEALTH.finditer(text):
        if not _HEALTH_PLAIN.fullmatch(m.group(0)):
            return True
        whole = re.match(r"[\w\-]*", text[m.end():]).group(0) == ""      # "kidney-shaped", "riñonera" are not it
        if whole and _PERSONAL.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


_RULES = (   # (label, pattern) in precedence order; the first that matches a trusted caller turn wins
    ("spam_call", r"congratulations|qualif(y|ies) for|free government grant|press one|felicidades|califica para|"
                  r"beca del gobierno|marque uno"),
    ("wrong_number", r"\bis this the\b|¿?\bes (la|el) (farmacia|banco|hospital|escuela)|wrong number|"
                     r"n[uú]mero equivocado|refill a prescription|surtir una receta"),
    ("vendor", r"\bsupplier\b|\bvendor\b|\bproveedor\b|purchasing|\bcompras\b"),
    ("complaint", r"last visit|not good|third time|nobody calls me back|\bmanager\b|complain|"
                  r"última visita|no fue buena|tercera vez|nadie me regresa|\bgerente\b|queja"),
    ("reschedule", r"move my appointment|reschedule|change my appointment|cambiar mi cita|mover mi cita"),
    ("existing_customer", r"already a customer|about my account|ya soy cliente|sobre mi cuenta"),
    ("price", r"how much|\bprice\b|\bcost\b|\bcharge\b|cu[aá]nto|cobran|cuesta|precio"),
    ("hours", r"your hours|what time do you|are you open|horario|a qu[eé] hora abren"),
    ("booking", r"\bbook\b|appointment|reservation|\bschedule\b|\bcita\b|reservar|discount|percent off|descuento"),
)


@dataclass(frozen=True)
class Summary:
    transcript_id: str
    name: Optional[str]
    callback_number: Optional[str]
    need: str
    urgency: str                      # "emergency" | "routine"
    next_action: str
    low_confidence_flagged: bool
    corrected_value_used: bool
    text: str
    notes: tuple = field(default_factory=tuple)
    possible_emergency: bool = False  # caller wording that might describe one, with no 911 instruction: CHECK NOW


def _fold(text: str) -> str:
    """NFKC, then every apostrophe look-alike as "'" (before and after NFKC: some only appear after it)."""
    return unicodedata.normalize("NFKC", text.translate(_APOSTROPHES)).translate(_APOSTROPHES)


# a need word right after a negation ("No complaints", "not calling to reschedule", "I'm not a vendor") is not the need
_NEGATED = re.compile(r"(?:\bno|\bnot|n't|\bnever|\bnor|\bwithout|\bno es|\bno soy|\bsin)(?:\s+\w+){0,3}\s*$",
                      re.IGNORECASE)


def _need(trusted: list[str], pack: dict, emergency: bool, any_words: bool) -> str:
    if emergency:
        return "emergency"
    joined = " \n ".join(_fold(t) for t in trusted)
    low = joined.lower()
    if _health_need(joined):
        return "treatment_question"
    for label, pat in _RULES:
        if any(not _NEGATED.search(joined[max(0, m.start() - 30):m.start()])
               for m in re.finditer(pat, joined, re.IGNORECASE)):
            if label == "price":
                raw = pack.get("services")
                services = [s.replace("_", " ") for s in raw if isinstance(s, str)] if isinstance(raw, dict) else []
                return "pricing_question" if any(s in low for s in services) else "pricing"
            if label == "booking":
                raw = pack.get("reason_categories")
                cats = [c for c in raw if isinstance(c, str) and c] if isinstance(raw, (list, tuple)) else []
                return cats[0] if cats else "booking"
            return label
    if not any_words or (trusted and all(_is_greeting(t) for t in trusted)):
        return "long_silence"      # nothing spoken, or nothing but a greeting into silence
    return "unclear"


def _said_911(agent_text: str) -> bool:
    """The receptionist told the caller to call 911: the whole trusted turn is exactly one of the instruction's fixed
    forms. Anything said with it ("In an emergency. Call 911.", "When in doubt, call 911", "We are not an emergency
    line. Call 911.") is not the instruction (AEGIS round 5); the caller's own words still raise CHECK NOW."""
    return _SAID_911.fullmatch(agent_text.strip()) is not None


def _confidence(raw: object) -> Fraction:
    """A finite number from 0 to 1, else 0 (so a spoken turn with an unreadable confidence is flagged, never used)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return Fraction(0)
    if isinstance(raw, int):
        return Fraction(raw) if 0 <= raw <= 1 else Fraction(0)          # no float conversion: 10**400 is just "out"
    if not math.isfinite(raw):
        return Fraction(0)
    value = Fraction(str(raw))         # the written value: Fraction(0.6) alone is 0.59999... and would read as low
    return value if 0 <= value <= 1 else Fraction(0)


def _callback(trusted: list[str], notes: list[str]) -> tuple[Optional[str], bool]:
    """(the callback number, whether a correction picked it)."""
    found = []                         # (number, text before it since the previous accepted number)
    joined = " \n ".join(_fold(x) for x in trusted)
    last_end = 0
    for m in _NUM.finditer(joined):
        before = joined[last_end:m.start()]
        if _REJECTED_NUMBER.search(joined[max(0, m.start() - 60):m.start()]) or \
                _REJECTED_AFTER.search(joined[m.end():m.end() + 60]):
            last_end = m.end()
            notes.append("rejected_number_not_used")
            continue
        found.append(("+1" + "".join(m.groups()), before, m.end()))
        last_end = m.end()
    if not found:
        return None, False
    end = found[-1][2]
    nxt = _NUM.search(joined, end)
    if _PARTIAL_CORRECTION.search(joined[end:nxt.start() if nxt else end + 40]):
        notes.append("ambiguous_number")   # "0110, no wait, 0101": a correction we cannot complete (AEGIS round 5)
        return None, False
    distinct = []
    for number, before, _ in found:
        if distinct and number != distinct[-1][0] and not _CORRECTION.search(before[-30:]):
            if number not in [d[0] for d in distinct]:
                notes.append("ambiguous_number")
                return None, False
        if not distinct or number != distinct[-1][0]:
            distinct.append((number, before))
    return distinct[-1][0], len({d[0] for d in distinct}) > 1


# a receptionist turn confirms a name only when it is a confirmation ("Let me confirm: Ana Reyes"), not any mention
# ("I'll note your landlord Ana Reyes")
_CONFIRM_CUE = re.compile(r"\bconfirm|\bconfirmo|\bis that (?:right|correct)|\bdid i get that right|\bcorrect\?|"
                          r"\b¿?es correcto|\bverify|\bjust to (?:check|be sure)|\bso that'?s", re.IGNORECASE)


def _names(trusted: list[str], confirmations: Optional[list[str]] = None) -> list[str]:
    """Names the caller gave; with ``confirmations`` (the trusted receptionist turns), only those it repeated."""
    said = " \n ".join(_fold(x).casefold() for x in confirmations if _CONFIRM_CUE.search(_fold(x))) \
        if confirmations is not None else None
    names: list[str] = []
    for x in trusted:
        x = _fold(x)
        for m in list(_LEAD_NAME.finditer(x)) + list(_WEAK_LEAD_NAME.finditer(x)) + \
                list(_NAME_THEN_NUMBER.finditer(x)):
            cand = m.group(1)
            words = cand.split()
            if not all(w[0].isupper() for w in words):
                continue                   # the lead-in match is case-blind; the name itself must be capitalised
            if any(w.casefold() in _NOT_NAME_WORD for w in words) or _HEALTH.search(cand):
                continue
            if re.search(r"'s?$", words[-1]):
                continue                   # "I'm Ana Reyes's husband": a possessive names someone else (AEGIS round 5)
            if said is not None and not re.search(rf"(?<!\w){re.escape(cand.casefold())}(?!\w)", said):
                continue                   # nobody confirmed it: a landlord, a business, a mother is never a name
            if cand not in names:
                names.append(cand)
    return names


def summarize(transcript: dict, pack: Optional[dict] = None) -> Summary:
    if not isinstance(transcript, dict):
        raise ValueError("a transcript must be an object")
    tid = transcript.get("transcript_id")
    if not isinstance(tid, str) or not tid:
        raise ValueError("transcript_id is required")
    turns = transcript.get("turns")
    if not isinstance(turns, list):
        raise ValueError("turns must be a list")
    pack = pack if isinstance(pack, dict) else {}
    notes: list[str] = []
    caller, agent = [], []
    for t in turns:
        if not isinstance(t, dict) or not isinstance(t.get("text", ""), str):
            raise ValueError("every turn needs a text")
        speaker = t.get("speaker")
        if speaker not in ("caller", "agent"):
            notes.append("unknown_speaker_ignored")   # never read as the receptionist (AEGIS F3 round 4)
            continue
        (caller if speaker == "caller" else agent).append((_confidence(t.get("confidence")), t.get("text") or ""))
    spoken = [(c, x) for c, x in caller
              if x.strip() and not re.fullmatch(r"\[silence[^\]]*\]", x.strip(), re.IGNORECASE)]
    trusted = [x for c, x in spoken if c >= LOW_CONFIDENCE]
    flagged = any(c < LOW_CONFIDENCE for c, _ in spoken)
    if flagged:
        notes.append("low_confidence_turn_not_used")

    callback, corrected = _callback(trusted, notes)
    agent_trusted = [x for c, x in agent if c >= LOW_CONFIDENCE]       # a receptionist turn is checked too
    names = _names(trusted, agent_trusted)
    if not names and _names(trusted):
        notes.append("name_not_confirmed")
    name = names[0] if len(names) == 1 else None
    if len(names) > 1:
        notes.append("ambiguous_name:" + "|".join(names))

    said_911 = any(_said_911(_fold(x)) for x in agent_trusted)
    emergency = said_911
    # the CHECK NOW net also reads low-confidence words: they are never a fact, but they can still be a warning
    possible = not emergency and any(_EMERGENCY.search(_fold(x)) or _EMERGENCY_WORDS.search(_fold(x))
                                     for _, x in spoken)
    if possible:
        notes.append("possible_emergency")
    confirmed = ["+1" + "".join(m) for x in agent_trusted for m in _NUM.findall(x)]
    if callback and confirmed and confirmed[-1] != callback:
        notes.append("confirmation_disagrees_with_caller")
    need = _need(trusted, pack, emergency, bool(spoken))
    if flagged and need == "long_silence":
        need = "unclear"               # words were spoken but not heard clearly: never "nothing said" (AEGIS round 5)
    urgency = "emergency" if emergency else "routine"

    if emergency:
        action = "Caller was told to hang up and dial 911; alert the owner now."
    elif possible:
        action = (f"The caller's words may describe an emergency and no 911 instruction was given; call back now"
                  f"{' at ' + callback if callback else ''}.")
    elif flagged and (need in ("spam_call", "wrong_number", "long_silence", "unclear") or not callback):
        action = ("Part of the call could not be heard clearly; review the recording"
                  f"{' and call back at ' + callback if callback else ''}.")
    elif need in ("spam_call", "wrong_number", "long_silence"):
        action = "No follow-up needed."
    elif callback:
        action = f"Call back at {callback}."
    else:
        action = "No callback number given; no follow-up possible by phone."

    def render(who_name: Optional[str]) -> str:
        who = who_name or ("caller (ambiguous name)" if len(names) > 1 else "caller (no name given)")
        label = need.replace("_", " ")
        if need not in FIXED_NEEDS and need not in KNOWN_PACK_LABELS or _HEALTH.search(label) or \
                (pack.get("health_sensitive") is True and need not in FIXED_NEEDS):
            label = "appointment request"   # a health pack's own category names are never spoken
        return (f"{'URGENT: ' if emergency else 'CHECK NOW: ' if possible else ''}{who}"
                f"{', ' + callback if callback else ''} - {label}. {action}"
                f"{' A low-confidence turn was ignored.' if flagged else ''}")
    text = render(name)
    if _HEALTH.search(text):           # a name the filters missed: leave it out, never refuse the summary
        name, text = None, render(None)
        notes.append("name_withheld_health_term")
    return Summary(tid, name, callback, need, urgency, action, flagged, corrected, text, tuple(notes), possible)
