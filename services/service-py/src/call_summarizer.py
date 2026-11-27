"""
RR-F3 — call summarizer (MVP spec v0.1, front-desk team). One job: turn one call transcript into a short, correct
summary for the business owner: who called (name and callback number), what they need (one category from a fixed
list, never their own words), how urgent it is, and the next action. Deterministic; no LLM.

Rules (spec v0.1, the front-desk shared rules, the Nov 26 spec-freeze rulings):
- Nothing is added that the caller did not say. Facts come only from the CALLER's turns that speech recognition
  trusts (confidence >= 0.6); the receptionist's own confirmation line is checked against them, never used as the
  source. A turn below 0.6 that has words is flagged and never used as a fact. A silent turn carries nothing and is
  not flagged (Nov 26 ruling): silence means re-ask, never guess.
- When the caller corrects themselves, the last number they gave wins.
- A name is reported only when exactly one name was given; two different names are reported as ambiguous, never
  picked between.
- No health detail ever reaches the summary: the summary is built only from the name, the number, a category from
  the fixed list and the urgency, never from the caller's free text, and it is checked against the health lexicon.
- Urgency is "emergency" when the receptionist gave the 911 instruction or the caller described an emergency
  (someone not breathing or collapsed, gas, smoke or fire, a weapon); otherwise "routine".
- The need is one label: the pack's own category (its booking category for a booking, ``treatment_question`` for a
  health matter, ``pricing_question`` for a priced service, ``pricing`` for a price the pack does not hold) or one of
  the front desk's fixed labels (emergency, complaint, existing_customer, reschedule, hours, vendor, wrong_number,
  spam_call, long_silence). When nothing matches, ``unclear`` (never a guess).
"""

from __future__ import annotations

import re
import unicodedata
from fractions import Fraction
from dataclasses import dataclass, field
from typing import Optional

LOW_CONFIDENCE = Fraction(3, 5)      # 0.6, kept exact: this service has no float literals (guardrail test)
FIXED_NEEDS = ("emergency", "complaint", "existing_customer", "reschedule", "hours", "vendor", "wrong_number",
               "spam_call", "long_silence", "pricing", "pricing_question", "treatment_question", "unclear")

_NUM = re.compile(r"(?<!\d)\(?([2-9]\d{2})\)?[ .\-]?([2-9]\d{2})[ .\-]?(\d{4})(?!\d)")
_WORD = r"[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü'\-]+"
_NAME = re.compile(rf"(?<![\w'])({_WORD} {_WORD})(?![\w'])")
# Lead-ins that introduce a name; removed before the two-capitalised-words reading so "I'm Ana Reyes" reads "Ana Reyes"
_LEAD_IN = re.compile(r"\b(?:I'm|I am|it's|it is|this is|my name is|me llamo|soy|habla|mi nombre es)\b",
                      re.IGNORECASE)
_GREETING = re.compile(r"^(?:\W*(?:hello|hi|hey|hola|bueno|al[oó]|yes|s[ií]|okay|ok)\W*)+$", re.IGNORECASE)
_NOT_NAME_START = {"Hi", "Hello", "Hola", "Okay", "Ok", "Yes", "Thanks", "Thank", "Gracias", "Please", "Sorry",
                   "Wait", "Oiga", "Bueno", "Muy", "Me", "Mi", "My", "This", "It", "Is", "Es", "Hay", "There",
                   "Someone", "Alguien", "Congratulations", "Felicidades", "Ignore", "Ignora", "Give", "Dame",
                   "Let", "Confirmo", "How", "What", "Can", "Could", "I", "No", "Not", "Est", "Está", "Estoy"}
_EMERGENCY = re.compile(
    r"not breathing|n[o']t breathing right|collapsed|unconscious|chest pain|can'?t breathe|bleeding|"
    r"smell gas|gas leak|smoke|on fire|fire\b|knife|gun\b|threatening|"
    r"desmay|no est[aá] respirando|respirar|huele .*gas|fuga de gas|humo|quemando|incendio|cuchillo|pistola|"
    r"amenazando", re.IGNORECASE)
_HEALTH = ("pregnant", "blood thinner", "diabetes", "seizure", "embarazada", "anticoagulante", "convulsi",
           "cancer", "cáncer", "hiv", "vih", "medication", "medicamento", "surgery", "cirugía", "allergic", "alérgic")
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
    if any(term in low for term in _HEALTH):
        return "treatment_question"
    for label, pat in _RULES:
        if re.search(pat, joined, re.IGNORECASE):
            if label == "price":
                services = [s.replace("_", " ") for s in (pack.get("services") or {})]
                return "pricing_question" if any(s in low for s in services) else "pricing"
            if label == "booking":
                cats = pack.get("reason_categories") or []
                return cats[0] if cats else "booking"
            return label
    if not any_words or all(_GREETING.fullmatch(t.strip()) for t in trusted):
        return "long_silence"      # nothing but a greeting into silence
    return "unclear"


def summarize(transcript: dict, pack: Optional[dict] = None) -> Summary:
    tid = transcript.get("transcript_id")
    if not isinstance(tid, str) or not tid:
        raise ValueError("transcript_id is required")
    turns = transcript.get("turns")
    if not isinstance(turns, list):
        raise ValueError("turns must be a list")
    pack = pack or {}
    notes: list[str] = []
    caller, agent = [], []
    for t in turns:
        if not isinstance(t, dict) or not isinstance(t.get("text", ""), str):
            raise ValueError("every turn needs a text")
        conf = t.get("confidence")
        conf = conf if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0
        (caller if t.get("speaker") == "caller" else agent).append((conf, t.get("text") or ""))
    spoken = [(c, x) for c, x in caller if x.strip() and not re.fullmatch(r"\[silence[^\]]*\]", x.strip())]
    trusted = [x for c, x in spoken if c >= LOW_CONFIDENCE]
    flagged = any(c < LOW_CONFIDENCE for c, _ in spoken)
    if flagged:
        notes.append("low_confidence_turn_not_used")

    numbers = ["+1" + "".join(m) for x in trusted for m in _NUM.findall(x)]
    callback = numbers[-1] if numbers else None
    corrected = len(set(numbers)) > 1
    names = []
    for x in trusted:
        for m in _NAME.finditer(_LEAD_IN.sub(" ", _fold(x))):
            first = m.group(1).split()[0]
            if first not in _NOT_NAME_START and m.group(1) not in names:
                names.append(m.group(1))
    name = names[0] if len(names) == 1 else None
    if len(names) > 1:
        notes.append("ambiguous_name:" + "|".join(names))

    agent_text = " ".join(x for _, x in agent)
    said_911 = "911" in agent_text
    emergency = said_911 or any(_EMERGENCY.search(_fold(x)) for x in trusted)
    confirmed = ["+1" + "".join(m) for _, x in agent for m in _NUM.findall(x)]
    if callback and confirmed and confirmed[-1] != callback:
        notes.append("confirmation_disagrees_with_caller")
    need = _need(trusted, pack, emergency, bool(spoken))
    urgency = "emergency" if emergency else "routine"

    if emergency:
        action = "Caller was told to hang up and dial 911; alert the owner now."
    elif need in ("spam_call", "wrong_number", "long_silence"):
        action = "No follow-up needed."
    elif callback:
        action = f"Call back at {callback}."
    else:
        action = "No callback number given; no follow-up possible by phone."
    who = name or ("caller (ambiguous name)" if len(names) > 1 else "caller (no name given)")
    text = (f"{'URGENT: ' if emergency else ''}{who}"
            f"{', ' + callback if callback else ''} - {need.replace('_', ' ')}. {action}"
            f"{' A low-confidence turn was ignored.' if flagged else ''}")
    low = text.lower()
    if any(term in low for term in _HEALTH):          # cannot happen by construction; refuse rather than leak
        raise RuntimeError("health term in summary")
    return Summary(tid, name, callback, need, urgency, action, flagged, corrected, text, tuple(notes))
