#!/usr/bin/env python3
"""
Mutation check for the four MVP front-desk agents (RR-F5 consent keeper, RR-F2 text-back, RR-F4 transfer router,
RR-F3 call summarizer). Each mutant is one deliberate defect; the agent's own test file must fail on it.

    cd services/service-py && python3 devtools/frontdesk_mutants.py            # all agents
    cd services/service-py && python3 devtools/frontdesk_mutants.py textback   # one agent

Every mutant is applied to a fresh copy of ``src/`` and ``tests/`` in a temporary directory (the checkout is never
edited), then the agent's test file runs with ``-x``. A mutant that no longer applies (its text is gone) is reported
``NOT_APPLIED`` and fails the run, so the list cannot quietly rot. Mutants marked equivalent are expected to survive
(they cannot change any observable behaviour) and are listed with the reason. Exit 0 only when every
non-equivalent mutant is killed. A kill counts only when pytest exits 1 (tests ran and failed); any other exit
code is ``RUN_ERROR`` and fails the run. Standard library only, plus pytest.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (agent module, label, old text, new text, equivalent-because or None)
MUTANTS: list[tuple[str, str, str, str, str | None]] = [
    # ---- RR-F5 consent keeper
    ("consent_keeper", "no same-instant order", '(rows[i]["t"], _ORDER[rows[i]["kind"]], rank[i], i)',
     '(rows[i]["t"], rank[i], i)', None),
    ("consent_keeper", "same-instant reason by input order", '(rows[i]["t"], _ORDER[rows[i]["kind"]], rank[i], i)',
     '(rows[i]["t"], _ORDER[rows[i]["kind"]], i)', None),
    ("consent_keeper", "unrecognised replies allowed", '    return "unrecognised_reply"', "    return None", None),
    ("consent_keeper", "staff review clears anything",
     '            if rec.suppressed == "unrecognised_reply" and rec.suppressed_at is not None and e["t"] > rec.suppressed_at:',
     '            if rec.suppressed is not None and rec.suppressed_at is not None and e["t"] > rec.suppressed_at:', None),
    ("consent_keeper", "staff review at the reply's instant counts",
     '            if rec.suppressed == "unrecognised_reply" and rec.suppressed_at is not None and e["t"] > rec.suppressed_at:',
     '            if rec.suppressed == "unrecognised_reply" and rec.suppressed_at is not None and e["t"] >= rec.suppressed_at:',
     "a review at the reply's own instant is applied before the reply (_ORDER), so there is nothing yet to clear"),
    ("consent_keeper", "staff opt-out ruling ignored", '        elif kind == "staff_review" and e["ruling"] == "opt_out":',
     '        elif False:', None),
    ("consent_keeper", "opt-in after STOP in one instant", '_ORDER = {"opt_in": 0, "staff_review": 0, "inbound_sms": 1',
     '_ORDER = {"opt_in": 1, "staff_review": 0, "inbound_sms": 0',
     "the opt-in time guard (t > suppressed_at) already refuses an opt-in at the opt-out's own instant"),
    ("consent_keeper", "START restores", '                rec.suppressed = "start_keyword_does_not_restore"',
     "                rec.suppressed = None", None),
    ("consent_keeper", "typo ignored", '    if level == "suspected" or channels.possible_opt_out(text):',
     "    if False:", None),
    ("consent_keeper", "no marketing check", 'if mclass == "marketing" and rec.written_consent_at is None:', "if False:",
     None),
    ("consent_keeper", "per-number only", 'return self._records.setdefault((client, number), Record())',
     'return self._records.setdefault(("", number), Record())', None),
    ("consent_keeper", "new consent never restores",
     "                rec.suppressed, rec.suppressed_at = None, None\n            # the evidence keeps",
     "                pass\n            # the evidence keeps", None),
    ("consent_keeper", "any consent counts", 'if e.get("consent") != "express_written" or e.get("source") not in '
     'CONSENT_SOURCES:', "if False:", None),
    ("consent_keeper", "loose number", "return cand if _E164.fullmatch(cand) else None", "return cand", None),
    ("consent_keeper", "unknown class allowed", "        if mclass not in MESSAGE_CLASSES:", "        if False:", None),
    ("consent_keeper", "state carries over", "        self._records = {}", "        pass", None),
    ("consent_keeper", "unreadable opt-out dropped",
     '        if kind != "send_attempt" and normalise_number(e.get("number")) is None:', "        if False:", None),
    ("consent_keeper", "calls-only wording ignored", "    if _no_contact(text) or", "    if False or", None),
    ("consent_keeper", "whole-message net removed", '    if _refusal_near_contact(text):\n        return "possible_opt_out"',
     '    if False:\n        return "possible_opt_out"', None),
    ("consent_keeper", "Spanish list removal ignored", "            _SPANISH_REMOVE.search(",
     "            False and _SPANISH_REMOVE.search(", None),
    ("consent_keeper", "glued refusal ignored", '    if any(re.match(r"^(?:dont|donot|no|never|stop)',
     '    if False and any(re.match(r"^(?:dont|donot|no|never|stop)', None),
    ("consent_keeper", "no digit folding", "    text = _ascii_digits(raw).strip()", "    text = raw.strip()", None),
    ("consent_keeper", "no client folding",
     '    folded = _strip_invisible(unicodedata.normalize("NFC", raw)).translate(_DASHES).translate(_APOSTROPHES)\n',
     "    folded = raw\n", None),
    ("consent_keeper", "apostrophe look-alikes make two clients", ".translate(_DASHES).translate(_APOSTROPHES)\n",
     ".translate(_DASHES)\n", None),
    ("consent_keeper", "final sigma makes two clients", '.replace("\\u03c2", "\\u03c3")', "", None),
    ("consent_keeper", "a modifier mark is a reviewer", '    return any(unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lo", "Nd", "Nl", "No")',
     '    return any(unicodedata.category(c)[0] in "LN"', None),
    ("textback", "subclass record values read",
     "    if any(isinstance(v, (str, int, float)) and type(v) not in (str, int, float, bool) for v in call.values()):",
     "    if False:", None),
    ("consent_keeper", "client ids case-folded",
     '    return "".join(c.lower() if len(c.lower()) == 1 else c for c in folded)', "    return folded.casefold()", None),
    ("consent_keeper", "only ASCII letters lower-cased",
     '    return "".join(c.lower() if len(c.lower()) == 1 else c for c in folded)',
     '    return "".join(c.lower() if c.isascii() else c for c in folded)', None),
    ("consent_keeper", "apostrophe look-alikes kept", "    text = _unify(text)\n    level = channels.opt_out_level(text)",
     "    level = channels.opt_out_level(text)", None),
    ("consent_keeper", "revocation stems ignored", " or _REVOCATION.search(_plain(text)) or", " or", None),
    ("consent_keeper", "non-text opt-in source accepted",
     '        if kind == "opt_in" and e.get("source") is not None and not isinstance(e["source"], str):',
     "        if False:", None),
    ("consent_keeper", "refusal window ignored", "            if calls and any(x in _REFUSAL for x in words[max(0, i - 5):i]):",
     "            if calls and any(x in _REFUSAL for x in words[i - 1:i]):", None),
    ("consent_keeper", "duplicate send ids allowed", '                if e["send_id"] in seen:', "                if False:",
     None),
    ("consent_keeper", "carrier check removed from the decision", "        if rec.carrier_blocked:\n            return",
     "        if False:\n            return", "the carrier reason also stays in rec.suppressed, which no opt-in clears"),
    ("consent_keeper", "reassignment keeps consent",
     '            rec.written_consent_at = None\n            self._suppress(rec, "number_reassigned"',
     '            self._suppress(rec, "number_reassigned"',
     "the suppression blocks every send until a new opt-in, which sets consent again"),
    ("consent_keeper", "opt-in time guard removed",
     '            if (rec.suppressed_at is None or e["t"] > rec.suppressed_at) and rec.suppressed in',
     "            if rec.suppressed in", "a single sorted replay always applies an earlier opt-in before the opt-out"),
    # ---- RR-F2 text-back
    ("textback", "300 s blocks", "        if since < REPEAT_WINDOW_SECONDS:", "        if since <= REPEAT_WINDOW_SECONDS:",
     None),
    ("textback", "299 s allowed", "        if since < REPEAT_WINDOW_SECONDS:", "        if since < 299:", None),
    ("textback", "quiet hours from 21:01", "    if start <= seconds < end:", "    if start <= seconds <= end:", None),
    ("textback", "08:00 waits", "    if start <= seconds < end:", "    if start < seconds < end:", None),
    ("textback", "unknown consent sends", "    if blocked is not False:", "    if blocked is True:", None),
    ("textback", "unknown line sends", '    if line not in TEXTABLE_LINES:\n        return no("unknown_line_type")', "",
     None),
    ("textback", "keeper ignored", 'blocked = opted_out(number) if opted_out is not None else call.get("number_opted_out")',
     'blocked = call.get("number_opted_out")', None),
    ("textback", "string beats the clock", "    if now is not None:\n        try:",
     '    if now is not None and call.get("recipient_local_time") is None:\n        try:', None),
    ("textback", "zone not required", "    if z is None:\n        return None, None", "    if False:\n        return None, None",
     None),
    ("textback", "deadline may land after 21:00",
     "    end = channels.QUIET_END_HOUR * 3600 - (SEND_DEADLINE_SECONDS if local is not None else 0)",
     "    end = channels.QUIET_END_HOUR * 3600", None),
    ("textback", "offset-less clock trusted",
     "    if type(off) is not timedelta:\n        return None",
     "    if off is None:\n        off = timedelta(0)", None),
    ("textback", "gate forgets", "            self._last[number] = utc", "            pass", None),
    ("textback", "gate on wall clock", "since = int((utc - last).total_seconds()) if last else None",
     "since = int((now - last.astimezone(now.tzinfo)).total_seconds()) if last else None", None),
    ("textback", "placeholder zones used",
     '    if not isinstance(name, str) or "/" not in name or _PLACEHOLDER_ZONES.fullmatch(name):',
     "    if not isinstance(name, str):", None),
    ("textback", "business name used", '    who = "Sorry we missed your call."',
     '    who = f"{business_name}: sorry we missed your call." if business_name else "Sorry we missed your call."', None),
    ("textback", "a caller's seconds_since_gate used", '        kw.pop("seconds_since_gate", None)', "        pass", None),
    ("textback", "a raising keeper allows", "        blocked = None", "        blocked = False", None),
    ("textback", "missing field means never texted",
     '    if "seconds_since_last_text_to_number" not in call:\n        return no("unreadable_last_text_time")', "",
     None),
    # ---- RR-F4 transfer router
    ("transfer_router", "truthy hours", 'if case.get("in_business_hours") is not True:',
     'if case.get("in_business_hours") is False:', None),
    ("transfer_router", "31 s allowed", "    if raw > MAX_SECONDS:", "    if raw > 31:", None),
    ("transfer_router", "last on-call wins", "    for person in p.ring:\n        if seen.get(person) == ANSWERS:",
     "    for person in reversed(p.ring):\n        if seen.get(person) == ANSWERS:", None),
    ("transfer_router", "anyone who answers", "    for person in p.ring:\n        if seen.get(person) == ANSWERS:",
     "    for person in seen:\n        if seen.get(person) == ANSWERS:", None),
    ("transfer_router", "no hung-up path", "    if late:", "    if False:", None),
    ("transfer_router", "late answerers not logged",
     '                               tuple(notes + [f"answered_after_hang_up:{person}" for person in late]))',
     "                               tuple(notes))", None),
    ("transfer_router", "any object is an outcome", "        if type(v) is not str:", "        if False:", None),
    ("transfer_router", "str subclass whisper played", "    return type(text) is str and text in WHISPER_LINES",
     "    return isinstance(text, str) and text in WHISPER_LINES", None),
    ("transfer_router", "fallback without text-back",
     'return TransferOutcome(p.case_id, FALLBACK, None, p.timeout_seconds, False, False, True,\n'
     '                           tuple(notes + ["nobody_answered"]))',
     'return TransferOutcome(p.case_id, FALLBACK, None, p.timeout_seconds, False, False, False,\n'
     '                           tuple(notes + ["nobody_answered"]))', None),
    ("transfer_router", "unreadable case raises", '        return RingPlan("unknown", (), MAX_SECONDS, "unreadable_case")',
     '        raise ValueError("case_id is required")',
     "resolve's own catch-all turns the raise into the same fallback noted unreadable_case"),
    ("transfer_router", "resolve's catch-all removed", '    except Exception:\n        cid = "unknown"',
     '    except ZeroDivisionError:\n        cid = "unknown"', None),
    ("transfer_router", "tuples ring nobody", "    if order is not None and type(order) not in (list, tuple):",
     "    if order is not None and type(order) is not list:", None),
    ("transfer_router", "truthy urgent", "{'Urgent: ' if urgent is True else ''}", "{'Urgent: ' if urgent else ''}", None),
    ("transfer_router", "huge ids read", "        return str(p) if -_MAX_INT_ID <= p <= _MAX_INT_ID else None",
     "        return str(p)", None),
    ("transfer_router", "last record wins", '        merged[sid] = v if merged.get(sid, v) == v else "conflicting_records"',
     "        merged[sid] = v", None),
    ("transfer_router", "caller name spoken", "Transferring a caller about {why}. {_SCREEN}",
     "Transferring {caller_name} about {why}. {_SCREEN}", None),
    ("transfer_router", "the record's need is spoken",
     "    why = NEED_PHRASES.get(need, DEFAULT_NEED) if isinstance(need, str) else DEFAULT_NEED",
     "    why = need if isinstance(need, str) else DEFAULT_NEED", None),
    ("transfer_router", "any whisper is played", "            played = _is_whisper_line(whisper)",
     "            played = isinstance(whisper, str) and bool(whisper.strip())", None),
    ("transfer_router", "bool timeout read", "    if type(raw) is not int or raw < 1:",
     "    if not isinstance(raw, int) or raw < 1:", None),
    # ---- RR-F3 call summarizer
    ("call_summarizer", "low-confidence turns trusted", "    trusted = [x for c, x in spoken if c >= LOW_CONFIDENCE]",
     "    trusted = [x for c, x in spoken]", None),
    ("call_summarizer", "silence flagged", "    flagged = any(c < LOW_CONFIDENCE for c, _ in spoken)",
     "    flagged = any(c < LOW_CONFIDENCE for c, _ in caller)", None),
    ("call_summarizer", "first number wins", "    return distinct[-1][0], len({d[0] for d in distinct}) > 1",
     "    return distinct[0][0], len({d[0] for d in distinct}) > 1", None),
    ("call_summarizer", "health not routed", '    if _health_need(joined):\n        return "treatment_question"', "",
     None),
    ("call_summarizer", "ordinary-meaning health words always a need",
     "        if not _HEALTH_PLAIN.fullmatch(m.group(0)):\n            return True",
     "        if True:\n            return True", None),
    ("call_summarizer", "names unconfirmed", "            if said is not None and not re.search(",
     "            if False and not re.search(", None),
    ("call_summarizer", "low-confidence receptionist trusted",
     "    agent_trusted = [x for c, x in agent if c >= LOW_CONFIDENCE]", "    agent_trusted = [x for c, x in agent]", None),
    ("call_summarizer", "emergency net removed", "    possible = not emergency and any(", "    possible = False and any(",
     None),
    ("call_summarizer", "context patterns dropped from the net", "_EMERGENCY.search(_fold(x)) or _EMERGENCY_WORDS",
     "_EMERGENCY_WORDS", None),
    ("call_summarizer", "greeting scan breaks", "    return bool(words) and all(w in _GREETINGS for w in words)",
     "    return False", None),
    ("call_summarizer", "unknown pack labels spoken", "        if need not in FIXED_NEEDS and need not in KNOWN_PACK_LABELS or",
     "        if False or", None),
    ("call_summarizer", "agent confirmation used as source", "    callback, corrected = _callback(trusted, notes)",
     "    callback, corrected = _callback([x for _, x in agent], notes)", None),
    ("call_summarizer", "any capitalised pair is a name",
     "        for m in list(_LEAD_NAME.finditer(x)) + list(_WEAK_LEAD_NAME.finditer(x)) + \\\n"
     "                list(_NAME_THEN_NUMBER.finditer(x)):",
     '        for m in re.finditer(rf"({_WORD} {_WORD})", x):', None),
    ("call_summarizer", "weak lead-in reads any pair", "list(_WEAK_LEAD_NAME.finditer(x))",
     'list(re.finditer(rf"(?:\\bit\'?s|\\bthis is|\\bhabla)\\s+({_WORD} {_WORD})", x, re.IGNORECASE))', None),
    ("call_summarizer", "rejection before a number ignored",
     "_REJECTED_NUMBER.search(joined[max(0, m.start() - 60):m.start()]) or", "False or", None),
    ("call_summarizer", "rejection after a number ignored", "_REJECTED_AFTER.search(joined[m.end():m.end() + 60])",
     "False", None),
    ("call_summarizer", "two numbers without a cue: later wins",
     "        if distinct and number != distinct[-1][0] and not _CORRECTION.search(before[-30:]):", "        if False:",
     None),
    ("call_summarizer", "a cue anywhere in the gap", "not _CORRECTION.search(before[-30:])",
     "not _CORRECTION.search(before)", None),
    ("call_summarizer", "health label spoken", '            label = "appointment request"', "            pass", None),
    ("call_summarizer", "stop-word list ignored",
     "            if any(w.casefold() in _NOT_NAME_WORD for w in words) or _HEALTH.search(cand):",
     "            if _HEALTH.search(cand):", None),
    ("call_summarizer", "health words kept in names",
     "            if any(w.casefold() in _NOT_NAME_WORD for w in words) or _HEALTH.search(cand):",
     "            if any(w.casefold() in _NOT_NAME_WORD for w in words):", None),
    ("call_summarizer", "final health guard removed", "    if _HEALTH.search(text):", "    if False:",
     "a second fence: the name reader drops any name holding a health word and the label check rewords any pack "
     "category that names a condition; nothing else in the text comes from the caller or the pack"),
    ("call_summarizer", "lower-case names read", "            if not all(w[0].isupper() for w in words):\n                continue",
     "", None),
    ("call_summarizer", "911 anywhere", "    said_911 = any(c >= LOW_CONFIDENCE and",
     '    said_911 = any("911" in x for _, x in agent) or any(c >= LOW_CONFIDENCE and', None),
    ("consent_keeper", "inner spaces make two clients",
     '    folded = re.sub(r"\\s+", " ", folded).strip()', "    folded = folded.strip()", None),
    ("consent_keeper", "an invisible reviewer counts",
     '        if kind == "staff_review" and (not _is_named(e.get("reviewer"))',
     '        if kind == "staff_review" and (not isinstance(e.get("reviewer"), str)', None),
    ("textback", "clock offset read twice", "            local = utc.astimezone(z)",
     "            local = now.astimezone(z)", None),
    ("textback", "dict subclass read through its own get", "    call = _plain(call)\n    mcid", "    mcid", None),
    ("textback", "int subclass last-text time",
     "        if type(since) is not int or since < 0:",
     "        if not isinstance(since, int) or isinstance(since, bool) or since < 0:",
     "the record-wide plain-type check (AEGIS round 8) refuses an int subclass before the last-text rule reads it"),
    ("transfer_router", "list subclass on-call order", "    if order is not None and type(order) not in (list, tuple):",
     "    if order is not None and not isinstance(order, (list, tuple)):", None),
    ("transfer_router", "str subclass staff id", "    if type(p) is str and p.strip():",
     "    if isinstance(p, str) and p.strip():", None),
    ("transfer_router", "int subclass timeout", "    if type(raw) is not int or raw < 1:",
     "    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:", None),
    ("transfer_router", "late answer beside a live one not logged",
     '            notes += [f"answered_after_hang_up:{q}" for q in p.ring if seen.get(q) == ANSWERS_LATE]\n', "", None),
    ("transfer_router", "dict subclass case", "    case = _plain(case)\n    p = plan(case)", "    p = plan(case)", None),
    ("call_summarizer", "caller words make it urgent", "    emergency = said_911\n",
     "    emergency = said_911 or any(_EMERGENCY_WORDS.search(_fold(x)) for x in trusted)\n", None),
    ("call_summarizer", "binary float boundary", "    value = Fraction(str(raw))", "    value = Fraction(raw)", None),
    ("call_summarizer", "flag threshold inclusive", "    flagged = any(c < LOW_CONFIDENCE for c, _ in spoken)",
     "    flagged = any(c <= LOW_CONFIDENCE for c, _ in spoken)", None),
    ("call_summarizer", "out-of-range confidence trusted", "    return value if 0 <= value <= 1 else Fraction(0)",
     "    return value", None),
    ("consent_keeper", "a weaker block replaces a stronger one",
     "        if rec.suppressed is not None and _STRENGTH[reason] < _STRENGTH[rec.suppressed]:", "        if False:", None),
    ("consent_keeper", "restart word with anything after it", "    return key in RESTART_WORDS or plain in RESTART_WORDS",
     "    return any(plain.startswith(w) for w in RESTART_WORDS)", None),
    ("consent_keeper", "marked letters make a restart word",
     '    if not re.fullmatch(r"[a-zà-ÿ]+(?: [a-zà-ÿ]+)*", key):\n        return False', "", None),
    ("consent_keeper", "evidence loses the block's cause",
     "            rec.evidence = rec.evidence + [i] if rec.carrier_blocked or rec.suppressed is not None else [i]",
     "            rec.evidence = rec.evidence + [i] if rec.carrier_blocked else [i]", None),
    ("consent_keeper", "non-list replay accepted", "        if not isinstance(events, (list, tuple)):", "        if False:", None),
    ("textback", "976 exchange offered", ' or exchange == "976"', "", None),
    ("textback", "international-rate area offered", "\n                or area in _INTERNATIONAL_RATE_AREAS)", ")", None),
    ("transfer_router", "urgent line for any case",
     ' and whisper.startswith("Urgent: ") == (case.get("urgent") is True)', "", None),
    ("call_summarizer", "unknown speaker read as receptionist", '        if speaker not in ("caller", "agent"):',
     "        if False:", None),
    ("call_summarizer", "911 anywhere in the turn", "    return _SAID_911.fullmatch(text) is not None",
     "    return _SAID_911.search(agent_text) is not None", None),
    ("call_summarizer", "a sentence split across turns", '''    if previous is not None and not re.search(r"[.!?…]\\s*$", previous):
        return False''', "", None),
    ("call_summarizer", "punctuation breaks the fixed form",
     '    text = re.sub(r"[.!]+$", ".", re.sub(r"\\s+", " ", text.replace(",", " "))).replace(" .", ".")', "", None),
    ("call_summarizer", "an answer is a greeting", '_GREETINGS = frozenset({"hello", "hi", "hey", "hola", "bueno", "alo", "aló", "oiga"})',
     '_GREETINGS = frozenset({"hello", "hi", "hey", "hola", "bueno", "alo", "aló", "oiga", "yes", "si", "sí", "okay", "ok"})',
     None),
    ("call_summarizer", "a refused confirmation counts",
     "    return any(not _NO_CONFIRM.search(text[:m.start()]) for m in _CONFIRM_CUE.finditer(text))",
     "    return _CONFIRM_CUE.search(text) is not None", None),
    ("call_summarizer", "spam wins over a real need", "    for label in real[:1] or matched[:1]:",
     "    for label in matched[:1]:", None),
    ("call_summarizer", "a caller who left a number is not called back",
     '    elif need in ("spam_call", "wrong_number", "long_silence") and not callback:',
     '    elif need in ("spam_call", "wrong_number", "long_silence"):', None),
    ("call_summarizer", "spelled or distant corrections ignored", "[^\\n]{0,40}?\\b\" + _DIGIT_WORD", "[\\s,:\\-]{0,5}\" + _DIGIT_WORD",
     None),
    ("call_summarizer", "badly heard words read as silence", '''    if flagged and need == "long_silence":
        need = "unclear"''', "    pass", None),
    ("call_summarizer", "CHECK NOW ignores low-confidence words", "                                     for _, x in spoken)",
     "                                     for x in trusted)", None),
    ("call_summarizer", "partial correction ignored", '''        notes.append("ambiguous_number")   # "0110, no wait, 0101"''',
     "        return found[-1][0], False\n        notes.append(\"ambiguous_number\")   # \"0110, no wait, 0101\"", None),
    ("call_summarizer", "possessive read as a name",
     '''            if re.search(r"'s?$", words[-1]) or re.match(r"\\s*'", x[m.end(1):]):''',
     "            if False:", None),
    ("call_summarizer", "negated need words counted",
     "        if any(not _NEGATED.search(joined[max(0, m.start() - 30):m.start()])",
     "        if any(True or _NEGATED.search(joined[max(0, m.start() - 30):m.start()])", None),
    ("call_summarizer", "any mention confirms a name",
     "    said = \" \\n \".join(_fold(x).casefold() for x in confirmations if _confirms(_fold(x))) \\",
     "    said = \" \\n \".join(_fold(x).casefold() for x in confirmations) \\", None),
    ("call_summarizer", "low-confidence receptionist turn checked",
     "    confirmed = [\"+1\" + \"\".join(m) for x in agent_trusted for m in _NUM.findall(x)]",
     "    confirmed = [\"+1\" + \"\".join(m) for _, x in agent for m in _NUM.findall(x)]", None),
]
TESTS = {"consent_keeper": "tests/test_consent_keeper.py", "textback": "tests/test_textback.py",
         "transfer_router": "tests/test_transfer_router.py", "call_summarizer": "tests/test_call_summarizer.py"}


def run(only: str | None = None) -> int:
    rows, bad = [], 0
    with tempfile.TemporaryDirectory(prefix="frontdesk-mutants-") as tmp:
        for module, label, old, new, equivalent in MUTANTS:
            if only and module != only:
                continue
            # the same layout as the checkout, so the certification tests find fixtures/mvp_pools where they expect
            repo = Path(tmp) / f"m{len(rows)}"
            work = repo / "services" / "service-py"
            shutil.copytree(ROOT / "src", work / "src")
            shutil.copytree(ROOT / "tests", work / "tests")
            shutil.copy(ROOT / "pytest.ini", work / "pytest.ini")
            (repo / "fixtures").symlink_to(ROOT.parents[1] / "fixtures", target_is_directory=True)
            target = work / "src" / f"{module}.py"
            text = target.read_text()
            if text.count(old) != 1:
                rows.append((module, label, "NOT_APPLIED", equivalent))
                bad += 1
                continue
            target.write_text(text.replace(old, new, 1))
            proc = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                                   TESTS[module]], cwd=work, capture_output=True, text=True,
                                  env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
            # pytest exits 1 only when tests ran and failed; 2-5 (interrupted, internal or usage error, no tests) mean
            # the mutant broke the run itself, which proves nothing about the tests (AEGIS F3 round 1)
            killed = proc.returncode == 1
            if proc.returncode not in (0, 1):
                state = f"RUN_ERROR (pytest exit {proc.returncode})"
            else:
                state = "killed" if killed else ("survived (equivalent)" if equivalent else "SURVIVED")
            if not killed and not (equivalent and proc.returncode == 0):
                bad += 1
            rows.append((module, label, state, equivalent))
    width = max(len(r[1]) for r in rows) if rows else 10
    for module, label, state, equivalent in rows:
        print(f"{module:16} {label:{width}}  {state}" + (f"  ({equivalent})" if equivalent and "equivalent" in state
                                                          else ""))
    killed = sum(1 for r in rows if r[2] == "killed")
    print(f"\n{killed} killed / {len(rows)} mutants; {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
