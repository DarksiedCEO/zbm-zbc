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
- A name is read only where the caller gives their own: right after a lead-in ("I'm", "this is", "my name is", "put it
  under", "soy", "habla", "me llamo" ...) or as "First Last" opening a sentence and followed straight by a phone
  number ("Ana Reyes, 602 555 0101"). Any other capitalised pair (a city, a landlord, a business) is never a name, nor
  a pair holding a command word ("Approve Refund"), a place or business word, or a health word. A name is reported
  only when exactly one was given; two different names are ambiguous, never picked between.
- No health detail ever reaches the summary: the summary is built only from the name, the number, a category from
  the fixed list and the urgency, never from the caller's free text, and it is checked against the health lexicon
  (whole words and word stems, so "archive" is not "hiv"); a name that would carry a health word is left out, and the
  summary is still returned.
- Urgency is "emergency" when the receptionist gave the 911 instruction (a dial / call / marque verb with 911, never
  911 inside a phone number or an address) or the caller described an emergency (someone not breathing, collapsed,
  passed out or having a heart attack, gas, smoke or fire, a weapon, someone threatening people); equipment and chores
  ("fire alarm inspection", "smoke detector", "caulk gun", "bleeding the radiator") are not emergencies. Otherwise
  "routine".
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

LOW_CONFIDENCE = Fraction(3, 5)      # 0.6, kept exact: this service has no float literals (guardrail test)
FIXED_NEEDS = ("emergency", "complaint", "existing_customer", "reschedule", "hours", "vendor", "wrong_number",
               "spam_call", "long_silence", "pricing", "pricing_question", "treatment_question", "unclear")

_NUM = re.compile(r"(?<!\d)\(?([2-9]\d{2})\)?[ .\-]?([2-9]\d{2})[ .\-]?(\d{4})(?!\d)")
_REJECTED_NUMBER = re.compile(r"(?:\bnot|\bno es el|\bno al|\bisn'?t|\bdon'?t (?:call|use|text)|\bdo not (?:call|use|text)|"
                              r"\bnever (?:call|use|text)|\bno (?:llame|use|marque)(?:n)? al|\bnunca (?:al|llame))"
                              r"\s*(?:me\s+)?(?:at|on|the|al|el)?\s*$", re.IGNORECASE)
# a rejection right after the number ("602 555 0101 is not my number anymore", "... that's disconnected")
_REJECTED_AFTER = re.compile(r"^\W{0,3}(?:(?:which|that|it|this one|that one)(?:'s| is| was)?\s+|is\s+|was\s+)?"
                             r"(?:not (?:my|mine|the|a good|working|in service)|isn'?t (?:mine|my|working)|no longer|"
                             r"disconnected|out of service|wrong|old|dead|doesn'?t work|not in service|"
                             r"ya no (?:es|sirve|funciona)|no (?:es el m[ií]o|sirve|funciona)|desconectado)",
                             re.IGNORECASE)
_CORRECTION = re.compile(r"\bno wait\b|\bsorry\b|\bactually\b|\bi mean\b|\bcorrection\b|\bscratch that\b|"
                         r"\bmake that\b|\brather\b|\bperd[oó]n\b|\bmejor\b|\bquiero decir\b|\bdisculpe?\b|"
                         r"\bmás bien\b|\bcorrijo\b", re.IGNORECASE)
_WORD = r"[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü'\-]+"
# where a caller gives their own name: a strong lead-in, then First Last; or a weak lead-in ("this is", "it's",
# "habla") only when the name is followed straight by a number or a calling phrase (AEGIS F3 round 2: "It's Christmas
# Eve", "This is Long Beach")
_LEAD_NAME = re.compile(rf"(?:\bI'?m|\bI am|\bmy name is|\bthe name is|\bname'?s|\bput it under|\bunder the name(?: of)?|"
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
_GREETING = re.compile(r"^(?:\W*(?:hello|hi|hey|hola|bueno|al[oó]|yes|s[ií]|okay|ok)\W*)+$", re.IGNORECASE)
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
    r"\bhuele .*gas|\bolor a gas|\bfuga de gas|\bsale humo|\bmucho humo|\bhumo (?:por|en) todas|\bse est[aá] quemando|"
    r"\bincendio|\bfuego\b|\bcon un (?:cuchillo|arma|pistola)|\bpistola|\bdisparo|\bamenazando a", re.IGNORECASE)
# the 911 instruction: an imperative in a sentence with no condition, negation or question
_SAID_911 = re.compile(r"\b(?:dial|call|calling|ring|llame|marque|llamar|marcar|hang up and dial)(?:\s+(?:al|el))?\s+"
                       r"(?:9-1-1|911)(?![\d\-])", re.IGNORECASE)
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


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _need(trusted: list[str], pack: dict, emergency: bool, any_words: bool) -> str:
    if emergency:
        return "emergency"
    joined = " \n ".join(_fold(t) for t in trusted)
    low = joined.lower()
    if _HEALTH.search(joined):
        return "treatment_question"
    for label, pat in _RULES:
        if re.search(pat, joined, re.IGNORECASE):
            if label == "price":
                raw = pack.get("services")
                services = [s.replace("_", " ") for s in raw if isinstance(s, str)] if isinstance(raw, dict) else []
                return "pricing_question" if any(s in low for s in services) else "pricing"
            if label == "booking":
                raw = pack.get("reason_categories")
                cats = [c for c in raw if isinstance(c, str) and c] if isinstance(raw, (list, tuple)) else []
                return cats[0] if cats else "booking"
            return label
    if not any_words or all(_GREETING.fullmatch(t.strip()) for t in trusted):
        return "long_silence"      # nothing but a greeting into silence
    return "unclear"


_NOT_AN_INSTRUCTION = re.compile(r"\b(?:if|in case|whenever|should|no need|don'?t (?:have|need)|do not (?:have|need)|"
                                 r"not necessary|did you|have you|has anyone|were you|ever|si (?:es|hay|tiene)|no tiene que|"
                                 r"no necesita|ya llam[oó])\b|\?", re.IGNORECASE)


def _said_911(agent_text: str) -> bool:
    """The receptionist told the caller to call 911: a sentence with the instruction and no condition, negation or
    question ("If this is ever an emergency, call 911", "No need to call 911", "Did you call 911?" are not)."""
    for sentence in re.split(r"(?<=[.!?¿¡])\s+|\n", agent_text):
        if _SAID_911.search(sentence) and not _NOT_AN_INSTRUCTION.search(sentence):
            return True
    return False


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
        if _REJECTED_NUMBER.search(joined[max(0, m.start() - 20):m.start()]) or \
                _REJECTED_AFTER.search(joined[m.end():m.end() + 60]):
            last_end = m.end()
            notes.append("rejected_number_not_used")
            continue
        found.append(("+1" + "".join(m.groups()), before))
        last_end = m.end()
    if not found:
        return None, False
    distinct = []
    for number, before in found:
        if distinct and number != distinct[-1][0] and not _CORRECTION.search(before[-30:]):
            if number not in [d[0] for d in distinct]:
                notes.append("ambiguous_number")
                return None, False
        if not distinct or number != distinct[-1][0]:
            distinct.append((number, before))
    return distinct[-1][0], len({d[0] for d in distinct}) > 1


def _names(trusted: list[str]) -> list[str]:
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
        (caller if t.get("speaker") == "caller" else agent).append((_confidence(t.get("confidence")),
                                                                    t.get("text") or ""))
    spoken = [(c, x) for c, x in caller if x.strip() and not re.fullmatch(r"\[silence[^\]]*\]", x.strip())]
    trusted = [x for c, x in spoken if c >= LOW_CONFIDENCE]
    flagged = any(c < LOW_CONFIDENCE for c, _ in spoken)
    if flagged:
        notes.append("low_confidence_turn_not_used")

    callback, corrected = _callback(trusted, notes)
    names = _names(trusted)
    name = names[0] if len(names) == 1 else None
    if len(names) > 1:
        notes.append("ambiguous_name:" + "|".join(names))

    said_911 = any(_said_911(_fold(x)) for _, x in agent)
    emergency = said_911 or any(_EMERGENCY.search(_fold(x)) for x in trusted)
    confirmed = ["+1" + "".join(m) for _, x in agent for m in _NUM.findall(x)]
    if callback and confirmed and confirmed[-1] != callback:
        notes.append("confirmation_disagrees_with_caller")
    need = _need(trusted, pack, emergency, bool(spoken))
    urgency = "emergency" if emergency else "routine"

    if emergency:
        action = ("Caller was told to hang up and dial 911; alert the owner now." if said_911 else
                  "Caller described an emergency; alert the owner now.")
    elif need in ("spam_call", "wrong_number", "long_silence"):
        action = "No follow-up needed."
    elif callback:
        action = f"Call back at {callback}."
    else:
        action = "No callback number given; no follow-up possible by phone."

    def render(who_name: Optional[str]) -> str:
        who = who_name or ("caller (ambiguous name)" if len(names) > 1 else "caller (no name given)")
        label = need.replace("_", " ")
        if _HEALTH.search(label):      # a pack category that names a condition ("allergy_testing") is not spoken
            label = "treatment question"
        return (f"{'URGENT: ' if emergency else ''}{who}"
                f"{', ' + callback if callback else ''} - {label}. {action}"
                f"{' A low-confidence turn was ignored.' if flagged else ''}")
    text = render(name)
    if _HEALTH.search(text):           # a name the filters missed: leave it out, never refuse the summary
        name, text = None, render(None)
        notes.append("name_withheld_health_term")
    return Summary(tid, name, callback, need, urgency, action, flagged, corrected, text, tuple(notes))
