"""AEGIS round 7 on 27bcd16 (NOT BLOCKING; cleared for wiring): the three Lows, each regression failing on 27bcd16 and
passing after the fix (ADR 0015 amendment, round 7)."""

from __future__ import annotations

import hashlib

from helpers import Harness, rid, wired_ports


def _key(label: str) -> str:
    return hashlib.sha256(f"influencer-py test-only requester {label}".encode()).hexdigest()


def _link(h, email, who=None):
    h.clock.advance(seconds=1)
    body = {"request_id": rid(), "email": email}
    if who is not None:
        body["requester_key"] = _key(who)
    return h.ok(h.post("/applications", body, caller="hub"), 201)


def _queued(h, email):
    eh = h.svc._email(email)[1]
    return [m for m in h.svc.messages.values() if m.get("purpose") == "confirmation" and m["to_hash"] == eh
            and m["status"] == "queued"]


def _h(tmp_path, **kw):
    return Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_QUEUE_MAX="5", INF_CONFIRMATION_DAILY_CAP="1", **kw)


# ------------------------------------------------------------------------------------------------ L1

def test_l1_rotating_keys_cannot_evict_a_reasked_creator(tmp_path):
    """The reviewer's probe: the bucket is chosen by first-time mail only, so re-asked mail is not evicted while any
    first-time mail is queued."""
    h = _h(tmp_path)
    n = 0
    for _ in range(5):
        _link(h, f"j{n}@example.test", f"ip{n}")
        n += 1
    _link(h, "victim@example.test", "victim-ip")
    for _ in range(5):
        _link(h, f"j{n}@example.test", f"ip{n}")
        n += 1
    assert not _queued(h, "victim@example.test")                # evicted once, first-time mail
    _link(h, "victim@example.test", "victim-ip")                # the re-ask
    assert _queued(h, "victim@example.test")[0]["reasked"]
    for _ in range(20):
        _link(h, f"j{n}@example.test", f"ip{n}")                # each from a new key
        n += 1
    assert _queued(h, "victim@example.test")                    # 27bcd16: evicted again by the rotation
    h.ok(h.job("send-queue"))
    assert h.ports.email.sent[0][1] == "victim@example.test"


# ------------------------------------------------------------------------------------------------ L2

def test_l2_one_reask_priority_per_eviction_cycle(tmp_path):
    h = _h(tmp_path)
    for i in range(5):
        _link(h, f"r{i}@example.test")                          # five re-askers' first mails
    for i in range(5):
        _link(h, f"f{i}@example.test")                          # evict r0..r4
    for i in range(5):
        _link(h, f"r{i}@example.test")                          # their re-asks: reasked, evict f0..f4
    assert all(_queued(h, f"r{i}@example.test")[0]["reasked"] for i in range(5))
    for i in range(5, 10):
        _link(h, f"f{i}@example.test")                          # all queued mail re-asked: re-asked mail goes
    gone = [i for i in range(5) if not _queued(h, f"r{i}@example.test")]
    assert gone
    _link(h, f"r{gone[0]}@example.test")                        # asks AGAIN in the same cycle
    m = _queued(h, f"r{gone[0]}@example.test")[0]
    assert m["reasked"] is False                                # 27bcd16: a second (third, ...) priority


def test_l2_the_oldest_evicted_reask_is_kept_longest(tmp_path):
    h = _h(tmp_path)
    _link(h, "victim@example.test")                             # evicted first of all
    for i in range(5):
        _link(h, f"a{i}@example.test")
    for i in range(4):
        _link(h, f"b{i}@example.test")                          # evicts a0..a3 later
    _link(h, "victim@example.test")                             # re-asked (first evicted: rank 0)
    for i in range(4):
        _link(h, f"a{i}@example.test")                          # re-asked too (ranks 1..4); pool now all re-asked
    assert all(m["reasked"] for m in h.svc.messages.values() if m["status"] == "queued")
    _link(h, "newcomer@example.test")
    assert _queued(h, "victim@example.test")                    # 27bcd16: the victim (oldest) went first
    assert not _queued(h, "a3@example.test")                    # the most recently first-evicted link gave way


def test_l2_a_sent_mail_starts_a_new_cycle(tmp_path):
    h = _h(tmp_path)
    _link(h, "cyc@example.test")
    for i in range(5):
        _link(h, f"c{i}@example.test")
    _link(h, "cyc@example.test")
    assert _queued(h, "cyc@example.test")[0]["reasked"]
    h.ok(h.job("send-queue"))                                    # its mail goes (re-asked first)
    conf = next(c for c in h.svc.confirmations.values() if c["email_hash"] == h.svc._email("cyc@example.test")[1])
    assert conf["reask_spent"] is False


# ------------------------------------------------------------------------------------------------ L3

def test_l3_a_resend_never_reuses_a_message_id_under_a_frozen_clock(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_QUEUE_MAX="1")
    a = h.ok(h.link("a@example.test"), 201)["confirmation_id"]
    h.ok(h.link("b@example.test"), 201)                          # evicts a's mail (same instant)
    h.ok(h.link("a@example.test"), 201)                          # a's link mailed again (same instant)
    mails = [m for m in h.svc.messages.values() if m.get("confirmation_id") == a]
    assert len(mails) == 2 and len({m["message_id"] for m in mails}) == 2   # 27bcd16: the same id, overwritten
    assert [m["status"] for m in mails] == ["cancelled", "queued"]
