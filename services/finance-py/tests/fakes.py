"""
TEST-ONLY fakes. They live here, never in src/, so no production code path can be wired to a dependency that says
"fine" (spec G3). Each fake has an ``available`` switch (and a few finer ones) so a test can make exactly one gate
input absent. The fake bank and rail keep balances that move only when money would move, so reconciliation compares
the journal with an independent side.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clock import iso  # noqa: E402
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, event_field_problems, payload_sha256  # noqa: E402
from ports import (BankBalance, BankTransfer, Certification, ClawbackPage, ComplianceRuling, HoldsAnswer,  # noqa: E402
                   JurisdictionAnswer, LegalAnswer, RailAccount, RailBalance, RailLookup, RailSubmit, RegisterRow,
                   SanctionsAnswer, TaxAgentAnswer, TierAnswer)

RAIL_SIG = "sig-ok-test-only"


@dataclass
class FakeLedgerClient:
    """Enforces the §2 field rules and idempotency (201/200/409); ``fail_all`` simulates an outage."""

    events: list = field(default_factory=list)
    fail_all: bool = False
    fail_on_type: Optional[str] = None
    readable: bool = True
    calls: int = 0
    by_id: dict = field(default_factory=dict)

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        self.calls += 1
        if self.fail_all or (self.fail_on_type and event_type == self.fail_on_type):
            raise LedgerNotRecorded("simulated ledger outage (test double)")
        bad = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
        if bad:
            raise LedgerNotRecorded(f"HTTP 400 (invalid {bad}, test double)")
        entry = {"event_id": event_id, "department": department, "event_type": event_type, "actor": actor,
                 "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        e = self.by_id.get(event_id)
        if e is not None:
            if {k: e[k] for k in entry} == entry:
                return
            raise LedgerConflict("409 (test double)")
        rec = {**entry, "payload": payload}
        self.events.append(rec)
        self.by_id[event_id] = rec

    def verify(self) -> bool:
        return not self.fail_all

    def entries(self) -> list:
        if self.fail_all or not self.readable:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} for e in self.events]

    def of_type(self, t: str) -> list:
        return [e for e in self.events if e["event_type"] == t]

    def copy(self) -> "FakeLedgerClient":
        c = FakeLedgerClient(events=[dict(e) for e in self.events])
        c.by_id = {e["event_id"]: e for e in c.events}
        return c


class FakeVI:
    def __init__(self):
        self.available = True
        self.certs: dict[str, dict] = {}
        self.clawback_items: list[dict] = []

    def certify(self, sid, clipper, campaign, views, create_time, status="certified", certified_at=None,
                open_finding=False):
        self.certs[sid] = {"submission_id": sid, "certification_id": "vi-cert-" + hashlib.sha256(sid.encode()).hexdigest()[:26].upper(),
                           "status": status, "certified_views": views, "campaign_id": campaign, "clipper_id": clipper,
                           "platform": "tiktok", "create_time": create_time, "certified_at": certified_at or create_time,
                           "open_finding": open_finding, "rules_version": 1}
        return self.certs[sid]

    def certification(self, sid):
        if not self.available or sid not in self.certs:
            return Certification(False)
        c = self.certs[sid]
        return Certification(True, c["submission_id"], c["certification_id"], c["status"], c["certified_views"],
                             c["campaign_id"], c["clipper_id"], c["platform"], c["create_time"], c["certified_at"],
                             c["open_finding"], c["rules_version"])

    def add_clawback(self, sid, views_delta, cause="platform_revision_down"):
        c = self.certs[sid]
        n = len(self.clawback_items) + 1
        item = {"clawback_id": f"vi-clb-{n:026d}", "certification_id": c["certification_id"], "views_delta": views_delta,
                "cause": cause, "rule_id": "VI-10" if cause == "void_upheld_fraud" else "VI-05"}
        self.clawback_items.append(item)
        if cause == "void_upheld_fraud":
            c["status"] = "voided"
        else:
            c["status"] = "revised"
            c["certified_views"] = c["certified_views"] + views_delta
        return item

    def clawbacks(self, cursor):
        if not self.available:
            return ClawbackPage(False)
        items = self.clawback_items[cursor:cursor + 500]
        nxt = cursor + len(items)
        return ClawbackPage(True, tuple(dict(i) for i in items), nxt if nxt < len(self.clawback_items) else None)


class FakeCompliance:
    def __init__(self, clock):
        self.clock = clock
        self.rulings_available = True
        self.holds_available = True
        self.sanctions_available = True
        self.rows_available = True
        self.juris_available = True
        self.rulings: dict[str, dict] = {}
        self.open_holds: dict[str, list[str]] = {}
        self.sanctions: dict[str, dict] = {}
        self.rows_status: dict[str, str] = {}
        self.juris: dict[str, str] = {}
        self.calls: list = []

    def rule(self, ruling_id, subject, allowed=True, gate="payout", evaluated_at=None):
        self.rulings[ruling_id] = {"ruling_id": ruling_id, "gate": gate, "subject_id": subject, "allowed": allowed,
                                   "evaluated_at": evaluated_at or iso(self.clock.now())}

    def ruling(self, ruling_id):
        self.calls.append(("ruling", ruling_id))
        r = self.rulings.get(ruling_id)
        if not self.rulings_available or r is None:
            return ComplianceRuling(False)
        return ComplianceRuling(True, r["ruling_id"], r["gate"], r["subject_id"], r["allowed"], r["evaluated_at"], 1)

    def holds(self, subjects):
        if not self.holds_available:
            return HoldsAnswer(False)
        ids = []
        for _k, sid in subjects:
            ids += self.open_holds.get(sid, [])
        return HoldsAnswer(True, tuple(sorted(ids)))

    def sanctions_status(self, subject_id, role):
        if not self.sanctions_available:
            return SanctionsAnswer(False)
        s = self.sanctions.get(subject_id, {})
        return SanctionsAnswer(True, s.get("screen_id", f"cmp-scr-{subject_id}"), s.get("result", "clear"),
                               s.get("list_version", "sdn-2026-10-01"), s.get("list_current", True),
                               s.get("screened_at", iso(self.clock.now())), s.get("fresh", True))

    def row(self, oid):
        if not self.rows_available:
            return RegisterRow(False, oid)
        return RegisterRow(True, oid, self.rows_status.get(oid, "verified"), None, 1)

    def jurisdiction(self, country, region):
        if not self.juris_available:
            return JurisdictionAnswer(False)
        return JurisdictionAnswer(True, self.juris.get(country, "operate"))


class FakeCN:
    def __init__(self):
        self.available = True
        self.tiers: dict = {}
        self.notices: list = []

    def tier(self, campaign_id, clipper_id):
        if not self.available:
            return TierAnswer(False)
        return TierAnswer(True, self.tiers.get((campaign_id, clipper_id), "T0"))

    def notify(self, template, clipper_id, ref):
        self.notices.append((template, clipper_id, ref))
        return True


class FakeLegal:
    def __init__(self):
        self.available = True
        self.current = True
        self.acceptance = True

    def document_status(self, doc_id, version, doc_sha256, acceptance_id):
        if not self.available:
            return LegalAnswer(False)
        return LegalAnswer(True, self.current, self.acceptance)


class FakeRail:
    """A rail with platform balance, payouts deduplicated by idempotency key, and NO debit method (FIN-16)."""

    def __init__(self, name, clock):
        self.name = name
        self.clock = clock
        self.status_available = True
        self.balance_available = True
        self.lookup_available = True
        self.submit_available = True
        self.account_status_value = "verified"
        self.payouts_enabled = True
        self.accounts: dict[str, dict] = {}
        self.funds = Decimal("0.00")
        self.payouts: dict[str, dict] = {}          # idempotency key -> payout
        self.calls: list[tuple] = []                # every method call (A7: never a debit)
        self.fail_next = 0                          # transport errors before the call reaches the rail
        self.lost_next = 0                          # the rail accepts, but the answer is lost (transport error)
        self.reject_next = 0

    def create_account(self, payee_id, country):
        self.calls.append(("create_account", payee_id))
        if not self.status_available:
            return RailAccount(False)
        ref = f"acct_{hashlib.sha256(payee_id.encode()).hexdigest()[:12]}"
        self.accounts[ref] = {"payee_id": payee_id, "fingerprint": f"fp-{payee_id}-1"}
        return RailAccount(True, ref, self.account_status_value, self.payouts_enabled, self.accounts[ref]["fingerprint"])

    def account_status(self, ref):
        self.calls.append(("account_status", ref))
        if not self.status_available or ref not in self.accounts:
            return RailAccount(False)
        return RailAccount(True, ref, self.account_status_value, self.payouts_enabled, self.accounts[ref]["fingerprint"])

    def submit(self, key, ref, amount, item_id):
        self.calls.append(("submit", key, amount, item_id))
        if not self.submit_available:
            return RailSubmit("unavailable")
        if self.fail_next:
            self.fail_next -= 1
            return RailSubmit("transport_error")
        if self.reject_next:
            self.reject_next -= 1
            return RailSubmit("rejected")
        if key not in self.payouts:
            self.payouts[key] = {"rail_ref": f"po_{len(self.payouts) + 1}", "amount": amount, "item_id": item_id,
                                 "status": "submitted"}
        if self.lost_next:
            self.lost_next -= 1
            return RailSubmit("transport_error")
        return RailSubmit("accepted", self.payouts[key]["rail_ref"])

    def lookup(self, key, item_id):
        self.calls.append(("lookup", key, item_id))
        if not self.lookup_available:
            return RailLookup(False)
        p = self.payouts.get(key)
        return RailLookup(True, p is not None, p["rail_ref"] if p else None, p["status"] if p else None)

    def balance(self):
        self.calls.append(("balance",))
        if not self.balance_available:
            return RailBalance(False)
        return RailBalance(True, f"{self.funds:.2f}", iso(self.clock.now()), "f" * 64)

    def verify_event(self, body, signature):
        return signature == RAIL_SIG

    def paid(self, key):
        p = self.payouts[key]
        p["status"] = "paid"
        self.funds -= Decimal(p["amount"])
        return p


class FakeBank:
    def __init__(self, clock, rails: dict):
        self.clock = clock
        self.rails = rails
        self.available = True
        self.transfer_available = True
        self.balances: dict[tuple, Decimal] = {}
        self.transfers: list[tuple] = []

    def deposit(self, entity, account, amount):
        k = (entity, account)
        self.balances[k] = self.balances.get(k, Decimal("0.00")) + Decimal(amount)

    def balance(self, entity, account):
        if not self.available:
            return BankBalance(False)
        return BankBalance(True, f"{self.balances.get((entity, account), Decimal('0.00')):.2f}", iso(self.clock.now()),
                           "b" * 64)

    def transfer(self, entity, src, dst, amount, key):
        if not self.transfer_available:
            return BankTransfer("unavailable")
        if any(t[4] == key for t in self.transfers):
            return BankTransfer("accepted", f"bt-{key[-8:]}")
        amt = Decimal(amount)
        self.transfers.append((entity, src, dst, amount, key))
        self.balances[(entity, src)] = self.balances.get((entity, src), Decimal("0.00")) - amt
        if dst in ("1040", "1041"):
            self.rails["stripe" if dst == "1040" else "trolley"].funds += amt
        elif dst in ("1010", "1020"):
            self.balances[(entity, dst)] = self.balances.get((entity, dst), Decimal("0.00")) + amt
        return BankTransfer("accepted", f"bt-{key[-8:]}")


class FakeTax:
    def __init__(self):
        self.available = True
        self.status_by: dict[str, dict] = {}

    def status(self, pid):
        if not self.available:
            return TaxAgentAnswer(False)
        s = {"form_kind": "w9", "form_on_file": True, "tin_match": "matched", "w8_current": None,
             "services_outside_us_attested": None, **self.status_by.get(pid, {})}
        return TaxAgentAnswer(True, s["form_kind"], s["form_on_file"], "2026-09-01T00:00:00Z", s["tin_match"],
                              "2026-09-02T00:00:00Z", s["w8_current"], s["services_outside_us_attested"], f"tax-{pid}")


class FakeGL:
    def trial_balance(self, entity, period):
        from ports import GLAnswer
        return GLAnswer(False)


class FakeVault:
    def __init__(self):
        self.available = True
        self.key = b"identity-hmac-key-for-tests-0123456789"

    def identity_hmac_key(self):
        return self.key if self.available else None

    def contact_ref_valid(self, ref):
        return (ref or "").startswith("vault:") if self.available else None


class FakePeople:
    def second_approver_active(self):
        return None


class FakeClientMail:
    def __init__(self):
        self.sent: list = []
        self.accept = True

    def send_receipt(self, client_id, receipt):
        if not self.accept:
            return False
        self.sent.append((client_id, receipt))
        return True


class FakePush:
    def __init__(self):
        self.pushed: list = []

    def push(self, kind, briefing):
        self.pushed.append((kind, briefing))
        return True
