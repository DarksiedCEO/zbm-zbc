"""AEGIS round 16 findings on Clipper Network (N16-2, N16-4, N16-5, N16-7, N16-9, N16-11; N16-6 is
tests/test_contract_vi.py). Each test was written first and failed on integration-2026-09-24 @ 923e20c."""

from __future__ import annotations

import json
import random
from datetime import timedelta

import httpx
import pytest

from clock import iso
from fakes import PassingVI
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from httpclients import HttpVerificationIntegrity
from ports import Certification, Ports


def _banned(h):
    cid = h.admitted_clipper()
    h.vi.add_strike(cid, "S3")
    prop = h.run("/cn/v1/discipline/sync").json()["ban_proposals"][0]
    return cid, prop


def _all_bytes(h) -> str:
    parts = [json.dumps(h.ledger.events, default=str), json.dumps([r for r in h.svc.log.iter_records()], default=str),
             json.dumps(h.svc.contacts.__dict__, default=str)]
    return "\n".join(parts)


# ============================================================ N16-2 ban propagation carries Andre's token

def test_n16_2_ban_reaches_vi_with_the_exact_andre_token_and_cn_never_stores_it():
    h = Harness().ready()
    cid, prop = _banned(h)
    r = h.post(f"/cn/v1/clippers/{cid}/ban-decision", {"request_id": rid(), "proposal_id": prop, "decision": "approve",
                                                       "note": "upheld"}, andre=ANDRE_TOKEN).json()
    assert r["vi_ban_propagation"] == "done", r
    assert h.vi.ban_tokens == [ANDRE_TOKEN]
    assert ANDRE_TOKEN not in _all_bytes(h)


def test_n16_2_http_client_sends_the_andre_header_and_refuses_without_one():
    seen = []

    def handler(req):
        seen.append(req.headers.get("X-Andre-Approval-Token"))
        body = json.loads(req.content)
        return httpx.Response(200, json={"request_id": body["request_id"], "clipper_id": body["clipper_id"],
                                         "rules_pinned": True, "ban": {}})

    c = HttpVerificationIntegrity("http://vi.test", "svc-token-000000000000", "caller-token-0000000000",
                                  transport=httpx.MockTransport(handler))
    ok = c.ban("rq-1", "cn-clp-1", "cn-band-1", iso(NOW), ANDRE_TOKEN)
    assert ok.available and ok.ok and seen == [ANDRE_TOKEN]
    no = c.ban("rq-2", "cn-clp-1", "cn-band-1", iso(NOW), None)
    assert not no.ok and seen == [ANDRE_TOKEN]                 # refused locally: V&I is never asked without it


def test_n16_2_scheduler_retry_cannot_propagate_without_andre_then_andre_retries():
    h = Harness().ready()
    cid, prop = _banned(h)
    h.ports.vi = Ports().vi                                      # V&I down at approval time
    r = h.post(f"/cn/v1/clippers/{cid}/ban-decision", {"request_id": rid(), "proposal_id": prop, "decision": "approve",
                                                       "note": "upheld"}, andre=ANDRE_TOKEN).json()
    assert r["vi_ban_propagation"] == "pending"
    h.ports.vi = PassingVI()
    out = h.run("/cn/v1/offboarding/run").json()
    assert out["ban_propagation"][0]["result"] == "needs_andre" and h.ports.vi.bans == []
    r2 = h.post(f"/cn/v1/clippers/{cid}/ban-decision", {"request_id": rid(), "proposal_id": prop, "decision": "approve",
                                                        "note": "propagate again"}, andre=ANDRE_TOKEN)
    assert r2.status_code == 200 and r2.json()["vi_ban_propagation"] == "done", r2.text
    assert h.ports.vi.ban_tokens == [ANDRE_TOKEN]


# ============================================================ N16-4 CN-21 holds on day one (Finance stand-in)

def test_n16_4_applicant_exit_with_production_stand_ins_deletes_contact_at_the_deadline():
    h = Harness(stand_ins=True)
    h.approve_seed()
    cid = h.apply("exit@example.com").json()["clipper_id"]
    o = h.post(f"/cn/v1/clippers/{cid}/offboarding", {"request_id": rid(), "trigger": "clipper_request"},
               caller="hub").json()
    assert o["finance_open_items"] == "unknown" and o["delete_after"]
    h.clock.advance(days=31)
    out = h.run("/cn/v1/offboarding/run").json()
    assert o["offboarding_id"] in out["deleted"], out
    assert h.svc.contacts.get(f"clipper:{cid}") is None
    off = h.get(f"/cn/v1/clippers/{cid}/offboarding", caller="hub").json()
    assert off["finance_question"]["status"] == "unresolved" and off["finance_question"]["flagged_to_andre"]
    assert off["status"] == "pending_finance"                  # the record stays open until Finance answers none


def test_n16_4_connections_kept_until_settlement_have_a_hard_end_at_the_deadline():
    h = Harness().ready()
    cid = h.admitted_clipper()
    far = iso(NOW + timedelta(days=200))
    h.vi.certs[cid] = [Certification("vi-cert-1", "sub-1", "camp-1", "youtube", "pending", None, far)]
    o = h.post(f"/cn/v1/clippers/{cid}/offboarding", {"request_id": rid(), "trigger": "clipper_request"},
               caller="hub").json()
    assert o["keep_connections"] is True
    assert o["revoke_after"] == o["delete_after"], o          # never later than the retention deadline
    h.clock.advance(days=31)
    h.run("/cn/v1/offboarding/run")
    assert h.vi.revoked and h.svc.contacts.get(f"clipper:{cid}") is None


@pytest.mark.parametrize("seed", range(6))
def test_n16_4_property_contact_gone_and_connections_revoked_by_the_deadline_over_365_days(seed):
    rnd = random.Random(seed)
    h = Harness().ready()
    cids = []
    for i in range(4):
        cid = h.admitted_clipper(f"p{seed}-{i}@example.com")
        h.vi.certs[cid] = [Certification(f"vi-cert-{i}", f"sub-{i}", "camp-1", "youtube", "pending", None,
                                         iso(NOW + timedelta(days=rnd.randint(1, 365))))]
        cids.append(cid)
    fin = rnd.choice(["stand_in", "open", "unknown_then_none"])
    if fin == "stand_in":
        h.ports.finance = Ports().finance
    else:
        h.ports.finance.open_state = "open"
    deadlines = {}
    for cid in cids:
        keep = rnd.choice([None, True, False])
        body = {"request_id": rid(), "trigger": "clipper_request"}
        if keep is not None:
            body["keep_connections_until_settlement"] = keep
        o = h.post(f"/cn/v1/clippers/{cid}/offboarding", body, caller="hub").json()
        deadlines[cid] = o["delete_after"]
        h.clock.advance(hours=rnd.randint(0, 72))
    for day in range(365):
        h.clock.advance(days=1)
        if fin == "unknown_then_none" and day == 200:
            h.ports.finance.open_state = "none"
        h.run("/cn/v1/offboarding/run")
        now = h.clock.now()
        for cid in cids:
            if now >= datetime_of(deadlines[cid]) + timedelta(days=1):
                assert h.svc.contacts.get(f"clipper:{cid}") is None, (seed, day, cid)
                accts = h.svc.st["clippers"][cid]["connected_accounts"]
                assert all(a["status"] != "active" for a in accts), (seed, day, cid, accts)


def datetime_of(s):
    from clock import parse_iso
    return parse_iso(s)


# ============================================================ N16-5 one appeal per underlying clip / strike

def test_n16_5_one_appeal_per_clip_whatever_kind_is_chosen():
    h = Harness().ready()
    cid = h.admitted_clipper()
    s = h.vi.add_strike(cid, "S2", subject_refs=("sub-9",))
    h.run("/cn/v1/discipline/sync")
    notice = [m for m in h.messages(cid) if m["template_id"] == "strike_notice"][0]
    res = []
    for kind, ref in (("strike", s.strike_id), ("vi_finding", s.finding_ids[0]), ("clip_flag", "sub-9")):
        d = h.post("/cn/v1/disputes", {"request_id": rid(), "clipper_id": cid, "subject_kind": kind, "subject_ref": ref,
                                       "notice_message_id": notice["message_id"], "statement": f"appeal as {kind}",
                                       "evidence_refs": []}, caller="hub").json()
        res.append((d["status"], [u["code"] for u in d["decision_items"]]))
    assert res[0][0] == "open"
    assert res[1] == ("refused", ["DISPUTE_ALREADY_FILED"]) and res[2] == ("refused", ["DISPUTE_ALREADY_FILED"]), res


# ============================================================ N16-7 a forged version event does not brick start-up

def test_n16_7_forged_rules_version_event_needs_andre_void_not_a_brick(tmp_path):
    h = Harness(data_dir=str(tmp_path / "d"))
    h.approve_seed()
    ep = h.svc.log.epoch
    fake = f"cn-ver-{ep}-99-{'a' * 32}"
    h.ledger.record_event(fake, "clipper_network", "rules_version_published", "andre", "rules:v99", {"version": 99},
                          "forged")

    def restart(**env):
        return Harness(data_dir=str(tmp_path / "d"), ledger=h.ledger, clock=h.clock, ports=h.ports, env=env or None)

    with pytest.raises(RuntimeError, match="refusing to start"):
        restart()
    y = restart(CN_RECONCILE_MODE="1")
    plan = y.client.get("/cn/v1/reconcile", headers=y.headers(andre=ANDRE_TOKEN)).json()
    assert fake in plan["voidable"]["event_ids"] and not plan["fatal"], plan
    r = y.post("/cn/v1/reconcile", {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                                    "void_lines": plan["voidable"]["lines"],
                                    "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    z = restart()
    assert z.client.get("/health").json()["rules_version"] == 1


# ============================================================ N16-9 allowed is the authority on an age answer

@pytest.mark.parametrize("allowed,status", [(False, "adult"), (True, "unknown"), (True, "minor")])
def test_n16_9_contradictory_age_answer_is_not_adult(allowed, status):
    def handler(req):
        return httpx.Response(200, json={"allowed": allowed, "status": status, "attestation_id": "vi-age-X",
                                         "subject_id": "cn-clp-1", "unmet": [], "detail": "vi-age-X",
                                         "rules_pinned": True})

    c = HttpVerificationIntegrity("http://vi.test", "svc-token-000000000000", "caller-token-0000000000",
                                  transport=httpx.MockTransport(handler))
    a = c.age_subject("cn-clp-1")
    assert not (a.available and a.status == "adult"), a


# ============================================================ N16-11 display_name is a name, rendered escaped

@pytest.mark.parametrize("name", ['Hi <a href="https://evil.example/claim">Verify</a>', "Visit https://evil.example/x",
                                  "Amy‮gnp.exe", "A B", "evil.example/login", "Amy​Smith",
                                  "{automation_disclosure}", "x" * 81, "www.evil.example"])
def test_n16_11_hostile_display_names_are_refused_at_intake(name):
    h = Harness().ready()
    assert h.apply("dn@example.com", display_name=name).status_code == 422


@pytest.mark.parametrize("name", ["Zoë Ångström", "Anne-Marie O'Neil", "J. R. Smith", "李小龍", "Ólafur Arnalds"])
def test_n16_11_real_names_are_accepted(name):
    h = Harness().ready()
    assert h.apply("dn@example.com", display_name=name).status_code == 201


def test_n16_11_display_name_is_escaped_for_the_channel():
    h = Harness().ready()
    r = h.apply("esc@example.com", display_name="Anne-Marie O'Neil")
    assert r.status_code == 201
    bodies = [b for (_, ch, _, b) in h.ports.messaging.sent if ch in ("email", "in_app")]
    assert bodies and all("O&#x27;Neil" in b and "O'Neil" not in b for b in bodies), bodies
