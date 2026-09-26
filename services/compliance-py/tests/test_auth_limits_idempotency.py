"""Auth (401 never 500), caller authorization (403), request limits, malformed input, idempotency."""

import json

import pytest

import api
from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, Harness, client_facts, clip_facts, creator_facts, rid

ROUTES = [
    ("POST", "/compliance/v1/rule"), ("POST", "/compliance/v1/review"), ("POST", "/compliance/v1/gates/activation"),
    ("POST", "/compliance/v1/gates/payout"), ("POST", "/compliance/v1/gates/publish"),
    ("GET", "/compliance/v1/rulings/cmp-rul-x"), ("POST", "/compliance/v1/sanctions/screen"),
    ("POST", "/compliance/v1/accessibility/checks"), ("GET", "/compliance/v1/register"),
    ("GET", "/compliance/v1/register/HR-01"), ("GET", "/compliance/v1/register/versions"),
    ("POST", "/compliance/v1/register/proposals"), ("GET", "/compliance/v1/inbox"),
    ("POST", "/compliance/v1/register/decisions"), ("GET", "/compliance/v1/controls"),
    ("GET", "/compliance/v1/controls/C-01"), ("POST", "/compliance/v1/controls/C-08/results"),
    ("GET", "/compliance/v1/trust-center"), ("GET", "/compliance/v1/holds"),
    ("POST", "/compliance/v1/holds/hold-aaaaaaaaaaaaaaaaaaaa/release"), ("POST", "/compliance/v1/jurisdictions/resolve"),
    ("POST", "/compliance/v1/watcher/run"), ("POST", "/compliance/v1/controls/internal/run"),
    ("GET", "/compliance/v1/audit/export"), ("GET", "/intelligences"),
]
BAD_BEARERS = [None, "", "Bearer", "Bearer wrong", "Basic abc", f"Bearer {SERVICE_TOKEN} ",
               "Bearer töken".encode("latin-1"), "Bearer ☃☃".encode("utf-8")]


@pytest.mark.parametrize("method, path", ROUTES)
@pytest.mark.parametrize("auth", BAD_BEARERS)
def test_every_route_but_health_needs_the_bearer_token_401_never_500(h, method, path, auth):
    headers = {"X-Compliance-Caller-Token": CALLERS["scheduler"], "X-Andre-Approval-Token": ANDRE_TOKEN}
    if auth is not None:
        headers["Authorization"] = auth
    r = h.client.request(method, path, headers=headers, json={"request_id": "x"} if method == "POST" else None)
    assert r.status_code == 401, (path, r.status_code, r.text)
    assert r.headers.get("www-authenticate") == "Bearer"


def test_health_is_open_and_docs_are_off(h):
    assert h.client.get("/health").status_code == 200
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(p).status_code == 404


@pytest.mark.parametrize("path, body, allowed", [
    ("/compliance/v1/rule", {"subject_id": "c", "lane": "client", "facts": {}}, "onboarding"),
    ("/compliance/v1/review", {"subject_kind": "zbc_clip", "subject_id": "s", "facts": {}}, "creative_production"),
    ("/compliance/v1/sanctions/screen", {"subject_id": "s", "role": "payee", "legal_name": "N", "country": "US"}, "onboarding"),
    ("/compliance/v1/accessibility/checks", {"asset_ref": "a", "asset_type": "site", "content_sha256": "a" * 64,
                                             "owner_id": "o"}, "creative_production"),
    ("/compliance/v1/watcher/run", {}, "scheduler"),
    ("/compliance/v1/controls/internal/run", {}, "scheduler"),
])
def test_wrong_caller_is_403(h, path, body, allowed):
    for name in CALLERS:
        r = h.post(path, {"request_id": rid(), **body}, caller=name)
        if name == allowed or (path.endswith("screen") and name == "finance_31"):
            assert r.status_code == 200, (name, r.text)
        else:
            assert r.status_code == 403, (name, r.status_code)
    assert h.post(path, {"request_id": rid(), **body}, caller=None).status_code == 403
    r = h.client.post(path, json={"request_id": rid(), **body},
                      headers={"Authorization": f"Bearer {SERVICE_TOKEN}", "X-Compliance-Caller-Token": "é".encode("latin-1")})
    assert r.status_code == 403


def test_proposals_need_legal_37_or_andre(hs):
    row = dict(hs.svc.current.by_id()["US-FTC-437-01"])
    body = {"kind": "amend", "target_id": row["id"], "proposed_row": {**row, "status": "unverified", "verified_at": None}}
    assert hs.propose(body, andre=None, caller="onboarding").status_code == 403
    assert hs.propose(body, andre="nope").status_code == 403
    assert hs.propose(body, andre=None, caller="legal_37").status_code == 201
    assert hs.propose(body).status_code == 201


def test_control_results_only_from_the_owner(hs):
    body = {"request_id": rid(), "result": "pass", "tested_at": "2026-09-26T11:00:00Z",
            "evidence": [{"kind": "ack_export", "ref": "exp-1", "sha256": "a" * 64}]}
    assert hs.post("/compliance/v1/controls/C-08/results", body, caller="vendor_33").status_code == 403
    assert hs.post("/compliance/v1/controls/C-08/results", body, caller="people_43").status_code == 200
    assert hs.post("/compliance/v1/controls/C-11/results", {**body, "request_id": rid()}, caller="scheduler").status_code == 403
    assert hs.post("/compliance/v1/controls/C-03/results", {**body, "request_id": rid()}, caller="scheduler").status_code == 403
    assert hs.post("/compliance/v1/controls/C-03/results", {**body, "request_id": rid()}, andre=ANDRE_TOKEN).status_code == 200
    assert hs.post("/compliance/v1/controls/C-06/results", {**body, "request_id": rid()},
                   caller="creative_production").status_code == 200


def test_refuses_to_start_on_bad_configuration():
    import config as c
    good = {"COMPLIANCE_SERVICE_TOKEN": SERVICE_TOKEN}
    with pytest.raises(RuntimeError):
        c.load({})
    for bad in ('{"onboarding": "short"}', '{"mallory": "' + "x" * 40 + '"}', "not json", "[1]",
                json.dumps({"onboarding": SERVICE_TOKEN}),
                json.dumps({"onboarding": "a" * 40, "scheduler": "a" * 40})):
        with pytest.raises(RuntimeError):
            c.load({**good, "COMPLIANCE_CALLER_TOKENS": bad})
    for k, v in (("COMPLIANCE_SANCTIONS_PROVIDER", "acme"), ("COMPLIANCE_A11Y_PROVIDER", "axe"),
                 ("COMPLIANCE_AUTO_REVERIFY_UNCHANGED", "1"), ("COMPLIANCE_WAYBACK_CAPTURE", "1"),
                 ("COMPLIANCE_SANCTIONS_FRESHNESS_DAYS", "0"), ("COMPLIANCE_WATCHER_ENABLED", "yes"),
                 ("COMPLIANCE_SITE_OWNER_CALLER", "mallory")):
        with pytest.raises(RuntimeError):
            c.load({**good, k: v})
    assert c.load(good).caller_tokens == {}


def test_no_caller_tokens_configured_means_every_caller_route_is_403():
    x = Harness(env={"COMPLIANCE_CALLER_TOKENS": "__unset__"})
    assert x.post("/compliance/v1/rule", {"request_id": rid(), "subject_id": "c", "lane": "client", "facts": {}},
                  caller="onboarding").status_code == 403


# --- limits and malformed input -----------------------------------------------------------

def test_body_over_the_route_limit_is_413(h):
    big = {"request_id": rid(), "subject_id": "c", "lane": "client", "facts": {"x": "a" * (300 * 1024)}}
    assert h.post("/compliance/v1/rule", big, caller="onboarding").status_code == 413
    small_route = {"request_id": rid(), "reason": "a" * (20 * 1024)}
    assert h.post("/compliance/v1/holds/hold-aaaaaaaaaaaaaaaaaaaa/release", small_route, andre=ANDRE_TOKEN).status_code == 413
    r = h.client.post("/compliance/v1/rule", content=b"{}", headers={**h.headers("onboarding"), "Content-Length": "99999999",
                                                                     "Content-Type": "application/json"})
    assert r.status_code == 413


def test_chunked_body_over_the_limit_is_413(h):
    def gen():
        for _ in range(400):
            yield b"a" * 1024
    r = h.client.post("/compliance/v1/rule", content=gen(), headers={**h.headers("onboarding"),
                                                                    "Content-Type": "application/json"})
    assert r.status_code == 413


@pytest.mark.parametrize("content, ctype, expect", [
    (b"{not json", "application/json", 422),
    (b'{"request_id": "a"}', "text/plain", 415),
    (b'{"request_id": "a"}', None, 415),
    (b"[" * 100 + b"]" * 100, "application/json", 422),
    (b"[]", "application/json", 422),
    (b'"just a string"', "application/json", 422),
])
def test_malformed_bodies(h, content, ctype, expect):
    headers = h.headers("onboarding")
    if ctype:
        headers["Content-Type"] = ctype
    r = h.client.post("/compliance/v1/rule", content=content, headers=headers)
    assert r.status_code == expect, r.text
    assert len(r.content) < 8192


def test_too_many_members_is_422(h):
    body = json.dumps({"request_id": "a", "subject_id": "c", "lane": "client", "facts": {"k": [0] * 30000}}).encode()
    r = h.client.post("/compliance/v1/rule", content=body, headers={**h.headers("onboarding"), "Content-Type": "application/json"})
    assert r.status_code == 422 and "members" in r.text


def test_long_request_target_and_head(h):
    assert h.client.get("/compliance/v1/register?" + "a=b&" * 2000, headers=h.headers("scheduler")).status_code == 414
    r = h.client.get("/compliance/v1/register", headers={**h.headers("scheduler"), "X-Pad": "a" * 17000})
    assert r.status_code == 431


@pytest.mark.parametrize("facts_over, expect", [
    ({"network_country_signal": "US\x00"}, 422),
    ({"accounts": [{"platform": "youtube", "handle_sha256": "b" * 64}] * 21}, 422),
    ({"age": "a" * 600}, 422),
    ({"creator_agreement_version": "v" * 200}, 200),   # wrong format (id > 128 chars): fact_missing, not 422
])
def test_control_chars_and_oversize_values_in_facts(hs, facts_over, expect):
    s = hs.screen("clipper-1")
    r = hs.rule("clipper-1", "zbc_creator", {**creator_facts(s["screen_id"]), **facts_over})
    assert r.status_code == expect, r.text
    if expect == 200:
        body = r.json()
        assert body["allowed"] is False
        assert any(u["code"] == "fact_missing:creator_agreement_version" for u in body["unmet"])


def test_wrong_types_are_fact_missing_not_422(hs):
    s = hs.screen("clipper-1")
    f = {**creator_facts(s["screen_id"]), "disclosure_training_attested": "yes", "payee_type": 7,
         "jurisdiction": {"declared_country": "usa", "declared_region": "US-CA", "attested": True, "attestation_ref": "a"}}
    r = hs.rule("clipper-1", "zbc_creator", f)
    assert r.status_code == 200
    codes = {u["code"] for u in r.json()["unmet"]}
    assert {"fact_missing:disclosure_training_attested", "fact_missing:payee_type",
            "fact_missing:jurisdiction.declared_country"} <= codes


def test_validation_errors_never_echo_input(h):
    secret = "SENSITIVE-" + "z" * 50
    r = h.post("/compliance/v1/sanctions/screen", {"request_id": rid(), "subject_id": "s", "role": "payee",
                                                   "legal_name": secret, "country": "usa"}, caller="onboarding")
    assert r.status_code == 422 and secret not in r.text
    r2 = h.rule("c", "client", {"unknown_" + "q" * 300: secret})
    assert r2.status_code == 422 and secret not in r2.text


def test_unknown_subject_kind_and_bad_ids_are_422(h):
    assert h.post("/compliance/v1/review", {"request_id": rid(), "subject_kind": "zbc_other", "subject_id": "s",
                                             "facts": {}}, caller="creative_production").status_code == 422
    assert h.post("/compliance/v1/rule", {"request_id": "bad id with spaces", "subject_id": "s", "lane": "client",
                                           "facts": {}}, caller="onboarding").status_code == 422
    assert h.post("/compliance/v1/rule", {"request_id": rid(), "subject_id": "s/../x", "lane": "client", "facts": {}},
                  caller="onboarding").status_code == 422
    assert h.post("/compliance/v1/gates/payout", {"request_id": rid(), "subject_kind": "zbm_work", "subject_id": "s",
                                                   "facts": {}}, caller="creative_production").status_code == 422


def test_caller_context_is_bounded_and_only_hashed(hs):
    r = hs.review("zbm_work", "w-1", {}, caller_context={"export": "x" * 9000})
    assert r.status_code == 422
    ok = hs.review("zbm_work", "w-2", {}, caller_context={"export": {"opaque": True}, "rights": {"a": 1}})
    assert ok.status_code == 200
    rec = hs.svc.rulings[ok.json()["ruling_id"]]
    assert rec["caller_context_sha256"] and "caller_context" not in rec


# --- idempotency ---------------------------------------------------------------------------

def test_identical_retry_returns_the_stored_answer_and_records_nothing_new(hs):
    s = hs.screen("clipper-1")
    body = {"request_id": "idem-1", "subject_id": "clipper-1", "lane": "zbc_creator", "facts": creator_facts(s["screen_id"])}
    a = hs.post("/compliance/v1/rule", body, caller="onboarding")
    n = len(hs.ledger.events)
    b = hs.post("/compliance/v1/rule", body, caller="onboarding")
    assert a.json() == b.json() and len(hs.ledger.events) == n


def test_request_id_reuse_after_15_minutes_is_409(hs):
    body = {"request_id": "idem-2", "subject_id": "c", "lane": "client", "facts": client_facts()}
    assert hs.post("/compliance/v1/rule", body, caller="onboarding").status_code == 200
    hs.clock.advance(minutes=16)
    r = hs.post("/compliance/v1/rule", body, caller="onboarding")
    assert r.status_code == 409 and "15 minutes" in r.json()["detail"]


def test_same_request_id_on_another_route_is_409(hs):
    assert hs.post("/compliance/v1/rule", {"request_id": "idem-3", "subject_id": "c", "lane": "client",
                                            "facts": client_facts()}, caller="onboarding").status_code == 200
    r = hs.post("/compliance/v1/gates/activation", {"request_id": "idem-3", "subject_id": "c", "lane": "client",
                                                     "facts": client_facts()}, caller="onboarding")
    assert r.status_code == 409


def test_request_ids_are_scoped_per_caller(hs):
    a = hs.post("/compliance/v1/sanctions/screen", {"request_id": "idem-4", "subject_id": "s", "role": "payee",
                                                     "legal_name": "N", "country": "US", "region": "US-CA"}, caller="onboarding")
    b = hs.post("/compliance/v1/sanctions/screen", {"request_id": "idem-4", "subject_id": "s", "role": "payee",
                                                     "legal_name": "N", "country": "US", "region": "US-CA"}, caller="finance_31")
    assert a.status_code == b.status_code == 200 and a.json()["screen_id"] != b.json()["screen_id"]


def test_retry_after_ledger_failure_reuses_the_same_event_ids(hs):
    hs.ledger.fail_on_type = "activation_ruling"
    body = {"request_id": "idem-5", "subject_id": "c", "lane": "client", "facts": client_facts()}
    assert hs.post("/compliance/v1/rule", body, caller="onboarding").status_code == 503
    hs.ledger.fail_on_type = None
    r = hs.post("/compliance/v1/rule", body, caller="onboarding")
    assert r.status_code == 200
    assert r.json()["ledger_event_id"] == r.json()["ruling_id"]
    assert len([e for e in hs.ledger.events if e["event_id"] == r.json()["ruling_id"]]) == 1


def test_decision_replay_is_idempotent_and_a_decided_proposal_cannot_be_decided_again(h):
    seed = [p for p in h.inbox() if p["kind"] == "seed"][0]
    body = {"request_id": "dec-1", "decisions": [{"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"],
                                                  "decision": "approve"}]}
    a = h.post("/compliance/v1/register/decisions", body, andre=ANDRE_TOKEN)
    b = h.post("/compliance/v1/register/decisions", body, andre=ANDRE_TOKEN)
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    c = h.post("/compliance/v1/register/decisions", {**body, "request_id": "dec-2"}, andre=ANDRE_TOKEN)
    assert c.status_code == 409 and h.svc.version_number == 1


def test_module_level_app_defaults_fail_closed():
    svc = api.app.state.service
    assert svc.health()["in_memory"] is True
    with pytest.raises(Exception):
        svc.ensure_seed_proposal()  # unconfigured ledger: nothing can be recorded
