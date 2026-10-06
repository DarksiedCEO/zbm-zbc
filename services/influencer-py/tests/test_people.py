"""Influencers (ADR 0015 decisions 6-8): the creator application and its 18+ attestation, researched prospects,
discovery through NOT_BUILT ports only, verified first names."""

from __future__ import annotations

import pytest

from helpers import FakeSource, rid, wired_ports


# ------------------------------------------------------------------------------------------------ 18+ attestation

def test_an_adult_application_is_recorded_with_the_flag_only(h):
    link = h.ok(h.link(), 201)
    assert set(link) == {"confirmation_id", "confirmation_status"} and not h.svc.influencers   # an address only
    s = h.ok(h.confirm(link["confirmation_id"]))
    assert s["record_exists"] is False and not h.svc.influencers
    inf = h.ok(h.submit(s), 201)
    assert inf["adult_attested"] is True and inf["source"] == "inbound_application" and inf["email_confirmed"]
    assert inf["attestation"] == {"text_version": "age-v1", "text_sha256": inf["attestation"]["text_sha256"],
                                  "source": "creator_form"}
    assert inf["fit"]["zbc"]["tier"] == "A" and [x["handle"] for x in inf["handles"]] == ["creator.one"]
    raw = str(h.svc.log.records)
    assert "birth" not in raw and '"age"' not in raw
    ev = h.ledger.of_type("age_attestation_recorded")[0]["_payload"]
    assert ev["adult_18_plus"] is True and "creator@example.test" not in str(ev)


@pytest.mark.parametrize("adult,code", [(False, "MINOR_REFUSED"), (None, "AGE_ATTESTATION_REQUIRED")])
def test_a_minor_or_no_attestation_is_refused_and_nothing_kept(h, adult, code):
    h.code(h.application(adult=adult), 422, code)
    assert not h.svc.influencers
    assert {r["kind"] for r in h.svc.log.records} == {"pii_key_bound", "confirmation_requested", "session_opened"}


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_only_a_literal_true_attests(h, value):
    assert h.application(adult=value).status_code == 422
    assert not h.svc.influencers


def test_the_public_form_takes_an_address_only(h):
    for extra in ({"adult_18_plus": True}, {"handles": [{"platform": "x", "handle": "@a"}]}, {"display_name": "A"},
                  {"attestation_text_version": "v1"}):
        assert h.link(**extra).status_code == 422
    assert not h.svc.confirmations


def test_a_declared_minor_freezes_the_record_we_hold_until_andre_reviews(w):
    p = w.prospect(email="young@example.test", handles=(("tiktok", "@young.one"),))
    t = w.template()
    w.ok(w.post(f"/influencers/{p['influencer_id']}/first-name", {"request_id": rid(), "first_name": "Young"}))
    queued = w.ok(w.email(p, t), 201)
    w.code(w.application(email="Young+x@example.test", adult=False), 422, "MINOR_REFUSED")
    inf = w.ok(w.get(f"/influencers/{p['influencer_id']}"))
    assert inf["blocked"] == "MINOR_DECLARED" and inf["adult_attested"] is False
    assert inf["suppressed"] is False            # frozen for Andre's review, never opted out for good on its own
    assert w.svc.messages[queued["message_id"]]["reason"] == "INFLUENCER_BLOCKED"
    w.code(w.email(inf, t), 403, "INFLUENCER_BLOCKED")
    w.code(w.link(email="young@example.test"), 403, "INFLUENCER_BLOCKED")
    ev = w.ledger.of_type("influencer_blocked")[0]["_payload"]
    assert ev == {"influencer_id": p["influencer_id"], "reason": "MINOR_DECLARED"}


def test_andre_confirms_a_minor_for_good(w):
    p = w.prospect(email="young@example.test")
    w.code(w.application(email="young@example.test", adult=False), 422, "MINOR_REFUSED")
    url = f"/influencers/{p['influencer_id']}/minor-review"
    w.code(w.post(url, {"request_id": rid(), "decision": "confirm_minor"}), 403, "ANDRE_APPROVAL_REQUIRED")
    inf = w.ok(w.post(url, {"request_id": rid(), "decision": "confirm_minor"}, andre=True))
    assert inf["blocked"] == "MINOR_CONFIRMED" and inf["suppressed"] is True
    w.code(w.post(url, {"request_id": rid(), "decision": "not_a_minor"}, andre=True), 409, "NO_MINOR_REVIEW")


def test_andre_releases_a_false_declaration_and_the_prior_attestation_returns(w):
    inf = w.creator(email="real@example.test", handles=(("x", "@real"),))
    c = w.campaign()
    b = w.brief(c)
    w.code(w.application(email="real@example.test", adult=False), 422, "MINOR_REFUSED")   # a slip of the mouse
    w.code(w.deal(inf, c, b), 403, "INFLUENCER_BLOCKED")
    out = w.ok(w.post(f"/influencers/{inf['influencer_id']}/minor-review",
                      {"request_id": rid(), "decision": "not_a_minor"}, andre=True))
    assert out["blocked"] is None and out["adult_attested"] is True and out["suppressed"] is False
    assert out["attestation"] == inf["attestation"]
    w.ok(w.deal(out, c, b), 201)                                       # AEGIS R1-L4: not halted


def test_a_released_record_that_never_attested_stays_unattested(w):
    p = w.prospect(email="young@example.test")
    w.code(w.application(email="young@example.test", adult=False), 422, "MINOR_REFUSED")
    out = w.ok(w.post(f"/influencers/{p['influencer_id']}/minor-review",
                      {"request_id": rid(), "decision": "not_a_minor"}, andre=True))
    assert out["blocked"] is None and out["adult_attested"] is False


def test_the_minor_refusal_replays_as_a_refusal(h):
    p = h.prospect(email="young@example.test")
    s = h.session("young@example.test")
    body = {"request_id": rid(), "session_token": s["session_token"], "display_name": "Y", "adult_18_plus": False,
            "attestation_text_version": "v1", "attestation_text_sha256": "a" * 64}
    h.code(h.post("/sessions/application", body, caller="hub"), 422, "MINOR_REFUSED")
    h.code(h.post("/sessions/application", body, caller="hub"), 422, "MINOR_REFUSED")
    assert len(h.ledger.of_type("influencer_blocked")) == 1 and h.svc.influencers[p["influencer_id"]]["blocked"]


def test_only_the_creator_form_attests(h):
    s = h.session("a@b.test")
    body = {"request_id": rid(), "session_token": s["session_token"], "display_name": "A", "adult_18_plus": True,
            "attestation_text_version": "v1", "attestation_text_sha256": "a" * 64}
    for caller in ("dashboard", "influencer_agent", "provider_events"):
        h.code(h.post("/sessions/application", body, caller=caller), 403, "CALLER_NOT_ALLOWED")
        h.code(h.post("/applications", {"request_id": rid(), "email": "a@b.test"}, caller=caller), 403,
               "CALLER_NOT_ALLOWED")


def test_an_application_with_a_prospects_address_attests_that_prospect(h):
    p = h.prospect(email="found@example.test", handles=(("tiktok", "@found.one"),))
    assert p["adult_attested"] is False
    s = h.session("found@example.test")
    assert s["influencer_id"] == p["influencer_id"] and s["record_exists"] is True
    inf = h.ok(h.submit(s, handles=(("tiktok", "@other"), ("x", "@found_x"), ("instagram", "@creator.one"))), 201)
    assert inf["influencer_id"] == p["influencer_id"] and inf["adult_attested"] is True
    plats = {x["platform"]: x["handle"] for x in inf["handles"]}
    assert plats == {"tiktok": "found.one", "x": "found_x", "instagram": "creator.one"}   # tiktok kept, never moved


def test_a_handle_held_by_another_record_is_not_moved_by_an_application(h):
    p = h.prospect(email=None, handles=(("tiktok", "@famous"),))
    inf = h.ok(h.application(email="impostor@example.test", handles=(("tiktok", "@famous"),)), 201)
    assert inf["influencer_id"] != p["influencer_id"] and inf["handles"] == []
    assert h.svc.handle_index[h.svc.influencers[p["influencer_id"]]["handles"][0]["handle_hash"]] == p["influencer_id"]


@pytest.mark.parametrize("handle", ["@crеator", "a b", "@", "...", "x" * 61, "creator/../x", "名前"])
def test_a_handle_is_refused_never_altered(h, handle):
    h.code(h.application(handles=(("instagram", handle),)), 422, "HANDLE_INVALID")


def test_one_handle_per_platform(h):
    h.code(h.application(handles=(("instagram", "@a1"), ("instagram", "@a2"))), 422, "HANDLE_PLATFORM_REPEATED")


# ------------------------------------------------------------------------------------------------ prospects, discovery

def test_only_the_console_records_researched_prospects(h):
    body = {"request_id": rid(), "display_name": "F", "handles": [{"platform": "x", "handle": "@f1"}],
            "evidence_ref": "ev-1"}
    for caller in ("influencer_agent", "hub", "scheduler"):
        h.code(h.post("/influencers", body, caller=caller), 403, "CALLER_NOT_ALLOWED")
    p = h.ok(h.post("/influencers", body), 201)
    assert p["source"] == "manual_research" and p["adult_attested"] is False
    h.code(h.post("/influencers", {**body, "request_id": rid()}), 409, "INFLUENCER_EXISTS")


@pytest.mark.parametrize("source", ["public_profile", "paid_database"])
def test_discovery_ports_are_not_wired(h, source):
    r = h.post("/discovery/import", {"request_id": rid(), "source": source}, caller="influencer_agent")
    h.code(r, 503, "SOURCE_NOT_WIRED")
    assert not h.svc.influencers
    h.code(h.post("/discovery/import", {"request_id": rid(), "source": source}, caller="hub"), 403,
           "CALLER_NOT_ALLOWED")


def test_a_wired_source_records_prospects_through_the_same_gates(tmp_path):
    from helpers import Harness
    good = {"display_name": "Good", "handles": [{"platform": "youtube", "handle": "@good"}], "evidence_ref": "pd-1",
            "niches": ["gaming"]}
    records = [good, {**good, "handles": [{"platform": "x", "handle": "@x1"}], "dob": "2010-01-01"},
               {**good, "handles": [{"platform": "x", "handle": "@x2"}], "evidence_ref": "123-45-6789"},
               {**good, "handles": [{"platform": "x", "handle": "@x3"}], "adult_18_plus": True},
               {**good, "evidence_ref": "pd-2"}, "not a dict"]
    h = Harness(tmp_path, ports=wired_ports(sources={"public_profile": FakeSource(records),
                                                     "paid_database": FakeSource()}))
    out = h.ok(h.post("/discovery/import", {"request_id": rid(), "source": "public_profile"},
                      caller="influencer_agent"))
    assert len(out["created"]) == 1 and out["skipped"] == 4 and out["duplicates"] == 1
    inf = h.svc.influencers[out["created"][0]]
    assert inf["source"] == "public_profile" and inf["adult_attested"] is False


def test_a_prospect_gets_no_deal_until_it_applies(w):
    p = w.prospect()
    c = w.campaign()
    b = w.brief(c)
    w.code(w.deal(p, c, b), 403, "AGE_ATTESTATION_REQUIRED")
    w.code(w.tax(p), 403, "AGE_ATTESTATION_REQUIRED")


# ------------------------------------------------------------------------------------------------ first names

@pytest.mark.parametrize("name", ["Acc0unt Suspended http://x.test", "a@b", "www.x", "J" * 41])
def test_first_name_must_be_merge_safe(h, name):
    inf = h.ok(h.application(), 201)
    r = h.post(f"/influencers/{inf['influencer_id']}/first-name", {"request_id": rid(), "first_name": name})
    assert r.status_code == 422


def test_only_the_console_verifies_a_first_name(h):
    inf = h.ok(h.application(), 201)
    h.code(h.post(f"/influencers/{inf['influencer_id']}/first-name", {"request_id": rid(), "first_name": "Al"},
                  caller="influencer_agent"), 403, "CALLER_NOT_ALLOWED")
