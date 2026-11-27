"""Front-desk test sets for RR-F2 (missed-call text-back), RR-F4 (warm-transfer router) and RR-F5 (consent keeper).

Each set is generated from small scripted EPISODES whose right answer is known by construction; the answer key is
the episode's planted outcome. ``frontdesk_oracle.py`` re-derives every answer independently from the events.

Rules encoded (agent draft spec v0.1 + test-set spec v0):
- F2: text within 60 s of a missed call (bar: p95 <= 30 s); never text a landline, a blocked caller ID or an opted-out
  number; one text per caller per 5 minutes; quiet hours 08:00-21:00 in the RECIPIENT's time zone (outside it the text
  waits until 08:00); never marketing.
- F4: every path ends in a connected call or a fallback (message + text-back) within 30 s; never a dropped caller.
- F5: every send is checked; a STOP (and its variants, Spanish ALTO, and anything that looks like an opt-out) wins,
  including against a send at the same instant; HELP changes nothing; the START keyword does NOT restore consent (ZBM
  rule, service-py ADR 0014) - only a new express written consent does; a reassigned number or a carrier opt-out kills
  consent; marketing needs express written consent, an informational reply to the caller does not.
"""

from __future__ import annotations

from . import common
from .common import Stream

F2_POOL, F4_POOL, F5_POOL = "frontdesk-f2-textback", "frontdesk-f4-transfer", "frontdesk-f5-consent"
AREA_TZ = {a: tz for a, _s, tz in common.REGION_AREA_CODES}

STOP_WORDS = ("STOP", "stop", "Stop.", "STOP!", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPT OUT",
              "stop texting me", "ALTO", "alto", "BAJA")
TYPO_STOPS = ("STPO", "stopp", "SOTP", "Stop pls", "st0p")      # treated as an opt-out: fail toward suppression


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------- F2: missed-call text-back (300 missed calls)
F2_KINDS = (("send", 120), ("landline", 30), ("blocked_caller_id", 25), ("opted_out", 30),
            ("repeat_within_5_min", 35), ("quiet_hours", 40), ("voip_textable", 20))


def generate_f2(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{F2_POOL}|{seed}")
    events, key = [], []
    n = 0
    for kind, count in F2_KINDS:
        for _ in range(count):
            s = root.child(f"{kind}-{n}")
            area = s.pick(sorted(AREA_TZ))
            caller = common.fixture_phone(area, 50 + s.below(50))
            status = s.pick(["busy", "after_hours", "dropped", "voicemail", "no_answer"])
            local = _hhmm(s.between(1260, 1439) if kind == "quiet_hours" else s.between(480, 1199))
            if kind == "quiet_hours" and s.chance(1, 2):
                local = _hhmm(s.between(0, 479))
            # boundaries: 21:00 and 07:59 are quiet; 20:59 and 08:00 may text
            if s.chance(1, 4):
                local = s.pick(["21:00", "07:59"]) if kind == "quiet_hours" else s.pick(["20:59", "08:00"])
            base = {"fixture": common.FIXTURE_LABEL, "pool": F2_POOL, "missed_call_id": common.opaque_id("mc", seed, n),
                    "caller_id": None if kind == "blocked_caller_id" else caller,
                    "line_type": "landline" if kind == "landline" else "voip" if kind == "voip_textable" else "mobile",
                    "status": status, "recipient_local_time": local, "recipient_time_zone": AREA_TZ[area],
                    "number_opted_out": kind == "opted_out", "seconds_since_last_text_to_number": None}
            if kind == "repeat_within_5_min":
                base["seconds_since_last_text_to_number"] = s.pick([299, s.between(10, 298)])
            elif s.chance(1, 5):
                # boundary: exactly 5 minutes (300 s) after the last text may text again
                base["seconds_since_last_text_to_number"] = s.pick([300, s.between(301, 86400)])
            events.append(base)
            action = {"send": "send_now", "voip_textable": "send_now", "landline": "do_not_send",
                      "blocked_caller_id": "do_not_send", "opted_out": "do_not_send",
                      "repeat_within_5_min": "do_not_send", "quiet_hours": "defer_until_0800"}[kind]
            key.append({"missed_call_id": base["missed_call_id"], "case": kind, "action": action,
                        "max_latency_seconds": 30 if action == "send_now" else None,
                        "message_class": "informational", "must_not": ["marketing_content", "send_to_opted_out"]})
            n += 1
    order = root.child("order").shuffled(list(range(len(events))))
    return [events[i] for i in order], [key[i] for i in order]


# ---------------------------------------------------------------- F4: warm-transfer router (200 rule cases)
F4_KINDS = (("connect_first", 60), ("first_no_answer_second_connects", 35), ("nobody_on_call", 30),
            ("all_on_call_no_answer", 30), ("caller_hangs_up_during_transfer", 25), ("outside_hours", 20))


def generate_f4(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{F4_POOL}|{seed}")
    cases, key = [], []
    n = 0
    for kind, count in F4_KINDS:
        for _ in range(count):
            s = root.child(f"{kind}-{n}")
            people = [f"staff-{c}" for c in "abc"][: s.between(2, 3)]
            on_call = [] if kind == "nobody_on_call" else people[:2]
            answers = {p: "no_answer" for p in people}
            if kind == "connect_first":
                answers[on_call[0]] = "answers"
            elif kind == "first_no_answer_second_connects":
                answers[on_call[1]] = "answers"
            elif kind == "caller_hangs_up_during_transfer":
                answers[on_call[0]] = "answers_after_caller_hung_up"
            in_hours = kind != "outside_hours"
            cases.append({"fixture": common.FIXTURE_LABEL, "pool": F4_POOL, "case_id": common.opaque_id("xfer", seed, n),
                          "ring_mode": "simultaneous",   # all on-call staff ring at once, so 30 s holds for any list
                          "in_business_hours": in_hours, "on_call_order": on_call, "staff_behavior": answers,
                          "ring_timeout_seconds": s.pick([12, 15, 20, 30]),   # 30 = on the bar, still valid
                          "caller_number": common.fixture_phone(s.pick(sorted(AREA_TZ)), 50 + s.below(50))})
            if kind == "connect_first":
                path, target = "connected", on_call[0]
            elif kind == "first_no_answer_second_connects":
                path, target = "connected", on_call[1]
            elif kind == "caller_hangs_up_during_transfer":
                path, target = "caller_hung_up_log_and_text_back", None
            else:
                path, target = "fallback_message_and_text_back", None
            key.append({"case_id": cases[-1]["case_id"], "case": kind, "path": path, "connect_to": target,
                        "max_seconds_to_connect_or_fallback": 30,
                        "must_not": ["drop_caller_without_fallback", "transfer_outside_hours"],
                        "whisper_summary_required": path == "connected"})
            n += 1
    order = root.child("order").shuffled(list(range(len(cases))))
    return [cases[i] for i in order], [key[i] for i in order]


# ---------------------------------------------------------------- F5: consent keeper (10,000-event replay)
F5_EPISODES = (
    # name, events as (kind, payload) with relative seconds, expected decision for the final send
    ("written_consent_then_marketing", "allow", None),
    ("no_consent_marketing", "block", "no_written_consent"),
    ("informational_reply_no_prior_consent", "allow", None),
    ("stop_then_send", "block", "opted_out"),
    ("spanish_alto_then_send", "block", "opted_out"),
    ("typo_stop_then_send", "block", "possible_opt_out"),
    ("help_then_send", "allow", None),
    ("stop_then_start_keyword_then_send", "block", "start_keyword_does_not_restore"),
    ("stop_then_new_written_consent_then_send", "allow", None),
    ("number_reassigned_then_send", "block", "number_reassigned"),
    ("carrier_opt_out_then_send", "block", "carrier_opt_out"),
    ("race_stop_and_send_same_instant", "block", "opted_out"),
    ("look_alike_word_then_send", "allow", None),     # not an opt-out: over-suppression is a failure too
)
LOOK_ALIKES = ("STAR", "Stay", "Spot", "Post", "Thanks", "OK")


def _episode(name: str, s: Stream, client: str, number: str, t0: int) -> list[dict]:
    ev = []

    def add(dt: int, kind: str, **kw) -> None:
        ev.append({"t": t0 + dt, "client_id": client, "number": number, "kind": kind, **kw})

    marketing = name in ("written_consent_then_marketing", "no_consent_marketing")
    if name not in ("no_consent_marketing", "informational_reply_no_prior_consent"):
        add(0, "opt_in", consent="express_written", source=s.pick(["web_form", "paper_form", "keyword_join"]))
    if name in ("stop_then_send", "stop_then_start_keyword_then_send", "stop_then_new_written_consent_then_send"):
        add(60, "inbound_sms", text=s.pick(STOP_WORDS[:11]))
    elif name == "spanish_alto_then_send":
        add(60, "inbound_sms", text=s.pick(("ALTO", "alto", "BAJA")))
    elif name == "typo_stop_then_send":
        add(60, "inbound_sms", text=s.pick(TYPO_STOPS))
    elif name == "look_alike_word_then_send":
        add(60, "inbound_sms", text=s.pick(LOOK_ALIKES))
    elif name == "help_then_send":
        add(60, "inbound_sms", text=s.pick(("HELP", "help", "INFO")))
    elif name == "number_reassigned_then_send":
        add(60, "number_reassigned", source="reassigned_numbers_database")
    elif name == "carrier_opt_out_then_send":
        add(60, "carrier_opt_out")
    if name == "stop_then_start_keyword_then_send":
        add(120, "inbound_sms", text=s.pick(("START", "UNSTOP", "YES")))
    if name == "stop_then_new_written_consent_then_send":
        add(180, "opt_in", consent="express_written", source="web_form")
    if name == "race_stop_and_send_same_instant":
        add(60, "inbound_sms", text="STOP")
        add(60, "send_attempt", message_class="informational", send_id=f"send-{client}")
        return ev
    add(240, "send_attempt", message_class="marketing" if marketing else "informational",
        send_id=f"send-{client}")
    return ev


def generate_f5(seed: str, target_events: int = 10000) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{F5_POOL}|{seed}")
    events: list[dict] = []
    key: list[dict] = []
    i = 0
    block: list[tuple] = []
    while len(events) < target_events:
        if not block:   # every episode type once per block, in a fresh order: position says nothing
            block = root.child(f"block{i}").shuffled(F5_EPISODES)
        name, decision, reason = block.pop()
        s = root.child(f"ep{i}")
        # Consent is held per (client business, number). Each episode is its own client, so episodes never share a
        # consent record even though the fictional number range is small; the same number appears for many clients.
        number = common.fixture_phone(s.pick(sorted(AREA_TZ)), 50 + s.below(50))
        ep = _episode(name, s, common.opaque_id("client", seed, i), number, s.between(0, 30 * 86400))
        events.extend(ep)
        send = [e for e in ep if e["kind"] == "send_attempt"][-1]
        key.append({"send_id": send["send_id"], "episode": name, "decision": decision, "reason": reason})
        i += 1
    # Stable sort by time; within one instant the generator keeps file order, and the keeper must STILL apply the
    # opt-out first (the race episode lists the STOP first, but replay order must not be what saves it).
    order = root.child("order").shuffled(list(range(len(events))))
    shuffled = [events[k] for k in order]
    shuffled.sort(key=lambda e: e["t"])
    for e in shuffled:
        e.update({"fixture": common.FIXTURE_LABEL, "pool": F5_POOL})
    return shuffled, key
