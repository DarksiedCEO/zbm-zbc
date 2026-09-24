"""
P9 — ad-disclosure training + pre-post disclosure check for ZBC clippers.

Pre-post check (explicit rule, DRAFT pending counsel/FTC-guidance review):
a sponsored post's caption must contain a clear disclosure marker, and it
must appear within the first ``VISIBLE_CHARS`` characters (so it is not
buried below a "more" fold or after a wall of hashtags). No marker, or a
buried one => the post is blocked, with the exact fix.
"""

from __future__ import annotations

import re

VISIBLE_CHARS = 100
_MARKER = re.compile(r"(?i)(#ad\b|#sponsored\b|#paidpartnership\b|\bpaid partnership\b|\bsponsored\b|\badvertisement\b)")


def check_caption(caption: str) -> tuple[bool, str]:
    m = _MARKER.search(caption or "")
    if m is None:
        return False, 'blocked: no ad disclosure. Add "#ad" (or "Paid partnership") at the start of the caption.'
    if m.start() >= VISIBLE_CHARS:
        return False, f'blocked: the disclosure appears after character {VISIBLE_CHARS}, where viewers may not see it. Move "#ad" to the start of the caption.'
    return True, "disclosure present and visible"
