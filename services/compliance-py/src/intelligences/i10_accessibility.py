"""
Intelligence 10 — Accessibility Check (spec C.8).

Pass/fail of the provider's WCAG 2.1 AA check for an EXACT content hash.
The publish gate passes HR-09 only with a stored ``passed`` result for the
same ``asset_content_sha256``, no older than COMPLIANCE_A11Y_MAX_AGE_DAYS,
from a scan run with overlay scripts disabled; for video the provider must
cover captions (WCAG 1.2.x), else fail. An overlay widget never substitutes
for a fix, and there is no pass without a provider result.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from clock import parse_iso

NUMBER, NAME, ACTOR = 10, "Accessibility Check", "intel_10_accessibility"


def a11y_problem(results: list[dict], content_sha256: Optional[str], asset_type: Optional[str], now: datetime,
                 max_age_days: int) -> Optional[str]:
    if not content_sha256:
        return "no content hash to match an accessibility result against"
    candidates = [r for r in results if r.get("content_sha256") == content_sha256 and r.get("available")]
    if not candidates:
        return "no accessibility result for this exact content (provider not wired or never run)"
    latest = max(candidates, key=lambda r: r["checked_at"])
    if not latest.get("passed"):
        return f"latest WCAG 2.1 AA check failed ({latest.get('violations_count', 0)} violations)"
    if latest.get("standard") != "WCAG 2.1 AA":
        return "latest result is not a WCAG 2.1 AA check"
    if not latest.get("overlay_scripts_disabled"):
        return "scan did not run with overlay scripts disabled (overlays are not a remediation)"
    if asset_type == "ad_video" and not latest.get("covers_captions"):
        return "provider does not cover captions (WCAG 1.2.x) for video"
    try:
        at = parse_iso(latest["checked_at"])
    except (KeyError, ValueError):
        return "accessibility result has no valid check time"
    if at < now - timedelta(days=max_age_days):
        return f"accessibility result older than {max_age_days} days: re-check required"
    return None
