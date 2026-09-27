"""The hash-chained local log anchored on the ledger, the instance lease and Andre's reconcile — the same protections
as verification-py / compliance-py (ADR 0006 decisions 4-5, amendments N14-4, N15-1, N15-2, N16-7)."""

from __future__ import annotations

import shutil

import pytest

from helpers import ANDRE_TOKEN, Harness, rid

RECONCILE = "/legal/v1/reconcile"


def _durable(tmp_path, name="d"):
    x = Harness(data_dir=str(tmp_path / name))
    x.approve_rules()
    x.engage()
    x.approve_doc("clipper_agreement", "clipper agreement text")
    x.ok(x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                       "kind": "demand_letter", "subject_refs": ["clipper:cn-clp-X"]}, caller="hub"), 201)
    return x


def _restart(x, tmp_path, name="d", **env):
    return Harness(data_dir=str(tmp_path / name), ledger=x.ledger, clock=x.clock, ports=x.ports, env=env or None)


def _plan(y):
    r = y.client.get(RECONCILE, headers=y.headers(andre=ANDRE_TOKEN))
    assert r.status_code == 200, r.text
    return r.json()


def _reconcile(y, plan=None):
    plan = plan or _plan(y)
    return y.post(RECONCILE, {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                              "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]},
                  andre=ANDRE_TOKEN)


def _forge(ledger, event_id, event_type, subject="lg-log", actor="intel_10_evidence_audit"):
    ledger.record_event(event_id, "legal", event_type, actor, subject, {"forged": event_id}, "forged")


def test_clean_restart_restores_everything_and_integrity_is_green(tmp_path):
    x = _durable(tmp_path)
    cur = x.ok(x.get("/legal/v1/documents/clipper_agreement/current"))
    y = _restart(x, tmp_path)
    assert y.ok(y.get("/legal/v1/documents/clipper_agreement/current")) == cur
    assert y.ok(y.get("/legal/v1/holds/check", subject_ref="clipper:cn-clp-X"))["held"] is True
    h = y.ok(y.client.get("/health"))
    assert h["rules_version"] == 1 and h["in_memory"] is False
    assert y.ok(y.get("/legal/v1/integrity"))["status"] == "green"
    text = y.client.get("/legal/v1/documents/clipper_agreement/versions/1.0/text", headers=y.headers(andre=ANDRE_TOKEN))
    assert y.ok(text)["text"] == "clipper agreement text"
    z = _restart(y, tmp_path)
    assert z.ok(z.get("/legal/v1/integrity"))["status"] == "green"
    assert len(x.ledger.of_type("instance_lease")) == 3


def test_idempotent_answer_survives_a_restart(tmp_path):
    x = _durable(tmp_path)
    body = {"request_id": "same-after-restart", "channel": "email", "requester_ref": "r", "kind": "question"}
    a = x.ok(x.post("/legal/v1/requests", body, caller="hub"), 201)
    y = _restart(x, tmp_path)
    assert y.ok(y.post("/legal/v1/requests", body, caller="hub"), 201) == a
    assert y.post("/legal/v1/requests", {**body, "kind": "routine_contract"}, caller="hub").status_code == 409


def test_truncated_local_log_refuses_to_start(tmp_path):
    x = _durable(tmp_path)
    p = tmp_path / "d" / "legal_log.jsonl"
    lines = p.read_bytes().splitlines()
    p.write_bytes(b"\n".join(lines[:-1]) + b"\n")
    with pytest.raises(RuntimeError, match="truncated"):
        _restart(x, tmp_path)


def test_empty_log_against_a_ledger_that_anchors_one_refuses(tmp_path):
    x = _durable(tmp_path)
    (tmp_path / "d" / "legal_log.jsonl").write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        _restart(x, tmp_path)


def test_injected_line_without_an_anchor_is_fatal(tmp_path):
    x = _durable(tmp_path)
    y = Harness(data_dir=str(tmp_path / "other"))           # a different ledger: its lines are not anchored on x's
    y.approve_rules()
    shutil.copy(tmp_path / "other" / "legal_log.jsonl", tmp_path / "d" / "legal_log.jsonl")
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_failed_commit_after_anchor_only_andre_reconciles(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    r = x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r", "kind": "question"},
               caller="hub")
    assert r.status_code == 503
    assert x.ok(x.get("/legal/v1/integrity"))["status"] == "red"
    with pytest.raises(RuntimeError, match="only Andre"):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, LEGAL_RECONCILE_MODE="1")
    assert y.ok(y.client.get("/health"))["reconcile_required"] is True
    assert y.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                         "kind": "question"}, caller="hub").status_code == 503
    assert y.get("/legal/v1/register").status_code == 200                # reads still answer
    plan = _plan(y)
    assert plan["voidable"]["lines"] and not plan["fatal"]
    r = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"],
                           "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]})
    assert r.status_code == 403
    bad = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": [],
                             "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert bad.status_code == 409
    assert _reconcile(y, plan).status_code == 200
    assert x.ledger.of_type("reconcile")
    z = _restart(x, tmp_path)
    assert z.ok(z.client.get("/health"))["reconcile_required"] is False
    assert z.ok(z.get("/legal/v1/integrity"))["status"] == "green"


def test_reconcile_is_honoured_only_while_the_ledger_event_matches(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                         "kind": "question"}, caller="hub").status_code == 503
    y = _restart(x, tmp_path, LEGAL_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    ev = x.ledger.of_type("reconcile")[0]
    ev["payload_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_forged_anchor_beyond_head_is_a_dos_only_andre_lifts(tmp_path):
    x = _durable(tmp_path)
    n, ep = len(x.svc.log), x.svc.log.epoch
    _forge(x.ledger, f"lg-log-{ep}-{n + 1}-{'e' * 40}", "local_log_appended")
    with pytest.raises(RuntimeError):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, LEGAL_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    _restart(x, tmp_path)


def test_ghost_ruling_on_the_ledger_turns_integrity_red(tmp_path):
    x = _durable(tmp_path)
    assert x.ok(x.get("/legal/v1/integrity"))["status"] == "green"
    _forge(x.ledger, "lg-apv-" + "a" * 40, "document_version_approved", subject="doc:clipper_agreement",
           actor="andre")
    i = x.ok(x.get("/legal/v1/integrity"))
    assert i["status"] == "red" and "ruling" in " ".join(i["problems"])


def test_rules_rollback_is_voidable_only_by_andre(tmp_path):
    x = _durable(tmp_path)
    row = dict(x.svc.current.by_id()["LG-16"], title="DSAR clock (secondary source)")
    p = x.ok(x.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "LG-16",
                                                   "proposed_row": row}), 201)["proposal"]
    x.ok(x.apost("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
         "acknowledge_weakening": True}]}))
    assert x.svc.rules_version == 2
    path = tmp_path / "d" / "legal_log.jsonl"
    lines = path.read_bytes().splitlines()
    i = max(k for k, ln in enumerate(lines) if b'"kind":"decision"' in ln)
    path.write_bytes(b"\n".join(lines[:i]) + b"\n")
    with pytest.raises(RuntimeError, match="match no decision in the local log"):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, LEGAL_RECONCILE_MODE="1")
    plan = _plan(y)
    assert any(e.startswith("lg-ver-") for e in plan["voidable"]["event_ids"]) and not plan["fatal"]
    assert _reconcile(y, plan).status_code == 200
    z = _restart(x, tmp_path)
    assert z.svc.rules_version == 1


def test_copied_data_dir_red_on_the_original_and_older_copies_refuse(tmp_path):
    x = _durable(tmp_path, "a")
    shutil.copytree(tmp_path / "a", tmp_path / "c")
    shutil.copytree(tmp_path / "a", tmp_path / "e")
    c = Harness(data_dir=str(tmp_path / "c"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert c.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                         "kind": "question"}, caller="hub").status_code == 201
    assert x.ok(x.get("/legal/v1/integrity"))["status"] == "red"
    with pytest.raises(RuntimeError, match="refusing to start"):
        Harness(data_dir=str(tmp_path / "e"), ledger=x.ledger, clock=x.clock, ports=x.ports)


def test_ledger_unreadable_at_start_refuses(tmp_path):
    x = _durable(tmp_path)
    x.ledger.readable = False
    with pytest.raises(RuntimeError, match="cannot be verified"):
        _restart(x, tmp_path)


def test_in_memory_mode_says_so_and_nothing_survives(h):
    assert h.ok(h.client.get("/health"))["in_memory"] is True
    h.approve_rules()
    y = Harness(ledger=h.ledger)
    assert y.svc.rules_version is None
