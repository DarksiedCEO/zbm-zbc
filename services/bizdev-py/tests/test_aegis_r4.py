"""AEGIS round 4 on 20e08c6 (Oct 6 2026, BLOCKING: two Highs, both from the transitive hold group). Regression tests
from the reviewer's probes (scratchpad aegis-nbd/test_r4.py, test_r4c.py); each fails on 20e08c6.

New design: no groups. A decision covers EXACTLY the holds Andre names (each with its reply) under a hash over that
set and the action; body-named addresses get their own holds and never merge; opt_out suppresses only what the
decided hold may suppress; listing holds is paginated and linear.
"""

from __future__ import annotations

import time

from helpers import Harness, rid, wired_ports


def _reply(h, **kw):
    return h.ok(h.post("/replies", {"request_id": rid(), **kw}, caller="provider_events"), 201)


def _held(h, c):
    return h.ok(h.get(f"/contacts/{c['contact_id']}"))["held"]


def test_r4_reviewer_sequence_only_the_named_hold_is_decided(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c, t = h.contact(), h.template()
    m = h.ok(h.queue(c, t), 201)
    h.ok(h.job("send-queue"))
    r1 = _reply(h, message_id=m["message_id"], text="Thanks, let me think")
    seen = h.holds()
    h.clock.advance(days=1)
    r2 = _reply(h, message_id=m["message_id"], text="Actually please do not reach out again to our team")
    out = h.ok(h.decide(r1["hold_id"], "resume", seen=seen))
    assert out["decided"] == [r1["hold_id"]]
    assert _held(h, c) is True                                       # reply 2's hold still covers the contact
    open_tasks = {x["task_id"] for x in h.ok(h.get("/tasks?status=open"))}
    assert r2["task_id"] in open_tasks and r1["task_id"] not in open_tasks


def test_r4_h1_a_stranger_cannot_glue_two_contacts_together(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    a, b, t = h.contact(email="alice@a.test"), h.contact(email="bob@b.test"), h.template()
    ma, mb = h.ok(h.queue(a, t), 201), h.ok(h.queue(b, t), 201)
    h.ok(h.job("send-queue"))
    _reply(h, message_id=mb["message_id"], text="Please never write to me again, I mean it")
    ra = _reply(h, message_id=ma["message_id"], text="Sounds good, tell me more")
    glue = _reply(h, from_email="mallory@evil.test", text="fyi alice@a.test bob@b.test")
    holds = h.holds()
    assert all(len(x["contact_ids"]) <= 1 for x in holds.values())   # nothing merged
    assert len(glue["hold_ids"]) == 3                                 # mallory, alice-named, bob-named: separate
    h.ok(h.decide(ra["hold_id"], "resume"))
    assert _held(h, b) is True
    h.refused(h.queue(b, t), 403, "CONTACT_HELD")


def test_r4_h2_opting_out_a_spam_hold_never_suppresses_a_body_named_contact(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    a, t = h.contact(email="alice@a.test"), h.template()
    s = _reply(h, from_email="spammer@evil.test", text="BUY NOW alice@a.test")
    h.ok(h.decide(s["hold_id"], "opt_out"))                           # the spammer's (sender) hold only
    view = h.ok(h.get(f"/contacts/{a['contact_id']}"))
    assert view["suppressed"] is False and view["held"] is True       # alice: her own named hold, undecided
    named = [x for x in h.holds().values() if x["contact_ids"] == [a["contact_id"]]]
    assert len(named) == 1 and named[0]["kind"] == "named"
    h.ok(h.decide(named[0]["hold_id"], "resume"))
    assert _held(h, a) is False
    h.ok(h.queue(a, t), 201)


def test_r4_h2_a_named_contact_is_opted_out_only_by_its_own_hold(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    a = h.contact(email="alice@a.test")
    _reply(h, from_email="spammer@evil.test", text="BUY NOW alice@a.test")
    named = next(x for x in h.holds().values() if x["contact_ids"] == [a["contact_id"]])
    h.ok(h.decide(named["hold_id"], "opt_out"))
    assert h.ok(h.get(f"/contacts/{a['contact_id']}"))["suppressed"] is True


def test_r4_m1_a_reply_stream_cannot_starve_a_decision(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    a = h.contact(email="alice@a.test")
    ok = 0
    for i in range(5):
        r = _reply(h, from_email="alice@a.test", text="interested")
        seen = h.holds()
        _reply(h, from_email=f"x{i}@evil.test", text="hi alice@a.test")   # lands between view and decision
        ok += h.decide(r["hold_id"], "resume", seen=seen).status_code == 200
    assert ok == 5
    assert _held(h, a) is True                                           # the later named holds still stand


def test_r4_decision_hash_binds_the_set_and_the_action(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    r = _reply(h, from_email="pat@x.test", text="hello")
    seen = h.holds()
    wrong_action = h.decision_sha("opt_out", [seen[r["hold_id"]]])
    h.refused(h.decide(r["hold_id"], "resume", sha=wrong_action), 409, "STATE_HASH_MISMATCH")
    bad = h.post("/holds/decision", {"request_id": rid(), "decision": "resume",
                                     "holds": [{"hold_id": r["hold_id"], "reply_id": "nb-rpl-" + "0" * 40}],
                                     "decision_sha256": "0" * 64}, andre=True)
    h.refused(bad, 409, "STATE_HASH_MISMATCH")
    h.refused(h.decide(r["hold_id"], "resume", andre=False), 403)


def test_r4_info_a_task_without_linked_holds_is_never_auto_closed(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    r = _reply(h, from_email="pat@x.test", text="hello")
    t = h.svc.tasks[r["task_id"]]
    lone = dict(t, task_id="nb-tsk-" + "e" * 40, hold_ids=[])
    h.svc.tasks[lone["task_id"]] = lone
    h.svc._close_task_if_decided(lone["task_id"], "2026-10-06T18:00:00Z", "resume")
    assert h.svc.tasks[lone["task_id"]]["status"] == "open"


def _glued_holds(h, n: int) -> None:
    """n synthetic active holds all covering one contact (the reviewer's 1,500-glued-holds shape), indexed."""
    a = h.contact(email="alice@a.test")
    for i in range(n):
        hid = "nb-hld-" + f"{i:040x}"
        h.svc.holds[hid] = {"hold_id": hid, "reply_id": "nb-rpl-" + f"{i:040x}", "kind": "named",
                            "contact_ids": [a["contact_id"]], "hashes": [a["email_hash"]],
                            "suppress_hashes": [a["email_hash"]], "task_id": "nb-tsk-" + f"{i:040x}",
                            "status": "active", "at": "2026-10-06T18:00:00Z", "decided_at": None, "decision": None}
        h.svc.hold_by_contact.setdefault(a["contact_id"], set()).add(hid)
        h.svc.hold_by_hash.setdefault(a["email_hash"], set()).add(hid)


def test_r4_perf_two_thousand_holds_list_well_under_a_second(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    _glued_holds(h, 2000)
    cpu = time.process_time()                    # CPU time, not wall clock (hygiene rule L1)
    out = h.ok(h.get("/holds", params={"limit": 2000}))
    used = time.process_time() - cpu
    assert len(out) == 2000
    assert used < 1.0, used


def test_r4_perf_holds_view_paginates(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    _glued_holds(h, 300)
    page = h.ok(h.get("/holds", params={"limit": 50, "offset": 100}))
    assert len(page) == 50 and page[0]["hold_id"] == "nb-hld-" + f"{100:040x}"
