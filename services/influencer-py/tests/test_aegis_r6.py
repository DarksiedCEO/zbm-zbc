"""AEGIS round 6 on cd64e6b (NOT BLOCKING): R6-L1, a sustained flood could keep one creator's link mail evicted for
ever (eviction and sending both went oldest first). Regressions fail on cd64e6b and pass after the fix (ADR 0015
amendment, round 6): per-requester fairness from the hub's opaque ``requester_key``."""

from __future__ import annotations

import hashlib

import pytest

from helpers import Harness, rid, wired_ports


def _key(label: str) -> str:
    return hashlib.sha256(f"influencer-py test-only requester {label}".encode()).hexdigest()


def _link(h, email, who=None):
    body = {"request_id": rid(), "email": email}
    if who is not None:
        body["requester_key"] = _key(who)
    return h.post("/applications", body, caller="hub")


def _queued_for(h, email):
    eh = next(c["email_hash"] for c in h.svc.confirmations.values() if c["email"] == email)
    return [m for m in h.svc.messages.values() if m["status"] == "queued" and m["to_hash"] == eh]


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_QUEUE_MAX="5", INF_CONFIRMATION_DAILY_CAP="1")


def test_l1_a_keyless_flood_cannot_keep_a_creator_evicted(h):
    """The reviewer's probe: the fallback (a re-ask after an eviction is kept and sent ahead of first-time mail)."""
    for i in range(5):
        h.ok(h.link(f"j{i}@example.test"), 201)
    n = 5
    for _ in range(3):
        h.ok(h.link("victim@example.test"), 201)                 # the creator asks (again)
        for _ in range(5):                                         # the flood continues before the next send
            h.ok(h.link(f"j{n}@example.test"), 201)
            n += 1
        h.ok(h.job("send-queue"))
        h.clock.advance(hours=24, minutes=1)
    assert any(x[1] == "victim@example.test" for x in h.ports.email.sent)     # cd64e6b: never, over three days


def test_l1_a_keyed_flood_evicts_only_its_own_mail(h):
    for i in range(5):
        h.ok(_link(h, f"j{i}@example.test", "flooder"), 201)
    h.ok(_link(h, "victim@example.test", "creator"), 201)
    for i in range(5, 40):
        h.ok(_link(h, f"j{i}@example.test", "flooder"), 201)
    assert len(_queued_for(h, "victim@example.test")) == 1        # cd64e6b: evicted by the flood
    flood = [m for m in h.svc.messages.values() if m["status"] == "queued" and m.get("requester")
             and m["to_hash"] != _queued_for(h, "victim@example.test")[0]["to_hash"]]
    assert len(flood) == 3                                         # INF_CONFIRMATION_PER_REQUESTER (default 3)
    h.ok(h.job("send-queue"))
    assert h.ports.email.sent[0][1] == "victim@example.test"


def test_l1_the_per_requester_cap_is_a_setting(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_PER_REQUESTER="1")
    for i in range(4):
        h.ok(_link(h, f"x{i}@example.test", "one"), 201)
    queued = [m for m in h.svc.messages.values() if m["status"] == "queued"]
    assert len(queued) == 1 and len(h.ledger.of_type("confirmation_mail_evicted")) == 3


def test_l1_keyless_requests_never_evict_keyed_mail(h):
    keyed = [h.ok(_link(h, f"k{i}@example.test", f"person{i}"), 201) for i in range(5)]   # five requesters, one each
    for i in range(20):
        h.ok(h.link(f"anon{i}@example.test"), 201)                 # cd64e6b: evicted the keyed mail
    for k in keyed:
        assert h.svc.messages[h.svc.confirmations[k["confirmation_id"]]["message_id"]]["status"] == "queued"
    anon = [m for m in h.svc.messages.values() if m["status"] == "queued" and m.get("requester") is None]
    assert len(anon) == 1                                          # the keyless bucket only displaces itself


def test_l1_the_requester_key_is_validated_and_never_stored_raw(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=wired_ports())
    for bad in ("short", "A" * 64, _key("x")[:63], 12345):
        r = h.post("/applications", {"request_id": rid(), "email": "v@example.test", "requester_key": bad},
                   caller="hub")
        assert r.status_code == 422, bad
    raw = _key("visible")
    h.ok(_link(h, "v@example.test", "visible"), 201)
    assert raw.encode() not in (tmp_path / "d" / "influencer_log.jsonl").read_bytes()
    assert raw not in str(h.ledger.events)
    m = next(iter(h.svc.messages.values()))
    assert m["requester"].startswith("rq-") and raw not in m["requester"]
