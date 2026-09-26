"""
Record-first: when the evidence ledger (or the local store, or the contact store) cannot record, the API
answers 503 {"issued": false} and NOTHING changed — for every write route.
"""

from __future__ import annotations

import copy

import pytest

from fakes import KIT_SHA
from helpers import ANDRE_TOKEN, Harness, rid


def world():
    h = Harness().ready()
    a = h.admitted_clipper("a@example.com")
    h.config()
    enr = h.enrol(a).json()
    b = h.ready_applicant("b@example.com")
    h.vi.add_strike(a, "S3")
    opt = h.post("/cn/v1/opt-ins", {"request_id": rid(), "email": "r@example.com", "recipient_country": "US",
                                    "time_zone": "America/Los_Angeles", "consent_text_sha256": "c" * 64,
                                    "source_form_id": "f", "captured_at": "2026-09-28T10:00:00Z",
                                    "age_18_plus_confirmed": True}, caller="hub").json()
    rc = h.post("/cn/v1/recruiting/campaigns", {"request_id": rid(), "channel": "email_opt_in",
                                                "template_id": "recruiting_invite", "recipients": ["r@example.com"]},
                andre=ANDRE_TOKEN).json()
    return h, {"a": a, "b": b, "enr": enr, "opt": opt, "rc": rc}


def w_s1(h, x):     # a strike mirrored first (for the ban decision)
    h.run("/cn/v1/discipline/sync")
    return h.svc.st["ban_proposals"]


OPS = {
    "apply": lambda h, x: h.apply(email="new@example.com"),
    "opt_in": lambda h, x: h.post("/cn/v1/opt-ins", {"request_id": rid(), "email": "o2@example.com", "recipient_country": "US",
                                                     "time_zone": "America/Los_Angeles", "consent_text_sha256": "c" * 64,
                                                     "source_form_id": "f", "captured_at": "2026-09-28T10:00:00Z",
                                                     "age_18_plus_confirmed": True}, caller="hub"),
    "opt_out": lambda h, x: h.post("/cn/v1/opt-outs", {"request_id": rid(), "email": "r@example.com"}, caller="hub"),
    "recruiting_create": lambda h, x: h.post("/cn/v1/recruiting/campaigns", {"request_id": rid(), "channel": "email_opt_in",
                                                                             "template_id": "recruiting_invite",
                                                                             "recipients": ["r@example.com"]}, andre=ANDRE_TOKEN),
    "recruiting_send": lambda h, x: h.run(f"/cn/v1/recruiting/campaigns/{x['rc']['recruit_id']}/send"),
    "connection_start": lambda h, x: h.post(f"/cn/v1/clippers/{x['b']}/connections/start",
                                            {"request_id": rid(), "platform": "tiktok", "redirect_uri": "https://hub.example/cb",
                                             "handle": "@b"}, caller="hub"),
    "connection_complete": lambda h, x: h.post(f"/cn/v1/clippers/{x['b']}/connections/complete",
                                               {"request_id": rid(), "state": "st-unknown", "code": "c0de"}, caller="hub"),
    "age_check": lambda h, x: h.post(f"/cn/v1/clippers/{x['b']}/age-check",
                                     {"request_id": rid(), "dob": "1990-01-01", "dob_field_neutral": True,
                                      "method": "photo_id_match", "provider_session_ref": "p"}, caller="hub"),
    "agreement": lambda h, x: h.accept(x["b"]),
    "training": lambda h, x: h.post(f"/cn/v1/clippers/{x['b']}/disclosure-training",
                                    {"request_id": rid(), "training_version": "dt-2", "attested": True}, caller="hub"),
    "admission": lambda h, x: h.admit(x["b"]),
    "network_config": lambda h, x: h.config(campaign="camp-9"),
    "announcement": lambda h, x: h.post("/cn/v1/campaigns/camp-1/rulebook-announcements", {"request_id": rid(), "version": 1},
                                        caller="creative_production"),
    "enrolment": lambda h, x: h.enrol(x["a"], campaign="camp-1"),
    "kit_ack": lambda h, x: h.post(f"/cn/v1/enrolments/{x['enr']['enrolment_id']}/kit-acknowledgment",
                                   {"request_id": rid(), "kit_delivery_id": x["enr"]["kit_delivery_id"], "kit_sha256": KIT_SHA,
                                    "rulebook_version": 1, "rate_card_version": "v1", "rulebook_received": True,
                                    "disclosure_section_received": True}, caller="hub"),
    "rule_proposal": lambda h, x: h.propose_rule({"kind": "retire", "target_id": "CN-25"}),
    "template_proposal": lambda h, x: h.propose_template({"kind": "retire", "target_id": "data_export_ready"}),
    "discipline_sync": lambda h, x: h.run("/cn/v1/discipline/sync"),
    "tiers_run": lambda h, x: h.run("/cn/v1/tiers/run"),
    "nomination": lambda h, x: h.post(f"/cn/v1/clippers/{x['a']}/tier-nomination", {"request_id": rid(), "nominate": True},
                                      andre=ANDRE_TOKEN),
    "offboarding": lambda h, x: h.post(f"/cn/v1/clippers/{x['a']}/offboarding", {"request_id": rid(),
                                                                                 "trigger": "clipper_request"}, caller="hub"),
    "export": lambda h, x: h.get(f"/cn/v1/clippers/{x['a']}/export", caller="hub"),
    "audit_export": lambda h, x: h.get("/cn/v1/audit/export"),
}


def snapshot(h):
    return (copy.deepcopy(h.svc.st), copy.deepcopy(h.svc.proposals), len(h.svc.versions), len(h.svc.log),
            h.svc.contacts.keys(), sorted(h.svc.idem))


@pytest.mark.parametrize("name", sorted(OPS))
def test_ledger_down_nothing_changes(name):
    h, x = world()
    before = snapshot(h)
    h.ledger.fail_all = True
    r = OPS[name](h, x)
    assert r.status_code == 503, (name, r.status_code, r.text[:300])
    assert r.json()["issued"] is False
    assert snapshot(h) == before, name


@pytest.mark.parametrize("name", sorted(OPS))
def test_local_store_down_nothing_changes(name):
    h, x = world()
    before = snapshot(h)
    h.svc.log.fail_next_append = True
    r = OPS[name](h, x)
    if name in ("audit_export", "tiers_run"):
        # these write no local line here (the audit page is recorded on the ledger only; no tier changed)
        assert r.status_code == 200 and snapshot(h)[:4] == before[:4], name
        return
    assert r.status_code == 503, (name, r.status_code, r.text[:200])
    after = snapshot(h)
    assert after[:4] == before[:4] and after[5] == before[5], name
    assert set(after[4]) <= set(before[4]), name        # no orphan contact left behind


def test_ledger_down_for_decisions_and_ban_and_disputes():
    h, x = world()
    props = w_s1(h, x)
    pid = next(iter(props))
    before = snapshot(h)
    h.ledger.fail_all = True
    r = h.post(f"/cn/v1/clippers/{x['a']}/ban-decision", {"request_id": rid(), "proposal_id": pid, "decision": "approve",
                                                          "note": "n"}, andre=ANDRE_TOKEN)
    assert r.status_code == 503 and snapshot(h) == before
    h.ledger.fail_all = False
    p = h.propose_rule({"kind": "retire", "target_id": "CN-25"}).json()["proposal"]
    before = snapshot(h)
    h.ledger.fail_all = True
    assert h.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
                      "acknowledge_weakening": True}]).status_code == 503
    assert snapshot(h) == before


def test_contact_store_failure_nothing_changes():
    h, x = world()
    before = snapshot(h)
    h.svc.contacts.fail_next_write = True
    r = h.apply(email="c@example.com")
    assert r.status_code == 503 and snapshot(h) == before


def test_ledger_down_after_commit_leaves_the_message_queued_for_flush():
    h = Harness().ready()
    h.ledger.fail_on_type = "message_sent"
    r = h.apply()
    assert r.status_code == 201                            # the application itself took effect
    cid = r.json()["clipper_id"]
    m = h.messages(cid)[0]
    assert m["delivery_status"] == "queued"                 # delivery could not be recorded: not marked sent
    h.ledger.fail_on_type = None
    h.run("/cn/v1/messages/flush")
    assert h.messages(cid)[0]["delivery_status"] == "sent"
