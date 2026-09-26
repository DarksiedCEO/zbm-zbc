"""
Intelligence 8 — Stolen-Content Check (spec §C.8, VI-14): clear | match (hold) | incomplete (human). Never
rejects on its own.

At registration the submitted file's signature (perceptual, via the hasher) or its exact SHA-256 (fallback,
via the media intake) is compared with (a) every other clipper's submissions (all campaigns) and (b) the
campaign's seed clips. A match opens a ``stolen_content`` finding and a STOLEN_MATCH hold, naming the earliest
V&I receipt as the presumed original. Hasher unavailable and no exact match → STOLEN_CHECK_INCOMPLETE → hold
for a human (unknown → human).
"""

from __future__ import annotations

from typing import Callable, Optional

NUMBER, NAME, ACTOR = 8, "Stolen-Content Check", "intel_08_stolen_content"


def check(media_sha: Optional[str], signature: Optional[dict], others: list[dict], seeds: list[dict],
          match_fn: Callable[[dict, dict], Optional[bool]]) -> dict:
    """``others``: [{submission_id, clipper_id, media_sha256, signature, received_seq}] of OTHER clippers;
    ``seeds``: [{ref, media_sha256, signature}]. Returns {status, matched: [...], presumed_original}."""
    matched = []
    perceptual_complete = signature is not None
    for o in others:
        if o.get("media_sha256") is None and o.get("signature") is None:
            continue      # that clip's own file is unreadable: its own check is incomplete and held, not this one
        hit = media_sha is not None and o.get("media_sha256") == media_sha
        if not hit and signature is not None and o.get("signature") is not None:
            m = match_fn(signature, o["signature"])
            if m is None:
                perceptual_complete = False
            hit = bool(m)
        elif signature is not None and o.get("signature") is None and not hit:
            perceptual_complete = False
        if hit:
            matched.append({"kind": "submission", "id": o["submission_id"], "clipper_id": o["clipper_id"],
                            "received_seq": o["received_seq"]})
    for s in seeds:
        hit = media_sha is not None and s.get("media_sha256") == media_sha
        if not hit and signature is not None and s.get("signature") is not None:
            m = match_fn(signature, s["signature"])
            if m is None:
                perceptual_complete = False
            hit = bool(m)
        elif signature is not None and s.get("signature") is None and not hit:
            perceptual_complete = False
        if hit:
            matched.append({"kind": "seed", "id": s["ref"], "clipper_id": None, "received_seq": 0})
    if matched:
        first = min(matched, key=lambda m: m["received_seq"])
        return {"status": "match", "matched": matched, "presumed_original": first["id"]}
    if media_sha is None and signature is None:
        return {"status": "incomplete", "matched": [], "presumed_original": None,
                "why": "submitted file unreadable (media intake / hasher not wired)"}
    if not perceptual_complete:
        return {"status": "incomplete", "matched": [], "presumed_original": None,
                "why": "exact-hash comparison only: a re-encoded copy would not match (hasher unavailable)"}
    return {"status": "clear", "matched": [], "presumed_original": None}
