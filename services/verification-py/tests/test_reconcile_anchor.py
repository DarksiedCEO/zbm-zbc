"""The hash-chained local log anchored on the ledger, the instance lease and Andre's reconcile — the same
protections as compliance-py (ADR 0006 decisions 4-5, amendments N14-4, N15-1, N15-2)."""

from __future__ import annotations

import shutil

import pytest

from helpers import ANDRE_TOKEN, Harness, rid
from test_cert_scenarios import run_to_day

RECONCILE = "/vi/v1/reconcile"


def _durable(tmp_path, name="d"):
    x = Harness(data_dir=str(tmp_path / name))
    x.approve_rules()
    x.clean_clip("r1")
    run_to_day(x, 2)
    return x


def _restart(x, tmp_path, name="d", **env):
    return Harness(data_dir=str(tmp_path / name), ledger=x.ledger, clock=x.clock, fakes=x.fakes, env=env or None)


def _plan(y):
    r = y.client.get(RECONCILE, headers=y.headers(andre=ANDRE_TOKEN))
    assert r.status_code == 200, r.text
    return r.json()


def _reconcile(y, plan=None):
    plan = plan or _plan(y)
    return y.post(RECONCILE, {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                              "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]},
                  andre=ANDRE_TOKEN)


def _forge(ledger, event_id, event_type, subject="vi-log", actor="intel_10_evidence_audit"):
    """A ledger-token holder posts an event by hand (no V&I token, no data-dir access)."""
    ledger.record_event(event_id, "verification_integrity", event_type, actor, subject, {"forged": event_id}, "forged")


def test_clean_restart_restores_everything_and_integrity_is_green(tmp_path):
    x = _durable(tmp_path)
    before = x.cert("r1")
    y = _restart(x, tmp_path)
    assert y.cert("r1") == before
    assert y.ok(y.get("/health"))["rules_version"] == 1 and y.ok(y.get("/health"))["in_memory"] is False
    assert y.ok(y.get("/vi/v1/integrity"))["status"] == "green"
    # the platform-data side store survived too (raw post_ref needed for the next fetch)
    assert y.svc.side.get("r1:post_ref")
    run_to_day(y, 14, 3)
    assert y.cert("r1")["status"] == "certified"
    z = _restart(y, tmp_path)
    assert z.cert("r1")["status"] == "certified"
    assert len(x.ledger.of_type("instance_lease")) == 3


def test_truncated_local_log_refuses_to_start(tmp_path):
    x = _durable(tmp_path)
    p = tmp_path / "d" / "vi_log.jsonl"
    lines = p.read_bytes().splitlines()
    p.write_bytes(b"\n".join(lines[:-1]) + b"\n")
    with pytest.raises(RuntimeError, match="truncated"):
        _restart(x, tmp_path)


def test_empty_log_against_a_ledger_that_anchors_one_refuses(tmp_path):
    x = _durable(tmp_path)
    (tmp_path / "d" / "vi_log.jsonl").write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        _restart(x, tmp_path)


def test_rules_rollback_refuses_even_in_reconcile_mode(tmp_path):
    x = _durable(tmp_path)
    row = dict(x.svc.current.by_id()["VI-15c"], statement="Tightened TikTok retention statement.")
    p = x.ok(x.post("/vi/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "VI-15c",
                                               "proposed_row": row}, andre=ANDRE_TOKEN), 201)["proposal"]
    x.ok(x.post("/vi/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
         "acknowledge_weakening": True}]}, andre=ANDRE_TOKEN))
    assert x.ok(x.get("/health"))["rules_version"] == 2
    path = tmp_path / "d" / "vi_log.jsonl"
    lines = path.read_bytes().splitlines()
    i = max(k for k, ln in enumerate(lines) if b'"decision"' in ln[:80] or b'"kind":"decision"' in ln)
    path.write_bytes(b"\n".join(lines[:i]) + b"\n")
    with pytest.raises(RuntimeError, match="rollback"):
        _restart(x, tmp_path, VI_RECONCILE_MODE="1")


def test_failed_commit_after_anchor_only_andre_reconciles(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    r = x.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "k9", "email": "k9@example.com"},
               caller="clipper_network")
    assert r.status_code == 503
    assert x.ok(x.get("/vi/v1/integrity"))["status"] == "red"
    with pytest.raises(RuntimeError, match="only Andre"):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, VI_RECONCILE_MODE="1")
    assert y.ok(y.get("/health"))["reconcile_required"] is True
    assert y.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "k8", "email": "k8@example.com"},
                  caller="clipper_network").status_code == 503          # nothing else runs in reconcile mode
    assert y.get("/vi/v1/holds").status_code == 200                      # reads still answer
    plan = _plan(y)
    assert plan["voidable"]["lines"] and not plan["fatal"]
    r = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"],
                           "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]})
    assert r.status_code == 403                                            # service token alone: no
    bad = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": [],
                             "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert bad.status_code == 409
    assert _reconcile(y, plan).status_code == 200
    assert x.ledger.of_type("reconcile")
    z = _restart(x, tmp_path)
    assert z.ok(z.get("/health"))["reconcile_required"] is False
    assert z.ok(z.get("/vi/v1/integrity"))["status"] == "green"


def test_reconcile_is_honoured_only_while_the_ledger_event_matches(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert x.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "k9", "email": "k9@example.com"},
                  caller="clipper_network").status_code == 503
    y = _restart(x, tmp_path, VI_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    ev = x.ledger.of_type("reconcile")[0]
    ev["payload_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)
    x.ledger.events.remove(ev)
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_forged_anchor_beyond_head_is_a_dos_only_andre_lifts(tmp_path):
    x = _durable(tmp_path)
    n, ep = len(x.svc.log), x.svc.log.epoch
    _forge(x.ledger, f"vi-log-{ep}-{n + 1}-{'e' * 40}", "local_log_appended")
    with pytest.raises(RuntimeError):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, VI_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    _restart(x, tmp_path)


def test_ghost_certification_on_the_ledger_turns_integrity_red(tmp_path):
    x = _durable(tmp_path)
    assert x.ok(x.get("/vi/v1/integrity"))["status"] == "green"
    _forge(x.ledger, "vi-cer-" + "a" * 40, "certification_issued", subject="r-ghost", actor="intel_02_view_certifier")
    i = x.ok(x.get("/vi/v1/integrity"))
    assert i["status"] == "red" and "ruling" in " ".join(i["problems"])


def test_copied_data_dir_red_on_the_original_and_older_copies_refuse(tmp_path):
    x = _durable(tmp_path, "a")
    shutil.copytree(tmp_path / "a", tmp_path / "c")
    shutil.copytree(tmp_path / "a", tmp_path / "e")
    c = Harness(data_dir=str(tmp_path / "c"), ledger=x.ledger, clock=x.clock, fakes=x.fakes)
    assert c.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "kc", "email": "kc@example.com"},
                  caller="clipper_network").status_code == 200
    i = x.ok(x.get("/vi/v1/integrity"))
    assert i["status"] == "red"
    with pytest.raises(RuntimeError, match="refusing to start"):
        Harness(data_dir=str(tmp_path / "e"), ledger=x.ledger, clock=x.clock, fakes=x.fakes)


def test_ledger_unreadable_at_start_refuses(tmp_path):
    x = _durable(tmp_path)
    x.ledger.readable = False
    with pytest.raises(RuntimeError, match="cannot be verified"):
        _restart(x, tmp_path)


def test_decision_whose_version_reached_the_ledger_is_restored_from_the_unwritten_line(tmp_path):
    x = _durable(tmp_path)
    row = dict(x.svc.current.by_id()["VI-15d"], statement="Tightened Instagram retention statement.")
    p = x.ok(x.post("/vi/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "VI-15d",
                                               "proposed_row": row}, andre=ANDRE_TOKEN), 201)["proposal"]
    x.svc.log.fail_next_append = True
    r = x.post("/vi/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
         "acknowledge_weakening": True}]}, andre=ANDRE_TOKEN)
    assert r.status_code == 503
    side = list((tmp_path / "d").glob("vi_log.jsonl.unwritten-*"))
    assert len(side) == 1
    with pytest.raises(RuntimeError, match="rollback"):
        _restart(x, tmp_path, VI_RECONCILE_MODE="1")
    logf = tmp_path / "d" / "vi_log.jsonl"
    logf.write_bytes(logf.read_bytes() + side[0].read_bytes())
    y = _restart(x, tmp_path)
    assert y.ok(y.get("/health"))["rules_version"] == 2
