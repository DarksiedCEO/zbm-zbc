"""
TEST-ONLY fakes. They live here, never in src/, so no production path can be
wired to a department or provider that says "fine" (guardrail G3).
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, event_field_problems, payload_sha256  # noqa: E402
from ports import (Ack, AgeAnswer, Certification, CertificationsAnswer, CompleteAnswer, ComplianceRuling,  # noqa: E402
                   Connection, ConnectionsAnswer, DelegateAnswer, DocVersionAnswer, Finding, FindingAnswer,
                   IdentityAnswer, IntegrityAnswer, JurisdictionAnswer, KitAnswer, OpenItemsAnswer, RateCardAnswer,
                   RulebookAnswer, SendAnswer, StartAnswer, Strike, StrikeFeed, TaxAnswer)

AGREEMENT_SHA = hashlib.sha256(b"Clipper Agreement v3 (test fixture)").hexdigest()
KIT_SHA = hashlib.sha256(b"kit-1 as read").hexdigest()
RATE_SHA = hashlib.sha256(b"rate card rc-1 v1").hexdigest()


@dataclass
class FakeLedgerClient:
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
        if self.fail_all or not self.readable:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} for e in self.events]

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


def _age(dob: str, on: date) -> int:
    d = date.fromisoformat(dob)
    return on.year - d.year - ((on.month, on.day) < (d.month, d.day))


class PassingVI:
    """A V&I that answers from its own state. DOBs and OAuth codes it receives are kept HERE only (so tests can
    prove CN relayed them and stored them nowhere)."""

    def __init__(self, today: date = date(2026, 9, 28)):
        self.today = today
        self.age: dict[str, AgeAnswer] = {}
        self.identity: dict[str, str] = {}
        self.conns: dict[str, list[Connection]] = {}
        self.pending: dict[str, tuple[str, str]] = {}       # state -> (clipper, connection)
        self.integrity_clear: dict[str, bool] = {}
        self.strike_feed: list[Strike] = []
        self.findings: dict[str, Finding] = {}
        self.certs: dict[str, list[Certification]] = {}
        self.bans: list[tuple] = []
        self.revoked: list[str] = []
        self.received_dobs: list[str] = []
        self.received_codes: list[str] = []
        self.received_emails: list[str] = []
        self.calls: list[str] = []
        self.default_connection = True

    # helpers for tests
    def connect(self, clipper_id: str, platform: str = "youtube", status: str = "active") -> str:
        cid = f"vi-con-{clipper_id[-8:]}-{platform}"
        self.conns.setdefault(clipper_id, [])
        self.conns[clipper_id] = [c for c in self.conns[clipper_id] if c.connection_id != cid] + [Connection(cid, platform, status)]
        return cid

    def age_subject(self, clipper_id):
        self.calls.append("age_subject")
        return self.age.get(clipper_id, AgeAnswer(True, "adult", f"vi-age-{clipper_id[-10:]}"))

    def age_check(self, request_id, clipper_id, dob, dob_field_neutral, method, provider_session_ref):
        self.calls.append("age_check")
        self.received_dobs.append(dob)
        st = "adult" if _age(dob, self.today) >= 18 else "minor"
        a = AgeAnswer(True, st, f"vi-age-{clipper_id[-10:]}")
        self.age[clipper_id] = a
        return a

    def identity_check(self, request_id, clipper_id, email):
        self.calls.append("identity_check")
        self.received_emails.append(email)
        st = self.identity.get(clipper_id, "clear")
        return IdentityAnswer(True, st, ("vi-fnd-dup-1",) if st == "duplicate" else ())

    def connections(self, clipper_id):
        self.calls.append("connections")
        if clipper_id not in self.conns and self.default_connection:
            self.connect(clipper_id)
        return ConnectionsAnswer(True, tuple(self.conns.get(clipper_id, [])))

    def connection_start(self, request_id, clipper_id, platform, redirect_uri):
        self.calls.append("connection_start")
        cid = f"vi-con-{clipper_id[-8:]}-{platform}"
        state = f"st-{cid}"
        self.pending[state] = (clipper_id, cid, platform)
        return StartAnswer(True, True, cid, f"https://auth.example/{platform}?state={state}", "2026-09-28T19:00:00Z")

    def connection_complete(self, request_id, state, code):
        self.calls.append("connection_complete")
        self.received_codes.append(code)
        if state not in self.pending:
            return CompleteAnswer(True, None, "refused", ("unknown state",))
        clipper_id, cid, platform = self.pending.pop(state)
        self.connect(clipper_id, platform)
        return CompleteAnswer(True, cid, "active")

    def connection_revoke(self, request_id, connection_id):
        self.calls.append("connection_revoke")
        self.revoked.append(connection_id)
        for k, lst in self.conns.items():
            self.conns[k] = [Connection(c.connection_id, c.platform, "revoked") if c.connection_id == connection_id else c
                             for c in lst]
        return Ack(True, True, connection_id)

    def integrity(self, clipper_id):
        self.calls.append("integrity")
        return IntegrityAnswer(True, self.integrity_clear.get(clipper_id, True))

    def strikes(self, cursor):
        self.calls.append("strikes")
        return StrikeFeed(True, tuple(self.strike_feed), None)

    def finding(self, finding_id):
        self.calls.append("finding")
        return FindingAnswer(True, self.findings.get(finding_id))

    def certifications(self, clipper_id):
        self.calls.append("certifications")
        return CertificationsAnswer(True, tuple(self.certs.get(clipper_id, [])))

    def ban(self, request_id, clipper_id, cn_decision_id, approved_at):
        self.calls.append("ban")
        self.bans.append((clipper_id, cn_decision_id))
        return Ack(True, True, cn_decision_id)

    # scenario helpers
    def add_strike(self, clipper_id: str, cls: str, n: int = 1, status: str = "active", evidence=("vi-ev-1",),
                   finding_status: str = "upheld", subject_refs=("sub-1",), resolve: bool = True) -> Strike:
        fid = f"vi-fnd-{clipper_id[-6:]}-{cls}-{n}"
        if resolve:
            self.findings[fid] = Finding(fid, clipper_id, "bought_engagement", finding_status, tuple(evidence),
                                         subject_refs[0] if subject_refs else None)
        s = Strike(f"vi-stk-{clipper_id[-6:]}-{cls}-{n}", clipper_id, cls, status, "VI-10", (fid,), tuple(evidence),
                   "2026-09-27T10:00:00Z", tuple(subject_refs))
        self.strike_feed = [x for x in self.strike_feed if x.strike_id != s.strike_id] + [s]
        return s


class PassingCompliance:
    def __init__(self):
        self.classes: dict[str, str] = {}
        self.creator_allowed = True
        self.brand_allowed = True
        self.review_allowed = True
        self.latest: dict[tuple, str] = {}
        self.facts_seen: list[dict] = []
        self.reviews: list[dict] = []
        self.n = 0

    def resolve_person(self, request_id, declared_country, declared_region, attested, attestation_ref):
        cls = self.classes.get(declared_region or "", self.classes.get(declared_country, "operate"))
        return JurisdictionAnswer(True, cls, f"jur-{declared_country}")

    def creator_activation(self, request_id, clipper_id, facts):
        self.facts_seen.append(facts)
        self.n += 1
        rid = f"cmp-rul-{self.n}"
        self.latest[("zbc_creator", clipper_id)] = rid
        return ComplianceRuling(True, self.creator_allowed, rid,
                                () if self.creator_allowed else ("compliance_38/HR-02/fact_missing:age: blocked [no source url]",))

    def latest_activation(self, lane, subject_id):
        if lane == "zbc_brand":
            return ComplianceRuling(True, self.brand_allowed, f"cmp-brand-{subject_id}",
                                    () if self.brand_allowed else ("compliance_38/HR-03/x: blocked",))
        rid = self.latest.get((lane, subject_id))
        return ComplianceRuling(True, rid is not None and self.creator_allowed, rid or "none",
                                () if rid and self.creator_allowed else ("latest refused",))

    def review_email_campaign(self, request_id, subject_id, facts):
        self.reviews.append(facts)
        return ComplianceRuling(True, self.review_allowed, f"cmp-rev-{subject_id}",
                                () if self.review_allowed else ("compliance_38/US-FTC-CANSPAM-01/x: blocked",))


class PassingCreative:
    def __init__(self):
        self.live: dict[str, int] = {}
        self.kits: dict[str, KitAnswer] = {}

    def live_rulebook(self, campaign_id):
        return RulebookAnswer(True, self.live.get(campaign_id, 1))

    def kit(self, campaign_id):
        return self.kits.get(campaign_id, KitAnswer(True, f"kit-{campaign_id}", self.live.get(campaign_id, 1), "signed",
                                                    KIT_SHA))


class PassingFinance:
    def __init__(self):
        self.form = True
        self.open_state = "none"
        self.notified: list[tuple] = []
        self.cards: dict[tuple, RateCardAnswer] = {}

    def tax_status(self, clipper_id):
        return TaxAnswer(True, self.form)

    def rate_card(self, doc_id, version):
        return self.cards.get((doc_id, version), RateCardAnswer(True, True, doc_id, version,
                                                                hashlib.sha256(f"rate card {doc_id} {version}".encode()).hexdigest()))

    def open_items(self, clipper_id):
        return OpenItemsAnswer(True, self.open_state)

    def notify_offboarding(self, clipper_id, offboarding_id):
        self.notified.append((clipper_id, offboarding_id))
        return Ack(True, True, offboarding_id)


class PassingLegal:
    def __init__(self, version: str = "v3", sha: str = AGREEMENT_SHA):
        self.version, self.sha = version, sha

    def current_version(self, doc_id):
        return DocVersionAnswer(True, self.version, self.sha)


class PassingPeople:
    def __init__(self, active=("maria",)):
        self.active = set(active)

    def delegate_active(self, name):
        return DelegateAnswer(True, name in self.active)


class FakeMessaging:
    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.sent: list[tuple] = []

    def send(self, message_id, channel, recipient, body):
        self.sent.append((message_id, channel, recipient, body))
        return SendAnswer(self.deliver, f"prov-{message_id[-6:]}" if self.deliver else None,
                          "" if self.deliver else "provider refused")


class FakeHub:
    def __init__(self):
        self.revoked: list[str] = []

    def revoke_session(self, clipper_id):
        self.revoked.append(clipper_id)
        return Ack(True, True, clipper_id)


class FakePush:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.pushed: list[tuple] = []

    def push(self, topic, briefing):
        self.pushed.append((topic, briefing))
        return Ack(True, self.ok)


class ExplodingPort:
    """Any call raises: the service must treat it as unavailable, never a pass."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("port exploded")
        return boom
