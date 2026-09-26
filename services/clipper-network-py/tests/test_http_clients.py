"""Thin clients (V&I, Compliance, Creative): echo checks, pinned flags, size cap, retry, fail-closed mapping.
httpx.MockTransport only — no sockets."""

from __future__ import annotations

import json

import httpx
import pytest

from httpclients import HttpCompliance, HttpCreative, HttpVerificationIntegrity, facts_sha256


def mock(handler):
    return httpx.MockTransport(handler)


def vi(handler, **kw):
    return HttpVerificationIntegrity("http://vi.test", "svc", "caller", transport=mock(handler), **kw)


def cmp_(handler, **kw):
    return HttpCompliance("http://cmp.test", "svc", "caller", transport=mock(handler), **kw)


def test_vi_age_subject_maps_and_refuses_inconsistent():
    def ok(req):
        assert req.headers["X-VI-Caller-Token"] == "caller" and req.headers["Authorization"] == "Bearer svc"
        return httpx.Response(200, json={"subject_id": "cn-clp-1", "status": "adult", "attestation_id": "vi-age-1",
                                         "rules_pinned": True})
    a = vi(ok).age_subject("cn-clp-1")
    assert a.available and a.status == "adult" and a.attestation_id == "vi-age-1"
    for body in ({"subject_id": "other", "status": "adult", "attestation_id": "x", "rules_pinned": True},
                 {"subject_id": "cn-clp-1", "status": "ADULT", "attestation_id": "x", "rules_pinned": True},
                 {"subject_id": "cn-clp-1", "status": "adult", "attestation_id": None, "rules_pinned": True},
                 {"subject_id": "cn-clp-1", "status": "adult", "attestation_id": "x", "rules_pinned": False},
                 {"subject_id": "cn-clp-1", "status": "adult", "attestation_id": "x"}, ["not", "a", "dict"]):
        assert vi(lambda r, b=body: httpx.Response(200, json=b)).age_subject("cn-clp-1").available is False, body
    unp = vi(lambda r: httpx.Response(200, json={"subject_id": "cn-clp-1", "status": "adult", "attestation_id": "x",
                                                 "rules_pinned": False}), accept_unpinned=True)
    assert unp.age_subject("cn-clp-1").available is True


def test_vi_age_check_must_echo_request_and_facts():
    seen = {}

    def h(req):
        body = json.loads(req.content)
        seen.update(body)
        facts = {k: v for k, v in body.items() if k != "request_id"}
        return httpx.Response(200, json={"request_id": body["request_id"], "facts_sha256": facts_sha256(facts),
                                         "subject_id": body["subject_id"], "status": "minor", "attestation_id": "vi-a",
                                         "rules_pinned": True})
    a = vi(h).age_check("rq-1", "cn-clp-1", "2009-01-01", True, "photo_id_match", "p")
    assert a.available and a.status == "minor" and seen["dob"] == "2009-01-01"

    def wrong(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"request_id": body["request_id"], "facts_sha256": "0" * 64,
                                         "subject_id": body["subject_id"], "status": "adult", "attestation_id": "vi-a",
                                         "rules_pinned": True})
    assert vi(wrong).age_check("rq-1", "cn-clp-1", "2009-01-01", True, "photo_id_match", "p").available is False


def test_non_200_timeout_oversize_and_encoded_are_unavailable():
    assert vi(lambda r: httpx.Response(404, json={})).integrity("c").available is False
    assert vi(lambda r: httpx.Response(200, content=b"x" * (1024 * 1024 + 10))).integrity("c").available is False
    assert vi(lambda r: httpx.Response(200, content=b"{}", headers={"content-encoding": "gzip"})).integrity("c").available is False

    def boom(req):
        raise httpx.ConnectError("down")
    assert vi(boom).integrity("c").available is False
    assert vi(lambda r: httpx.Response(200, content=b"not json")).integrity("c").available is False


def test_one_retry_on_5xx_with_the_same_request():
    calls = []

    def h(req):
        calls.append(json.loads(req.content)["request_id"])
        if len(calls) == 1:
            return httpx.Response(502)
        return httpx.Response(200, json={"request_id": calls[-1], "clipper_id": "c", "status": "clear",
                                         "finding_ids": [], "rules_pinned": True})
    a = vi(h).identity_check("rq-9", "c", "a@b.co")
    assert a.available and a.status == "clear" and calls == ["rq-9", "rq-9"]


def test_vi_finding_404_means_unknown_not_unavailable():
    a = vi(lambda r: httpx.Response(404, json={"detail": "no"})).finding("vi-fnd-1")
    assert a.available is True and a.finding is None


def test_vi_strike_feed_validation():
    good = {"strikes": [{"strike_id": "s1", "clipper_id": "c", "class": "S3", "status": "active", "rule_id": "VI-10",
                         "finding_ids": ["f"], "evidence_ids": ["e"], "issued_at": "2026-09-27T10:00:00Z"}],
            "next_cursor": None, "rules_pinned": True}
    f = vi(lambda r: httpx.Response(200, json=good)).strikes(None)
    assert f.available and f.strikes[0].strike_class == "S3"
    bad = json.loads(json.dumps(good))
    bad["strikes"][0]["class"] = "S9"
    assert vi(lambda r: httpx.Response(200, json=bad)).strikes(None).available is False


def test_compliance_rule_echo_and_pinning():
    def h(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"ruling_id": "cmp-rul-1", "subject_id": body["subject_id"], "lane": "zbc_creator",
                                         "allowed": False, "unmet_lines": ["compliance_38/HR-02/fact_missing:age: x"],
                                         "request_id": body["request_id"], "facts_sha256": facts_sha256(body["facts"]),
                                         "seed_pinned": True})
    r = cmp_(h).creator_activation("rq", "cn-clp-1", {"a": 1})
    assert r.available and not r.allowed and r.unmet_lines[0].startswith("compliance_38/")

    def unpinned(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"ruling_id": "cmp-rul-1", "subject_id": body["subject_id"], "lane": "zbc_creator",
                                         "allowed": True, "unmet_lines": [], "request_id": body["request_id"],
                                         "facts_sha256": facts_sha256(body["facts"]), "seed_pinned": False})
    assert cmp_(unpinned).creator_activation("rq", "cn-clp-1", {"a": 1}).available is False

    def lying(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"ruling_id": "cmp-rul-1", "subject_id": body["subject_id"], "lane": "zbc_creator",
                                         "allowed": True, "unmet_lines": ["contradiction"], "request_id": body["request_id"],
                                         "facts_sha256": facts_sha256(body["facts"]), "seed_pinned": True})
    assert cmp_(lying).creator_activation("rq", "cn-clp-1", {"a": 1}).available is False


def test_compliance_resolve_and_missing_latest_route():
    def h(req):
        return httpx.Response(200, json={"resolution_id": "jur-x", "register_version": 3,
                                         "answers": [{"who": "person", "code": "US-CA", "class": "operate"}]})
    j = cmp_(h).resolve_person("rq", "US", "US-CA", True, "a")
    assert j.available and j.jurisdiction_class == "operate"
    assert cmp_(lambda r: httpx.Response(200, json={"resolution_id": "jur-x", "register_version": None,
                                                    "answers": []})).resolve_person("rq", "US", "US-CA", True, "a").available is False
    # the latest-activation route does not exist on compliance-py yet: 404 -> unavailable -> blocked
    assert cmp_(lambda r: httpx.Response(404, json={"detail": "Not Found"})).latest_activation("zbc_creator", "c").available is False


def test_creative_rulebooks_and_kit():
    def h(req):
        if req.url.path.endswith("/rulebooks"):
            return httpx.Response(200, json={"campaign_id": "camp-1", "versions": [{"version": 1, "status": "superseded"},
                                                                                  {"version": 2, "status": "live"}],
                                             "next_offset": None})
        return httpx.Response(200, json={"kit_id": "kit-1", "campaign_id": "camp-1", "rulebook_version": 2, "status": "signed"})
    c = HttpCreative("http://cre.test", "tok", transport=mock(h))
    assert c.live_rulebook("camp-1").live_version == 2
    k = c.kit("camp-1")
    assert k.available and k.status == "signed" and len(k.kit_sha256) == 64

    def two_live(req):
        return httpx.Response(200, json={"campaign_id": "camp-1", "versions": [{"version": 1, "status": "live"},
                                                                              {"version": 2, "status": "live"}],
                                         "next_offset": None})
    assert HttpCreative("http://cre.test", "tok", transport=mock(two_live)).live_rulebook("camp-1").available is False
    assert HttpCreative("http://cre.test", "tok", transport=mock(lambda r: httpx.Response(405))).kit("camp-1").available is False


def test_clients_are_wired_only_when_fully_configured():
    import config as config_mod
    from helpers import base_env
    from ports import Ports
    from httpclients import clients_from_settings
    s = config_mod.load(base_env(CN_VI_URL="http://127.0.0.1:9", CN_VI_SERVICE_TOKEN="a" * 40, CN_VI_CALLER_TOKEN="b" * 40))
    p = Ports()
    clients_from_settings(s, p)
    assert type(p.vi).__name__ == "HttpVerificationIntegrity" and type(p.compliance).__name__ == "NotBuiltCompliance"
    # a wired client whose service is down answers "unavailable" (no socket: the transport raises)
    assert p.vi.integrity("c").available is False
