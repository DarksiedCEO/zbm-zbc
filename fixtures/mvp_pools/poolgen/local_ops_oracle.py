"""Independent oracle for SEO-L1 (listing sync) and SEO-L6 (review requests).

L1 re-derives each platform's action by diffing the listing against the approved record itself (phone compared by
digits, so a formatting change is not drift) and reading the edit log: a differing field the OWNER edited after the
last sync is a conflict to surface; any other difference is drift to update with approval; a failed platform call is
reported and never marked synced.

L6 re-derives send/refuse and the reason from the scenario itself: no transaction, an SMS without SMS consent, an
opt-out, an audience filter (review gating), an incentive, suggested review text, or a "contact us instead of
reviewing" line (review suppression) each refuse. The message checks use this module's own patterns; they are
written against English messages only.
"""

from __future__ import annotations

import re

L1_FIELDS = ("name", "phone", "hours", "website", "primary_category")


def _norm(field: str, value):
    if field == "phone" and isinstance(value, str):
        digits = re.sub(r"\D", "", value)
        return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    return value


def derive_l1_platform(record: dict, listing: dict) -> dict:
    if listing["api_status"] != "ok":
        return {"platform": listing["platform"], "action": "report_platform_error", "fields": [], "mark_synced": False}
    diffs = sorted(f for f in L1_FIELDS if _norm(f, listing["fields"].get(f)) != _norm(f, record.get(f)))
    if not diffs:
        return {"platform": listing["platform"], "action": "none", "fields": [], "mark_synced": True}
    owner_after_sync = {e["field"] for e in listing["edit_log"]
                        if e["edited_by"] == "owner" and e["days_ago"] < listing["last_sync_days_ago"]}
    action = "surface_conflict" if owner_after_sync & set(diffs) else "update_with_approval"
    return {"platform": listing["platform"], "action": action, "fields": diffs, "mark_synced": False}


def check_l1(subjects: list[dict], key: list[dict]) -> list[str]:
    errors: list[str] = []
    if sorted(s["business_id"] for s in subjects) != sorted(k["business_id"] for k in key) or \
            len({k["business_id"] for k in key}) != len(key):
        errors.append("L1: answer rows do not match the businesses one to one")
    by_id = {k["business_id"]: k for k in key}
    for s in subjects:
        k = by_id.get(s["business_id"])
        if k is None:
            continue
        want = {"business_id": s["business_id"],
                "platforms": [derive_l1_platform(s["approved_record"], lst) for lst in s["listings"]]}
        if k != want:
            errors.append(f"L1 {s['business_id']}: key differs from the listings")
    return errors


_INCENTIVE = re.compile(r"% off|gift card|chance to win|\bis free\b|\bfor free\b|\bdiscount\b|\breward\b", re.IGNORECASE)
_SUGGESTED = re.compile(r"you could say|copy this|here'?s what to write", re.IGNORECASE)
_SUPPRESS = re.compile(r"instead of (posting|leaving) a review|contact us (first|instead)", re.IGNORECASE)


def derive_l6(req: dict) -> dict:
    c, msg = req["customer"], req["message"]
    checks = (("no_transaction", c["transaction"] is None),
              ("no_sms_consent", req["channel"] == "sms" and not c["sms_consent"]),
              ("opted_out", c["opted_out"]),
              ("gating", bool(req["audience_filter"])),
              ("incentive", bool(_INCENTIVE.search(msg))),
              ("suggested_text", bool(_SUGGESTED.search(msg))),
              ("review_suppression", bool(_SUPPRESS.search(msg))))
    failed = [name for name, bad in checks if bad]
    return {"request_id": req["request_id"], "decision": "refuse" if failed else "send",
            "reason": failed[0] if failed else None}


def check_l6(subjects: list[dict], key: list[dict]) -> list[str]:
    errors: list[str] = []
    if sorted(s["request_id"] for s in subjects) != sorted(k["request_id"] for k in key) or \
            len({k["request_id"] for k in key}) != len(key):
        errors.append("L6: answer rows do not match the requests one to one")
    by_id = {k["request_id"]: k for k in key}
    for s in subjects:
        k = by_id.get(s["request_id"])
        if k is not None and k != derive_l6(s):
            errors.append(f"L6 {s['request_id']}: key {k} differs from the scenario {derive_l6(s)}")
    return errors
