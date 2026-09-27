"""
Intelligence 9 — Music/Rights Policy Gate (Legal spec §C.9, LG-12). Decides allow/block by the music policy;
never clears a track or judges a licence (Creative Production's Rights & Clearance executes clearance).

All rules are evaluated (complete reasons, no short-circuit):
- no music -> allowed (LG-12a);
- ``music_changed_since_approval`` or a swap on a repost/re-edit -> blocked always (LG-12b; the Quince complaint
  alleges a repost after "replacing the music");
- ``source = licensed`` -> blocked (policy D3: platform commercial libraries only; Andre may change the policy);
- ``source = commercial_library`` needs a track id AND the platform's library rule verified (LG-12 parameter
  ``verified_platform_libraries``; TikTok only today) AND CQ-21 verified -> else blocked ``HELD_PENDING_COUNSEL``
  citing CQ-21;
- music present with ``source = none`` -> blocked (undeclared source).
"""

from __future__ import annotations

import reasons as R

NUMBER, NAME, ACTOR = 9, "Music/Rights Policy Gate", "intel_09_music_policy"
SOURCES = ("commercial_library", "licensed", "none")


def ruling(req: dict, verified_libraries: list[str], cq21_verified: bool) -> tuple[bool, list[dict]]:
    m = req["music"]
    out = []
    if not m["present"] and (m["source"] != "none" or m.get("track_or_license_id")):
        out.append(R.item("MUSIC_SOURCE_UNDECLARED", "music marked absent but a source or track id was given"))
    if req["music_changed_since_approval"]:
        out.append(R.item("MUSIC_CHANGED", "a music swap on a repost or re-edit: blocked always"
                          if req["reposted_or_reedited_by_zbc"] else "music changed since approval: blocked always"))
    if not m["present"]:
        return (not out), out
    if m["source"] == "none":
        out.append(R.item("MUSIC_SOURCE_UNDECLARED", "music present with no declared source"))
    elif m["source"] == "licensed":
        out.append(R.item("MUSIC_LICENSED_BLOCKED", "policy D3 allows platform commercial-library music only"))
    else:
        if not m.get("track_or_license_id"):
            out.append(R.item("TRACK_ID_MISSING", "commercial-library music needs its track id per clip"))
        if req["platform"] not in verified_libraries:
            out.append(R.item("PLATFORM_LIBRARY_UNVERIFIED", f"the {req['platform']} commercial-library rule is not "
                              "verified"))
        if not cq21_verified:
            out.append(R.item("HELD_PENDING_COUNSEL", "whether a platform commercial library licenses this use is a "
                              "counsel question", cq_id="CQ-21"))
    return (not out), out
