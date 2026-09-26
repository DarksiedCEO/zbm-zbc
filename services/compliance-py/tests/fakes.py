"""
TEST-ONLY fakes. They live here, never in src/, so no production code path
can be wired to a department or provider that says "fine" (spec H.28).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fetcher import FetchFailed, FetchResult  # noqa: E402
from ledger import LedgerConflict, LedgerNotRecorded, event_field_problems, payload_sha256  # noqa: E402
from ports import A11yAnswer, AgeAttestation, ClipAttestation, DocVersion, RailStatus, ScreenAnswer, TaxStatus  # noqa: E402


@dataclass
class FakeLedgerClient:
    """Enforces the §2 field rules and idempotency (201/200/409); ``fail_all``
    simulates an outage; ``verify_ok`` is what GET /ledger/verify answers."""

    events: list[dict] = field(default_factory=list)
    fail_all: bool = False
    fail_on_type: Optional[str] = None
    verify_ok: bool = True
    readable: bool = True
    calls: int = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        self.calls += 1
        if self.fail_all or (self.fail_on_type and event_type == self.fail_on_type):
            raise LedgerNotRecorded("simulated ledger outage (test double)")
        bad = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
        if bad:
            raise LedgerNotRecorded(f"HTTP 400 (invalid {bad}, test double)")
        entry = {"event_id": event_id, "department": department, "event_type": event_type, "actor": actor,
                 "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        for e in self.events:
            if e["event_id"] == event_id:
                if {k: e[k] for k in entry} == entry:
                    return
                raise LedgerConflict("409 (test double)")
        self.events.append({**entry, "payload": payload})

    def verify(self) -> bool:
        return self.verify_ok and not self.fail_all

    def entries(self) -> list[dict]:
        """GET /ledger/entries as ledger-rust answers it (no payloads, ledger order)."""
        if self.fail_all or not self.readable:
            from ledger import LedgerQueryFailed
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} for e in self.events]

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


class PassingVerification:
    def __init__(self, age_status: str = "adult", clip_ok: bool = True, strike: bool = False):
        self.age_status, self.clip_ok, self.strike = age_status, clip_ok, strike
        self.calls: list[tuple] = []

    def age_attestation(self, attestation_id):
        self.calls.append(("age", attestation_id))
        return AgeAttestation(True, self.age_status, attestation_id)

    def attest_clip(self, submission_id, post_ref, platform, posted_at, settlement_lag_days):
        self.calls.append(("clip", submission_id))
        ok = self.clip_ok
        return ClipAttestation(True, ok, ok, not ok, ok, self.strike, f"vi-{submission_id}")


class PassingFinance:
    def __init__(self, form_kind="w9", tin_match=True, w8_current=True, services_outside_us=True, rail="verified"):
        self.form_kind, self.tin_match, self.w8_current = form_kind, tin_match, w8_current
        self.services_outside_us, self.rail = services_outside_us, rail

    def tax_status(self, payee_id):
        return TaxStatus(True, self.form_kind, True, self.tin_match, self.w8_current, self.services_outside_us)

    def rail_status(self, payee_id):
        return RailStatus(True, self.rail, self.rail == "verified")


class PassingLegal:
    def __init__(self, versions: Optional[dict] = None):
        self.versions = versions or {}

    def current_version(self, doc_id):
        return DocVersion(True, self.versions.get(doc_id, "v1"))


class FakeSanctions:
    def __init__(self, result="clear", list_version="L1"):
        self.result, self.list_version = result, list_version
        self.seen: list[tuple] = []

    def screen(self, legal_name, aliases, dob, country, region):
        self.seen.append((legal_name, dob))
        return ScreenAnswer(self.result, self.list_version if self.result != "unavailable" else None, None,
                            "prov-ref-1")

    def current_list_version(self):
        return self.list_version


class FakeA11y:
    def __init__(self, passed=True, covers_captions=True, overlay_off=True):
        self.passed, self.covers_captions, self.overlay_off = passed, covers_captions, overlay_off

    def check(self, asset_ref, asset_type, content_sha256):
        return A11yAnswer(True, self.passed, "WCAG 2.1 AA", 0 if self.passed else 3, "a" * 64, "fake-axe", "1.0", None,
                          self.covers_captions, self.overlay_off)


class ExplodingPort:
    """Any call raises: the service must treat it as unavailable, never a pass."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("port exploded")
        return boom


class FakeFetcher:
    """Serves bytes per URL; unknown URL -> FetchFailed."""

    def __init__(self, clock, pages: Optional[dict] = None):
        self.clock, self.pages = clock, dict(pages or {})
        self.requested: list[str] = []

    def fetch(self, url):
        from clock import iso

        self.requested.append(url)
        if url not in self.pages:
            raise FetchFailed("no fixture for this URL")
        return FetchResult(url, 200, self.pages[url], iso(self.clock.now()))
