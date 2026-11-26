"""Independent re-derivation of the F2/F4/F5 answer keys from the events alone (see oracle.py for the idea).

Written from the rules, not from the generator: a reference consent keeper that replays the event stream, a reference
text-back decider and a reference transfer router. Any disagreement with the planted key is reported.
"""

from __future__ import annotations

import re

from . import common

from .verticals import VERTICALS

OPT_OUT_EXACT = {"stop", "stop.", "stop!", "unsubscribe", "cancel", "end", "quit", "revoke", "opt out",
                 "stop texting me", "alto", "baja"}
HELP_WORDS = {"help", "info"}
RESTART_WORDS = {"start", "unstop", "yes"}
QUIET_START, QUIET_END = 21 * 60, 8 * 60      # sends allowed 08:00 <= t < 21:00 recipient-local


def _osa_distance(a: str, b: str) -> int:
    """Edit distance with adjacent transpositions (optimal string alignment)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _looks_like_stop(text: str) -> bool:
    """A near-miss of STOP (typo, swapped letters, a zero for the o, extra words after it) counts as an opt-out;
    other short words (STAR, Stay, Spot) do not."""
    words = text.strip().lower().replace("0", "o").split()
    return bool(words) and (words[0].startswith("stop") or _osa_distance(words[0], "stop") <= 1)


def decide_f5(events: list[dict]) -> dict[str, tuple[str, str | None]]:
    state: dict[tuple[str, str], dict] = {}
    decisions: dict[str, tuple[str, str | None]] = {}
    by_time: dict[int, list[dict]] = {}
    for e in events:
        by_time.setdefault(e["t"], []).append(e)
    for t in sorted(by_time):
        batch = by_time[t]
        # Within one instant, every consent change applies before any send (the opt-out wins the race).
        for e in sorted(batch, key=lambda x: x["kind"] == "send_attempt"):
            k = (e["client_id"], e["number"])
            st = state.setdefault(k, {"written": False, "blocked": None})
            if e["kind"] == "opt_in":
                if e["consent"] == "express_written":
                    st["written"], st["blocked"] = True, None
            elif e["kind"] == "inbound_sms":
                text = e["text"].strip().lower()
                if text in OPT_OUT_EXACT:
                    st["written"], st["blocked"] = False, "opted_out"
                elif text in RESTART_WORDS and st["blocked"]:
                    st["blocked"] = "start_keyword_does_not_restore"
                elif text in HELP_WORDS:
                    pass
                elif _looks_like_stop(text):
                    st["written"], st["blocked"] = False, "possible_opt_out"
            elif e["kind"] == "number_reassigned":
                st["written"], st["blocked"] = False, "number_reassigned"
            elif e["kind"] == "carrier_opt_out":
                st["written"], st["blocked"] = False, "carrier_opt_out"
            elif e["kind"] == "send_attempt":
                if st["blocked"]:
                    decisions[e["send_id"]] = ("block", st["blocked"])
                elif e["message_class"] == "marketing" and not st["written"]:
                    decisions[e["send_id"]] = ("block", "no_written_consent")
                else:
                    decisions[e["send_id"]] = ("allow", None)
    return decisions


def check_f5(events: list[dict], key: list[dict]) -> list[str]:
    got = decide_f5(events)
    errors = [f"{k['send_id']}: key {k['decision']}/{k['reason']} vs replay {got.get(k['send_id'])}"
              for k in key if got.get(k["send_id"]) != (k["decision"], k["reason"])]
    ids = [k["send_id"] for k in key]
    if sorted(got) != sorted(ids) or len(set(ids)) != len(ids):
        errors.append(f"answer rows do not match the sends one to one ({len(got)} sends, {len(key)} rows)")
    for k in key:
        if set(k) != {"send_id", "episode", "decision", "reason"}:
            errors.append(f"{k['send_id']}: unexpected fields {sorted(k)}")
    return errors


def decide_f2(e: dict) -> str:
    if e["caller_id"] is None or e["line_type"] == "landline" or e["number_opted_out"]:
        return "do_not_send"
    if e["seconds_since_last_text_to_number"] is not None and e["seconds_since_last_text_to_number"] < 300:
        return "do_not_send"
    h, m = e["recipient_local_time"].split(":")
    minute = int(h) * 60 + int(m)
    if minute >= QUIET_START or minute < QUIET_END:
        return "defer_until_0800"
    return "send_now"


def check_f2(events: list[dict], key: list[dict]) -> list[str]:
    """Every field except the case label must equal the rule's answer; rows match one to one."""
    errors = []
    if sorted(e["missed_call_id"] for e in events) != sorted(k["missed_call_id"] for k in key) or \
            len({k["missed_call_id"] for k in key}) != len(key):
        errors.append("answer rows do not match the missed calls one to one")
    by_id = {k["missed_call_id"]: k for k in key}
    for e in events:
        k = by_id.get(e["missed_call_id"])
        action = decide_f2(e)
        want = {"missed_call_id": e["missed_call_id"], "action": action,
                "max_latency_seconds": 30 if action == "send_now" else None, "message_class": "informational",
                "must_not": ["marketing_content", "send_to_opted_out"]}
        if k is None or common.differs({f: v for f, v in k.items() if f != "case"}, want):
            errors.append(f"{e['missed_call_id']}: key {k} vs rule {want}")
    return errors


def decide_f4(c: dict) -> tuple[str, str | None]:
    if not c["in_business_hours"] or not c["on_call_order"]:
        return "fallback_message_and_text_back", None
    behaviors = [c["staff_behavior"][p] for p in c["on_call_order"]]
    if "answers_after_caller_hung_up" in behaviors:
        return "caller_hung_up_log_and_text_back", None
    for p in c["on_call_order"]:
        if c["staff_behavior"][p] == "answers":
            return "connected", p
    return "fallback_message_and_text_back", None


def check_f4(cases: list[dict], key: list[dict]) -> list[str]:
    """Every field except the case label must equal the rule's answer; rows match one to one."""
    errors = []
    if sorted(c["case_id"] for c in cases) != sorted(k["case_id"] for k in key) or \
            len({k["case_id"] for k in key}) != len(key):
        errors.append("answer rows do not match the cases one to one")
    by_id = {k["case_id"]: k for k in key}
    for c in cases:
        k = by_id.get(c["case_id"])
        path, target = decide_f4(c)
        want = {"case_id": c["case_id"], "path": path, "connect_to": target, "max_seconds_to_connect_or_fallback": 30,
                "must_not": ["drop_caller_without_fallback", "transfer_outside_hours"],
                "whisper_summary_required": path == "connected"}
        if k is None or common.differs({f: v for f, v in k.items() if f != "case"}, want):
            errors.append(f"{c['case_id']}: key {k} vs rule {want}")
        if c["ring_mode"] != "simultaneous" or c["ring_timeout_seconds"] > 30:
            errors.append(f"{c['case_id']}: the ringing plan cannot meet the 30 s bar")
    return errors


# ---------------------------------------------------------------- F3: call summarizer transcripts
_CONFIRM = re.compile(r"^(?:Let me confirm|Confirmo): (?P<name>.+), (?:at|al) (?P<num>\d{3} \d{3} \d{4})\.$")
_NUM = re.compile(r"\b(\d{3}) (\d{3}) (\d{4})\b")
F3_LOW_CONFIDENCE = 0.6
# The checker's own health-detail lexicon (EN/ES). A summarizer checker needs one anyway; it is not the generator's list.
HEALTH_LEXICON = ("pregnant", "blood thinners", "diabetes", "seizure", "embarazada", "anticoagulantes", "convulsión")


# The checker's own list of call purposes (from the test-set spec), used to bound the F3 "need" label.
CALL_PURPOSES = frozenset({"angry_caller", "asks_if_ai", "booking", "complaint", "emergency", "existing_customer",
                           "health_details", "hours", "long_silence", "price_not_in_pack", "pricing_question",
                           "prompt_injection", "reschedule", "spam_call", "vendor", "wrong_number"})


_CALLER_NAME = re.compile(r"(?:^|(?<=My name is )|(?<=Me llamo )|(?<=I'm )|(?<=[Ss]oy )|(?<=This is )|(?<=Habla )"
                          r"|(?<=It's )|(?<=\. ))([A-Z][a-z]+ [A-Z][a-z]+)(?=[.,])", re.MULTILINE)


def derive_f3(t: dict, need_label: str) -> dict:
    caller = [x for x in t["turns"] if x["speaker"] == "caller" and x["text"]]
    confirms = [m for x in t["turns"] if x["speaker"] == "agent" and (m := _CONFIRM.match(x["text"]))]
    last = confirms[-1] if confirms else None
    trusted = [x for x in caller if x["confidence"] >= F3_LOW_CONFIDENCE]
    trusted_seq = ["".join(m) for x in trusted for m in _NUM.findall(x["text"])]
    trusted_numbers = set(trusted_seq)
    # AEGIS round 3: the facts come from what the CALLER said (trusted turns only; the last number given wins), not
    # from the agent's own confirmation line, which check_f3 then requires to agree.
    names = sorted({n for x in trusted for n in _CALLER_NAME.findall(x["text"])})
    return {
        "transcript_id": t["transcript_id"],
        "key_facts": {"name": (names[0] if len(names) == 1 else f"AMBIGUOUS:{names}") if last else None,
                      "callback_number": "+1" + trusted_seq[-1] if last and trusted_seq else None,
                      "need": need_label,
                      "urgency": "emergency" if any("911" in x["text"] for x in t["turns"] if x["speaker"] == "agent")
                      else "routine"},
        "must_flag_low_confidence": any(0 < x["confidence"] < F3_LOW_CONFIDENCE for x in caller),
        "corrected_value_must_win": len(trusted_numbers) > 1,
        "must_not": ["invent_fact", "include_health_detail", "use_low_confidence_turn_as_fact"],
    }


def check_f3(transcripts: list[dict], key: list[dict]) -> list[str]:
    errors = []
    by_id = {k["transcript_id"]: k for k in key}
    if sorted(by_id) != sorted(t["transcript_id"] for t in transcripts) or len(by_id) != len(key):
        errors.append("answer rows do not match the transcripts one to one")
    for t in transcripts:
        k = by_id.get(t["transcript_id"])
        if k is None or not isinstance(k.get("key_facts"), dict):
            errors.append(f"{t['transcript_id']}: missing or malformed answer row")
            continue
        need = k["key_facts"].get("need")
        # "need" is the caller's purpose: a label the transcript can't pin down word for word, so it is checked against
        # the allowed vocabulary (any pack's reason categories, or a call purpose). A swap to another valid
        # purpose is not caught: "need" is a label, listed as such in the README.
        allowed = {c for v in VERTICALS.values() for c in v["reason_categories"]} | CALL_PURPOSES
        if need not in allowed:
            errors.append(f"{t['transcript_id']}: need {need!r} is not a reason category or call purpose")
        want = derive_f3(t, need)
        confirms = [m for x in t["turns"] if x["speaker"] == "agent" and (m := _CONFIRM.match(x["text"]))]
        if confirms:
            said = (confirms[-1]["name"], "+1" + confirms[-1]["num"].replace(" ", ""))
            if said != (want["key_facts"]["name"], want["key_facts"]["callback_number"]):
                errors.append(f"{t['transcript_id']}: the agent confirmed {said}, not what the caller said")
        text = " ".join(x["text"] for x in t["turns"] if x["speaker"] == "caller").lower()
        want["forbidden_terms_in_summary"] = sorted(term for term in HEALTH_LEXICON if term in text)
        got = dict(k, forbidden_terms_in_summary=sorted(k.get("forbidden_terms_in_summary") or []))
        if common.differs(got, want):
            errors.append(f"{t['transcript_id']}: key differs from the transcript")
    return errors
