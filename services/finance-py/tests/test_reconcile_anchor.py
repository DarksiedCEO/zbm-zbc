"""The hash-chained local log anchored on the ledger, the instance lease and Andre's reconcile — verification-py's
protections (ADR 0006 decisions 4-5, amendments N14-4, N15-1, N15-2; ADR 0007 N16-7), plus the journal chain (G6)."""

from __future__ import annotations

import json
import shutil

import pytest

from helpers import ANDRE_TOKEN, Harness, rid

RECONCILE = "/fin/v1/reconcile"


def _durable(tmp_path, name="d"):
    x = Harness(data_dir=str(tmp_path / name)).ready()
    x.fund_campaign()
    x.payee()
    x.accrue()
    return x


def _restart(x, tmp_path, name="d", **env):
    return Harness(data_dir=str(tmp_path / name), ledger=x.ledger, clock=x.clock, fakes=x.f, env=env or None)


def _plan(y):
    r = y.client.get(RECONCILE, headers=y.headers(andre=ANDRE_TOKEN))
    assert r.status_code == 200, r.text
    return r.json()


def _reconcile(y, plan=None):
    plan = plan or _plan(y)
    return y.post(RECONCILE, {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                              "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]},
                  andre=ANDRE_TOKEN)


def _forge(ledger, event_id, event_type, subject="fin-log", actor="intel_10_evidence_audit"):
    ledger.record_event(event_id, "finance", event_type, actor, subject, {"forged": event_id}, "forged")


def test_clean_restart_restores_books_and_integrity_is_green(tmp_path):
    x = _durable(tmp_path)
    tb = x.ok(x.get("/fin/v1/journal/zbc/trial-balance"))
    y = _restart(x, tmp_path)
    assert y.ok(y.get("/fin/v1/journal/zbc/trial-balance")) == tb
    assert y.ok(y.get("/health"))["rules_version"] == x.svc.rules_version and y.ok(y.get("/health"))["in_memory"] is False
    assert y.ok(y.get("/fin/v1/integrity"))["status"] == "green"
    y.recon()
    b = y.run()["batch"]
    assert b["totals"]["net"] == "29.01"
    z = _restart(y, tmp_path)
    assert z.batch(b["batch_id"])["status"] == "proposed"
    assert len(x.ledger.of_type("instance_lease")) == 3


def test_truncated_local_log_refuses_to_start(tmp_path):
    x = _durable(tmp_path)
    p = tmp_path / "d" / "fin_log.jsonl"
    lines = p.read_bytes().splitlines()
    p.write_bytes(b"\n".join(lines[:-1]) + b"\n")
    with pytest.raises(RuntimeError, match="truncated"):
        _restart(x, tmp_path)


def test_empty_log_against_a_ledger_that_anchors_one_refuses(tmp_path):
    x = _durable(tmp_path)
    (tmp_path / "d" / "fin_log.jsonl").write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        _restart(x, tmp_path)


def test_edited_journal_line_refuses_to_start_g6(tmp_path):
    x = _durable(tmp_path)
    p = tmp_path / "d" / "fin_log.jsonl"
    raw = p.read_bytes()
    assert b'"debit":"29.01"' in raw
    p.write_bytes(raw.replace(b'"debit":"29.01"', b'"debit":"92.01"', 1))
    with pytest.raises(Exception):
        _restart(x, tmp_path)


def test_rewritten_log_with_recomputed_hashes_refuses(tmp_path):
    """A whole-file rewrite that recomputes the local chain still has no ledger anchors for the changed lines."""
    import hashlib
    from store import GENESIS, encode
    x = _durable(tmp_path)
    p = tmp_path / "d" / "fin_log.jsonl"
    recs = [json.loads(ln) for ln in p.read_bytes().splitlines()]
    prev, out = GENESIS, []
    for rec in recs:
        rec = dict(rec)
        body = json.dumps(rec["data"]).replace('"29.01"', '"92.01"')
        rec["data"] = json.loads(body)
        rec["prev_line_sha256"] = prev
        rec.pop("record_sha256")
        rec["record_sha256"] = hashlib.sha256(encode(rec)).hexdigest()
        line = encode(rec)
        prev = hashlib.sha256(line).hexdigest()
        out.append(line)
    p.write_bytes(b"\n".join(out) + b"\n")
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_rules_rollback_is_voidable_only_by_andre(tmp_path):
    x = _durable(tmp_path)
    v = x.svc.rules_version
    path = tmp_path / "d" / "fin_log.jsonl"
    lines = path.read_bytes().splitlines()
    i = max(k for k, ln in enumerate(lines) if b'"kind":"decision"' in ln)
    path.write_bytes(b"\n".join(lines[:i]) + b"\n")
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    plan = _plan(y)
    assert any(e.startswith("fin-ver-") for e in plan["voidable"]["event_ids"]) and not plan["fatal"]
    assert _reconcile(y, plan).status_code == 200
    z = _restart(x, tmp_path)
    assert z.svc.rules_version == v - 1


def test_failed_commit_after_anchor_only_andre_reconciles(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    r = x.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "k9", "kind": "clipper", "declared_country": "US"},
               caller="onboarding")
    assert r.status_code == 503
    assert x.ok(x.get("/fin/v1/integrity"))["status"] == "red"
    with pytest.raises(RuntimeError, match="only Andre"):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    assert y.ok(y.get("/health"))["reconcile_required"] is True
    r = y.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "k8", "kind": "clipper", "declared_country": "US"},
               caller="onboarding")
    assert r.status_code == 503                                        # nothing else runs in reconcile mode
    plan = _plan(y)
    r = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"],
                           "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]})
    assert r.status_code == 403
    bad = y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": [],
                             "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert bad.status_code == 409
    assert _reconcile(y, plan).status_code == 200
    z = _restart(x, tmp_path)
    assert z.ok(z.get("/health"))["reconcile_required"] is False
    assert z.ok(z.get("/fin/v1/integrity"))["status"] == "green"


def test_reconcile_is_honoured_only_while_the_ledger_event_matches(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert x.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "k9", "kind": "clipper",
                                     "declared_country": "US"}, caller="onboarding").status_code == 503
    y = _restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    ev = x.ledger.of_type("reconcile")[0]
    ev["payload_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="refusing to start"):
        _restart(x, tmp_path)


def test_forged_anchor_beyond_head_is_a_dos_only_andre_lifts(tmp_path):
    x = _durable(tmp_path)
    n, ep = len(x.svc.log), x.svc.log.epoch
    _forge(x.ledger, f"fin-log-{ep}-{n + 1}-{'e' * 40}", "local_log_appended")
    with pytest.raises(RuntimeError):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    assert _reconcile(y).status_code == 200
    _restart(x, tmp_path)


def test_ghost_journal_entry_on_the_ledger_turns_integrity_red(tmp_path):
    x = _durable(tmp_path)
    assert x.ok(x.get("/fin/v1/integrity"))["status"] == "green"
    _forge(x.ledger, "fin-je-" + "a" * 40, "journal_entry_posted", subject="fin-je-GHOST", actor="intel_01_journal")
    i = x.ok(x.get("/fin/v1/integrity"))
    assert i["status"] == "red" and "ruling" in " ".join(i["problems"])
    x.recon()                                                         # FC-04 recorded red -> runs blocked
    r = x.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert r.status_code == 409 and "CONTROL_RED:FC-04" in [c["code"] for c in r.json()["reasons"]]


def test_copied_data_dir_red_on_the_original_and_older_copies_refuse(tmp_path):
    x = _durable(tmp_path, "a")
    shutil.copytree(tmp_path / "a", tmp_path / "c")
    shutil.copytree(tmp_path / "a", tmp_path / "e")
    c = Harness(data_dir=str(tmp_path / "c"), ledger=x.ledger, clock=x.clock, fakes=x.f)
    assert c.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "kc", "kind": "clipper",
                                     "declared_country": "US"}, caller="onboarding").status_code == 200
    assert x.ok(x.get("/fin/v1/integrity"))["status"] == "red"
    with pytest.raises(RuntimeError, match="refusing to start"):
        Harness(data_dir=str(tmp_path / "e"), ledger=x.ledger, clock=x.clock, fakes=x.f)


def test_ledger_unreadable_at_start_refuses(tmp_path):
    x = _durable(tmp_path)
    x.ledger.readable = False
    with pytest.raises(RuntimeError, match="cannot be verified"):
        _restart(x, tmp_path)


def test_withhold_24_policy_refuses_start_without_counsel_row(tmp_path):
    with pytest.raises(RuntimeError, match="FIN-CQ-06"):
        Harness(env={"FIN_UNMATCHED_TIN_POLICY": "withhold_24"})
    x = Harness(data_dir=str(tmp_path / "w")).ready(counsel=("FIN-CQ-06",))
    y = Harness(data_dir=str(tmp_path / "w"), ledger=x.ledger, clock=x.clock, fakes=x.f,
                env={"FIN_UNMATCHED_TIN_POLICY": "withhold_24"})
    assert y.svc.cfg.unmatched_tin_policy == "withhold_24"
