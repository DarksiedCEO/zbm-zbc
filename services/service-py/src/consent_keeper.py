"""
RR-F5 — the consent / STOP keeper (MVP spec v0.1, front-desk team). One job: hold the single consent and opt-out
record for every (client business, phone number) pair, and answer "may this text or call go out now?" for every send
the front-desk team makes. Deterministic; no LLM; no network; nothing is sent from here.

Rules (spec v0.1, the Nov 26 spec-freeze rulings, ADR 0014):
- Every send is checked. A send this keeper cannot decide (bad number, unknown message class) is BLOCKED.
- An opt-out wins: the reading of an inbound text is ``channels.opt_out_level``, the same classifier Customer
  Service already uses, so the whole company reads "STOP" one way. ``exact`` -> ``opted_out``; ``suspected`` (one
  typo of stop / unsubscribe) -> ``possible_opt_out``; both block (STOP typos count as an opt-out: fail toward
  suppression).
- Calls are decided here too, so calls-only revocations also block (``opted_out``): inside one clause, a refusal word
  ("don't", "no", "never", "stop", "dejen", "nao", "ne" ...) within five words before a calling word ("call", "calls",
  "phone", "contact", "llamar" and its forms, "ligar", "appeler" ...), read after leet and doubled letters are undone,
  and "DNC" on its own. This fails toward suppression: "no calls on Sunday" blocks. The wording Customer Service
  hands to a human ("opt me out") blocks too (``possible_opt_out``).
- A state-changing event (opt-in, inbound text, carrier opt-out, reassignment) for a number that cannot be read, a
  non-object event, or a send_id used twice refuses the whole replay (``ValueError``): an opt-out is never dropped.
  Numbers are read after NFKC with any script's digits, dash look-alikes and invisibles folded, so one person never
  becomes two records. Client ids are read with NFC (not NFKC), every format character removed, dashes folded and
  ASCII letters lower-cased: one client typed differently is one record, two different clients never merge.
- Every ``replay`` is the whole history: no state carries over between calls.
- Express written consent counts only from a known source (web form, paper form, keyword join, signed agreement,
  e-sign); a START reply is never one.
- Within one instant every state change is applied before any send at that instant, and opt-outs after opt-ins, so
  a STOP and a send in the same second never lets the send through, whatever order the events arrive in.
- HELP changes nothing. START / UNSTOP / YES and the like never restore consent (ZBM rule); the block's reason
  becomes ``start_keyword_does_not_restore`` so the owner can see why. Only a NEW express written consent, recorded
  after the opt-out, restores texting.
- A carrier opt-out blocks and is never cleared by our own records (the carrier holds it). A reassigned number
  (reassigned-numbers database) kills consent; only a new express written consent recorded after it restores it.
- Marketing needs express written consent. An informational reply to the person who contacted the business does not,
  but every block above still applies to it.

Consent is held per (client, number): a STOP sent to one client's number never silently governs another client's
texts, and one client's consent never covers another's.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

import channels

KINDS = ("opt_in", "inbound_sms", "carrier_opt_out", "number_reassigned", "send_attempt")
MESSAGE_CLASSES = ("informational", "marketing")
# Within one instant: consent first, then what a person said, then hard suppression, then sends.
_ORDER = {"opt_in": 0, "inbound_sms": 1, "carrier_opt_out": 2, "number_reassigned": 2, "send_attempt": 3}
# Keywords that ask to resume. None of them restores consent; they only change the reason on the block.
RESTART_WORDS = frozenset({"start", "unstop", "yes", "subscribe", "resume", "resubscribe", "optin", "opt in",
                           "unblock", "si", "reanudar", "comenzar"})
# Consent is express written only when it comes from one of these (a START reply is never one of them).
CONSENT_SOURCES = frozenset({"web_form", "paper_form", "keyword_join", "signed_agreement", "esign"})
# Revocations the text classifier deliberately leaves to the phone side: this keeper decides texts AND calls, so any
# of these blocks both (fail toward suppression). Matched on the normalised text, as whole words.
# Calls-only revocations (AEGIS round 2: "I don't want any calls", "no me llame", "d0nt c4ll", PT/FR): inside one
# clause, a refusal word within five words before a calling word. Read after leet and repeated letters are undone.
_CALL_WORD = re.compile(r"^(?:call|calls|called|calling|cal|cals|caling|phone|phoned|phoning|telephone|ring|ringing|"
                        r"contact|contacted|contacting|dial|llam\w*|contact\w*|marqu\w*|telefon\w*|ligu\w*|ligar|"
                        r"lig\w*|appel\w*|mensaj\w*|escrib\w*|manden|mandes|env[ií]en|text|texts|texting|message|messages|"
                        r"habl\w*|marc\w*|yam\w*|phonecall\w*|voicemail\w*)$")
_REFUSAL = frozenset({"dont", "don't", "not", "no", "never", "stop", "quit", "cease", "nao", "não", "ne", "nunca",
                      "dejen", "deje", "deja", "basta", "without", "dnc", "nada", "paren", "pare", "sin", "cero",
                      "ninguna", "ninguno", "enough", "nope", "nah", "noo"})
# Words about being contacted at all, for the whole-message net below (calls, texts, voicemail, lists, numbers, being
# bothered), English and Spanish, after leet and doubled letters are undone.
_CONTACT_WORD = re.compile(r"^(?:call\w*|cal|cals|phone\w*|telephone\w*|ring\w*|contact\w*|dial\w*|voicemail\w*|"
                           r"text\w*|message\w*|msg\w*|sms|list|lists|number|numbers|robocall\w*|"
                           r"llam\w*|lam\w*|yam\w*|marc\w*|habl\w*|telefon\w*|mensaj\w*|escrib\w*|molest\w*|"
                           r"list\w*|n[uú]mero\w*|contact\w*|lig\w*|appel\w*|mand\w*|env[ií]\w*)$")
_SPANISH_REMOVE = re.compile(r"\b(?:s[aá]quen|qu[ií]ten|b[oó]rren|eliminen|borra|quita|saca|den de baja)\w*\b.*"
                             r"\b(?:lista|n[uú]mero|base de datos|registro)\b|\bno me (?:molest|interes)\w*|"
                             r"\bno (?:estoy|estamos) interesad", re.IGNORECASE)
_NO_PHONE_EMOJI = ("\U0001F4F5", "\U0001F515")        # no mobile phones, bell with slash
# Every apostrophe look-alike read as "'" before anything else reads the text (AEGIS round 4: "Don\u2018t", "Don`t",
# "don\u00b4t" split into "don t" and matched no refusal word)
_APOSTROPHES = dict.fromkeys(map(ord, "\u2018\u2019\u201b\u0060\u00b4\u02b9\u02bb\u02bc\u02bd\u02c8\u2032\u05f3\ua78c"
                                      "\uff07\u055a\u1fbd\u1fbf"), "'")
# Revocation wording in any grammatical form (AEGIS round 4: "I withdraw my consent", "I'm opting out", "I no longer
# wish to receive these", "Retiro mi consentimiento", "Déjenme en paz", "Ya basta"), plus the common French, German,
# Italian, Portuguese and Chinese opt-outs. Read on the text with accents removed and apostrophes unified. Fails
# toward suppression: "No more questions, thanks" blocks too.
_REVOCATION = re.compile(
    r"\brevok|\brevoc|\bwithdr[ae]w\w*\b.{0,20}\bconsent|\brescind|\bopt(?:s|ing|ed)?[\s\-]*out\b|\bunsub|"
    r"\bno longer (?:wish|want|consent|interested|need|like)|\b(?:do not|don'?t|dont|no longer) consent\b|"
    r"\bnot consent\b|\brefrain\b|\bremove me\b|\bbe removed\b|\btake me off\b|\bcease\b|\bleave me alone|"
    r"\bno more\b|\bstop (?:sending|messaging|bothering|contacting)|\bquit (?:sending|texting|messaging)|"
    r"\bdesuscri|\bdesinscri|\bdesapunt|\bdar(?:me|nos)? de baja|\bde baja\b|\bretir\w* (?:mi |el )?consentimiento|"
    r"\b(?:ya )?no (?:doy|autorizo)|\bdej(?:en|e|a)me en paz|\bno mas\b|\bya basta|\bbasta ya|\bno quiero saber nada|"
    r"\bno quiero (?:recibir|mas)|\bya no (?:quiero|me)|\barret(?:ez|er|e)\b|\bdesabonn|\bdesinscri|\bstopp\b|"
    r"\bnicht mehr\b|\babmelden|\bkeine (?:nachrichten|sms|anrufe|werbung)|\bnon (?:scrivermi|chiamarmi|contattarmi|"
    r"voglio)|\bcancellami|\bdisiscriv|\bpar(?:e|em) de\b|\bdescadastr|\bsair da lista|退订|取消订阅|不要再|别再")


def _unify(text: str) -> str:
    return text.translate(_APOSTROPHES)


def _plain(text: str) -> str:
    """Lower-case, apostrophes unified, accents removed: the text the revocation wording is read on."""
    text = unicodedata.normalize("NFKD", _unify(_ascii_digits(text)).lower())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())
_CLAUSE = re.compile(r"[.!?;,\n\u3002\uff0c]+")


def _no_contact(text: str) -> bool:
    if re.fullmatch(r"\W*d\W*n\W*c\W*", channels.normalise(_ascii_digits(text)).strip() or "x"):
        return True                                       # "DNC", "D.N.C.", "d-n-c" on their own
    for raw_clause in _CLAUSE.split(_ascii_digits(text)):
        folded = channels.normalise(channels._leet(raw_clause))
        if re.search(r"\bd\s*n\s*c\b", folded):
            return True
        words = folded.split()
        for i, w in enumerate(words):
            calls = _CALL_WORD.match(w) or _CALL_WORD.match(re.sub(r"(.)\1+", r"\1", w)) or w.startswith("dontcal") \
                or re.match(r"^m(?:appel|ap+el)", w)                      # French "m'appelez" folds to "mappelez"
            if calls and any(x in _REFUSAL for x in words[max(0, i - 5):i]):
                return True
    return False


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d"), "-")
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")


def _strip_invisible(text: str) -> str:
    """Every format character (Unicode Cf: bidi marks and isolates, joiners, tags, U+061C, U+180E ...), the
    combining grapheme joiner and the variation selectors."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf" and c != "\u034f"
                   and not ("\ufe00" <= c <= "\ufe0f" or "\U000e0100" <= c <= "\U000e01ef"))
REASONS = ("opted_out", "possible_opt_out", "start_keyword_does_not_restore", "carrier_opt_out", "number_reassigned",
           "no_written_consent", "invalid_number", "unknown_message_class")
_E164 = re.compile(r"\+1[2-9][0-9]{2}[2-9][0-9]{6}")


def _ascii_digits(text: str) -> str:
    """NFKC, then every Unicode decimal digit as its ASCII digit, dash look-alikes as "-", invisibles removed."""
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).translate(_DASHES)
    return "".join(str(unicodedata.decimal(c)) if not c.isascii() and unicodedata.decimal(c, None) is not None
                   else c for c in text)


def normalise_client(raw: str) -> str:
    """One client id, however it was typed upstream: NFC (not NFKC, so "acme2" and "acme\u00b2" stay two clients),
    every invisible removed, dashes folded, trimmed, lower-cased letter by letter (\u00c9cole and \u00e9cole are one client;
    not case-folded, so "strasse" and "stra\u00dfe" stay two clients; a letter whose lower case is longer than one
    letter keeps its case)."""
    folded = _strip_invisible(unicodedata.normalize("NFC", raw)).translate(_DASHES).strip()
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in folded)


def normalise_number(raw: object) -> Optional[str]:
    """A North American number as +1NXXNXXXXXX, or None when it is not one (never guessed)."""
    if not isinstance(raw, str):
        return None
    text = _ascii_digits(raw).strip()
    if text.lower().startswith("tel:"):
        text = text[4:]
    digits = re.sub(r"[\s().\-/]", "", text)
    if digits.startswith("+"):
        cand = digits
    elif len(digits) == 11 and digits.startswith("1"):
        cand = "+" + digits
    elif len(digits) == 10:
        cand = "+1" + digits
    else:
        return None
    return cand if _E164.fullmatch(cand) else None


def _restart_word(text: str) -> bool:
    norm = channels.normalise(text).strip()
    return norm in RESTART_WORDS


def _refusal_near_contact(text: str) -> bool:
    """The whole-message net: any refusal word and any contact word anywhere in the same message, in either order
    ("Call me never", "llamadas no", "I really do not want you or anyone from your office to call me")."""
    folded = channels.normalise(channels._leet(_ascii_digits(text)))
    words = [w for raw in folded.split() for w in {raw, re.sub(r"(.)\1+", r"\1", raw)}]
    # a refusal glued to the contact word ("dontcall", "nocalls", "nevertext")
    if any(re.match(r"^(?:dont|donot|no|never|stop)(?:call|cal|text|txt|contact|message|phone|ring)", w) for w in words):
        return True
    return any(w in _REFUSAL for w in words) and any(_CONTACT_WORD.match(w) for w in words)


def opt_out_reading(text: str) -> Optional[str]:
    """``opted_out``, ``possible_opt_out`` or None. Customer Service's SMS classifier first (exact / suspected), then
    the wording it hands to a human (``possible_opt_out``: on SMS that still blocks), then calls-only revocations
    ("do not call me", "DNC", "no me llamen"), which this keeper must honour because it also decides calls."""
    text = _unify(text)
    level = channels.opt_out_level(text)
    if level == "exact":
        return "opted_out"
    if level == "suspected" or channels.possible_opt_out(text):
        return "possible_opt_out"
    if _no_contact(text) or any(e in text for e in _NO_PHONE_EMOJI) or _REVOCATION.search(_plain(text)) or \
            _SPANISH_REMOVE.search(channels.normalise(_ascii_digits(text))):
        return "opted_out"
    if _refusal_near_contact(text):
        return "possible_opt_out"     # fail toward suppression: a refusal anywhere near a contact word blocks
    return None


@dataclass
class Record:
    written_consent_at: Optional[int] = None
    suppressed: Optional[str] = None          # a reason in REASONS, or None
    suppressed_at: Optional[int] = None
    carrier_blocked: bool = False
    evidence: list = field(default_factory=list)   # indices of the events that set the current state


@dataclass(frozen=True)
class Decision:
    send_id: str
    decision: str                 # "allow" | "block"
    reason: Optional[str]         # None on allow
    t: int
    evidence: tuple               # event indices (input order) the decision rests on


class ConsentKeeper:
    """Replays consent events and decides every send. ``replay`` is the one entry point; it never sends anything."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], Record] = {}

    def _record(self, client: str, number: str) -> Record:
        return self._records.setdefault((client, number), Record())

    @staticmethod
    def _validate(e: dict, i: int) -> None:
        if not isinstance(e, dict):
            raise ValueError(f"event {i}: not an object")
        kind = e.get("kind")
        if kind not in KINDS:
            raise ValueError(f"event {i}: unknown kind {kind!r}")
        if not isinstance(e.get("t"), int) or isinstance(e.get("t"), bool):
            raise ValueError(f"event {i}: t must be an integer")
        if not isinstance(e.get("client_id"), str) or not e["client_id"]:
            raise ValueError(f"event {i}: client_id is required")
        if kind == "send_attempt" and (not isinstance(e.get("send_id"), str) or not e["send_id"]):
            raise ValueError(f"event {i}: send_id is required")
        if kind == "opt_in" and e.get("source") is not None and not isinstance(e["source"], str):
            raise ValueError(f"event {i}: source must be text")
        if kind == "opt_in" and e.get("consent") is not None and not isinstance(e["consent"], str):
            raise ValueError(f"event {i}: consent must be text")
        if kind == "inbound_sms" and not isinstance(e.get("text"), str):
            raise ValueError(f"event {i}: text is required")
        if kind != "send_attempt" and normalise_number(e.get("number")) is None:
            # AEGIS Inquisitor c08c3f3f H1: an opt-out for a number we cannot read must not vanish; the replay refuses
            raise ValueError(f"event {i}: unreadable number on a {kind} event")

    def replay(self, events: Iterable[dict]) -> list[Decision]:
        """Decide every send_attempt in ``events`` (any order). Raises ValueError on a malformed event: the replay
        refuses as a whole rather than decide on part of the record."""
        rows = list(events)
        for i, e in enumerate(rows):
            self._validate(e, i)
        seen: set[str] = set()
        for i, e in enumerate(rows):
            if e["kind"] == "send_attempt":
                if e["send_id"] in seen:
                    raise ValueError(f"event {i}: send_id {e['send_id']!r} appears twice")
                seen.add(e["send_id"])
        # AEGIS Inquisitor c08c3f3f H2: every replay is the whole history; nothing carries over from an earlier call
        self._records = {}
        # at one instant an opt-out reading applies after a restart word, so the reason does not depend on input order
        rank = {i: 1 if e["kind"] == "inbound_sms" and opt_out_reading(e["text"]) else 0 for i, e in enumerate(rows)}
        order = sorted(range(len(rows)), key=lambda i: (rows[i]["t"], _ORDER[rows[i]["kind"]], rank[i], i))
        out: list[Decision] = []
        for i in order:
            e = rows[i]
            if e["kind"] == "send_attempt":
                out.append(self._decide(e, i))
            else:
                self._apply(e, i)
        return out

    def _apply(self, e: dict, i: int) -> None:
        number = normalise_number(e.get("number"))
        rec = self._record(normalise_client(e["client_id"]), number)     # _validate refused unreadable numbers
        kind = e["kind"]
        if kind == "opt_in":
            if e.get("consent") != "express_written" or e.get("source") not in CONSENT_SOURCES:
                return   # only express written consent from a known source counts; anything else is not consent
            rec.written_consent_at = e["t"]
            if (rec.suppressed_at is None or e["t"] > rec.suppressed_at) and rec.suppressed in ("opted_out", "possible_opt_out", "start_keyword_does_not_restore",
                                  "number_reassigned"):
                rec.suppressed, rec.suppressed_at = None, None
            rec.evidence = rec.evidence + [i] if rec.carrier_blocked else [i]
        elif kind == "inbound_sms":
            reading = opt_out_reading(e["text"])
            if reading is not None:
                self._suppress(rec, reading, e["t"], i)
            elif _restart_word(e["text"]) and rec.suppressed in ("opted_out", "possible_opt_out"):
                rec.suppressed = "start_keyword_does_not_restore"
                rec.evidence = rec.evidence + [i]
            # HELP, look-alike words and anything else change nothing
        elif kind == "carrier_opt_out":
            rec.carrier_blocked = True
            self._suppress(rec, "carrier_opt_out", e["t"], i)
        elif kind == "number_reassigned":
            rec.written_consent_at = None
            self._suppress(rec, "number_reassigned", e["t"], i)

    @staticmethod
    def _suppress(rec: Record, reason: str, t: int, i: int) -> None:
        if rec.carrier_blocked and reason != "carrier_opt_out":
            rec.evidence = rec.evidence + [i]
            return       # the carrier block stays the reason: our own records cannot lift it
        rec.suppressed, rec.suppressed_at = reason, t
        rec.evidence = [i]

    def _decide(self, e: dict, i: int) -> Decision:
        sid = e["send_id"]
        number = normalise_number(e.get("number"))
        if number is None:
            return Decision(sid, "block", "invalid_number", e["t"], (i,))
        mclass = e.get("message_class")
        if mclass not in MESSAGE_CLASSES:
            return Decision(sid, "block", "unknown_message_class", e["t"], (i,))
        rec = self._records.get((normalise_client(e["client_id"]), number), Record())
        if rec.carrier_blocked:
            return Decision(sid, "block", "carrier_opt_out", e["t"], tuple(rec.evidence) + (i,))
        if rec.suppressed is not None:
            return Decision(sid, "block", rec.suppressed, e["t"], tuple(rec.evidence) + (i,))
        if mclass == "marketing" and rec.written_consent_at is None:
            return Decision(sid, "block", "no_written_consent", e["t"], (i,))
        return Decision(sid, "allow", None, e["t"], tuple(rec.evidence) + (i,))
