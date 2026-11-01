"""AEGIS r13 (legal-py): R13-2 (each rollback branch and the 403/503 condition), R13-3, R13-7.

- A line whose apply fails part way restores, in memory, every kind of record it touched: a decision's proposals and
  the rules version it added (l2/l6), an update (l3), a quarantine acknowledgement (l4) and a proposal (l5).
- R13-7: the rollback snapshot is taken BEFORE the line is written (a failure between the write and the apply finds the
  pre-line state, never one read after the line went down).
- o2: a route that some OTHER configured caller may use answers 403 to a wrong caller token while compliance_38 is unset
  (503 is only for a route that nobody configured can call).
- R13-3: ``/health`` says ``warning`` (not ``ok``) and names ``optional_caller_unset:compliance_38`` while that token is
  unset, so the heartbeat (which requires ``ok``) keeps flagging the half-finished step D.
"""

from __future__ import annotations

import json

import pytest

from helpers import CALLERS, Harness

from test_aegis_r10_legal import _setup


_N = [0]


def _fail_after(x, records):
    """Commit ``records`` followed by an acceptance whose apply raises: the whole line must roll back in memory."""
    from service import Op
    svc = x.svc
    real = svc._apply

    def flaky(kind, r):
        if kind == "acceptance" and r.get("acceptance_id") == "acc-boom":
            raise TypeError("boom")
        return real(kind, r)
    svc._apply = flaky
    _N[0] += 1
    op = Op(svc, f"r13|fail|{_N[0]}", "intel_03_clickwrap", "client:acme")
    for kind, r in records:
        op.add(kind, r)
    op.add("acceptance", {"acceptance_id": "acc-boom", "party_ref": "client:acme", "doc_id": "client_msa",
                          "version": "1.0"})
    try:
        with svc.lock, pytest.raises(TypeError):
            svc._commit(op)
    finally:
        svc._apply = real


def _commit(x, records, op_id):
    from service import Op
    op = Op(x.svc, op_id, "intel_03_clickwrap", "client:acme")
    for kind, r in records:
        op.add(kind, r)
    with x.svc.lock:
        x.svc._commit(op)


def _proposal(pid):
    return {"proposal_id": pid, "kind": "seed", "rows": [], "content_sha256": "0" * 64}


def test_l5_a_failed_line_removes_the_proposal_it_added(tmp_path):
    x, _ = _setup(tmp_path)
    _fail_after(x, [("proposal", _proposal("p-r13-l5"))])
    assert "p-r13-l5" not in x.svc.proposals


def test_l2_l6_a_failed_decision_reopens_its_proposal_and_drops_its_rules_version(tmp_path):
    x, _ = _setup(tmp_path)
    _commit(x, [("proposal", _proposal("p-r13-dec"))], "r13|seed-proposal")
    before = len(x.svc.versions)
    cur = x.svc.versions[-1]
    version = {"version": cur.version + 1, "created_at": "2026-10-31T00:00:00Z", "approved_by": "andre",
               "proposal_ids": ["p-r13-dec"], "rows_sha256": "1" * 64, "prev_version_sha256": cur.rows_sha256,
               "rows": list(cur.rows)}
    _fail_after(x, [("decision", {"decisions": [{"proposal_id": "p-r13-dec", "decision": "approve"}],
                                  "decided_at": "2026-10-31T00:00:00Z", "version": version})])
    assert x.svc.proposals["p-r13-dec"]["status"] == "open"                      # l6
    assert x.svc.proposals["p-r13-dec"]["decided_at"] is None
    assert len(x.svc.versions) == before and x.svc.versions[-1] is cur          # l2


def test_l3_a_failed_line_undoes_its_update(tmp_path):
    x, _ = _setup(tmp_path)
    _commit(x, [("acceptance", {"acceptance_id": "acc-r13", "party_ref": "client:acme", "doc_id": "client_msa",
                                "version": "1.0"})], "r13|seed-acc")
    _fail_after(x, [("update", {"coll": "acceptances", "key": "acc-r13", "fields": {"note": "later"}})])
    assert "note" not in x.svc.acceptances["acc-r13"]


def test_l4_a_failed_line_undoes_its_quarantine_acknowledgement(tmp_path):
    x, _ = _setup(tmp_path)
    before = set(x.svc.quarantine_acks)
    _fail_after(x, [("quarantine_ack", {"seqs": [987654]})])
    assert x.svc.quarantine_acks == before and 987654 not in x.svc.quarantine_acks


def test_r13_7_the_snapshot_is_taken_before_the_line_is_written(tmp_path):
    x, _ = _setup(tmp_path)
    svc = x.svc
    order = []
    real_snap, real_append = svc._snapshot, svc.log.append_prepared
    svc._snapshot = lambda ops: (order.append("snapshot"), real_snap(ops))[1]
    svc.log.append_prepared = lambda rec, line: (order.append("write"), real_append(rec, line))[1]
    try:
        _commit(x, [("acceptance", {"acceptance_id": "acc-r13-7", "party_ref": "client:acme", "doc_id": "client_msa",
                                    "version": "1.0"})], "r13|order")
    finally:
        svc._snapshot, svc.log.append_prepared = real_snap, real_append
    assert order == ["snapshot", "write"], order


# --- o2 and R13-3: the optional compliance_38 caller -----------------------------------------------------------------------

def _unset():
    callers = dict(CALLERS, compliance_38="")
    return Harness(env={"LEGAL_CALLER_TOKENS": json.dumps(callers)})


def test_o2_a_shared_route_answers_403_not_503_while_compliance_38_is_unset():
    x = _unset()
    r = x.get("/legal/v1/acceptances/acc-none", caller="nobody")
    assert r.status_code == 403, r.text
    r = x.post("/legal/v1/music/rulings", {"request_id": "r-1"}, caller="nobody")
    assert r.status_code == 403, r.text


def test_r13_3_health_warns_while_an_optional_caller_is_unset():
    h = _unset().client.get("/health")
    assert h.status_code == 200
    body = h.json()
    assert body["status"] == "warning" and body["warnings"] == ["optional_caller_unset:compliance_38"], body


def test_r13_3_health_is_ok_once_every_optional_caller_is_set(tmp_path):
    x, _ = _setup(tmp_path)
    body = x.client.get("/health").json()
    assert body["status"] == "ok" and "warnings" not in body, body
