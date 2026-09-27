"""
Intelligence 7 — Takedown Desk (Legal spec §B.8, §C.7). Decides the notice-validity checklist, the counter-notice
restore window, forwarding and counts; never decides fair use or infringement.

- In: valid = all six 17 U.S.C. §512(c)(3)(A) elements present (a checklist, not a judgment). An invalid notice
  is recorded; if any element is marked arguable it is packaged for counsel. A valid notice against a
  ``platform_post`` is counted against its ``post_ref_sha256`` (V&I reads the count), forwarded to Clipper
  Network and Creative, and opens an S3 matter; against ``zbc_hosted`` material it is also marked for removal
  under the procedure (Legal removes nothing itself) and counted per uploader for the §512(i) policy.
- Counter-notice: restore window = receipt + 10 .. + 14 business days (§512(g)(2); ``bizdays``), unless the
  claimant files an action. Restoring before the 10th business day is refused.
- Count for V&I: open valid notices naming the post (not withdrawn, not restored after a counter-notice).
"""

from __future__ import annotations

NUMBER, NAME, ACTOR = 7, "Takedown Desk", "intel_07_takedowns"
ELEMENTS = ("signature", "work_identified", "material_located", "contact", "good_faith_statement",
            "perjury_statement")
COUNTED = ("actioned", "counter_noticed", "litigated")
PLATFORMS = ("tiktok", "youtube", "instagram", "x", "zbc_portal", "other")


def valid(elements: dict) -> bool:
    return all(elements.get(e) is True for e in ELEMENTS)


def counts(notice: dict) -> bool:
    return notice["direction"] == "in" and notice["valid"] and notice["status"] in COUNTED
