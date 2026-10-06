"""AEGIS round 5 on 4251b75 (Oct 6 2026, NOT BLOCKING). The reviewer's probe (scratchpad aegis-nbd/test_r5.py
``test_named_amplification``) and one test per Low; each fails on 4251b75.

R5-M1  one outside email held every contact it named, without limit, with one ledger event per hold.
Low    an evidence id was the request key alone, so a retry after the state changed hit a lasting LedgerConflict.
Low    decide_holds itself accepted duplicate hold ids (only the API refused them).
Low    one review task could gather an unbounded number of holds in a day.
"""

from __future__ import annotations

import uuid

import pytest

from errors import Invalid, Unavailable
from helpers import FakeLedger, Harness, rid, wired_ports
import svc_outreach

BODY_CONTACTS_MAX = getattr(svc_outreach, "BODY_CONTACTS_MAX", 10)
TASK_HOLDS_MAX = getattr(svc_outreach, "TASK_HOLDS_MAX", 50)


def _reply(h, **kw):
    return h.ok(h.post("/replies", {"request_id": rid(), **kw}, caller="provider_events"), 201)


def test_m1_named_amplification_probe(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    emails = [f"c{i}@corp{i}.test" for i in range(400)]
    for e in emails:
        h.ok(h.post("/contacts", {"request_id": str(uuid.uuid4()), "brand": "zbm", "email": e, "name": "N"}), 201)
    before = len(led.events)
    r = _reply(h, from_email="outsider@evil.test", text=" ".join(emails))
    assert r["hold_ids"] == [r["hold_id"]]                           # the outsider's sender hold only
    assert not any(h.svc._held(c) for c in h.svc.contacts.values())
    task = h.svc.tasks[r["task_id"]]
    assert task["sender_resolved"] is False and task["body_addresses_truncated"] == 400
    assert len(led.events) - before <= 3                              # reply evidence + log anchor (+ none per hold)


def test_m1_a_resolved_sender_holds_at_most_ten_named_contacts(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    emails = [f"c{i}@corp{i}.test" for i in range(25)]
    for e in emails:
        h.contact(email=e, verify=False)
    h.contact(email="pat@westagency.test", verify=False)
    led_before = len(h.ledger.events)
    r = _reply(h, from_email="pat@westagency.test", text=" ".join(emails))
    named = [x for x in h.holds().values() if x["kind"] == "named"]
    assert len(named) == BODY_CONTACTS_MAX == 10
    assert h.svc.tasks[r["task_id"]]["body_addresses_truncated"] == 15
    assert len(h.ledger.of_type("reply_holds_applied")) == 1
    assert h.ledger.of_type("reply_holds_applied")[0]["_payload"]["hold_ids"] == r["hold_ids"]
    assert len(h.ledger.events) - led_before == 2                    # one reply event + one anchor


def test_low_evidence_id_includes_the_payload_hash(h):
    rec = (("x", "rk-1"),)
    h.svc._commit("job_ran", {"job": "a"}, "scheduler", evidence=("probe_event", "probe:1", {"v": 1}, rec[0]))
    h.svc._commit("job_ran", {"job": "b"}, "scheduler", evidence=("probe_event", "probe:1", {"v": 2}, rec[0]))
    h.svc._commit("job_ran", {"job": "c"}, "scheduler", evidence=("probe_event", "probe:1", {"v": 2}, rec[0]))
    assert len(h.ledger.of_type("probe_event")) == 2                  # same payload: deduped; new payload: new id


def test_low_a_retry_after_the_state_changed_is_not_a_lasting_conflict(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    did = h.won_deal()["deal_id"]
    body = {"request_id": rid(), "finance_event_id": "fin-evt-" + "c" * 40, "deal_id": did, "kind": "payment",
            "amount": "100.00", "currency": "USD"}
    h.ledger.fail_types = {"log_anchor"}                              # the evidence lands, the line does not
    h.refused(h.post("/finance/events", body, caller="finance_31"), 503)
    h.ledger.fail_types = set()
    h.ok(h.money_event(did, "payment", "50.00"))                       # the state moves on
    h.ok(h.post("/finance/events", body, caller="finance_31"))         # the retry records a new evidence id


def test_low_decide_holds_refuses_duplicates_itself(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    r = _reply(h, from_email="pat@x.test", text="hello")
    x = h.holds()[r["hold_id"]]
    twice = [{"hold_id": x["hold_id"], "reply_id": x["reply_id"]}] * 2
    sha = h.decision_sha("resume", [x, x])
    with pytest.raises(Invalid):
        h.svc.decide_holds({"request_id": rid(), "decision": "resume", "holds": twice, "decision_sha256": sha})
    assert h.holds()[r["hold_id"]]["status"] == "active"


def test_low_a_task_is_split_into_numbered_parts(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    tasks = set()
    for _ in range(TASK_HOLDS_MAX + 5):
        tasks.add(_reply(h, from_email="pat@x.test", text="again")["task_id"])
    parts = sorted(h.svc.tasks[t]["part"] for t in tasks)
    assert parts == [1, 2]
    assert all(len(h.svc.tasks[t]["hold_ids"]) <= TASK_HOLDS_MAX for t in tasks)
    assert Unavailable
