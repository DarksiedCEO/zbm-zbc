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


SIZE_BASES = ("wire", "decoded", "read_before_stop", "block_prefix", "serialised_answer", "none_in_hand")
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
        """One quarantined input (spec B-OS-01): ``{kind, reason, url (stored form), bytes, sha256, size_basis,
        truncated, total_bytes, declared_bytes, at}``, never its content. ``size_basis`` says which bytes the size
        and hash are of, from the closed list SIZE_BASES: ``data`` in hand (``basis`` names it, default ``decoded``);
        ``partial``: a fetch stopped TOO_LARGE (``read_before_stop``, truncated, the declared Content-Length when
        sent); ``size`` / ``digest``: computed by the caller (``basis`` required: ``wire``, ``decoded``,
        ``block_prefix`` with ``total`` = the whole block, ``serialised_answer``); nothing in hand: ``none_in_hand``."""
        if reason not in REASONS:
            raise ValueError("unknown quarantine reason")
        if basis is not None and basis not in SIZE_BASES:
            raise ValueError("unknown size basis")
        q = {"kind": kind, "reason": reason, "url": safe_url(url), "bytes": None, "sha256": None,
             "size_basis": "none_in_hand", "truncated": False, "total_bytes": None, "declared_bytes": None,
             "at": iso(self.clock.now())}
        if data is not None:
            q["bytes"], q["sha256"] = len(data), hashlib.sha256(data).hexdigest()
            q["size_basis"] = basis or "decoded"
        elif partial is not None:
            q["bytes"], q["sha256"] = partial.get("bytes_read"), partial.get("sha256")
            q["size_basis"], q["truncated"] = "read_before_stop", True
            q["declared_bytes"] = partial.get("declared")
        elif size is not None:
            q["bytes"], q["sha256"] = size, digest
            q["size_basis"] = basis or "serialised_answer"
            if q["size_basis"] == "block_prefix" or reason == "JSONLD_TOO_LARGE":
                # round 2 Osei M2: a prefix was read; the record says so and gives the whole size when known
                q["size_basis"], q["truncated"], q["total_bytes"] = "block_prefix", True, total
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
