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
REASONS = ("opted_out", "possible_opt_out", "start_keyword_does_not_restore", "carrier_opt_out", "number_reassigned",
           "no_written_consent", "invalid_number", "unknown_message_class")
_E164 = re.compile(r"\+1[2-9]\d{2}[2-9]\d{6}")


def normalise_number(raw: object) -> Optional[str]:
    """A North American number as +1NXXNXXXXXX, or None when it is not one (never guessed)."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"[\s().\-]", "", raw.strip())
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
        kind = e.get("kind")
        if kind not in KINDS:
            raise ValueError(f"event {i}: unknown kind {kind!r}")
        if not isinstance(e.get("t"), int) or isinstance(e.get("t"), bool):
            raise ValueError(f"event {i}: t must be an integer")
        if not isinstance(e.get("client_id"), str) or not e["client_id"]:
            raise ValueError(f"event {i}: client_id is required")
        if kind == "send_attempt" and (not isinstance(e.get("send_id"), str) or not e["send_id"]):
            raise ValueError(f"event {i}: send_id is required")
        if kind == "inbound_sms" and not isinstance(e.get("text"), str):
            raise ValueError(f"event {i}: text is required")

    def replay(self, events: Iterable[dict]) -> list[Decision]:
        """Decide every send_attempt in ``events`` (any order). Raises ValueError on a malformed event: the replay
        refuses as a whole rather than decide on part of the record."""
        rows = list(events)
        for i, e in enumerate(rows):
            self._validate(e, i)
        order = sorted(range(len(rows)), key=lambda i: (rows[i]["t"], _ORDER[rows[i]["kind"]], i))
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
        if number is None:
            return       # a state change for an unreadable number can never unblock anything; sends to it block
        rec = self._record(e["client_id"], number)
        kind = e["kind"]
        if kind == "opt_in":
            if e.get("consent") != "express_written":
                return   # only express written consent counts; anything weaker is not recorded as consent
            rec.written_consent_at = e["t"]
            if rec.suppressed in ("opted_out", "possible_opt_out", "start_keyword_does_not_restore",
                                  "number_reassigned"):
                rec.suppressed, rec.suppressed_at = None, None
            rec.evidence = rec.evidence + [i] if rec.carrier_blocked else [i]
        elif kind == "inbound_sms":
            level = channels.opt_out_level(e["text"])
            if level is not None:
                self._suppress(rec, "opted_out" if level == "exact" else "possible_opt_out", e["t"], i)
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
        rec = self._records.get((e["client_id"], number), Record())
        if rec.carrier_blocked:
            return Decision(sid, "block", "carrier_opt_out", e["t"], tuple(rec.evidence) + (i,))
        if rec.suppressed is not None:
            return Decision(sid, "block", rec.suppressed, e["t"], tuple(rec.evidence) + (i,))
        if mclass == "marketing" and rec.written_consent_at is None:
            return Decision(sid, "block", "no_written_consent", e["t"], (i,))
        return Decision(sid, "allow", None, e["t"], tuple(rec.evidence) + (i,))
