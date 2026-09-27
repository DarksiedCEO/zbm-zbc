"""Record-first: when the evidence ledger (or the local store) cannot record, NOTHING takes effect (spec §A, §E, A14)."""

from __future__ import annotations

import copy
import json

import pytest

from helpers import ANDRE_TOKEN, Harness, rid


def _state(x):
    return json.dumps({"db": x.svc.db, "n": len(x.svc.entries), "log": len(x.svc.log), "rules": x.svc.rules_version,
                       "proposals": x.svc.proposals}, sort_keys=True, default=str)


@pytest.fixture
def rich():
    x = Harness().ready()
    inv = x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    return x, b, inv


def _calls(x, b, inv):
    doc = next(iter(x.svc.db["rate_cards"].values()))["doc_id"]
    facts = {"submission_id": "sub-9", "eligible": True, "blockers": [], "clip_review_outcome": "pass",
             "verification": {"verified": True}, "compliance": {"allowed": True, "reference": "cmp-rul-sub-9"}}
    x.vi.certify("sub-9", "clip-a", "camp-1", 1000, "2026-09-20T12:00:00Z")
    x.cmp.rule("cmp-rul-sub-9", "sub-9")
    return [
        ("POST", "/fin/v1/payout-handoffs", {"request_id": rid(), "submission_id": "sub-9", "facts": facts},
         {"caller": "creative_production"}),
        ("POST", "/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-z", "kind": "clipper",
                                    "declared_country": "US"}, {"caller": "onboarding"}),
        ("POST", f"/fin/v1/payout-batches/{b['batch_id']}/decision",
         {"request_id": rid(), "content_sha256": b["content_sha256"], "decision": "approve"}, {"andre": ANDRE_TOKEN}),
        ("POST", "/fin/v1/reconciliations/run", {"request_id": rid()}, {"caller": "scheduler"}),
        ("POST", "/fin/v1/rate-cards/proposals", {"request_id": rid(), "doc_id": doc, "campaign_id": "camp-1",
                                                  "creator_rate_per_1000": {t: "2.00" for t in ("T0", "T1", "T2", "T3")},
                                                  "max_paid_views_per_clip": 10, "effective_at": "2026-11-01T00:00:00Z"},
         {"andre": ANDRE_TOKEN}),
        ("POST", "/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service",
                                      "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "3.00"}],
                                      "payment_methods": ["ach"], "legal_ref": {"doc_id": "m", "version": 1,
                                                                                "doc_sha256": "b" * 64,
                                                                                "acceptance_id": "a"}},
         {"caller": "onboarding"}),
        ("POST", "/fin/v1/bank/events", {"request_id": rid(), "lines": [
            {"txn_ref_sha256": "e" * 64, "entity": "zbc", "account": "1020", "direction": "credit", "amount": "5.00",
             "value_date": "2026-10-02"}]}, {"caller": "bank_feed"}),
        ("POST", "/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "5.00"}, {"andre": ANDRE_TOKEN}),
        ("POST", "/fin/v1/payees/clip-a/tax/b-notices", {"request_id": rid(), "cp2100_received_on": "2026-10-01"},
         {"andre": ANDRE_TOKEN}),
        ("POST", "/fin/v1/jobs/accrual/run", {"request_id": rid()}, {"caller": "scheduler"}),
        ("POST", "/fin/v1/jobs/clawback-sync/run", {"request_id": rid()}, {"caller": "scheduler"}),
        ("POST", "/fin/v1/controls/FC-13/results", {"request_id": rid(), "result": "pass", "evidence_ref": "e"},
         {"andre": ANDRE_TOKEN}),
        ("POST", "/fin/v1/journal/zbm/corrections", {"request_id": rid(), "effective_date": "2026-10-02", "lines": [
            {"account": "1010", "debit": "1.00", "credit": "0.00"}, {"account": "3000", "debit": "0.00", "credit": "1.00"}]},
         {"andre": ANDRE_TOKEN}),
        ("POST", "/fin/v1/disputes", {"request_id": rid(), "kind": "invoice_dispute", "invoice_id": inv["invoice_id"],
                                      "amount": "1.00"}, {"andre": ANDRE_TOKEN}),
        ("POST", "/fin/v1/payees/clip-a/offboarding-notices", {"request_id": rid(), "offboarding_id": "off-1"},
         {"caller": "clipper_network"}),
        ("GET", "/fin/v1/audit/export", None, {"caller": "scheduler"}),
    ]


def test_every_write_route_has_no_effect_when_the_ledger_is_down(rich):
    x, b, inv = rich
    calls = _calls(x, b, inv)
    for method, path, body, kw in calls:
        before = _state(x)
        x.ledger.fail_all = True
        if method == "POST":
            r = x.post(path, body, **kw)
        else:
            r = x.get(path, **kw)
        x.ledger.fail_all = False
        assert r.status_code == 503, (path, r.status_code, r.text[:300])
        assert r.json()["took_effect"] is False
        assert _state(x) == before, path


@pytest.mark.parametrize("etype", ["journal_entry_posted", "payable_accrued", "local_log_appended", "handoff_received",
                                   "crossing_verification_integrity_requested"])
def test_a_failure_on_any_one_record_of_an_accrual_leaves_nothing(rich, etype):
    x, b, inv = rich
    x.vi.certify("sub-8", "clip-a", "camp-1", 1000, "2026-09-20T12:00:00Z")
    x.cmp.rule("cmp-rul-sub-8", "sub-8")
    before = _state(x)
    x.ledger.fail_on_type = etype
    r = x.handoff("sub-8", certify=False, rule=False)
    x.ledger.fail_on_type = None
    assert r.status_code == 503 and _state(x) == before
    assert x.ok(x.handoff("sub-8", certify=False, rule=False))["allowed"]


def test_local_store_failure_has_no_effect(rich):
    x, b, inv = rich
    before = _state(x)
    x.svc.log.fail_next_append = True
    r = x.post(f"/fin/v1/payout-batches/{b['batch_id']}/decision",
               {"request_id": rid(), "content_sha256": b["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    assert r.status_code == 503 and _state(x) == before


def test_release_with_the_ledger_down_submits_nothing(rich):
    x, b, inv = rich
    x.approve(b)
    x.fund_batch(b["batch_id"])
    x.clock.advance(hours=13)
    x.recon()
    x.ledger.fail_on_type = "item_submitted"
    r = x.post(f"/fin/v1/payout-batches/{b['batch_id']}/release", {"request_id": rid()}, caller="scheduler")
    x.ledger.fail_on_type = None
    assert r.status_code == 503
    assert not [c for c in x.stripe.calls if c[0] == "submit"]
    assert x.batch(b["batch_id"])["items"][0]["status"] == "approved"
    assert not [e for e in x.svc.entries if e["memo_code"] in ("F4b", "F4c", "F4d")]
    rel = x.release(b["batch_id"])
    assert rel["batch"]["items"][0]["status"] == "submitted"
