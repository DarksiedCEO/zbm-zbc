"""
Ledger anchoring of the local log, restart checks, instance lease, the contact store's integrity, and Andre's
reconcile procedure (the compliance-py N14-4 / N15-1 / N15-2 pattern, ADR 0006 amendments; ADR 0008 decision 5).
"""

from __future__ import annotations

import json
import shutil

import pytest

from helpers import ANDRE_TOKEN, Harness, rid

RECONCILE = "/cn/v1/reconcile"


def durable(tmp_path, name="d"):
    h = Harness(data_dir=str(tmp_path / name)).ready()
    cid = h.admitted_clipper()
    return h, cid


def restart(h, tmp_path, name="d", **env):
    return Harness(data_dir=str(tmp_path / name), ledger=h.ledger, clock=h.clock, ports=h.ports, env=env or None)


def lines(path):
    return [ln for ln in path.read_bytes().split(b"\n") if ln]


def test_restart_replays_state_and_every_line_is_anchored(tmp_path):
    h, cid = durable(tmp_path)
    h2 = restart(h, tmp_path)
    assert h2.get(f"/cn/v1/clippers/{cid}").json()["status"] == "active"
    assert h2.client.get("/health").json()["rules_version"] == h.svc.version_number
    n = len(lines(tmp_path / "d" / "cn_log.jsonl"))
    anchors = h.ledger.of_type("local_log_appended")
    assert len(anchors) == n and h.ledger.of_type("instance_lease")
    integ = h2.get("/cn/v1/integrity").json()
    assert integ["ok"] is True and integ["anchor_problems"] == []
    assert h2.get(f"/cn/v1/clippers/{cid}", caller="hub").json()["contact"]["email"] == "clip@example.com"


def test_truncated_tail_refuses_start_until_andre_reconciles(tmp_path):
    h, cid = durable(tmp_path)
    log = tmp_path / "d" / "cn_log.jsonl"
    ls = lines(log)
    log.write_bytes(b"\n".join(ls[:-2]) + b"\n")
    with pytest.raises(RuntimeError, match="truncated"):
        restart(h, tmp_path)
    y = restart(h, tmp_path, CN_RECONCILE_MODE="1")
    assert y.client.get("/health").json()["reconcile_mode"] is True
    assert y.apply(email="z@example.com").status_code == 503           # only the reconcile route writes
    plan = y.client.get(RECONCILE, headers=y.headers(andre=ANDRE_TOKEN)).json()
    assert plan["fatal"] == [] and plan["voidable"]["lines"]
    assert y.client.get(RECONCILE, headers=y.headers(caller="scheduler")).status_code == 403
    bad = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": [],
                             "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert bad.status_code == 409
    ok = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"],
                            "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]},
                andre=ANDRE_TOKEN)
    assert ok.status_code == 200 and ok.json()["restart_required"] is True, ok.text
    z = restart(h, tmp_path)
    assert z.get("/cn/v1/integrity").json()["ok"] is True


def test_rewritten_log_is_fatal(tmp_path):
    h, cid = durable(tmp_path)
    from store import GENESIS, encode
    import hashlib
    log = tmp_path / "d" / "cn_log.jsonl"
    recs = [json.loads(ln) for ln in lines(log)]
    recs[2]["data"]["record"]["note"] = "rewritten"
    prev, out = GENESIS, []
    for r in recs:                                   # recompute the whole chain: consistent, but not the ledger's
        r["prev_line_sha256"] = prev
        body = {k: v for k, v in r.items() if k != "record_sha256"}
        r["record_sha256"] = hashlib.sha256(encode(body)).hexdigest()
        ln = encode(r)
        prev = hashlib.sha256(ln).hexdigest()
        out.append(ln)
    log.write_bytes(b"\n".join(out) + b"\n")
    with pytest.raises(RuntimeError, match="no ledger anchor|other Clipper Network log"):
        restart(h, tmp_path)
    with pytest.raises(RuntimeError):
        restart(h, tmp_path, CN_RECONCILE_MODE="1")   # fatal: not even reconcile mode starts


def test_deleted_log_while_the_ledger_anchors_one_is_fatal(tmp_path):
    h, cid = durable(tmp_path)
    (tmp_path / "d" / "cn_log.jsonl").unlink()
    with pytest.raises(RuntimeError, match="empty"):
        restart(h, tmp_path)


def test_rule_version_rollback_refuses_start_and_only_andre_can_void_it(tmp_path):
    """AEGIS N16-7 changed this rule: a version event the local log no longer holds (a rollback, or a forgery) is
    VOIDABLE, not fatal. Start-up still refuses; reconcile mode starts; only Andre's recorded reconcile voids it."""
    h = Harness(data_dir=str(tmp_path / "d"))
    h.approve_seed()
    log = tmp_path / "d" / "cn_log.jsonl"
    keep = lines(log)
    h.clear_counsel("CN-CQ-01")                     # publishes rule version 2
    log.write_bytes(b"\n".join(keep) + b"\n")       # back to version 1
    with pytest.raises(RuntimeError, match="match no decision in the local log"):
        restart(h, tmp_path)
    y = restart(h, tmp_path, CN_RECONCILE_MODE="1")
    plan = y.client.get("/cn/v1/reconcile", headers=y.headers(andre=ANDRE_TOKEN)).json()
    assert any(e.startswith("cn-ver-") for e in plan["voidable"]["event_ids"]) and not plan["fatal"]
    r = y.post("/cn/v1/reconcile", {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                                    "void_lines": plan["voidable"]["lines"],
                                    "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    assert restart(h, tmp_path).client.get("/health").json()["rules_version"] == 1


def test_copied_data_directory_is_caught_by_the_lease(tmp_path):
    h, cid = durable(tmp_path)
    shutil.copytree(tmp_path / "d", tmp_path / "copy")
    other = restart(h, tmp_path, name="copy")       # a second instance on a copy writes its lease
    assert other.client.get("/health").status_code == 200
    integ = h.get("/cn/v1/integrity").json()        # the original now sees a newer lease from another instance
    assert integ["ok"] is False and any("lease" in p for p in integ["anchor_problems"])
    with pytest.raises(RuntimeError):
        restart(h, tmp_path)


def test_forged_stray_anchor_refuses_start_until_reconciled(tmp_path):
    h, cid = durable(tmp_path)
    epoch = h.svc.log.epoch
    n = len(h.svc.log) + 1
    h.ledger.record_event(f"cn-log-{epoch}-{n}-{'f' * 40}", "clipper_network", "local_log_appended",
                          "intel_10_evidence_audit", "cn-log", {"forged": True}, "forged by hand")
    with pytest.raises(RuntimeError, match="truncated|anchor"):
        restart(h, tmp_path)


def test_ledger_unreadable_at_start_refuses(tmp_path):
    h, cid = durable(tmp_path)
    h.ledger.readable = False
    with pytest.raises(RuntimeError, match="cannot be verified"):
        restart(h, tmp_path)


def test_contact_store_tamper_refuses_start_and_orphans_are_purged(tmp_path):
    h, cid = durable(tmp_path)
    path = tmp_path / "d" / "cn_contacts.json"
    data = json.loads(path.read_bytes())
    data[f"clipper:{cid}"]["email"] = "attacker@example.com"
    data["clipper:cn-clp-orphan"] = {"email": "orphan@example.com", "display_name": "o", "handles": {}}
    path.write_bytes(json.dumps(data).encode())
    with pytest.raises(RuntimeError, match="contact store was edited"):
        restart(h, tmp_path)
    data[f"clipper:{cid}"]["email"] = "clip@example.com"
    path.write_bytes(json.dumps(data, sort_keys=True).encode())
    y = restart(h, tmp_path)
    assert "clipper:cn-clp-orphan" not in json.loads(path.read_bytes())
    assert y.get(f"/cn/v1/clippers/{cid}", caller="hub").json()["contact"]["email"] == "clip@example.com"


def test_missing_contact_is_reported_and_fails_closed(tmp_path):
    h, cid = durable(tmp_path)
    path = tmp_path / "d" / "cn_contacts.json"
    data = json.loads(path.read_bytes())
    data.pop(f"clipper:{cid}")
    path.write_bytes(json.dumps(data).encode())
    y = restart(h, tmp_path)
    assert y.get("/cn/v1/integrity").json()["contacts_missing"] >= 1


def test_in_memory_mode_says_so_and_forgets(tmp_path):
    h = Harness().ready()
    assert h.client.get("/health").json()["in_memory"] is True
    h2 = Harness(ledger=h.ledger)
    assert h2.client.get("/health").json()["rules_version"] is None
