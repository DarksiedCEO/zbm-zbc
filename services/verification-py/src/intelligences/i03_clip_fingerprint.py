"""
Intelligence 3 — Clip Fingerprint (spec §C.3): same_clip pass | fail | unknown. Never downloads posted media.

Approval fingerprint (taken when Creative registers Clip Review ``pass``): video id SHA-256, author HMAC,
platform create time, duration, caption SHA-256 (NFKC, whitespace-collapsed), and — TikTok only, with
VI_TT_COVER_PDQ=1 and a wired hasher — the cover image PDQ. Later checks compare the settlement fetch with it:
id, author, create time or duration (> 0.5 s) differ, or PDQ distance > VI_PDQ_MAX_HAMMING → HASH_MISMATCH
(VI-07); caption hash changed → CAPTION_CHANGED (VI-08). With VI_REQUIRE_PERCEPTUAL_MATCH=1 (default) a
metadata-only platform, or no PDQ on either side, is FINGERPRINT_UNAVAILABLE: ``unknown`` counts as fail.
"""

from __future__ import annotations

from typing import Optional

from platforms import PERCEPTUAL_CAPABLE
from reasons import item

NUMBER, NAME, ACTOR = 3, "Clip Fingerprint", "intel_03_clip_fingerprint"
DURATION_TOLERANCE_MS = 500


def fingerprint(video_id_sha256: str, author_id_hmac: str, create_time: int, duration_ms: Optional[int],
                caption_sha256: Optional[str], cover_pdq: Optional[str]) -> dict:
    return {"video_id_sha256": video_id_sha256, "author_id_hmac": author_id_hmac, "create_time": create_time,
            "duration_ms": duration_ms, "caption_sha256": caption_sha256, "cover_pdq": cover_pdq}


def compare(approval: Optional[dict], now: Optional[dict], platform: str, *, require_perceptual: bool,
            cover_pdq_enabled: bool, pdq_distance: Optional[int], max_hamming: int, rules: dict,
            evidence: tuple = ()) -> tuple[str, list[dict]]:
    if approval is None:
        return "unknown", [item("FINGERPRINT_UNAVAILABLE", "no approval fingerprint (Clip Review pass not registered "
                                "or the approval fetch failed)", evidence, rules)]
    if now is None:
        return "unknown", [item("FINGERPRINT_UNAVAILABLE", "no platform fetch to compare with the approval fingerprint",
                                evidence, rules)]
    reasons = []
    diffs = [k for k in ("video_id_sha256", "author_id_hmac", "create_time") if approval[k] != now[k]]
    a_d, n_d = approval.get("duration_ms"), now.get("duration_ms")
    if a_d is not None and n_d is not None and abs(a_d - n_d) > DURATION_TOLERANCE_MS:
        diffs.append("duration")
    elif (a_d is None) != (n_d is None):
        diffs.append("duration_presence")
    if diffs:
        reasons.append(item("HASH_MISMATCH", "posted clip differs from the approved clip: " + ", ".join(diffs),
                            evidence, rules))
    if approval.get("caption_sha256") != now.get("caption_sha256"):
        reasons.append(item("CAPTION_CHANGED", "caption/description hash changed since approval", evidence, rules))
    if require_perceptual:
        if platform not in PERCEPTUAL_CAPABLE or not cover_pdq_enabled:
            reasons.append(item("FINGERPRINT_UNAVAILABLE", f"{platform}: metadata-only evidence while a perceptual "
                                "match is required (VI_REQUIRE_PERCEPTUAL_MATCH=1)", evidence, rules))
        elif approval.get("cover_pdq") is None or now.get("cover_pdq") is None or pdq_distance is None:
            reasons.append(item("FINGERPRINT_UNAVAILABLE", "cover PDQ unavailable (hasher not wired or cover not "
                                "fetched)", evidence, rules))
        elif pdq_distance > max_hamming:
            reasons.append(item("HASH_MISMATCH", f"cover PDQ distance {pdq_distance} > {max_hamming}", evidence, rules))
    elif (platform in PERCEPTUAL_CAPABLE and cover_pdq_enabled and pdq_distance is not None
          and pdq_distance > max_hamming):
        reasons.append(item("HASH_MISMATCH", f"cover PDQ distance {pdq_distance} > {max_hamming}", evidence, rules))
    if any(r["code"] in ("HASH_MISMATCH", "CAPTION_CHANGED") for r in reasons):
        return "fail", reasons
    if reasons:
        return "unknown", reasons
    return "pass", []
