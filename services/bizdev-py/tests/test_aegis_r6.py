"""AEGIS round 6 on af94543 (Oct 6 2026, NOT BLOCKING). The reviewer's probes (scratchpad aegis-nbd/test_r6.py) as
tests; each fails on af94543.

R6-M1  a retry after a state change recorded two typed evidence events for one logical action (record-first commit).
       Record-first stays; every evidence event now carries ``rk`` and the intended log ``seq``, the line names its
       events, and GET /nbd/v1/audit/evidence marks an event ``committed`` only when an anchored log line with that
       rk and seq names it. Unanchored evidence = attempted, not done.
Low    a resolved recipient could hold every contact, ten per reply: named-contact holds are now capped per sender
       per DAY (NAMED_HOLDS_DAY_MAX = 10, all replies together); the rest are counted as truncated.
Low    the review-task part lookup walked every part from 1 (O(parts) per reply, O(parts^2) per day): the current
       part per sender per day is remembered (rebuilt by replay).
"""

from __future__ import annotations

from collections import Counter

import svc_outreach
from helpers import FakeLedger, Harness, rid, wired_ports

NAMED_HOLDS_DAY_MAX = getattr(svc_outreach, "NAMED_HOLDS_DAY_MAX", 10)
TASK_HOLDS_MAX = getattr(svc_outreach, "TASK_HOLDS_MAX", 50)


def _evidence(h, **params):
    q = "&".join(f"{k}={v}" for k, v in {"limit": 1000, **params}.items())
    return h.ok(h.get(f"/audit/evidence?{q}", caller="compliance_38"))


def _one_committed_per_action(view):
    per = Counter((e["event_type"], e["rk"]) for e in view["evidence"] if e["status"] == "committed")
    assert per and all(n == 1 for n in per.values()), per


# ------------------------------------------------------------------------------------------------ R6-M1

def test_m1_reply_retry_after_state_change_probe(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    body = {"request_id": rid(), "from_email": "newbie@x.test", "text": "hello, interested"}
    led.fail_types = {"log_anchor"}
    h.refused(h.post("/replies", body, caller="provider_events"), 503)       # evidence lands, the line does not
    led.fail_types = set()
    assert h.svc.verify_integrity(force=True, always=True)["ok"]
    h.ok(h.post("/contacts", {"request_id": rid(), "brand": "zbm", "email": "newbie@x.test", "name": "New"}), 201)
    r2 = h.ok(h.post("/replies", body, caller="provider_events"), 201)       # retry after the state changed
    r3 = h.ok(h.post("/replies", body, caller="provider_events"), 201)       # replay
    assert r3 == r2
    assert len(led.of_type("reply_holds_applied")) == 2                      # record-first: two on the ledger ...
    view = _evidence(h, event_type="reply_holds_applied")
    assert (view["total"], view["committed"], view["attempted"]) == (2, 1, 1)  # ... exactly one committed
    assert view["rule"] == "unanchored evidence = attempted, not done"
    done = [e for e in view["evidence"] if e["status"] == "committed"][0]
    line = [r for r in h.svc.log.iter_records() if r["kind"] == "reply_received"]
    assert len(line) == 1 and done["seq"] == line[0]["seq"] and done["rk"] == line[0]["data"]["request_id"]
    assert done["log_kind"] == "reply_received"
    first = led.of_type("reply_holds_applied")[0]
    stale = [e for e in view["evidence"] if e["status"] == "attempted"][0]
    assert stale["event_id"] == first["event_id"] and stale["seq"] is None
    assert first["_payload"]["rk"] == done["rk"] and first["_payload"]["seq"] != done["seq"]   # its seq went to the contact
    _one_committed_per_action(_evidence(h))


def test_m1_finance_retry_after_state_change_probe(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    did = h.won_deal()["deal_id"]
    body = {"request_id": rid(), "finance_event_id": "fin-evt-" + "a" * 40, "deal_id": did, "kind": "payment",
            "amount": "100.00", "currency": "USD"}
    led.fail_types = {"log_anchor"}
    h.refused(h.post("/finance/events", body, caller="finance_31"), 503)
    led.fail_types = set()
    assert h.svc.verify_integrity(force=True, always=True)["ok"]
    h.ok(h.money_event(did, "payment", "50.00"))                              # state change between attempts
    r = h.ok(h.post("/finance/events", body, caller="finance_31"))
    assert r["commission"]["accrued"] == "15.00"
    assert len(led.of_type("client_money_event")) == 3                        # 2 real events + 1 attempt
    view = _evidence(h, event_type="client_money_event")
    assert (view["total"], view["committed"], view["attempted"]) == (3, 2, 1)
    _one_committed_per_action(view)
    rks = {e["rk"] for e in view["evidence"] if e["status"] == "committed"}
    assert any(body["request_id"] in k for k in rks)


def test_m1_every_evidence_payload_carries_rk_and_seq(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    h.won_deal()
    lines = {r["seq"]: r for r in h.svc.log.iter_records()}
    typed = [e for e in led.events if e["event_type"] != "log_anchor"]
    assert typed
    for e in typed:
        p = e["_payload"]
        assert isinstance(p.get("rk"), str) and isinstance(p.get("seq"), int)
        assert e["event_id"] in [x["event_id"] for x in lines[p["seq"]]["data"]["evidence"]]
    view = _evidence(h)
    assert view["attempted"] == 0 and view["committed"] == len(typed)
    _one_committed_per_action(view)


def test_m1_evidence_of_a_line_without_its_anchor_is_attempted(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    h.won_deal()
    before = _evidence(h)
    assert before["attempted"] == 0
    seq = before["evidence"][-1]["seq"]
    anchor_seq = [e for e in led.events if e["event_type"] == "log_anchor" and e["summary"].endswith(f"#{seq}")]
    assert len(anchor_seq) == 1
    led.events.remove(anchor_seq[0])                                          # the ledger no longer vouches for it
    after = _evidence(h)
    assert after["attempted"] >= 1 and after["committed"] == before["committed"] - after["attempted"]
    assert all(e["status"] == "attempted" for e in after["evidence"] if e["event_id"] in
               {x["event_id"] for x in before["evidence"] if x["seq"] == seq})


def test_m1_evidence_view_paginates_is_compliance_only_and_closes(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    h.won_deal()
    full = _evidence(h)
    page = h.ok(h.get("/audit/evidence?limit=1&offset=1", caller="dashboard"))
    assert page["total"] == full["total"] and page["evidence"] == full["evidence"][1:2]
    assert h.get("/audit/evidence", caller="bizdev_agent").status_code in (401, 403)
    h.svc.close()
    h.refused(h.get("/audit/evidence", caller="compliance_38"), 503, "SERVICE_CLOSED")


# ------------------------------------------------------------------------------------------------ Lows

def _flood(h, rounds):
    for i in range(10 * rounds):
        h.ok(h.post("/contacts", {"request_id": rid(), "brand": "zbm", "email": f"c{i}@k{i}.test", "name": "N"}), 201)
    me = h.contact(email="mal@evil.test")
    m = h.ok(h.queue(me, h.template()), 201)
    h.ok(h.job("send-queue"))
    out = []
    for k in range(rounds):
        txt = " ".join(f"c{i}@k{i}.test" for i in range(k * 10, k * 10 + 10))
        out.append(h.ok(h.post("/replies", {"request_id": rid(), "message_id": m["message_id"], "text": txt},
                               caller="provider_events"), 201))
    return me, m, out


def test_low_resolved_recipient_flood_probe(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=wired_ports())
    me, m, out = _flood(h, 6)
    held = [c for c in h.svc.contacts.values() if h.svc._held(c) and c["contact_id"] != me["contact_id"]]
    assert len(held) == NAMED_HOLDS_DAY_MAX == 10                            # was 60 / 60
    assert sum(r["body_addresses_truncated"] for r in out) == 50
    t = h.svc.tasks[out[0]["task_id"]]
    assert t["body_addresses_truncated"] == 50
    # the cap survives a restart (rebuilt by replay) and resets the next day
    h2 = h.restart()
    txt = " ".join(f"c{i}@k{i}.test" for i in range(10, 20))
    r = h2.ok(h2.post("/replies", {"request_id": rid(), "message_id": m["message_id"], "text": txt},
                      caller="provider_events"), 201)
    assert r["body_addresses_truncated"] == 10 and len(r["hold_ids"]) == 1
    h2.clock.advance(days=1)
    r = h2.ok(h2.post("/replies", {"request_id": rid(), "message_id": m["message_id"], "text": txt},
                      caller="provider_events"), 201)
    assert r["body_addresses_truncated"] == 0 and len(r["hold_ids"]) == 11


def test_low_part_lookup_starts_at_the_current_part(tmp_path, monkeypatch):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=wired_ports())
    parts = 6
    for _ in range(TASK_HOLDS_MAX * parts):
        h.ok(h.post("/replies", {"request_id": rid(), "from_email": "same@spam.test", "text": "x"},
                    caller="provider_events"), 201)
    assert len(h.svc.tasks) == parts
    for live in (True, False):                                               # live, and rebuilt by replay
        svc = h.svc if live else h.restart().svc
        calls = []
        real = svc._task
        monkeypatch.setattr(svc, "_task", lambda *a, _r=real, **k: calls.append(a) or _r(*a, **k))
        svc.reply("provider_events", {"request_id": rid(), "from_email": "same@spam.test", "text": "x"})
        assert len(calls) <= 2, len(calls)                                   # was one per part (O(parts))
