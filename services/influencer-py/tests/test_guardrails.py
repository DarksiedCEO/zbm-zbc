"""What is never accepted (ADR 0015 decisions 7, 8 and 13): raw tax ids by key or by value, dates of birth and ages,
floats for money; and the shared request limits."""

from __future__ import annotations

import pytest

import textguard
from helpers import MEDIA, rid


# ------------------------------------------------------------------------------------------------ raw tax ids

@pytest.mark.parametrize("key", ["ssn", "SSN", "tin", "ein", "EIN", "itin", "tax_id", "taxId", "tax-id", "TaxID",
                                 "taxpayer_identification_number", "social_security_number", "socialSecurityNo",
                                 "tin_last4", "businessTin", "employer_identification_number", "fein"])
def test_a_tax_id_key_anywhere_is_refused(w, key):
    inf = w.creator()
    body = {"request_id": rid(), "influencer_id": inf["influencer_id"], "tax_form": "w9",
            "tax_ref": "stripe:acct_TESTabcdefghijklmnop", "legal_form": "individual", "country": "US", key: "x"}
    w.code(w.post("/tax-profiles", body, caller="hub"), 422, "TAX_ID_REFUSED")
    nested = {"request_id": rid(), "display_name": "A", "email": "a@b.test", "adult_18_plus": True,
              "attestation_text_version": "v1", "attestation_text_sha256": "a" * 64, "extra": [{key: "1"}]}
    w.code(w.post("/applications", nested, caller="hub"), 422, "TAX_ID_REFUSED")
    assert inf["influencer_id"] in w.svc.influencers and w.svc.influencers[inf["influencer_id"]]["tax"] is None


@pytest.mark.parametrize("value", ["123-45-6789", "123456789", "123 45 6789", "12-3456789", "１２３-４５-６７８９",
                                   "123–45–6789"])
def test_a_tax_id_shaped_reference_is_refused(w, value):
    inf = w.creator()
    for ref in (f"stripe:acct_{value}abcdefghijklmn", f"vault:{value}", f"stripe:{value}"):
        r = w.tax(inf, ref=ref)
        assert r.status_code == 422, (ref, r.text)


@pytest.mark.parametrize("text", ["my SSN is 123-45-6789 thanks", "EIN 12-3456789", "ssn123456789",
                                  "code 123 45 6789 here"])
def test_a_tax_id_in_free_text_is_refused_everywhere(w, text):
    inf = w.creator()
    c = w.campaign()
    w.code(w.post("/briefs", {"request_id": rid(), "campaign_id": c["campaign_id"], "title": "t", "text": text,
                              "disclosure": "#ad"}, caller="influencer_agent"), 422, "TAX_ID_REFUSED")
    w.code(w.post("/dm-drafts", {"request_id": rid(), "influencer_id": inf["influencer_id"], "platform": "instagram",
                                 "brand": "zbm", "text": text}, caller="influencer_agent"), 422, "TAX_ID_REFUSED")
    w.code(w.post("/templates", {"request_id": rid(), "brand": "zbm", "name": "t1", "subject": "Hello",
                                 "body": text}, caller="influencer_agent"), 422, "TAX_ID_REFUSED")
    assert not w.svc.briefs and not w.svc.dm_drafts and not w.svc.templates


@pytest.mark.parametrize("ok", ["Call 310-555-0123 any time", "+1 310 555 0123", "Order 1234567890",
                                "Post on 10/6/2026 9am", "2026-10-06T18:00:00Z", "code SAVE20 and 4567-8912"])
def test_phone_numbers_dates_and_codes_are_not_tax_ids(ok):
    assert textguard.problem({"text": ok}) is None


def test_ids_and_hex_are_not_tax_ids_but_a_bare_nine_digit_id_is():
    assert textguard.problem({"request_id": "r-0123456789abcdef0123456789abcdef"}) is None
    assert textguard.problem({"evidence_ref": "ev-123456789"}) == "TAX_ID_REFUSED"
    assert textguard.problem({"content_sha256": "1" * 9 + "a" * 55}) is None
    assert textguard.problem({"fee": "123456789.00"}) is None          # money fields are canonical amounts


def test_a_reply_text_is_never_refused_for_its_content(w):
    inf = w.creator()
    r = w.post("/replies", {"request_id": rid(), "channel": "email", "from_email": "creator@example.test",
                            "text": "stop. my ssn is 123-45-6789"}, caller="provider_events")
    body = w.ok(r, 201)
    assert body["suppressed"] is True and body["influencer_id"] == inf["influencer_id"]
    w.code(w.post("/replies", {"request_id": rid(), "channel": "email", "from_email": "creator@example.test",
                               "ssn": "x", "text": "hi"}, caller="provider_events"), 422, "TAX_ID_REFUSED")


# ------------------------------------------------------------------------------------------------ minors, personal data

@pytest.mark.parametrize("key", ["dob", "date_of_birth", "dateOfBirth", "birthday", "birth_year", "age", "Age",
                                 "passport_number", "card_number", "bank_account", "ip_address", "gender"])
def test_no_date_of_birth_age_or_other_personal_field(w, key):
    r = w.application(**{key: "1990-01-01"})
    w.code(r, 422, "FORBIDDEN_FIELD")
    assert not w.svc.influencers


# ------------------------------------------------------------------------------------------------ money

@pytest.mark.parametrize("fee", [1000.0, 1000, "1000", "1000.0", "1e3", "-5.00", "1,000.00", " 1000.00", "NaN"])
def test_money_is_a_canonical_string_only(w, fee):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    r = w.deal(inf, c, b, fee=fee)
    assert r.status_code == 422, (fee, r.text)
    assert not w.svc.deals


def test_a_float_payout_amount_is_refused(w):
    inf, d, content = w.paid_ready()
    r = w.post("/payouts", {"request_id": rid(), "deal_id": d["deal_id"], "amount": 10.5,
                            "content_ids": [content["content_id"]]}, caller="influencer_agent")
    assert r.status_code == 422 and not w.svc.payouts


# ------------------------------------------------------------------------------------------------ request limits

def test_request_limits_and_auth(h):
    assert h.client.post("/inf/v1/campaigns", content=b"x" * (129 * 1024), headers={
        **h.headers("influencer_agent"), "content-type": "application/json"}).status_code == 413
    assert h.client.post("/inf/v1/campaigns", content=b"brand=zbm", headers={
        **h.headers("influencer_agent"), "content-type": "application/x-www-form-urlencoded"}).status_code == 415
    deep = b"[" * 40 + b"]" * 40
    assert h.client.post("/inf/v1/campaigns", content=deep, headers={
        **h.headers("influencer_agent"), "content-type": "application/json"}).status_code == 422
    r = h.client.get("/inf/v1/status", headers=[(b"authorization", b"Bearer t\xe9st-token")])
    assert r.status_code == 401                       # a non-ASCII token is a 401, never a 500
    r = h.client.post("/inf/v1/campaigns", json={"request_id": rid(), "brand": "zbm", "name": "n",
                                                  "kind": "influencer"},
                      headers={**h.headers(None), "X-INF-Caller-Token": "nope" * 10})
    h.code(r, 403, "CALLER_UNKNOWN")
    assert h.client.get("/docs").status_code == 404 and h.client.get("/openapi.json").status_code == 404


def test_errors_never_echo_the_request(w):
    inf = w.creator()
    r = w.post("/dm-drafts", {"request_id": rid(), "influencer_id": inf["influencer_id"], "platform": "instagram",
                              "brand": "zbm", "text": "x" * 1001 + "SECRET-MARKER"}, caller="influencer_agent")
    assert r.status_code == 422 and "SECRET-MARKER" not in r.text


def test_unknown_fields_are_refused(w):
    inf, d, content = w.paid_ready()
    r = w.post("/contents", {"request_id": rid(), "deal_id": d["deal_id"], "platform": "instagram",
                             "caption": "#ad hi", "media_sha256": MEDIA, "platform_label_on": True,
                             "approved": True}, caller="influencer_agent")
    assert r.status_code == 422


@pytest.mark.parametrize("value", ["١٢٣٤٥٦٧٨٩", "123 - 45 - 6789",
                                   "123.45.6789", "123_45_6789", "123−45−6789"])
def test_other_scripts_and_separators_are_still_tax_ids(value):
    assert textguard.problem({"note": f"id {value}"}) == "TAX_ID_REFUSED"
    assert textguard.problem({"evidence_ref": value}) == "TAX_ID_REFUSED"


def test_handles_read_like_ids(h):
    h.ok(h.application(handles=(("tiktok", "@gamer123456789"),)), 201)
    h.code(h.application(email="b@example.test", handles=(("x", "@123456789"),)), 422, "TAX_ID_REFUSED")


def test_a_reply_from_any_handle_or_address_always_lands(w):
    w.creator(handles=(("x", "@gamer123456789"),))
    for frm in ({"from_handle": "@123456789"}, {"from_handle": "@gamer123456789"}):
        out = w.ok(w.post("/replies", {"request_id": rid(), "channel": "x", "text": "stop", **frm},
                          caller="provider_events"), 201)
        assert out["suppressed"] is True
