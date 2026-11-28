"""RR-F3 call-summarizer set: 300 two-sided transcripts built on the call bank (test-set spec v0, F3).

Each transcript is what the summarizer receives after a call: the receptionist's turns and the caller's turns, with
speech-recognition confidence per turn. The summary must keep the key facts (name, callback number, need, urgency),
add nothing the caller didn't say, never carry a health detail, take the CORRECTED value when the caller corrects
themselves, and flag a low-confidence turn instead of trusting it.

Cases: every call-bank scenario; health details (forbidden terms); the caller's own correction; a speech-recognition
error (a low-confidence turn with a garbled number, then a clean repeat); emergencies (urgency = emergency).
"""

from __future__ import annotations

from . import call_bank, common
from .common import Stream

POOL_ID = "frontdesk-f3-transcripts"
COUNT = 300
LOW_CONFIDENCE = 0.6     # a SPOKEN turn below this is untrusted: flag it, never use it as a fact. A silent turn
# (no words, confidence 0.0) carries nothing to trust or distrust and is not flagged; confirm at the spec freeze.


def _spoken(e164: str) -> str:
    d = e164[2:]
    return f"{d[0:3]} {d[3:6]} {d[6:]}"


def generate(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{POOL_ID}|{seed}")
    calls, ckey = call_bank.generate(seed)
    picked = root.child("pick").shuffled(list(range(len(calls))))[:COUNT]
    transcripts, key = [], []
    for n, idx in enumerate(picked):
        call, k = calls[idx], ckey[idx]
        s = root.child(f"t{n}")
        es = call["language"] == "es"
        turns = [{"speaker": "agent", "confidence": 0.98,
                  "text": ("Gracias por llamar. Esta llamada puede ser grabada. ¿En qué le puedo ayudar?" if es else
                           "Thanks for calling. This call may be recorded. How can I help?")}]
        for t in call["turns"]:
            if t["behavior"] == "silence_8s":
                turns.append({"speaker": "caller", "confidence": 0.0, "text": "", "silence_seconds": 8})
                continue
            turns.append({"speaker": "caller", "confidence": round(0.85 + s.below(14) / 100, 2), "text": t["text"]})
        cap = k["must_capture"]
        asr_error = bool(cap) and s.chance(1, 6)
        if cap:
            number = _spoken(cap["callback_number"])
            if asr_error:
                # the recogniser garbles the number once; the caller repeats it cleanly when asked
                # a full, plausible, WRONG 10-digit number: trusting this turn gives a wrong callback number
                last2 = int(number[-2:])
                garbled = number[:-2] + f"{(last2 + s.between(1, 9)) % 100:02d}"
                turns.append({"speaker": "agent", "confidence": 0.97,
                              "text": "¿Me repite el número, por favor?" if es else "Sorry, could you repeat the number?"})
                turns.append({"speaker": "caller", "confidence": round(0.3 + s.below(25) / 100, 2), "text": garbled})
                turns.append({"speaker": "caller", "confidence": 0.95, "text": number})
            turns.append({"speaker": "agent", "confidence": 0.97,
                          "text": (f"Confirmo: {cap['name']}, al {number}." if es else
                                   f"Let me confirm: {cap['name']}, at {number}.")})
            turns.append({"speaker": "caller", "confidence": 0.96, "text": "Sí." if es else "Yes."})
            # the fixed consent question (Andre's ruling, Nov 27 2026): only a yes to it sets the callback number
            turns.append({"speaker": "agent", "confidence": 0.97,
                          "text": (f"¿Es el {number} el mejor número para devolverle la llamada?" if es else
                                   f"Is {number} the best number to call you back?")})
            turns.append({"speaker": "caller", "confidence": 0.96, "text": "Sí." if es else "Yes."})
        if k["emergency"]:
            turns.append({"speaker": "agent", "confidence": 0.98,
                          "text": ("Cuelgue y marque al 911 ahora mismo." if es else
                                   "Please hang up and dial 911 right now.")})
        tid = common.opaque_id("tr", seed, n)
        transcripts.append({"fixture": common.FIXTURE_LABEL, "pool": POOL_ID, "transcript_id": tid,
                            "language": call["language"], "vertical": call["vertical"], "turns": turns})
        key.append({
            "transcript_id": tid,
            "key_facts": {"name": cap.get("name"), "callback_number": cap.get("callback_number"),
                          "need": cap.get("reason_category") or k["scenario"],
                          "urgency": "emergency" if k["emergency"] else "routine"},
            "must_flag_low_confidence": asr_error,
            "corrected_value_must_win": k["corrected_callback"],
            "forbidden_terms_in_summary": list(k["forbidden_terms_in_record"]),
            "must_not": ["invent_fact", "include_health_detail", "use_low_confidence_turn_as_fact"],
        })
    return transcripts, key
