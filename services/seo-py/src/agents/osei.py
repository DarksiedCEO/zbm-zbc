"""
Osei-lite — data integration hygiene for one run (ADR 0017 decision 15): quarantine for malformed inputs,
freshness timestamps, refresh tiers. Full Osei (first-party source integration, cross-source reconciliation) needs
the first-party ports, all NOT_CONNECTED in Wave 1.

A quarantined input is never parsed further and never contributes a finding beyond "this input was quarantined";
the report lists every quarantine with its reason, size and SHA-256 (never its content).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Optional

from clock import iso

# How often each kind of observation should be refreshed (a schedule hint for the managed tier; Wave 1 has no
# scheduler running audits on its own).
REFRESH_TIERS = {"robots": timedelta(days=1), "sitemap": timedelta(days=1), "llms_txt": timedelta(days=7),
                 "page": timedelta(days=7), "structured_data": timedelta(days=7), "ai_probe": timedelta(days=7),
                 "entity_record": timedelta(days=180)}
REASONS = ("XML_DOCTYPE_REFUSED", "XML_MALFORMED", "TOO_LARGE", "NOT_UTF8", "JSONLD_INVALID",
           "PROVIDER_ANSWER_MALFORMED", "DECOMPRESS_FAILED")


class Osei:
    def __init__(self, clock):
        self.clock = clock
        self.quarantined: list = []
        self.observations: list = []

    def quarantine(self, kind: str, reason: str, url: Optional[str], data: Optional[bytes]) -> dict:
        if reason not in REASONS:
            raise ValueError("unknown quarantine reason")
        q = {"kind": kind, "reason": reason, "url": url, "bytes": len(data) if data is not None else None,
             "sha256": hashlib.sha256(data).hexdigest() if data is not None else None, "at": iso(self.clock.now())}
        if len(self.quarantined) < 200:
            self.quarantined.append(q)
        return q

    def observed(self, kind: str, url: Optional[str], state: str) -> dict:
        now = self.clock.now()
        o = {"kind": kind, "url": url, "state": state, "observed_at": iso(now),
             "refresh_due": iso(now + REFRESH_TIERS.get(kind, timedelta(days=7)))}
        if len(self.observations) < 500:
            self.observations.append(o)
        return o

    def summary(self) -> dict:
        return {"quarantined": list(self.quarantined), "observations": list(self.observations),
                "refresh_tiers_days": {k: v.days for k, v in REFRESH_TIERS.items()}}
