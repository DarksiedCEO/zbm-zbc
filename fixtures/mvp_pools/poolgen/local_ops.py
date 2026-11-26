"""SEO-L1 (listing sync) and SEO-L6 (review requests, FTC guard) rule sets (test-set spec v0).

L1: 60 businesses x 5 platforms. The client-approved record is the source of truth. Each platform listing is in sync,
drifted (changed by someone other than the owner: update it, with approval), hand-edited by the OWNER after the last
sync (surface the conflict, never overwrite), or the platform API fails mid-sync (report it, never mark it synced).
Traps on in-sync listings: the phone shown in national format ("(602) 555-0123", formatting only, not drift), and an
old owner edit made BEFORE the last sync (already reconciled, not a conflict).

L6: 300 review-request scenarios. A request goes out only to a real customer after a real transaction, with consent
for the channel, to everyone (no gating), with no incentive and no suggested review text (FTC 16 CFR Part 465, the
Consumer Review Rule; Google also bars selectively soliciting positive reviews). Everything else must be refused.
"""

from __future__ import annotations

from . import common
from .common import Stream

L1_POOL, L6_POOL = "seo-l1-sync", "seo-l6-review-requests"
PLATFORMS = ("google_business_profile", "apple_business_connect", "bing_places", "yelp", "facebook")
SYNC_FIELDS = ("name", "phone", "hours", "website", "primary_category")
L1_STATES = ("in_sync", "drifted", "owner_hand_edit", "api_error")


def _record(s: Stream) -> dict:
    area = s.pick(("602", "702", "503", "206", "213", "619"))
    return {"name": f"Fixture {s.pick(('Plumbing', 'Dental', 'Bistro', 'Auto Care', 'Med Spa'))} {s.between(100, 999)}",
            "phone": common.fixture_phone(area, s.below(50)),
            "hours": {"mon_fri": ["08:00", "17:00"], "sat": s.pick([None, ["09:00", "13:00"]]), "sun": None},
            "website": f"https://www.fixture-{s.between(1000, 9999)}.test/",
            "primary_category": s.pick(("Plumber", "Dentist", "Restaurant", "Auto repair shop", "Medical spa"))}


def _changed(field: str, value, s: Stream):
    if field == "name":
        return value + " " + s.pick(("Inc", "Express", "(Old)"))
    if field == "phone":
        return common.fixture_phone(value[2:5], (int(value[-2:]) + s.between(1, 49)) % 50)
    if field == "hours":
        return dict(value, mon_fri=["09:00", "16:00"])
    if field == "website":
        return value.replace("https://www.", "https://old.")
    return "Contractor"


def generate_l1(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{L1_POOL}|{seed}")
    subjects, key = [], []
    for i in range(60):
        s = root.child(f"b{i}")
        rec = _record(s.child("rec"))
        bid = common.opaque_id("sync", seed, i)
        listings, answers = [], []
        for p in PLATFORMS:
            ps = s.child(p)
            state = ps.pick(L1_STATES)
            current = dict(rec)
            edits: list[dict] = []
            fields: list[str] = []
            # the last sync's age says nothing about the state (AEGIS round 3: it used to be 30 whenever an edit existed)
            last_sync = ps.between(21, 60)
            if state in ("drifted", "owner_hand_edit"):
                fields = sorted(ps.shuffled(SYNC_FIELDS)[: ps.between(1, 2)])
                for f in fields:
                    current[f] = _changed(f, rec[f], ps)
                    # trap: some drift is never logged (an aggregator overwrite), so only the values reveal it
                    if state == "owner_hand_edit" or not ps.chance(1, 3):
                        edits.append({"field": f, "edited_by": "owner" if state == "owner_hand_edit" else
                                      ps.pick(("platform_suggested_edit", "public_user_edit", "data_aggregator")),
                                      "days_ago": ps.between(1, last_sync - 1)})
            if state == "in_sync" and ps.chance(1, 4):
                # trap: a logged non-owner edit since the sync that was already reverted, so the values still match
                edits.append({"field": ps.pick(SYNC_FIELDS), "edited_by": "public_user_edit",
                              "days_ago": ps.between(1, last_sync - 1)})
            if state == "in_sync" and ps.chance(1, 3):
                d = rec["phone"][2:]
                current["phone"] = f"({d[0:3]}) {d[3:6]}-{d[6:]}"
            if state == "in_sync" and ps.chance(1, 4):
                edits.append({"field": ps.pick(SYNC_FIELDS), "edited_by": "owner",
                              "days_ago": last_sync + ps.between(0, 60)})   # 0 = at the sync itself: reconciled
            listings.append({"platform": p, "fields": current, "last_sync_days_ago": last_sync,
                             "edit_log": edits,
                             "api_status": ps.pick(("error_503", "timeout", "error_mid_sync")) if state == "api_error"
                             else "ok"})
            action = {"in_sync": "none", "drifted": "update_with_approval", "owner_hand_edit": "surface_conflict",
                      "api_error": "report_platform_error"}[state]
            answers.append({"platform": p, "action": action, "fields": fields, "mark_synced": state == "in_sync"})
        subjects.append({"fixture": common.FIXTURE_LABEL, "pool": L1_POOL, "business_id": bid,
                         "approved_record": rec, "listings": listings})
        key.append({"business_id": bid, "platforms": answers})
    order = root.child("order").shuffled(list(range(len(subjects))))
    return [subjects[i] for i in order], [key[i] for i in order]


L6_CASES = (("send", 120), ("no_transaction", 25), ("no_sms_consent", 25), ("opted_out", 20), ("gating", 25),
            ("incentive", 20), ("suggested_text", 25), ("review_suppression", 20),
            # two violations at once: the key lists both (AEGIS round 3: single-violation rows hid the reason logic)
            ("gating+incentive", 5), ("opted_out+suggested_text", 5), ("no_transaction+review_suppression", 5),
            ("incentive+no_sms_consent", 5))
GATING = ({"only_if_rating_at_least": 4}, {"only_if_survey_score_at_least": 9}, {"exclude_if_complained": True})
INCENTIVES = ("Leave us a review and get 10% off your next visit!", "Review us and we'll send you a $25 gift card.", "Review us for a chance to win dinner for two.",
              "Post a 5-star review and your next cleaning is free.")
SUGGESTED = ("You could say: 'Best service in town, highly recommend!'",
             "Feel free to copy this: 'Fast, friendly and fair prices.'")
SUPPRESSION = ("If you weren't happy, please contact us instead of posting a review.",)
CLEAN = ("Thanks for choosing us! Would you share your honest experience? {link}",
         "How did we do? We read every review, good or bad: {link}",
         "Feel free to tell us how we did, good or bad: {link}")   # "free" here is not an incentive


def generate_l6(seed: str) -> tuple[list[dict], list[dict]]:
    root = Stream(f"{L6_POOL}|{seed}")
    subjects, key = [], []
    n = 0
    for case, count in L6_CASES:
        for _ in range(count):
            s = root.child(f"{case}-{n}")
            channel = s.pick(("sms", "email"))
            text = s.pick(CLEAN).format(link="https://reviews.example.com/r")
            filt = None
            parts = set(case.split("+"))
            if "no_sms_consent" in parts:
                channel = "sms"
            if "gating" in parts:
                filt = s.pick(GATING)
            if "incentive" in parts:
                text = s.pick(INCENTIVES) + " https://reviews.example.com/r"
            if "suggested_text" in parts:
                text = text + " " + s.pick(SUGGESTED)
            if "review_suppression" in parts:
                text = s.pick(SUPPRESSION) + " " + text
            sid = common.opaque_id("revreq", seed, n)
            subjects.append({
                "fixture": common.FIXTURE_LABEL, "pool": L6_POOL, "request_id": sid, "channel": channel,
                "customer": {"phone": common.fixture_phone("213", s.below(50)),
                             "transaction": None if "no_transaction" in parts else
                             {"type": s.pick(("purchase", "service_visit")), "days_ago": s.between(0, 30)},
                             "sms_consent": "no_sms_consent" not in parts if channel == "sms" else s.chance(1, 2),
                             "opted_out": "opted_out" in parts},
                "audience_filter": filt, "message": text})
            reasons = [] if case == "send" else sorted(parts)
            key.append({"request_id": sid, "decision": "refuse" if reasons else "send", "reasons": reasons})
            n += 1
    order = root.child("order").shuffled(list(range(len(subjects))))
    return [subjects[i] for i in order], [key[i] for i in order]
