"""
RR-F3 — call summarizer (MVP spec v0.1, front-desk team). One job: turn one call transcript into a short, correct
summary for the business owner: who called (name and callback number), what they need (one category from a fixed
list, never their own words), how urgent it is, and the next action. Deterministic; no LLM.

Rules (spec v0.1, the front-desk shared rules, the Nov 26 spec-freeze rulings):
- Nothing is added that the caller did not say. Facts come only from the CALLER's turns that speech recognition
  trusts (confidence >= 0.6); the receptionist's own confirmation line is checked against them, never used as the
  source. A turn below 0.6 that has words is flagged and never used as a fact. A silent turn carries nothing and is
  not flagged (Nov 26 ruling): silence means re-ask, never guess.
- A callback number is reported only when the receptionist read it back in the fixed form ("Let me confirm: ...,
  at 602 555 0101."), the caller gave that number in a sentence of plain words only, every caller sentence after it (in that
  turn and later ones) is plain words too, so nothing can reject it ("602 555 0101. That phone doesn't work
  anymore."; AEGIS round 13), and the caller then only affirmed (AEGIS round 10). Anything else is no callback, and the summary says to check the recording. The
  caller-side readings (rejections, correction cues, doubt after the number) still run as a second fence.
- A name is reported only when the caller gave it AND the receptionist confirmed it: the caller says it after a
  lead-in ("I'm", "my name is", "soy", "me llamo", "mi nombre es"; "this is", "it's", "habla" only before a number
  or a calling phrase) or as "First Last" opening a sentence before a number (a bare name only when the receptionist asked for
  the caller's own name, or it is the answer to the opening greeting with nobody else spoken of before it; AEGIS
  round 13), and a trusted receptionist turn confirms
  exactly that pair in the fixed form, right after the cue ("Let me confirm: Ana Reyes, at ...", "Confirmo: ...";
  AEGIS round 7). A pair nobody confirmed (a landlord, a business, a mother, a city) is never a
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
# any negation earlier in the number's own sentence ("Don't call my work number, 602 ...", "Never call this number:")
# rejects it (AEGIS round 9: the inverted default; the phrase list above kept missing wordings)
_NEG_BEFORE = re.compile(r"\b(?:don'?t|do not|never|not|no|nunca|ya no)\b", re.IGNORECASE)
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
_DIGIT_WORD = r"(?:\d+|zero|oh|one|two|three|four|five|six|seven|eight|nine|cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve)"
# after the last number, any no / not / wrong / oops / a digit or a spelled digit means the caller may have taken it
# back ("No, that's not it.", "Oops, it's 0101 at the end", "Last digit's a one"): the number is ambiguous and a person
# checks the recording (AEGIS round 7: the inverted default; a list of correction phrases kept missing some)
_DOUBT_AFTER = re.compile(r"\b(?:no|not|nope|nah|never|don'?t|do not|wrong|oops|incorrect|isn'?t|wasn'?t|actually|wait|"
                          r"sorry|digits?|last|ending|ends|mistake|scratch|mind|disconnected|instead|email|text me|"
                          r"equivocad[oa]|incorrecto|perd[oó]n|espere|n[uú]mero|mejor|olvide|desconectado)\b|"
                          r"\b" + _DIGIT_WORD + r"\b", re.IGNORECASE)
_PARTIAL_CORRECTION = re.compile(
    r"(?:" + _CORRECTION_CUE + r"|\bwait\b|\bhold on\b|\bno\b\s*[,.!]|\bending in\b|\bends in\b|\blast (?:four|4|digits)\b|"
    r"…|\.\.\.|\bespere\b|\btermina en\b)[^\n]{0,40}?\b" + _DIGIT_WORD + r"\b", re.IGNORECASE)
_WORD = r"[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü'\-]+"
# where a caller gives their own name: a strong lead-in, then First Last; or a weak lead-in ("this is", "it's",
# "habla") only when the name is followed straight by a number or a calling phrase (AEGIS F3 round 2: "It's Christmas
# Eve", "This is Long Beach")
# "put it under" / "a nombre de" name whoever the booking is for, not the caller, so they are not lead-ins (round 8)
_LEAD_NAME = re.compile(rf"(?:\bI'?m|\bI am|\bmy name is|"
                        rf"\bme llamo|\bsoy|\bmi nombre es)\s+({_WORD} {_WORD})"
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
# hello-type words only: "Yes." / "Okay." / "Sí." answer a question, so the caller was there (AEGIS round 6)
_GREETINGS = frozenset({"hello", "hi", "hey", "hola", "bueno", "alo", "aló", "oiga"})


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
    matched = [label for label, pat in _RULES
               if any(not _NEGATED.search(joined[max(0, m.start() - 30):m.start()])
                      for m in re.finditer(pat, joined, re.IGNORECASE))]
    # "spam" and "wrong number" end the call with no follow-up, so they win only when nothing else was asked for
    # ("Is this the plumber? I need to book", "Congratulations on the new shop, I'd like to book"; AEGIS round 6)
    real = [label for label in matched if label not in ("spam_call", "wrong_number")]
    for label in real[:1] or matched[:1]:
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


def _unmarked(text: str) -> str:
    """The turn with every quote mark, "¡", invisible format character and wrapping bracket or emphasis mark set aside
    (AEGIS rounds 8-11: „…“, ‹…›, U+200B, "(Please hang up ...)", "*...*")."""
    text = re.sub(r"<[^<>]{0,20}>", "", text).replace("。", ".").replace("！", "!")   # "<b>...</b>", "Okay。" (round 12)
    return "".join(c for c in text if c not in "\"'¡„‚()[]{}*_" and
                   unicodedata.category(c) not in ("Pi", "Pf", "Cf", "So", "Sk")).strip()   # emoji too


def _said_911(agent_text: str, previous: Optional[str] = None) -> Optional[str]:
    """"urgent" when the receptionist told the caller to call 911: the whole trusted turn is exactly one of the
    instruction's fixed forms and the receptionist's turn before it ended a sentence. Anything said with it ("In an
    emergency. Call 911.", "We are not an emergency line. Call 911.") is not the instruction (AEGIS round 5).
    Quotes, an opening "¡", inner punctuation and digit spacing ("9 1 1") are formatting; the final "." or "!" is not:
    without it (an ellipsis, a dash, ";" or ":" instead, or nothing) the turn may have been cut off, and with angle brackets in it words may have been set aside, so both
    are "ambiguous" (CHECK NOW; AEGIS round 13). When the turn before did not end a
    sentence ("There's no need to" / "Call 911."), the two turns may be one sentence: "ambiguous", which raises
    CHECK NOW instead of URGENT (AEGIS round 8: neither reading is safe on its own). Otherwise None."""
    # every quote mark and the opening "¡" are formatting wherever they sit (AEGIS rounds 8-9: „…“, ‹…›, ¡"…!")
    text = _unmarked(agent_text)
    text = re.sub(r"(?<!\d)9[\s.\-‐-―−]?1[\s.\-‐-―−]?1(?!\d)", "911", text)   # "9 1 1", "9-1-1", "9–1–1"
    # only a single "." or "!" (or "!!") ends the turn; any other ending ("...", " —", ";", ":", "‼", none) may be a
    # cut-off or a sentence that goes on, so the fixed form then raises CHECK NOW (AEGIS rounds 13-14)
    core = re.sub(r"[\W_]+$", "", text)
    ended = re.fullmatch(r"(?:\.|!+)", text[len(core):].strip()) is not None
    text = re.sub(r"\s+", " ", core.replace(",", " ")).strip() + "."
    if _SAID_911.fullmatch(text) is None:
        return None
    # the fixed form with no end mark may have been cut off, and anything inside angle brackets was set aside:
    # either way a person checks now (AEGIS round 13: "<no need to> call 911." read as URGENT; "Please hang up and
    # dial 911 right now" with no end mark read as routine)
    if not ended or "<" in agent_text or ">" in agent_text:
        return "ambiguous"
    prev = None if previous is None else _unmarked(previous)
    if prev is not None and not re.search(r"(?<![.…])[.!?]\s*$", prev):
        return "ambiguous"
    return "urgent"


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


def _callback(trusted: list[str], notes: list[str], rejected_out: Optional[set] = None) -> tuple[Optional[str], bool]:
    """(the callback number, whether a correction picked it); ``rejected_out`` collects every number rejected."""
    found = []                         # (number, text before it since the previous accepted number)
    rejected: set[str] = rejected_out if rejected_out is not None else set()
    joined = " \n ".join(_fold(x) for x in trusted)
    last_end = 0
    for m in _NUM.finditer(joined):
        before = joined[last_end:m.start()]
        # the number's own sentence since the previous number, a correction cue set aside ("..., no wait, sorry, 602 ...")
        sentence = re.split(r"[.!?\n]", joined[max(last_end, m.start() - 80):m.start()])[-1]
        sentence = _CORRECTION.sub(" ", re.sub(r"\bno\b(?=[\s,]*(?:wait|sorry|perd[oó]n|actually|i mean|espera|mejor))", " ",
                                              sentence, flags=re.IGNORECASE))
        if _REJECTED_NUMBER.search(joined[max(0, m.start() - 60):m.start()]) or _NEG_BEFORE.search(sentence) or \
                _REJECTED_AFTER.search(joined[m.end():m.end() + 60]):
            last_end = m.end()
            notes.append("rejected_number_not_used")
            rejected.add("+1" + "".join(m.groups()))
            continue
        found.append(("+1" + "".join(m.groups()), before, m.end()))
        last_end = m.end()
    if not found:
        return None, False
    end = found[-1][2]
    nxt = _NUM.search(joined, end)
    # after the last accepted number, any doubt at all (also around a later number the caller rejected), or that
    # same number rejected elsewhere, makes it ambiguous (AEGIS round 8: "Scratch that", "do not call ...")
    if _PARTIAL_CORRECTION.search(joined[end:nxt.start() if nxt else end + 60]) or \
            _DOUBT_AFTER.search(joined[end:]) or any(n in rejected for n, _, _ in found):
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


# the receptionist confirms a caller's name only in the fixed form, the name straight after the cue at the start of
# the turn ("Let me confirm: Ana Reyes, at ...", "Confirmo: Ana Reyes, al ..."). Anything else ("I can't confirm
# anything for Ana Reyes", "Let me confirm: your landlord Ana Reyes") confirms nothing (AEGIS round 7)
_CUE = r"(?:let me confirm|just to confirm|to confirm|so to confirm|confirmo|para confirmar)"
_NUM_TEXT = r"\(?[2-9]\d{2}\)?[ .\-]?[2-9]\d{2}[ .\-]?\d{4}"
# the fixed confirmation, the whole turn and nothing else (AEGIS round 11: "Let me confirm: you can't take calls at
# ..." passed a start-only check): "Let me confirm: Ana Reyes.", "Let me confirm: Ana Reyes, at 602 555 0101.",
# "Let me confirm: at 602 555 0101.", "Confirmo: Ana Reyes, al 602 555 0101."
_CONFIRM_LINE = re.compile(rf"{_CUE}\s*[:,]?\s*({_WORD} {_WORD})(?:,?\s+(?:at|al)\s+{_NUM_TEXT})?\s*[.!]?", re.IGNORECASE)
_READ_BACK = re.compile(rf"{_CUE}\s*[:,]?\s*(?:{_WORD} {_WORD},?\s+)?(?:at|al)\s+({_NUM_TEXT})\s*[.!]?", re.IGNORECASE)



_AFFIRM = re.compile(r"(?:(?:yes|yeah|yep|yup|correct|right|that'?s right|that'?s correct|exactly|ok|okay|uh-?huh|"
                     r"s[ií]|correcto|exacto|as[ií] es|eso es|claro)[\s,.!]*)+(?:thanks?|thank you|gracias)?[\s.!]*",
                     re.IGNORECASE)


_RETRACT = re.compile(r"\b(?:no|not|actually|sorry|misheard|wrong|wait|perd[oó]n|disculpe)\b", re.IGNORECASE)


# what may stand beside a callback number in the caller's sentence: the ways the shared pool and ordinary callers give
# one, a name, greetings and the correction cues. Any other word there ("stopped working", "Stop calling", "Avoid",
# "got shut off", "ya lo cancelé") means a person must check (AEGIS round 12: the inverted default for rejections)
_PLAIN_WORDS = re.compile(
    r"(?:my|the|best|cell|phone|mobile|number|is|it'?s|it|you|can|reach|me|call|back|at|on|text|and|okay|ok|yes|"
    r"yeah|so|hi|hello|this|i'?m|i|am|my name|name|please|thanks|thank|no|wait|sorry|perd[oó]n|mi|n[uú]mero|es|"
    r"ll[aá]m(?:e|en)me|al|el|me|puede|pueden|llamar|llamarme|soy|habla|muy|bien|bueno|hola|s[ií]|y|gracias|por|favor|"
    r"actually|mean|mejor|quiero|decir|that'?s|right|correct|exactly|yep|yup|uh-?huh|correcto|exacto|as[ií]|eso|"
    r"claro|much|very)",
    re.IGNORECASE)


_PHONE_TOPIC = re.compile(r"\b(?:phone|phones|cell|cellphone|number|numbers|line|landline|mobile|text|texts|texting|"
                          r"voicemail|celular|n[uú]mero|tel[eé]fono|l[ií]nea|mensajes?)\b", re.IGNORECASE)


def _only_plainly_given(number: str, trusted: list[str], names: Optional[list[str]] = None) -> bool:
    """No caller sentence before the number speaks of a phone in other than plain words, and every trusted caller
    sentence from the first that holds ``number`` to the end of the call holds nothing else but
    plain words, names and numbers. A sentence after the number ("That phone doesn't work anymore.", "That line got
    shut off in May.") may reject it, in the same turn or a later one, so it must be plain too (AEGIS round 13: the
    inverted default; a caller who describes the problem after giving the number gets "check the recording")."""
    seen = False
    for x in trusted:
        for sentence in re.split(r"[!?\n]|\.(?!\d)", _fold(x)):     # "602.555.0101" stays one number
            nums = ["+1" + "".join(m.groups()) for m in _NUM.finditer(sentence)]
            if number not in nums and not seen:
                # a sentence before the number that speaks of a phone in other than plain words ("Never call this
                # number.", "My cell got shut off last week.") may reject it (AEGIS round 14)
                if _PHONE_TOPIC.search(sentence) and \
                        any(not _PLAIN_WORDS.fullmatch(w) for w in re.findall(r"[^\W\d_][\w']*", _NUM.sub(" ", sentence))):
                    return False
                continue
            seen = True
            rest = _NUM.sub(" ", sentence)
            for n in names or ():           # the caller's own name, as read
                rest = re.sub(rf"(?<!\w){re.escape(n)}(?!\w)", " ", rest, flags=re.IGNORECASE)
            words = re.findall(r"[^\W\d_][\w']*", rest)
            if any(not _PLAIN_WORDS.fullmatch(w) for w in words):
                return False
    return seen


def _clean_confirmation(text: str) -> bool:
    """A read-back with no question and nothing taken back in it ("Actually no, I misheard"; AEGIS round 10)."""
    return "?" not in text and "¿" not in text and not _RETRACT.search(text)


def _affirmed(ordered: list, i: int) -> bool:
    """After turn ``i`` the caller said something, and everything the caller said was an affirmation."""
    later = [(c, x) for sp, c, x in ordered[i + 1:] if sp in ("caller", "unknown") and x.strip()]
    # a low-confidence "Yes." is never a fact, so it affirms nothing (AEGIS round 11); words under a speaker label it
    # cannot read after the read-back are never a yes either (round 12)
    return bool(later) and all(c >= LOW_CONFIDENCE and _AFFIRM.fullmatch(_fold(x).strip()) for c, x in later)


# what may come before a strong lead-in in its sentence: greetings and fillers only (AEGIS round 11: "A lady goes I'm
# Ana Reyes", "He was like I'm Mark Diaz" passed a list of reporting verbs; the inverted default allows only these)
_OPENS_SENTENCE = re.compile(r"\s*(?:(?:hi|hello|hey|yes|yeah|okay|ok|and|so|well|oh|um|uh|hola|s[ií]|bueno|muy bien|"
                             r"buenos d[ií]as|buenas tardes|good morning|good afternoon|thanks|gracias)[\s,]*)*",
                             re.IGNORECASE)


def _lead_prefix(m: re.Match) -> str:
    """The text of the lead-in's own sentence before the lead-in word ("Okay, and " in "Okay, and I'm Ana Reyes")."""
    return re.split(r"[.!?\n]", m.string[:m.start()])[-1]


# the turn that gives the name speaks of someone else ("my landlord ... Ana Reyes"), or the receptionist asked about
# someone else ("What is her name?", "Who is the owner?"): never the caller's own name (AEGIS round 12)
_THIRD_PARTY = re.compile(r"\b(?:my|his|her|their|our|mi|su|sus)\b(?!\s+(?:name|number|n[uú]mero|nombre|phone|cell|"
                          r"tel[eé]fono|celular|appointment|cita))|\b(?:landlord|tenant|owner|boss|wife|husband|"
                          r"mother|father|son|daughter|friend|neighbou?r|due[nñ]o|jefe|esposa|esposo)\b", re.IGNORECASE)
_ASKS_OTHER = re.compile(r"\b(?:her|his|their|owner|whose|who|landlord|tenant|su|due[nñ]o|qui[eé]n|person|"
                         r"someone|somebody|they|them|for whom|calling for|para qui[eé]n|persona|boss|wife|husband|mother|father|son|"
                         r"daughter|friend|neighbou?r|mom|dad|jefe|esposa|esposo)\b|\byour\s+[^\W\d_]+'s\b",
                         re.IGNORECASE)
# someone other than the caller, spoken of before a bare name ("Calling for the guy upstairs. Mark Diaz, 602 ...")
_OTHER_PERSON = re.compile(r"\b(?:he|she|they|him|them|his|hers|their|theirs|[eé]l|ella|ellos|ellas|guy|lady|man|woman|"
                           r"gentleman|kid|kids|tenant|landlord|owner|boss|wife|husband|mother|father|mom|dad|son|"
                           r"daughter|friend|neighbou?r|roommate|brother|sister|grandma|grandmother|grandpa|"
                           r"grandfather|calling for|on behalf|de parte|due[nñ]o|jefe|esposa|esposo|mam[aá]|pap[aá]|"
                           r"hijo|hija|amigo|amiga|vecino|vecina|inquilino|se[nñ]or|se[nñ]ora)\b", re.IGNORECASE)
# a name the caller gave without "I'm" / "my name is" / "this is" counts only as the answer to a question for the
# caller's own name (AEGIS round 13: "What's your son's name?", "the person you're calling for", a bare "Mark Diaz,
# 602 ..." after "Calling for the guy upstairs." were all reported as the caller's)
_ASKS_OWN = re.compile(r"\b(?:(?:what'?s|what is|may i (?:have|get)|can i (?:have|get)|could i (?:have|get)) your (?:full )?"
                       r"name|who am i speaking (?:with|to)|(?:cu[aá]l es|me da|me puede dar) su nombre|"
                       r"con qui[eé]n hablo)\b", re.IGNORECASE)


def _names(trusted: list[str], confirmations: Optional[list[str]] = None) -> list[str]:
    """Names the caller gave; with ``confirmations`` (the trusted receptionist turns), only those it repeated."""
    said = {m.group(1).casefold() for x in confirmations if "?" not in x and "¿" not in x
            for m in [_CONFIRM_LINE.fullmatch(_fold(x).strip())] if m} \
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
            if re.search(r"'s?$", words[-1]) or re.match(r"\s*'", x[m.end(1):]):
                continue                   # "I'm Ana Reyes's husband": a possessive names someone else (AEGIS round 5)
            before, after = x[:m.start(1)], re.split(r"[.!?\n]", x[m.end(1):])[0]
            if m.re is not _LEAD_NAME and (re.search(r"[?¿]", before[-60:]) or
                                           re.search(r"\b(?:my|his|her|their|our|mi|su|sus)\b", before[-60:], re.IGNORECASE)):
                continue                   # "... my boss? Ana Reyes, 602 ...": someone the caller spoke of (round 9)
            if m.re is _LEAD_NAME and not _OPENS_SENTENCE.fullmatch(_lead_prefix(m)):
                continue                   # "A lady goes I'm Ana Reyes": reported speech (AEGIS rounds 10-11)
            if m.re is _LEAD_NAME and not re.match(rf"\s*(?:[,.;:!?]|$|and\b|y\b|{_NUM_TEXT})", x[m.end(1):], re.IGNORECASE):
                continue                   # "I'm Ana Reyes daughter": the name must end there (AEGIS round 11)
            if re.search(r"\b(?:she|he|they|her|his|their|ella|[eé]l)\b", after, re.IGNORECASE):
                continue                   # "The name is Ana Reyes, she's my tenant" (AEGIS round 9)
            if said is not None and cand.casefold() not in said:
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
    unknown_text: list[str] = []
    ordered: list[tuple[str, Fraction, str]] = []
    for t in turns:
        if not isinstance(t, dict) or not isinstance(t.get("text", ""), str):
            raise ValueError("every turn needs a text")
        speaker = t.get("speaker")
        if type(speaker) is str and speaker.strip().rstrip(":").strip().casefold() == "caller":
            speaker = "caller"         # "Caller" is the caller; the receptionist label stays exact (AEGIS round 6)
        if speaker not in ("caller", "agent"):
            notes.append("unknown_speaker_ignored")   # never read as the receptionist (AEGIS F3 round 4)
            if (t.get("text") or "").strip():
                unknown_text.append(t.get("text"))  # never a fact, but never "nothing said" either (round 8)
                ordered.append(("unknown", Fraction(0), t.get("text")))   # ... and never a yes (round 12)
            continue
        (caller if speaker == "caller" else agent).append((_confidence(t.get("confidence")), t.get("text") or ""))
        ordered.append((speaker, _confidence(t.get("confidence")), t.get("text") or ""))
    spoken = [(c, x) for c, x in caller
              if x.strip() and not re.fullmatch(r"\[silence[\s:\-–—]*(?:\d+\s*(?:s|secs?|seconds)?)?\s*\]", x.strip(),
                                                re.IGNORECASE)]
    # "[silence - ...there's a fire]" carries words: it is speech, not silence (AEGIS round 6)
    trusted = [x for c, x in spoken if c >= LOW_CONFIDENCE]
    flagged = any(c < LOW_CONFIDENCE for c, _ in spoken)
    if flagged:
        notes.append("low_confidence_turn_not_used")

    rejected_numbers: set[str] = set()
    callback, corrected = _callback(trusted, notes, rejected_numbers)
    agent_trusted = [x for c, x in agent if c >= LOW_CONFIDENCE]       # a receptionist turn is checked too
    names = _names(trusted, agent_trusted)
    if not names and _names(trusted):
        notes.append("name_not_confirmed")
    name = names[0] if len(names) == 1 else None
    if name:                           # the caller said no after the confirmation ("No, I'm Ana Ruiz"): no name (round 8)
        at = [i for i, (sp, c, x) in enumerate(ordered) if sp == "agent" and c >= LOW_CONFIDENCE
              and (m := _CONFIRM_LINE.fullmatch(_fold(x).strip())) and m.group(1).casefold() == name.casefold()
              and _clean_confirmation(_fold(x))]
        # after the confirmation the caller may only affirm ("Yes.", "That's right", "Sí"); anything else ("Nah",
        # "Incorrecto", "Actually, I'm Ana Ruiz") withholds the name (AEGIS round 9: the inverted default)
        if not at or not _affirmed(ordered, at[-1]):     # and a "yes" must come (AEGIS round 10)
            name = None
            notes.append("name_denied")
    if name:                           # someone the caller speaks of, not the caller (AEGIS round 12)
        for i, (sp, c, x) in enumerate(ordered):
            at_name = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", _fold(x))
            if sp != "caller" or not at_name:
                continue
            x = _fold(x)[:at_name.end()]   # what the caller said up to the name
            prev_agent = next((y for sp2, _, y in reversed(ordered[:i]) if sp2 == "agent" and y.strip()), "")
            full = _fold(ordered[i][2])
            said_after = _fold(ordered[i][2])[at_name.end():] + " " + \
                " ".join(_fold(y) for sp2, _, y in ordered[i + 1:] if sp2 in ("caller", "unknown"))
            lead = any(m.group(1).casefold() == name.casefold() and m.start(1) == at_name.start()
                       for m in list(_LEAD_NAME.finditer(full)) + list(_WEAK_LEAD_NAME.finditer(full)))
            first_agent = next((y for sp2, _, y in ordered if sp2 == "agent" and y.strip()), "")
            said_before = " ".join(_fold(y) for sp2, _, y in ordered[:i] if sp2 in ("caller", "unknown")) + " " + x
            bare_ok = _ASKS_OWN.search(_fold(prev_agent)) or \
                (prev_agent == first_agent and not _OTHER_PERSON.search(said_before))
            # anyone else spoken of after the name, in that turn or later ("Mark Diaz, 602 ... He's my son"): the
            # name may be theirs (AEGIS round 14)
            if _OTHER_PERSON.search(said_after) or _THIRD_PARTY.search(_fold(x)) or _ASKS_OTHER.search(_ASKS_OWN.sub(" ", _fold(prev_agent))) or (not lead and not bare_ok):
                name = None
                notes.append("name_of_someone_else")
            break
    if len(names) > 1:
        notes.append("ambiguous_name:" + "|".join(names))

    spoken_agent = [(c, x) for c, x in agent if x.strip()]      # an empty receptionist turn is not a turn (round 7)
    readings = [_said_911(_fold(x), _fold(spoken_agent[i - 1][1]) if i else None)
                for i, (c, x) in enumerate(spoken_agent) if c >= LOW_CONFIDENCE]
    emergency = "urgent" in readings
    if not emergency and "ambiguous" in readings:
        notes.append("911_instruction_after_unfinished_turn")
    # the CHECK NOW net also reads low-confidence words: they are never a fact, but they can still be a warning
    # ... and the words of turns whose speaker label it cannot read (AEGIS round 8), and an ambiguous 911 instruction
    possible = not emergency and ("ambiguous" in readings or any(
        _EMERGENCY.search(_fold(x)) or _EMERGENCY_WORDS.search(_fold(x)) for x in [x for _, x in spoken] + unknown_text))
    if possible:
        notes.append("possible_emergency")
    # the callback is the number the receptionist read back in the fixed confirmation, that the caller gave and
    # then affirmed; anything else (no read-back, a read-back the caller did not affirm, a number the caller never
    # gave) is no callback and a person checks the recording (AEGIS round 10: the inverted default, after nine rounds
    # of phrase lists for rejected and corrected numbers)
    said_numbers = {"+1" + "".join(m.groups()) for x in trusted for m in _NUM.finditer(_fold(x))}
    read_back = None
    for i, (sp, c, x) in enumerate(ordered):
        fx = _fold(x).strip()
        nums = {"+1" + "".join(m.groups()) for m in _NUM.finditer(fx)}
        if sp == "agent" and c >= LOW_CONFIDENCE and _READ_BACK.fullmatch(fx) and _clean_confirmation(fx) and \
                len(nums) == 1:
            read_back = (nums.pop(), i)
    if read_back and read_back[0] in said_numbers and read_back[0] not in rejected_numbers and \
            _only_plainly_given(read_back[0], trusted, _names(trusted)) and \
            _affirmed(ordered, read_back[1]) and \
            (callback == read_back[0] or (callback is None and "ambiguous_number" not in notes)):
        # a caller-side doubt (an uncompletable correction, doubt after the number) is never overridden (round 11)
        if callback != read_back[0]:
            notes.append("callback_from_affirmed_read_back")
        callback = read_back[0]
    elif callback is not None:
        notes.append("callback_not_confirmed")
        callback = None
    confirmed = ["+1" + "".join(m) for x in agent_trusted for m in _NUM.findall(x)]
    if callback and confirmed and confirmed[-1] != callback:
        notes.append("confirmation_disagrees_with_caller")
    need = _need(trusted, pack, emergency, bool(spoken))
    if (flagged or unknown_text) and need == "long_silence":
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
    elif unknown_text and not callback:
        action = "Part of the call had a speaker label that could not be read; review the recording."
    elif need == "long_silence" and not spoken and not unknown_text and not callback and "ambiguous_number" not in notes \
            and "callback_not_confirmed" not in notes:
        action = "No follow-up needed."    # only a call where nothing was said (AEGIS round 7: the inverted default)
    elif need in ("spam_call", "wrong_number") and not callback and "ambiguous_number" not in notes \
            and "callback_not_confirmed" not in notes:
        action = "Looks like spam or a wrong number; a person should confirm before closing it."
    elif callback:
        action = f"Call back at {callback}."
    elif "ambiguous_number" in notes or "callback_not_confirmed" in notes:
        action = "The callback number was not confirmed; check the recording before calling back."
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
