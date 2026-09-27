"""Auth on every route, request limits, strict input, idempotency (house rules; spec §E conventions)."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, Harness, rid

LG = "lg-hld-0000000000000000000000000A"
BAD_BEARERS = [None, "Bearer wrong", "Basic abc", f"Bearer {SERVICE_TOKEN}x", "Bearer t\xf6ken".encode("latin-1"),
               "Bearer ☃☃".encode("utf-8")]


def _concrete(path: str) -> str:
    return (path.replace("{doc_id}", "clipper_agreement").replace("{version}", "1.0").replace("{doc_type}", "client_msa")
            .replace("{acceptance_id}", LG).replace("{obligation_id}", LG).replace("{client_id}", "acme")
            .replace("{cq_id}", "CQ-19").replace("{memo_id}", LG).replace("{matter_id}", LG).replace("{hold_id}", LG)
            .replace("{notice_id}", LG).replace("{filing_id}", LG).replace("{job}", "obligations"))


def all_routes(h):
    out = []
    for r in h.app.routes:
        if isinstance(r, APIRoute) and r.path != "/health":
            for m in r.methods:
                out.append((m, _concrete(r.path)))
    return sorted(out)


SPEC_ROUTES = {
    ("GET", "/legal/v1/documents/clipper_agreement/current"), ("GET", "/legal/v1/documents/clipper_agreement"),
    ("GET", "/legal/v1/documents/clipper_agreement/versions/1.0"), ("POST", "/legal/v1/documents/clipper_agreement/versions"),
    ("POST", "/legal/v1/documents/clipper_agreement/versions/1.0/counsel-review"),
    ("POST", "/legal/v1/documents/clipper_agreement/versions/1.0/counsel-signoff"),
    ("POST", "/legal/v1/documents/clipper_agreement/versions/1.0/decision"), ("POST", "/legal/v1/acceptances"),
    ("GET", f"/legal/v1/acceptances/{LG}"), ("POST", "/legal/v1/envelopes"), ("POST", "/legal/v1/esign/events"),
    ("POST", "/legal/v1/playbooks/client_msa/reviews"), ("POST", "/legal/v1/playbooks/proposals"),
    ("POST", "/legal/v1/playbooks/decisions"), ("GET", "/legal/v1/obligations"),
    ("POST", f"/legal/v1/obligations/{LG}/done"), ("POST", f"/legal/v1/obligations/{LG}/waive"),
    ("GET", "/legal/v1/contracts/acme/terms"), ("PUT", "/legal/v1/contracts/acme/terms"), ("GET", "/legal/v1/register"),
    ("GET", "/legal/v1/register/CQ-19"), ("POST", "/legal/v1/register/CQ-19/invalidate"), ("POST", "/legal/v1/memos"),
    ("GET", f"/legal/v1/memos/{LG}"), ("POST", "/legal/v1/requests"), ("GET", f"/legal/v1/matters/{LG}"),
    ("POST", f"/legal/v1/matters/{LG}/close"), ("POST", f"/legal/v1/holds/{LG}/acknowledgments"),
    ("POST", f"/legal/v1/holds/{LG}/release"), ("GET", "/legal/v1/holds/check"), ("POST", "/legal/v1/takedowns"),
    ("POST", f"/legal/v1/takedowns/{LG}/counter-notice"), ("POST", f"/legal/v1/takedowns/{LG}/claimant-action"),
    ("GET", "/legal/v1/takedowns/count"), ("POST", "/legal/v1/takedowns/outbound"), ("GET", "/legal/v1/filings"),
    ("POST", "/legal/v1/filings"), ("POST", f"/legal/v1/filings/{LG}/filed"), ("POST", "/legal/v1/signoffs"),
    ("POST", "/legal/v1/music/rulings"), ("GET", "/legal/v1/retention"), ("POST", "/legal/v1/jobs/obligations/run"),
    ("GET", "/legal/v1/rules"), ("POST", "/legal/v1/rules/proposals"), ("POST", "/legal/v1/rules/decisions"),
    ("GET", "/legal/v1/audit/export"),
}


def test_route_inventory_covers_the_spec_table(h):
    have = set(all_routes(h))
    assert SPEC_ROUTES <= have, sorted(SPEC_ROUTES - have)


@pytest.mark.parametrize("bearer", BAD_BEARERS)
def test_every_route_needs_the_bearer_401_never_500(h, bearer):
    for method, path in all_routes(h):
        headers = {"X-LEGAL-Caller-Token": CALLERS["scheduler"], "X-Andre-Approval-Token": ANDRE_TOKEN}
        if bearer is not None:
            headers["Authorization"] = bearer
        r = h.client.request(method, path, headers=headers, json={"request_id": "x"} if method in ("POST", "PUT") else None)
        assert r.status_code == 401, (method, path, r.status_code)


def test_docs_are_off_and_health_is_open(h):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(p).status_code == 404
    body = h.ok(h.client.get("/health"))
    assert set(body) >= {"status", "rules_version", "in_memory", "rules_pinned", "counsel_channel_wired"}


def test_caller_routes_refuse_missing_and_wrong_callers(hr):
    cases = [("POST", "/legal/v1/acceptances", "finance_31"), ("GET", f"/legal/v1/acceptances/{LG}", "hub"),
             ("POST", "/legal/v1/esign/events", "hub"), ("POST", "/legal/v1/playbooks/client_msa/reviews", "hub"),
             ("GET", "/legal/v1/contracts/acme/terms", "finance_31"), ("PUT", "/legal/v1/contracts/acme/terms", "hub"),
             ("POST", "/legal/v1/register/CQ-19/invalidate", "onboarding"),
             ("POST", f"/legal/v1/holds/{LG}/acknowledgments", "onboarding"),
             ("POST", "/legal/v1/takedowns", "onboarding"), ("GET", "/legal/v1/takedowns/count", "compliance_38"),
             ("POST", "/legal/v1/signoffs", "compliance_38"), ("POST", "/legal/v1/music/rulings", "onboarding"),
             ("POST", "/legal/v1/jobs/obligations/run", "hub"), ("POST", "/legal/v1/documents/sow/versions", "onboarding")]
    for method, path, wrong in cases:
        for caller in (None, wrong, "x" * 40):
            r = hr.client.request(method, path, headers=hr.headers(caller),
                                  json={"request_id": "x"} if method in ("POST", "PUT") else None,
                                  params={"post_ref_sha256": "a" * 64} if "count" in path else None)
            assert r.status_code == 403, (path, caller, r.status_code)


ANDRE_ROUTES = ["/legal/v1/documents/clipper_agreement/versions/1.0/counsel-review",
                "/legal/v1/documents/clipper_agreement/versions/1.0/counsel-signoff",
                "/legal/v1/documents/clipper_agreement/versions/1.0/decision", "/legal/v1/envelopes",
                "/legal/v1/playbooks/proposals", "/legal/v1/playbooks/decisions", f"/legal/v1/obligations/{LG}/waive",
                "/legal/v1/obligations", "/legal/v1/memos", f"/legal/v1/matters/{LG}/close", f"/legal/v1/holds/{LG}/release",
                "/legal/v1/takedowns/outbound", "/legal/v1/filings", f"/legal/v1/filings/{LG}/filed",
                f"/legal/v1/filings/{LG}/ready", "/legal/v1/rules/proposals", "/legal/v1/rules/decisions",
                "/legal/v1/reconcile"]


def test_andre_routes_refuse_callers_the_service_token_and_non_ascii(hr):
    for path in ANDRE_ROUTES:
        for tok in (None, CALLERS["scheduler"], SERVICE_TOKEN, "t\xf6ken".encode("latin-1"), ANDRE_TOKEN + "x"):
            hd = hr.headers("scheduler")
            if tok is not None:
                hd["X-Andre-Approval-Token"] = tok
            r = hr.client.post(path, json={"request_id": "x"}, headers=hd)
            assert r.status_code == 403, (path, tok, r.status_code)
    assert hr.client.get("/legal/v1/documents/clipper_agreement/versions/1.0/text",
                         headers=hr.headers("scheduler")).status_code == 403


def test_andre_token_equal_to_a_caller_token_refuses_start_and_no_andre_token_means_no_approval():
    import config as config_mod
    from helpers import base_env
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(LEGAL_ANDRE_APPROVAL_TOKEN=CALLERS["onboarding"]))
    x = Harness(env={"LEGAL_ANDRE_APPROVAL_TOKEN": "__unset__"})
    r = x.post("/legal/v1/rules/decisions", {"request_id": "r", "decisions": [
        {"proposal_id": "p", "content_sha256": "a" * 64, "decision": "approve"}]}, andre=ANDRE_TOKEN)
    assert r.status_code == 403 and x.svc.rules_version is None


def test_limits(hr):
    hd = hr.headers("hub")
    assert hr.client.get("/legal/v1/register?x=" + "a" * 5000, headers=hd).status_code == 414
    assert hr.client.get("/legal/v1/register", headers={**hd, "X-Pad": "a" * 17000}).status_code == 431
    assert hr.client.post("/legal/v1/requests", json={"request_id": "r", "pad": "a" * (300 * 1024)},
                          headers=hd).status_code == 413
    assert hr.client.post("/legal/v1/memos", json={"request_id": "r", "pad": "a" * (8 * 1024 * 1024)},
                          headers=hr.headers(andre=ANDRE_TOKEN)).status_code == 413
    assert hr.client.post("/legal/v1/requests", content=b"request_id=r", headers={**hd, "Content-Type": "text/plain"}
                          ).status_code == 415
    deep = {"request_id": "r"}
    cur = deep
    for _ in range(40):
        cur["a"] = {}
        cur = cur["a"]
    assert hr.client.post("/legal/v1/requests", json=deep, headers=hd).status_code == 422


def test_blob_route_accepts_5_mib_and_refuses_more(he):
    big = "x" * (5 * 1024 * 1024)
    r = he.apost("/legal/v1/documents/sow/versions", {"request_id": rid(), "entity": "zbm", "text": big})
    assert r.status_code == 201
    r = he.apost("/legal/v1/documents/sow/versions", {"request_id": rid(), "entity": "zbm",
                                                      "text": big + "y"})
    assert r.status_code == 422 and big not in r.text


def test_strict_input_and_bounded_errors(hr):
    r = hr.post("/legal/v1/requests", {"request_id": "r1", "channel": "email", "requester_ref": "x", "kind": "question",
                                       "unexpected": "field"}, caller="hub")
    assert r.status_code == 422 and len(r.text) < 4000
    r = hr.post("/legal/v1/requests", {"request_id": "r2", "channel": "email", "requester_ref": "x\x07", "kind": "question"},
                caller="hub")
    assert r.status_code == 422
    r = hr.post("/legal/v1/requests", {"request_id": "bad id!", "channel": "email", "requester_ref": "x",
                                       "kind": "question"}, caller="hub")
    assert r.status_code == 422
    r = hr.post("/legal/v1/music/rulings", {"request_id": "r3", "subject_kind": "zbc_clip", "subject_id": "c",
                                            "platform": "tiktok", "paid": "yes", "music": {"present": False, "source": "none"},
                                            "reposted_or_reedited_by_zbc": False, "music_changed_since_approval": False},
                caller="creative_production")
    assert r.status_code == 422                                   # strict: no coercion of "yes"


def test_idempotency_scoped_per_caller_and_route(hr):
    body = {"request_id": "idem-1", "channel": "email", "requester_ref": "x", "kind": "routine_contract"}
    a = hr.ok(hr.post("/legal/v1/requests", body, caller="hub"), 201)
    b = hr.ok(hr.post("/legal/v1/requests", body, caller="onboarding"), 201)
    assert a["matter_id"] != b["matter_id"]
    assert hr.ok(hr.post("/legal/v1/requests", body, caller="hub"), 201) == a
    r = hr.post("/legal/v1/music/rulings", {"request_id": "idem-1", "subject_kind": "zbc_clip", "subject_id": "c",
                                            "platform": "tiktok", "paid": True, "music": {"present": False, "source": "none"},
                                            "reposted_or_reedited_by_zbc": False, "music_changed_since_approval": False},
                caller="hub")
    assert r.status_code == 403
