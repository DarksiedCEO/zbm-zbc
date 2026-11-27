"""RR pool B: synthetic service businesses with planted revenue leaks (test-set spec v0, "RR pool B").

Two outputs, kept apart on purpose:
- ``world``: the simulated businesses (site, forms, mobile pages, Business-Profile-like listing, reviews, phone line,
  DM inbox, consent on file, 30-day lead log). This is what the test harness simulates; an agent under test sees it
  only through the harness (submitting a form, rendering a page, placing a consented test call), never by reading it.
- ``answer_key``: what each detector (RR-S1..S6) must report for each business, what it must refuse, and the planted
  effect (which leads were lost to which leak). Kept by the reviewer lane.

Controls have no planted leak and a clean log: the right answer for them is "nothing to report".
"""

from __future__ import annotations

from . import common
from .common import Stream
from .verticals import NAME_SUFFIX, NAME_WORDS, VERTICAL_IDS, VERTICALS

POOL_ID = "rr-pool-b"
LEAKY_COUNT = 120
CONTROL_COUNT = 20

FORM_BUILDERS = ("wordpress_cf7", "wix", "squarespace", "jotform", "typeform", "hubspot", "custom")
VIEWPORTS = ("360x800", "390x844", "768x1024")

# Leak classes per detector. Each maps to a class named in the test-set spec for that agent.
LEAK_CLASSES: dict[str, tuple[str, ...]] = {
    "RR-S1": ("form_js_error", "form_captcha_loop", "form_dead_endpoint", "form_mail_to_spam",
              "form_wrong_inbox", "form_slow_reply", "form_never_replied"),
    "RR-S2": ("ah_voicemail_full", "ah_no_answer", "ah_ivr_loop", "ah_voicemail_no_callback", "ah_slow_callback"),
    "RR-S3": ("tel_missing", "tel_broken", "popup_covers_call", "tap_target_small", "image_only_phone"),
    "RR-S4": ("gbp_missing_field", "gbp_empty_field", "gbp_wrong_phone", "gbp_wrong_hours"),
    "RR-S5": ("review_recency_gap", "review_low_reply_rate", "review_slow_reply", "review_star_trend_down",
              "review_burst"),
    "RR-S6": ("dm_never", "dm_slow", "dm_auto_reply_only"),
}
CLASS_TO_AGENT = {cls: agent for agent, classes in LEAK_CLASSES.items() for cls in classes}
ALL_CLASSES: tuple[str, ...] = tuple(CLASS_TO_AGENT)

# Classes whose answer is "flag it, don't score it" (review burst: possible fake reviews).
FLAG_ONLY = {"review_burst"}
# Detectors that contact the business and so need recorded consent first (agent spec, shared rule).
CONTACT_AGENTS = {"RR-S1": "form_test", "RR-S2": "call_test", "RR-S6": "dm_test"}
# Classes with a lead-log effect: the lost leads are planted in the log and named in the answer key.
EFFECT_CHANNEL = {
    **{c: "form" for c in LEAK_CLASSES["RR-S1"]},
    **{c: "after_hours_call" for c in LEAK_CLASSES["RR-S2"]},
    **{c: "dm" for c in LEAK_CLASSES["RR-S6"]},
}
GBP_FIELDS = ("name", "primary_category", "phone", "website", "hours", "address", "photos_count",
              "booking_link", "description", "attributes")


def _edge(s: Stream, boundary: int, lo: int, hi: int) -> int:
    """A value on the correct side of a threshold; one time in four exactly at the boundary, so off-by-one bites."""
    return boundary if s.chance(1, 4) else s.between(lo, hi)


def _name(s: Stream, vertical: str, index: int) -> str:
    return f"{s.pick(NAME_WORDS)} {NAME_SUFFIX[vertical]} {index:03d} (Fixture)"


def _site(s: Stream, slug: str, classes: set[str]) -> dict:
    builder = s.pick(FORM_BUILDERS)
    form = {
        "form_id": "contact",
        "page": "/contact",
        "builder": builder,
        "steps": 2 if s.chance(1, 4) else 1,           # adversarial: multi-step forms are not a fault
        "honeypot_field": s.chance(1, 3),              # adversarial: a hidden spam trap is not a fault
        "fields": ["name", "phone", "email", "message"],
        "js_error_on_submit": "form_js_error" in classes,
        "captcha": "loop" if "form_captcha_loop" in classes else s.pick(["none", "checkbox", "invisible"]),
        "endpoint_status": 404 if "form_dead_endpoint" in classes else 200,
        "rate_limit_after_submits": s.pick([None, None, 2, 5]),   # dependency case: must fail closed, not guess
        "delivery": ("spam_folder" if "form_mail_to_spam" in classes else
                     "unmonitored_inbox" if "form_wrong_inbox" in classes else "inbox"),
        "human_reply_minutes": (None if "form_never_replied" in classes else
                                _edge(s, 241, 300, 2880) if "form_slow_reply" in classes else _edge(s, 240, 3, 45)),
    }
    mobile = {}
    broken_vp = s.pick(VIEWPORTS[:2])
    for vp in VIEWPORTS:
        phone_display = "image" if "image_only_phone" in classes else "text"
        tel = ("missing" if "tel_missing" in classes else
               "malformed" if ("tel_broken" in classes and vp == broken_vp) else "ok")
        mobile[vp] = {
            "phone_display": phone_display,
            "tel_link": "missing" if phone_display == "image" else tel,
            "call_button_px": (s.pick([18, 23]) if ("tap_target_small" in classes and vp != "768x1024")
                               else s.pick([24, 44, 48, 56])),     # 24 px exactly meets WCAG 2.2 minimum
            "popup_covers_call_button": "popup_covers_call" in classes and vp != "768x1024",
            "sticky_call_bar": s.chance(1, 2),
        }
    return {"domain": common.fixture_domain(slug), "forms": [form], "mobile": mobile}


def _gbp(s: Stream, biz: dict, classes: set[str]) -> dict:
    service_area_only = biz["vertical"] in ("hvac", "plumbing") and s.chance(1, 3)
    gbp = {
        "name": biz["name"],
        "primary_category": VERTICALS[biz["vertical"]]["label"],
        "phone": biz["main_phone"],
        "website": "https://" + biz["website"]["domain"] + "/",
        "hours": VERTICALS[biz["vertical"]]["hours"],
        # Adversarial: a service-area business with no public address is CORRECT, not a missing field.
        "address": None if service_area_only else f"{100 + s.below(900)} Fixture Way, Unit {s.between(1, 9)}",
        "service_area_only": service_area_only,
        "photos_count": s.between(8, 60),
        "booking_link": "https://" + biz["website"]["domain"] + "/book",
        "description": f"{biz['name']} serves customers in {biz['state']}. Fixture listing.",
        "attributes": ["online_appointments"] if s.chance(1, 2) else ["wheelchair_accessible_entrance"],
        "seasonal_hours": s.chance(1, 6),  # adversarial: seasonal hours are not "wrong hours"
    }
    planted: list[str] = []
    candidates = [f for f in GBP_FIELDS if f not in ("name", "address", "phone", "hours")]
    if "gbp_missing_field" in classes:
        f = s.pick(candidates)
        gbp[f] = None
        planted.append(f)
    if "gbp_empty_field" in classes:
        f = s.pick([c for c in candidates if c not in planted])
        gbp[f] = [] if f == "attributes" else 0 if f == "photos_count" else ""
        planted.append(f)
    if "gbp_wrong_phone" in classes:
        area = biz["main_phone"][2:5]
        gbp["phone"] = common.fixture_phone(area, (int(biz["main_phone"][-2:]) + 7) % 50)
        planted.append("phone")
    if "gbp_wrong_hours" in classes:
        gbp["hours"] = dict(gbp["hours"], mon_fri=["10:00", "15:00"])
        planted.append("hours")
    return {"listing": gbp, "planted_fields": planted}


def _reviews(s: Stream, classes: set[str]) -> list[dict]:
    reviews = []
    n = s.between(3, 7) * 10          # a multiple of 10, so the 30% boundary case is exact
    edge_reply_rate = not ({"review_low_reply_rate", "review_star_trend_down"} & classes) and s.chance(1, 5)
    gap = "review_recency_gap" in classes
    newest = None
    for i in range(n):
        # A null business always has a recent review, so "no review in 90 days" is only ever the planted gap.
        # The first review carries the boundary (91 days = a gap; 90 = not); the rest spread out.
        if i == 0:
            days_ago = s.pick([91, s.between(120, 729)]) if gap else s.pick([90, s.between(1, 30)])
            newest = days_ago
        else:
            # when the newest review sits exactly on the 90-day line, nothing newer may undercut it
            days_ago = s.between(120, 729) if gap else s.between(90 if newest == 90 else 1, 729)
        stars = s.pick([5, 5, 5, 4, 4, 3]) if "review_star_trend_down" not in classes else (
            s.pick([5, 5, 4]) if days_ago > 180 else s.pick([2, 1, 2, 1]))
        if "review_low_reply_rate" in classes:
            replied = i % 5 == 0          # 1 in 5 organic reviews replied: always under the 30% line, never zero
        elif edge_reply_rate:
            replied = i % 10 < 3          # exactly 30%: on the line, NOT a low reply rate
        else:
            replied = s.chance(4, 5)
        reply_days = None
        if replied:
            reply_days = (s.pick([8, s.between(10, 40)]) if "review_slow_reply" in classes
                          else s.pick([7, s.between(0, 3)]))
        reviews.append({"review_id": None, "days_ago": days_ago, "stars": stars,
                        "owner_reply_after_days": reply_days})
    if "review_star_trend_down" in classes:
        # guarantee a recent window to judge: three more low-star reviews inside the last 180 days
        for j in range(3):
            reviews.append({"review_id": None, "days_ago": s.between(120, 180) if gap else s.between(1, 180),
                            "stars": s.pick([1, 2]), "owner_reply_after_days": None})
    # adversarial: owner replies on very old reviews must not inflate the recent reply rate
    reviews.append({"review_id": None, "days_ago": 900, "stars": 4, "owner_reply_after_days": 1})
    if "review_burst" in classes:
        # The burst sits in a stretch with no other review within 3 days either side, so the oracle can tell it apart
        # from organic reviews by the data alone (ten or more inside a 3-day window), never by an id.
        taken = {r["days_ago"] for r in reviews}
        lo, hi = (5, 60) if not gap else (150, 400)
        wide = s.shuffled(list(range(lo, 700)))          # fall back to anywhere inside the 730-day window
        start = next((d for d in s.shuffled(list(range(lo, hi + 1))) + wide
                      if not any(d - 3 <= t <= d + 5 for t in taken)), None)
        if start is None:
            raise AssertionError("no free stretch for the burst")
        for j in range(s.pick([10, 12])):    # 10 = exactly the burst line
            reviews.append({"review_id": None, "days_ago": start + (j % 3), "stars": 5,
                            "owner_reply_after_days": None})
    # Ids carry nothing: random hex, so neither an id nor the order of equal-day reviews hints at what was planted.
    ids = set()
    for r in reviews:
        while (rid := f"rev-{s.below(16 ** 10):010x}") in ids:
            pass
        ids.add(rid)
        r["review_id"] = rid
    return sorted(reviews, key=lambda r: (r["days_ago"], r["review_id"]))


def _phone_line(s: Stream, biz: dict, classes: set[str]) -> dict:
    behavior = ("voicemail_full" if "ah_voicemail_full" in classes else
                "no_answer" if "ah_no_answer" in classes else
                "ivr_loop" if "ah_ivr_loop" in classes else
                "voicemail" if ({"ah_voicemail_no_callback", "ah_slow_callback"} & classes) else
                s.pick(["answering_service", "forward_to_on_call"]))
    callback = (None if behavior in ("voicemail_full", "no_answer", "ivr_loop") or
                "ah_voicemail_no_callback" in classes else
                _edge(s, 121, 600, 1800) if "ah_slow_callback" in classes else _edge(s, 120, 5, 30))
    return {
        "business_hours": VERTICALS[biz["vertical"]]["hours"],
        "time_zone": biz["time_zone"],
        "holiday_closures": ["2026-12-25", "2027-01-01"] if s.chance(1, 2) else ["2026-12-25"],
        "ring_count_before_rollover": s.between(3, 6),
        "after_hours_behavior": behavior,
        "callback_minutes": callback,
    }


def _dm(s: Stream, classes: set[str]) -> dict:
    if "dm_never" in classes:
        return {"platforms": ["instagram", "facebook"], "auto_reply": False, "human_reply_minutes": None}
    if "dm_auto_reply_only" in classes:
        return {"platforms": ["instagram"], "auto_reply": True, "human_reply_minutes": None}
    if "dm_slow" in classes:
        return {"platforms": ["instagram", "facebook"], "auto_reply": s.chance(1, 2),
                "human_reply_minutes": _edge(s, 1441, 1500, 4320)}
    return {"platforms": ["instagram"], "auto_reply": s.chance(1, 2), "human_reply_minutes": _edge(s, 1440, 5, 90)}


def _lead_log(s: Stream, biz_id: str, classes: set[str]) -> tuple[list[dict], dict[str, list[str]]]:
    """30 days of leads. Each class with an effect loses a planted number of leads in its channel."""
    log: list[dict] = []
    lost: dict[str, list[str]] = {}
    n = 0

    def add(channel: str, outcome: str) -> str:
        nonlocal n
        n += 1
        lead_id = f"{biz_id}-L{n:03d}"
        log.append({"lead_id": lead_id, "day": s.between(1, 30), "channel": channel, "outcome": outcome})
        return lead_id

    for _ in range(s.between(15, 45)):
        add(s.pick(["form", "business_hours_call", "after_hours_call", "dm", "walk_in"]), "handled")
    for cls in sorted(classes):
        channel = EFFECT_CHANNEL.get(cls)
        if channel is None:
            continue
        outcome = "no_reply" if channel != "after_hours_call" else "unanswered"
        lost[cls] = [add(channel, outcome) for _ in range(s.between(2, 9))]
    log.sort(key=lambda r: (r["day"], r["lead_id"]))
    return log, lost


# One class at most per business for these detectors: their faults hide each other (a form that never submits can't
# also show a slow reply; a phone line has one after-hours behaviour). S4 and S5 faults can stack.
EXCLUSIVE_AGENTS = {"RR-S1", "RR-S2", "RR-S3", "RR-S6"}
CONFLICTS = {frozenset({"review_recency_gap", "review_burst"})}


def _compatible(classes: set[str], cls: str) -> bool:
    agent = CLASS_TO_AGENT[cls]
    if agent in EXCLUSIVE_AGENTS and any(CLASS_TO_AGENT[c] == agent for c in classes):
        return False
    return not any(frozenset({cls, c}) in CONFLICTS for c in classes)


def _assign_classes(slot: int, s: Stream) -> set[str]:
    """Round-robin the primary class over a SHUFFLED slot (every class appears; nothing visible gives the slot away),
    then 0-2 compatible extra classes."""
    classes = {ALL_CLASSES[slot % len(ALL_CLASSES)]}
    for _ in range(s.pick([0, 1, 1, 2])):
        cls = s.pick(ALL_CLASSES)
        if cls not in classes and _compatible(classes, cls):
            classes.add(cls)
    return classes


def generate(seed: str) -> tuple[list[dict], list[dict]]:
    """Return (world, answer_key) for one seed. Deterministic."""
    root = Stream(f"{POOL_ID}|{seed}")
    world: list[dict] = []
    key: list[dict] = []
    total = LEAKY_COUNT + CONTROL_COUNT
    # AEGIS round 2: vertical and phone used to follow the loop index, which gave away control status and the planted
    # class. Role, vertical and region now come from three independent per-seed shuffles.
    role = root.child("role").shuffled(list(range(total)))
    vslot = root.child("vertical").shuffled(list(range(total)))
    rslot = root.child("region").shuffled(list(range(total)))
    for i in range(total):
        s = root.child(f"biz{i}")
        control = role[i] >= LEAKY_COUNT
        vertical = VERTICAL_IDS[vslot[i] % len(VERTICAL_IDS)]
        area, state, tz = common.REGION_AREA_CODES[rslot[i] % len(common.REGION_AREA_CODES)]
        classes: set[str] = set() if control else _assign_classes(role[i], s.child("classes"))
        biz_id = common.opaque_id("svc", seed, i)
        name = _name(s, vertical, s.below(1000))
        biz = {
            "fixture": common.FIXTURE_LABEL,
            "pool": POOL_ID,
            "generator": common.GENERATOR_VERSION,
            "business_id": biz_id,
            "vertical": vertical,
            "name": name,
            "state": state,
            "time_zone": tz,
            "main_phone": common.fixture_phone(area, s.child("phone-no").below(50)),
        }
        biz["website"] = _site(s.child("site"), common.slugify(name) + "-" + biz_id[-6:], classes)
        gbp = _gbp(s.child("gbp"), biz, classes)
        biz["profile"] = gbp["listing"]
        biz["reviews"] = _reviews(s.child("reviews"), classes)
        biz["phone_line"] = _phone_line(s.child("phone"), biz, classes)
        biz["dm_inbox"] = _dm(s.child("dm"), classes)
        cs = s.child("consent")
        consent = {k: True for k in CONTACT_AGENTS.values()}
        # About 1 in 7 businesses lacks one consent: the detector that needs it must refuse (the refusal IS the pass).
        missing_consent = None
        if cs.chance(1, 7):
            missing_consent = cs.pick(sorted(CONTACT_AGENTS))
            consent[CONTACT_AGENTS[missing_consent]] = False
        biz["consent"] = dict(consent, window_local=["09:00", "17:00"], max_tests_per_type=3)
        biz["lead_log"], lost = _lead_log(s.child("log"), biz_id, classes)
        world.append(biz)

        expected, refusals = [], []
        for cls in sorted(classes):
            agent = CLASS_TO_AGENT[cls]
            if agent == missing_consent:
                continue
            entry = {"agent": agent, "class": cls, "action": "flag_not_score" if cls in FLAG_ONLY else "report"}
            if cls in lost:
                entry["effect"] = {"lost_leads_30d": len(lost[cls]), "lead_ids": lost[cls]}
            if agent == "RR-S4":
                entry["fields"] = gbp["planted_fields"]
            expected.append(entry)
        if missing_consent:
            hidden = sorted(c for c in classes if CLASS_TO_AGENT[c] == missing_consent)
            refusals.append({"agent": missing_consent, "reason": "consent_missing",
                             "planted_classes_hidden_by_refusal": hidden,
                             "hidden_effect_lead_ids": sorted(lid for c in hidden for lid in lost.get(c, []))})
        key.append({"business_id": biz_id, "control": control, "expected_findings": expected,
                    "must_refuse": refusals,
                    "not_a_fault": [x for x, on in (("service_area_no_address", gbp["listing"]["service_area_only"]),
                                                    ("seasonal_hours", gbp["listing"]["seasonal_hours"]),
                                                    ("multi_step_form", biz["website"]["forms"][0]["steps"] > 1),
                                                    ("honeypot_field", biz["website"]["forms"][0]["honeypot_field"]))
                                    if on]})
    # Shuffle so a record's position says nothing about whether it is a control or which class was planted.
    order = root.child("order").shuffled(list(range(len(world))))
    return [world[j] for j in order], [key[j] for j in order]
