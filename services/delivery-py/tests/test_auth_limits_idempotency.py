"""Auth, callers, the Andre gate on reconcile only, input limits, strict schemas and idempotency (spec §D)."""

from __future__ import annotations

import json

import pytest

from helpers import ANDRE_TOKEN, SERVICE_TOKEN, Harness, finding, findings_doc, rid, two_findings


@pytest.fixture(scope="module")
def h():
    x = Harness(wire_harness=False)
    x.svc._engine = object()
    yield x
    x.close()


def test_health_needs_no_auth_and_has_the_spec_shape(h):
    r = h.client.get("/health")
    assert r.status_code == 200
    body = r.json()
    for k in ("status", "in_memory", "ledger", "sandbox", "llm", "non_production", "config_sha256", "prompts_manifest_sha256",
              "policy_version", "deerflow_commit"):
        assert k in body, k
    assert body["llm"] == "fake" and body["non_production"] is True and body["sandbox"] == "available"
    assert body["deerflow_commit"] == "345f08be00c8a9495079b732a39b46aa9af1584e"


def test_bearer_required_and_non_ascii_is_401_not_500(h):
    assert h.client.get("/dlv/v1/policy").status_code == 401
    assert h.client.get("/dlv/v1/policy", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert h.client.get("/dlv/v1/policy", headers={"Authorization": "Bearer t\xf6ken-not-ascii-xxxxxxxxxxxxxxxxxxxxxxxxxx".encode("latin-1")}).status_code == 401
    assert h.client.get("/dlv/v1/policy", headers={"Authorization": "Basic abc"}).status_code == 401


def test_caller_token_required_and_route_scoped(h):
    assert h.client.get("/dlv/v1/policy", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}).status_code == 403
    assert h.get("/dlv/v1/policy", caller="scheduler").status_code == 200
    # the scheduler cannot open a run; aegis and andre_session can (spec §D)
    doc = two_findings(h.base_sha)
    assert h.post("/dlv/v1/fix-runs", doc, caller="scheduler").status_code == 403
    assert h.post("/dlv/v1/fix-runs", dict(doc, request_id=rid()), caller="andre_session").status_code == 202
    assert h.post("/dlv/v1/fix-runs", dict(doc, request_id=rid()), caller="aegis").status_code == 409     # run in progress


def test_no_docs_routes(h):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(path).status_code == 404


def test_reconcile_is_the_only_andre_route(h):
    hdr = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    assert h.client.get("/dlv/v1/reconcile", headers=hdr).status_code == 403                     # no token
    assert h.client.get("/dlv/v1/reconcile", headers={**hdr, "X-Andre-Approval-Token": "wrong"}).status_code == 403
    assert h.client.get("/dlv/v1/reconcile", headers={**hdr, "X-Andre-Approval-Token": "t\xf6ken".encode("latin-1")}).status_code == 403
    assert h.events("founder_approval_refused")
    r = h.client.get("/dlv/v1/reconcile", headers={**hdr, "X-Andre-Approval-Token": ANDRE_TOKEN})
    assert r.status_code == 200 and "voidable" in r.json()
    # an Andre token on any other route is not an identity (no caller → 403)
    assert h.client.get("/dlv/v1/policy", headers={**hdr, "X-Andre-Approval-Token": ANDRE_TOKEN}).status_code == 403
    assert h.client.post("/dlv/v1/fix-runs", json=two_findings(h.base_sha), headers={**hdr, "X-Andre-Approval-Token": ANDRE_TOKEN}).status_code == 403


def test_input_limits(h):
    hdr = h.headers()
    assert h.client.get("/dlv/v1/policy?" + "x" * 5000, headers=hdr).status_code == 414
    assert h.client.get("/dlv/v1/policy", headers={**hdr, "X-Big": "y" * 17000}).status_code == 431
    big = {"request_id": rid(), "pad": "z" * (1024 * 1024 + 10)}
    assert h.client.post("/dlv/v1/fix-runs", json=big, headers=hdr).status_code == 413
    assert h.client.post("/dlv/v1/fix-runs/dlv-run-" + "A" * 26 + "/cancel", json={"request_id": rid(), "reason": "x" * 9000},
                         headers=hdr).status_code == 413
    assert h.client.post("/dlv/v1/fix-runs", content=b"request_id=1", headers={**hdr, "Content-Type": "text/plain"}).status_code == 415
    deep = "[" * 40 + "]" * 40
    assert h.client.post("/dlv/v1/fix-runs", content=deep, headers={**hdr, "Content-Type": "application/json"}).status_code == 422
    assert h.client.post("/dlv/v1/fix-runs", headers=hdr).status_code == 422


def test_strict_schema_422(h):
    doc = two_findings(h.base_sha)
    bad = [dict(doc, extra="x"), dict(doc, findings=[]), dict(doc, service="Toy Py"), dict(doc, base_sha="zz"),
           dict(doc, findings=[dict(doc["findings"][0], id="bad id!")]),
           dict(doc, findings=[dict(doc["findings"][0], severity="minor")]),
           dict(doc, findings=[dict(doc["findings"][0], line=0)]), dict(doc, findings=[dict(doc["findings"][0], line=10 ** 7)]),
           dict(doc, findings=[dict(doc["findings"][0], file="services/other/x.py")]),
           dict(doc, findings=[dict(doc["findings"][0], file="services/toy-py/../x.py")]),
           dict(doc, findings=[dict(doc["findings"][0], reproduction="a\x00b")]),
           dict(doc, findings=[dict(doc["findings"][0], title="t\x07")]),
           dict(doc, findings=[dict(doc["findings"][0], reproduction="x" * 5000)]),
           dict(doc, findings=[doc["findings"][0], doc["findings"][0]]), dict(doc, source={"kind": "email", "ref": "r", "sha256": "a" * 64}),
           dict(doc, request_id="bad id")]
    for b in bad:
        r = h.post("/dlv/v1/fix-runs", b)
        assert r.status_code == 422, (json.dumps(b)[:200], r.text)
    # newlines are allowed in the free-text fields (a reproduction is multi-line; wave 21: and it names a test)
    ok = dict(doc, request_id=rid(), findings=[dict(doc["findings"][0],
                                                   reproduction="line1 tests/test_calc.py::test_add_returns_sum\nline2\ttab")])
    assert h.post("/dlv/v1/fix-runs", ok).status_code in (202, 409)
    # review and cancel schemas
    assert h.post("/dlv/v1/fix-runs/dlv-run-" + "A" * 26 + "/review", {"request_id": rid(), "review_ref": "r", "sha256": "a" * 64,
                                                                         "verdict": "pass", "reopened": ["N1-1"]}).status_code == 422
    assert h.post("/dlv/v1/fix-runs/dlv-run-" + "A" * 26 + "/review", {"request_id": rid(), "review_ref": "r", "sha256": "a" * 64,
                                                                         "verdict": "fail"}).status_code == 422
    assert h.post("/dlv/v1/fix-runs/not-a-run/cancel", {"request_id": rid(), "reason": "x"}).status_code == 422
    assert h.get("/dlv/v1/fix-runs/dlv-run-" + "A" * 26).status_code == 404
    assert h.get("/dlv/v1/fix-runs/dlv-run-" + "A" * 26 + "/evidence/dlv-ev-" + "0" * 26).status_code == 404
    assert h.get("/dlv/v1/fix-runs/dlv-run-" + "A" * 26 + "/evidence/nope").status_code == 422


def test_idempotency_window_and_stored_answer(h):
    x = Harness(wire_harness=False)
    try:
        x.svc._engine = object()
        doc = findings_doc(x.base_sha, [finding("N1-1")], request_id="req-idem-1")
        r1 = x.post("/dlv/v1/fix-runs", doc)
        r2 = x.post("/dlv/v1/fix-runs", doc)
        assert r1.status_code == 202 and r1.json() == r2.json()
        assert x.post("/dlv/v1/fix-runs", dict(doc, service="toy-py", findings=[finding("N1-2", line=11)])).status_code == 409
        x.clock.advance(minutes=16)
        r3 = x.post("/dlv/v1/fix-runs", doc)
        assert r3.status_code == 409                       # reused after the window
        # the stored answer survives a restart (it is in the local log)
        y = Harness(tmp=x.tmp, wire_harness=False, ledger=x.ledger, clock=x.clock)
        try:
            y.svc._engine = object()
            assert y.post("/dlv/v1/fix-runs", doc).status_code == 409
            assert y.run(r1.json()["run_id"])["status"] == "failed"     # a live run does not survive a restart (S9)
            assert y.events("fix_run_failed")
        finally:
            y.close()
    finally:
        x.close()


def test_every_post_answer_echoes_request_id_and_facts_and_policy(h):
    x = Harness(wire_harness=False)
    try:
        x.svc._engine = object()
        doc = two_findings(x.base_sha)
        body = x.post("/dlv/v1/fix-runs", doc).json()
        assert body["request_id"] == doc["request_id"] and len(body["facts_sha256"]) == 64
        assert body["policy_version"] == 1 and body["prompts_manifest_sha256"]
        run_id = body["run_id"]
        c = x.post(f"/dlv/v1/fix-runs/{run_id}/cancel", {"request_id": "req-c1", "reason": "test"}).json()
        assert c["request_id"] == "req-c1" and c["facts_sha256"] and c["status"] == "failed" and c["policy_version"] == 1
        assert x.events("fix_run_cancelled")
        assert x.run(run_id)["reasons"][0]["code"] == "CANCELLED"
        for path in (f"/dlv/v1/fix-runs/{run_id}", f"/dlv/v1/fix-runs/{run_id}/findings", "/dlv/v1/policy", "/dlv/v1/audit/export"):
            j = x.get(path).json()
            assert j["policy_version"] == 1 and j["prompts_manifest_sha256"]
    finally:
        x.close()
