"""
Osei-lite — data integration hygiene for one run (ADR 0017 decision 15): quarantine for malformed inputs,
freshness timestamps, refresh tiers. Full Osei (first-party source integration, cross-source reconciliation) needs
the first-party ports, all NOT_CONNECTED in Wave 1.

A quarantined input is never parsed further and never contributes a finding beyond "this input was quarantined";
the report lists every quarantine with its reason, size and SHA-256 (never its content), and every URL only in its
safe form (primitives/safe_url.py). Past the caps, records are counted (``quarantined_dropped``,
``observations_dropped``).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Optional

from clock import iso
from primitives.safe_url import safe_url

# How often each kind of observation should be refreshed (a schedule hint for the managed tier; Wave 1 has no
# scheduler running audits on its own).
REFRESH_TIERS = {"robots": timedelta(days=1), "sitemap": timedelta(days=1), "llms_txt": timedelta(days=7),
                 "page": timedelta(days=7), "structured_data": timedelta(days=7), "ai_probe": timedelta(days=7),
                 "entity_record": timedelta(days=180)}
REASONS = ("XML_DOCTYPE_REFUSED", "XML_MALFORMED", "TOO_LARGE", "NOT_UTF8", "JSONLD_INVALID",
           "PROVIDER_ANSWER_MALFORMED", "DECOMPRESS_FAILED", "JSONLD_TOO_LARGE")


QUARANTINED_MAX = 200
OBSERVATIONS_MAX = 500


class Osei:
    def __init__(self, clock):
        self.clock = clock
        self.quarantined: list = []
        self.observations: list = []
        self.quarantined_dropped = 0          # S-O1: past the caps the records are counted, never silently lost
        self.observations_dropped = 0

    def quarantine(self, kind: str, reason: str, url: Optional[str], data: Optional[bytes], *,
                   partial: Optional[dict] = None, size: Optional[int] = None, digest: Optional[str] = None,
                   basis: Optional[str] = None, total: Optional[int] = None) -> dict:
        """One quarantined input: reason, size and SHA-256 (ADR 0017 decision 15), never its content; the URL only
        in its safe form (primitives/safe_url.py). ``data``: the bytes in hand; ``partial``: a fetch stopped
        TOO_LARGE ({"bytes_read", "sha256", "declared"}: the size and hash are of the bytes read before the stop,
        and the record says so); ``size`` / ``digest``: a size and hash computed by the caller (a provider answer
        that is not bytes)."""
        if reason not in REASONS:
            raise ValueError("unknown quarantine reason")
        q = {"kind": kind, "reason": reason, "url": safe_url(url), "bytes": None, "sha256": None,
             "at": iso(self.clock.now())}
        if data is not None:
            q["bytes"], q["sha256"] = len(data), hashlib.sha256(data).hexdigest()
        elif partial is not None:
            q["bytes"], q["sha256"] = partial.get("bytes_read"), partial.get("sha256")
            q["size_basis"] = "bytes read before the fetch was stopped (the whole body is larger)"
            if partial.get("declared") is not None:
                q["declared_bytes"] = partial["declared"]
        elif size is not None:
            q["bytes"], q["sha256"] = size, digest
            q["size_basis"] = basis or "the answer as this service serialised it (no raw body was in hand)"
            if total is not None or reason == "JSONLD_TOO_LARGE":
                # round 2 Osei M2: a prefix was read; the record says so and gives the whole size when known
                q["truncated"], q["total_bytes"] = True, total
        else:
            q["size_basis"] = "no bytes were in hand (the failure came before any body)"
        if len(self.quarantined) < QUARANTINED_MAX:
            self.quarantined.append(q)
        else:
            self.quarantined_dropped += 1
        return q

    def observed(self, kind: str, url: Optional[str], state: str) -> dict:
        now = self.clock.now()
        o = {"kind": kind, "url": safe_url(url), "state": state, "observed_at": iso(now),
             "refresh_due": iso(now + REFRESH_TIERS.get(kind, timedelta(days=7)))}
        if len(self.observations) < OBSERVATIONS_MAX:
            self.observations.append(o)
        else:
            self.observations_dropped += 1
        return o

    def summary(self) -> dict:
        return {"quarantined": list(self.quarantined), "observations": list(self.observations),
                "quarantined_dropped": self.quarantined_dropped, "observations_dropped": self.observations_dropped,
                "caps": {"quarantined": QUARANTINED_MAX, "observations": OBSERVATIONS_MAX},
                "refresh_tiers_days": {k: v.days for k, v in REFRESH_TIERS.items()}}
