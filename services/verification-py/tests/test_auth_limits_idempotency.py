"""Auth on every route, request limits, strict input, idempotency (house rules; spec §D conventions)."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from clock import iso
from helpers import ANDRE_TOKEN, CALLERS, NOW, SERVICE_TOKEN, rid

VI = "vi-hld-0000000000000000000000000A"
BAD_BEARERS = [None, "Bearer wrong", "Basic abc", f"Bearer {SERVICE_TOKEN}x", "Bearer t\xf6ken".encode("latin-1"),
               "Bearer ☃☃".encode("utf-8")]


def _concrete(path: str) -> str:
    return (path.replace("{connection_id}", "vi-con-0000000000000000000000000A")
            .replace("{submission_id}", "s1").replace("{certification_id}", "vi-cert-0000000000000000000000000A")
            .replace("{attestation_id}", "vi-age-0000000000000000000000000A").replace("{subject_id}", "c1")
            .replace("{clipper_id}", "c1").replace("{hold_id}", VI).replace("{finding_id}", VI.replace("hld", "fnd"))
            .replace("{job}", "certify"))


def all_routes(h):
    out = []
    for r in h.app.routes:
        if isinstance(r, APIRoute) and r.path != "/health":
            for m in r.methods:
                out.append((m, _concrete(r.path)))
    return sorted(out)


def test_route_inventory_matches_the_spec(h):
    paths = {p for _, p in all_routes(h)}
    for need in ("/vi/v1/connections/start", "/vi/v1/connections/complete", "/vi/v1/submissions", "/vi/v1/clips/attest",
                 "/vi/v1/clips/hr13", "/vi/v1/results/attest", "/vi/v1/feed/verified-results", "/vi/v1/clawbacks",
                 "/vi/v1/age/checks", "/vi/v1/identity/checks", "/vi/v1/strikes", "/vi/v1/holds", "/vi/v1/findings",
                 "/vi/v1/bans", "/vi/v1/rules", "/vi/v1/rules/proposals", "/vi/v1/rules/decisions",
                 "/vi/v1/audit/export", "/vi/v1/jobs/certify/run"):
        assert need in paths, need
    assert len(all_routes(h)) >= 32


@pytest.mark.parametrize("bearer", BAD_BEARERS)
def test_every_route_needs_the_bearer_401_never_500(h, bearer):
    for method, path in all_routes(h):
        headers = {"X-VI-Caller-Token": CALLERS["scheduler"], "X-Andre-Approval-Token": ANDRE_TOKEN}
        if bearer is not None:
            headers["Authorization"] = bearer
        r = h.client.request(method, path, headers=headers, json={"request_id": "x"} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.status_code)


def test_docs_are_off_and_health_is_open(h):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(p).status_code == 404
    assert h.client.get("/health").status_code == 200


def test_caller_routes_refuse_missing_and_wrong_callers(hr):
    cases = [("POST", "/vi/v1/submissions", "clipper_network"), ("POST", "/vi/v1/clips/hr13", "creative_production"),
             ("POST", "/vi/v1/clips/attest", "compliance_38"), ("POST", "/vi/v1/connections/start", "onboarding"),
             ("POST", "/vi/v1/jobs/certify/run", "finance_31"), ("GET", "/vi/v1/clawbacks", "creative_production"),
             ("GET", "/vi/v1/age/attestations/vi-age-0000000000000000000000000A", "onboarding"),
             ("GET", "/vi/v1/strikes", "finance_31"), ("POST", "/vi/v1/age/checks", "compliance_38")]
    for method, path, wrong in cases:
        for caller in (None, wrong, "x" * 40):
            hd = hr.headers(caller)
            r = hr.client.request(method, path, headers=hd, json={"request_id": "x"} if method == "POST" else None)
            assert r.status_code == 403, (path, caller, r.status_code)


def test_limits(hr):
    hd = hr.headers("creative_production")
    assert hr.client.get("/vi/v1/holds?x=" + "a" * 5000, headers=hr.headers("scheduler")).status_code == 414
    assert hr.client.get("/vi/v1/holds", headers={**hr.headers("scheduler"), "X-Pad": "a" * 17000}).status_code == 431
    big = {"request_id": "r", "pad": "a" * (40 * 1024)}
    assert hr.client.post("/vi/v1/submissions", json=big, headers=hd).status_code == 413
    assert hr.client.post("/vi/v1/jobs/certify/run", json={"request_id": "r", "p": "a" * 17000},
                          headers=hr.headers("scheduler")).status_code == 413

    def chunks():
        for _ in range(40):
            yield b" " * 1024
    r = hr.client.post("/vi/v1/jobs/certify/run", content=chunks(), headers={**hr.headers("scheduler"),
                                                                              "Content-Type": "application/json"})
    assert r.status_code == 413
    assert hr.client.post("/vi/v1/jobs/certify/run", content=b"request_id=x",
                          headers={**hr.headers("scheduler"), "Content-Type": "application/x-www-form-urlencoded"}
                          ).status_code == 415
    deep = "[" * 40 + "]" * 40
    assert hr.client.post("/vi/v1/jobs/certify/run", content=deep.encode(),
                          headers={**hr.headers("scheduler"), "Content-Type": "application/json"}).status_code == 422
    assert hr.client.post("/vi/v1/jobs/certify/run", content=b"{not json",
                          headers={**hr.headers("scheduler"), "Content-Type": "application/json"}).status_code == 422


def test_strict_input_and_error_bodies_never_echo(hr):
    marker = "ZZ-MARKER-81723"
    base = {"request_id": rid(), "submission_id": "s", "campaign_id": "c", "rulebook_version": 1, "clipper_id": "k",
            "platform": "tiktok", "post_ref": "p", "posted_at": iso(NOW), "min_days_live": 7, "collab_permitted": False}
    for bad in ({**base, "unknown": marker}, {**base, "post_ref": "a\x00b" + marker}, {**base, "platform": marker},
                {**base, "rulebook_version": "1"}, {**base, "collab_permitted": "false"},
                {**base, "posted_at": "2026-10-01T09:00:00"}, {**base, "post_ref": "x" * 3000},
                {**base, "submission_id": "bad id!"}, {**base, "min_days_live": 0}):
        r = hr.post("/vi/v1/submissions", bad, caller="creative_production")
        assert r.status_code == 422, bad
        assert marker not in r.text
    r = hr.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "k", "email": "not-an-email"},
                caller="clipper_network")
    assert r.status_code == 422


def test_idempotency_on_write_routes(hr):
    body = {"request_id": "idem-1", "submission_id": "s-idem", "campaign_id": "c", "rulebook_version": 1,
            "clipper_id": "k", "platform": "tiktok", "post_ref": "https://www.tiktok.com/@c/video/1",
            "posted_at": iso(NOW), "min_days_live": 7, "collab_permitted": False}
    a = hr.ok(hr.post("/vi/v1/submissions", body, caller="creative_production"), 201)
    b = hr.ok(hr.post("/vi/v1/submissions", body, caller="creative_production"), 201)
    assert a == b
    assert hr.post("/vi/v1/submissions", dict(body, min_days_live=8), caller="creative_production").status_code == 409
    # a different request id for the same submission with different facts is a conflict too
    assert hr.post("/vi/v1/submissions", dict(body, request_id="idem-2", min_days_live=8),
                   caller="creative_production").status_code == 409
    ib = {"request_id": "idem-3", "clipper_id": "k", "email": "k@example.com"}
    x = hr.ok(hr.post("/vi/v1/identity/checks", ib, caller="clipper_network"))
    assert hr.ok(hr.post("/vi/v1/identity/checks", ib, caller="clipper_network")) == x
    assert hr.post("/vi/v1/identity/checks", dict(ib, email="q@example.com"), caller="clipper_network").status_code == 409
    ab = {"request_id": "idem-4", "subject_id": "k", "dob": "1990-01-01", "dob_field_neutral": True,
          "method": "photo_id_match", "provider_session_ref": "s"}
    y = hr.ok(hr.post("/vi/v1/age/checks", ab, caller="clipper_network"))
    assert hr.ok(hr.post("/vi/v1/age/checks", ab, caller="clipper_network")) == y
    assert hr.post("/vi/v1/age/checks", dict(ab, dob="1990-01-02"), caller="clipper_network").status_code == 409
    hr.clock.advance(minutes=16)
    assert hr.post("/vi/v1/identity/checks", ib, caller="clipper_network").status_code == 409
    # the same request id from a different caller is a different key
    assert hr.post("/vi/v1/age/checks", ab, caller="onboarding").status_code == 200


def test_job_route_unknown_job_and_request_id_format(hr):
    assert hr.post("/vi/v1/jobs/nope/run", {"request_id": rid()}, caller="scheduler").status_code == 422
    assert hr.post("/vi/v1/jobs/certify/run", {"request_id": "bad id"}, caller="scheduler").status_code == 422
