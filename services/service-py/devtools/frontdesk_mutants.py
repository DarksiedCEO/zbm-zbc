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
non-equivalent mutant is killed. Standard library only, plus pytest.
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
    ("consent_keeper", "no same-instant order", '(rows[i]["t"], _ORDER[rows[i]["kind"]], i)', '(rows[i]["t"], i)', None),
    ("consent_keeper", "opt-in after STOP in one instant", '_ORDER = {"opt_in": 0, "inbound_sms": 1',
     '_ORDER = {"opt_in": 1, "inbound_sms": 0',
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
     'and rec.suppressed in ("opted_out", "possible_opt_out", "start_keyword_does_not_restore",',
     'and rec.suppressed in ("never",', None),
    ("consent_keeper", "any consent counts", 'if e.get("consent") != "express_written" or e.get("source") not in '
     'CONSENT_SOURCES:', "if False:", None),
    ("consent_keeper", "loose number", "return cand if _E164.fullmatch(cand) else None", "return cand", None),
    ("consent_keeper", "unknown class allowed", "        if mclass not in MESSAGE_CLASSES:", "        if False:", None),
    ("consent_keeper", "state carries over", "        self._records = {}", "        pass", None),
    ("consent_keeper", "unreadable opt-out dropped",
     '        if kind != "send_attempt" and normalise_number(e.get("number")) is None:', "        if False:", None),
    ("consent_keeper", "calls-only wording ignored",
     "    if _no_contact(text):", "    if False:", None),
    ("consent_keeper", "no digit folding", "    text = _ascii_digits(raw).strip()", "    text = raw.strip()", None),
    ("consent_keeper", "no client folding",
     '    folded = _strip_invisible(unicodedata.normalize("NFC", raw)).translate(_DASHES).strip()',
     "    folded = raw", None),
    ("consent_keeper", "client ids case-folded",
     '    return "".join(c.lower() if c.isascii() else c for c in folded)', "    return folded.casefold()", None),
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
     "            if not isinstance(now, datetime) or now.utcoffset() is None:\n                return None, None",
     "            if not isinstance(now, datetime):\n                return None, None", None),
    ("textback", "gate forgets", "            self._last[number] = utc", "            pass", None),
    ("textback", "gate on wall clock", "since = int((utc - last).total_seconds()) if last else None",
     "since = int((now - last.astimezone(now.tzinfo)).total_seconds()) if last else None", None),
    ("textback", "placeholder zones used",
     '    if not isinstance(name, str) or "/" not in name or _PLACEHOLDER_ZONES.fullmatch(name):',
     "    if not isinstance(name, str):", None),
    ("textback", "look-alike letters kept",
     '    if any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in name):', "    if False:",
     "_SAFE_NAME already admits only ASCII and Latin-1/Latin Extended letters; the script check is a second fence"),
    ("textback", "promo names kept",
     "    if not name or not _SAFE_NAME.fullmatch(name) or _PROMO.search(name) or sum(c.isdigit() for c in name) > 3:",
     "    if not name:", None),
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
    ("transfer_router", "no hung-up path", "    if any(seen.get(person) == ANSWERS_LATE for person in p.ring):",
     "    if False:", None),
    ("transfer_router", "fallback without text-back",
     'return TransferOutcome(p.case_id, FALLBACK, None, p.timeout_seconds, False, False, True,\n'
     '                           tuple(notes + ["nobody_answered"]))',
     'return TransferOutcome(p.case_id, FALLBACK, None, p.timeout_seconds, False, False, False,\n'
     '                           tuple(notes + ["nobody_answered"]))', None),
    ("transfer_router", "unreadable case raises", '        return RingPlan("unknown", (), MAX_SECONDS, "unreadable_case")',
     '        raise ValueError("case_id is required")', None),
    ("transfer_router", "tuples ring nobody", "    if order is not None and not isinstance(order, (list, tuple)):",
     "    if order is not None and not isinstance(order, list):", None),
    ("transfer_router", "truthy urgent", "{'Urgent: ' if urgent is True else ''}", "{'Urgent: ' if urgent else ''}", None),
    ("transfer_router", "huge ids read", "        return str(p) if -_MAX_INT_ID <= p <= _MAX_INT_ID else None",
     "        return str(p)", None),
    ("transfer_router", "last record wins", '        merged[sid] = v if merged.get(sid, v) == v else "conflicting_records"',
     "        merged[sid] = v", None),
    ("transfer_router", "steering words kept", '    value = " ".join(_COMMAND_WORDS.sub(" ", value).split())[:limit].strip()',
     '    value = " ".join(value.split())[:limit].strip()', None),
    ("transfer_router", "bool timeout read", "    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:",
     "    if not isinstance(raw, int) or raw < 1:", None),
    # ---- RR-F3 call summarizer
    ("call_summarizer", "low-confidence turns trusted", "    trusted = [x for c, x in spoken if c >= LOW_CONFIDENCE]",
     "    trusted = [x for c, x in spoken]", None),
    ("call_summarizer", "silence flagged", "    flagged = any(c < LOW_CONFIDENCE for c, _ in spoken)",
     "    flagged = any(c < LOW_CONFIDENCE for c, _ in caller)", None),
    ("call_summarizer", "first number wins", "    callback = numbers[-1] if numbers else None",
     "    callback = numbers[0] if numbers else None", None),
    ("call_summarizer", "ambiguous name picked", "    name = names[0] if len(names) == 1 else None",
     "    name = names[0] if names else None", None),
    ("call_summarizer", "911 ignored", "    emergency = said_911 or any(", "    emergency = False or any(", None),
    ("call_summarizer", "health not routed", '    if any(term in low for term in _HEALTH):\n        return "treatment_question"',
     "", None),
    ("call_summarizer", "agent confirmation used as source",
     "    numbers = [\"+1\" + \"\".join(m) for x in trusted for m in _NUM.findall(x)]",
     "    numbers = [\"+1\" + \"\".join(m) for _, x in agent for m in _NUM.findall(x)]", None),
    ("call_summarizer", "no lead-in removal", "        for m in _NAME.finditer(_LEAD_IN.sub(\" \", _fold(x))):",
     "        for m in _NAME.finditer(_fold(x)):", None),
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
            killed = proc.returncode != 0
            state = "killed" if killed else ("survived (equivalent)" if equivalent else "SURVIVED")
            if not killed and not equivalent:
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
