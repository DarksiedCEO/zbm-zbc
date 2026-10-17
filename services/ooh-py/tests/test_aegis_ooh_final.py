"""AEGIS final red team on 0a1729e (NF-1 .. NF-7): the funds reference screen, the personal-data screen, a rate card
re-recorded under the same source, Finance (31) references, and the tests the surviving mutants showed missing."""
from __future__ import annotations

import pytest

import pii
import svc_campaigns as sc
from helpers import at, rid
from test_planning import estimate, rate_card
from test_registry import avails, face_body, requested_unit, supplier_attr


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


def _approved(h):
    ctx = h.to_proposal_sent("acme")
    h.ok(h.approve(ctx))
    return ctx


def _luhn(d: str) -> bool:
    t = 0
    for i, ch in enumerate(reversed(d)):
        x = int(ch)
        if i % 2:
            x = x * 2 - 9 if x > 4 else x * 2
        t += x
    return t % 10 == 0


# ---------------------------------------------------------------------------- NF-1: legitimate ACH refs are accepted

TRACE_LUHN = "021000021000018"          # a 15-digit ACH trace number that passes Luhn by chance


def test_nf1_an_ach_trace_number_that_passes_luhn_is_accepted(acme):
    assert _luhn(TRACE_LUHN)
    ctx = _approved(acme)
    r = acme.ok(acme.funds(ctx, ctx["sell"], ref=f"ACH-TRACE-{TRACE_LUHN}"))
    assert r["state"] == "FUNDS_VERIFIED"


def test_nf1_no_15_digit_trace_number_is_read_as_a_card():
    traces = [f"02100002{s:07d}" for s in range(1000000, 1002000)]
    assert sum(_luhn(t) for t in traces) >= 150               # the sample does contain Luhn-valid traces
    assert not any(sc.names_a_card(t) or sc.names_a_card(f"ACH-TRACE-{t}") for t in traces)


@pytest.mark.parametrize("ref", ["ACH-CREDIT-12345", "ACH-DEBIT-778", "APPLE-PAYMENT-0001", "GOOGLE.PAYMENT.99"])
def test_nf1_ach_credit_debit_and_payment_descriptions_are_accepted(acme, ref):
    ctx = _approved(acme)
    assert acme.ok(acme.funds(ctx, ctx["sell"], ref=ref))["state"] == "FUNDS_VERIFIED"


# ---------------------------------------------------------------------------- NF-2: non-ACH instruments are refused

@pytest.mark.parametrize("ref", ["4242.4242.4242.4242", "4242_4242_4242_4242", "4242:4242:4242:4242",
                                 "4242-4242-4242-4242"])
def test_nf2_a_card_number_behind_any_separator_is_refused_and_never_recorded(acme, ref):
    ctx = _approved(acme)
    acme.refused(acme.funds(ctx, ctx["sell"], ref=ref), 422, "FUNDS_METHOD_CONFLICT")
    assert not acme.ledger.of_type("client_funds_verified_by_andre")


@pytest.mark.parametrize("ref", ["westernunion-1", "western-union-1", "moneygram-1", "wu-77", "mo-5", "btc-1",
                                 "eth-9", "ltc-1", "doge-1", "stripe-ch-1", "square-1", "prepaid-1",
                                 "visa4242", "money-order-5", "debit-card-1", "credit-card-1", "apple-pay-1"])
def test_nf2_non_ach_instruments_are_refused(acme, ref):
    ctx = _approved(acme)
    acme.refused(acme.funds(ctx, ctx["sell"], ref=ref), 422, "FUNDS_METHOD_CONFLICT")


@pytest.mark.parametrize("card", ["4222222222222", "4111111111111111", "5555555555554444", "378282246310005",
                                  "6011111111111117", "4000000000000000006"])
def test_nf7_luhn_valid_card_numbers_of_13_to_19_digits_are_cards(card):
    assert _luhn(card) and sc.names_a_card(card) and sc.names_a_card(f"boa-{card}")


@pytest.mark.parametrize("digits", ["4111111111111112", "5555555555554445", "424242424242", "42424242424242424242",
                                    "1111111111111111", "cc3f9a77"])
def test_nf7_other_digit_runs_are_not_cards(digits):
    """Luhn-invalid, too short (12), too long (20), no card-network prefix, a random hex token."""
    assert not sc.names_a_card(digits)


def test_nf7_short_method_words_count_only_as_whole_tokens():
    assert sc.funds_ref_parts("cc-4242")[0] == {"card"}
    assert sc.funds_ref_parts("cc4242")[0] == set()           # accepted limit (NF-2): a bank prefix like CC4491
    assert sc.funds_ref_parts("visa4242")[0] == {"card"}
    assert sc.funds_ref_parts("eth")[0] == {"crypto"}
    assert sc.funds_ref_parts("ach-debit-1")[0] == {"ach"}


# ---------------------------------------------------------------------------- NF-3 / NF-4: the personal-data screen

def _text_attr(h, ref, text):
    fid = h.ok(h.post("/faces", face_body(h, ref=ref)), 201)["face_id"]
    f = h.ok(h.get(f"/faces/{fid}"))
    return h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                               "attribute": "location_description", "value": text,
                                               "source_id": h.source("lamar")})


PERSONAL = ["call 555−123−4567", "call 555-1234", "+52 55 1234 5678", "+44 20 7946 0958", "jane&#64;example.com",
            "jane@example[.]com", "jane@example(.)com", "jane<at>example<dot>com", "555,123,4567", "555 | 123 | 4567",
            "555·123·4567", "jane@example。com", "tel:555:123:4567", "JANE@EXAMPLE.COM", "Jane.Doe@Example.Com",
            "555‎123‎4567", "ref 123456789012"]
ORDINARY = ["Digital @ Sunset. Faces west", "Install crew @ Lamar. No access Sundays", "GPS 34.101 -118.3265",
            "34.0522, 118.2437", "Open at 6.pm daily", "lit 06:00-24:00", "Flight 2026-11-01 to 2026-11-28",
            "ZIP 90028-1234", "1,250,000 weekly impressions", "+15% markup suggested", "14x48 bulletin"]


@pytest.mark.parametrize("text", PERSONAL)
def test_nf3_personal_data_is_refused_in_free_text(acme, text):
    acme.refused(_text_attr(acme, f"p-{PERSONAL.index(text)}", text), 422, "ATTRIBUTE_VALUE_PERSONAL_DATA")


@pytest.mark.parametrize("text", ORDINARY)
def test_nf4_ordinary_ooh_text_is_accepted(acme, text):
    acme.ok(_text_attr(acme, f"o-{ORDINARY.index(text)}", text))


def test_nf3_a_phone_with_colons_in_a_reference_attribute_is_refused_and_not_recorded(acme):
    acme.refused(supplier_attr(acme, "lamar", "cancellation_policy_ref", "tel:555:123:4567"), 422,
                 "ATTRIBUTE_VALUE_PERSONAL_DATA")
    assert not any(e["_payload"].get("value") == "tel:555:123:4567"
                   for e in acme.ledger.of_type("supplier_attribute_sourced"))


@pytest.mark.parametrize("name", ["Jane'555'123'4567", "Jane 555&123&4567"])
def test_nf3_a_campaign_name_with_a_phone_is_refused(acme, name):
    r = acme.post("/tenants/acme/campaigns", {"request_id": rid(), "name": name, "markets": ["los-angeles"]})
    assert r.status_code == 422


def test_nf4_redaction_removes_only_the_personal_data():
    out = pii.redact("Digital @ Sunset. Faces west, call 555-123-4567 or jane@example.com")
    assert out == "Digital @ Sunset. Faces west, call [redacted] or [redacted]"


def test_nf4_vendor_text_keeps_its_location_words(acme):
    cid = acme.campaign()
    uid = acme.unit("acme", cid, ref="nf4", text="Digital @ Sunset. Faces west, lit 06:00-24:00")
    vt = acme.unit_get("acme", cid, uid)["vendor_text"]
    assert vt["personal_data_redacted"] is False and "Sunset" in vt["text"]


# ---------------------------------------------------------------------------- NF-5: the rate card an estimate used

@pytest.mark.parametrize("amount,period", [("28000.00", None), (None, "14")])
def test_nf5_an_estimate_stops_counting_when_its_card_is_re_recorded_under_the_same_source(acme, amount, period):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref=f"nf5-{period}")
    fid = h.svc.inventory[uid]["face_id"]
    sid = rate_card(h, fid, amount="2800.00", period="28")
    h.ok(estimate(h, cid, uid), 201)
    assert h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["lines"][0]["cost"]["basis"] == "VENDOR_COST_ESTIMATE"
    rate_card(h, fid, amount=amount, period=period, source=sid)
    line = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["lines"][0]["cost"]
    assert line["basis"] == "UNKNOWN" and line["amount"] is None


def test_nf5_an_estimate_still_counts_when_the_same_card_is_recorded_again(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="nf5-same")
    fid = h.svc.inventory[uid]["face_id"]
    sid = rate_card(h, fid, amount="2800.00", period="28")
    h.ok(estimate(h, cid, uid), 201)
    rate_card(h, fid, amount="2800.00", period="28", source=sid)
    assert h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["lines"][0]["cost"]["basis"] == "VENDOR_COST_ESTIMATE"


# ---------------------------------------------------------------------------- NF-6 / NF-7: Finance (31)

def _finance_body(h, ctx, ref):
    ap = h.view("acme", ctx["cid"])["client_approval"]
    return {"request_id": rid(), "approval_id": ap["approval_id"], "verified_amount": ap["amount"], "currency": "USD",
            "finance_ref": ref, "cleared": True}


def _funded_unverified(h):
    ctx = _approved(h)
    h.ok(h.funds(ctx, ctx["sell"]))
    return ctx


def _fv(h, ctx, body, **kw):
    return h.post(f"/tenants/acme/campaigns/{ctx['cid']}/finance-verifications", body, caller="finance_31", **kw)


@pytest.mark.parametrize("variant", ["FIN31-NF6-REF", "fin31.nf6.ref", "Fin31_Nf6_Ref"])
def test_nf6_one_finance_reference_in_any_spelling_verifies_one_campaign(acme, variant):
    h = acme
    a, b = _funded_unverified(h), _funded_unverified(h)
    h.ok(_fv(h, a, _finance_body(h, a, "fin31-nf6-ref")))
    h.refused(_fv(h, b, _finance_body(h, b, variant)), 409, "FUNDS_EVIDENCE_REUSED")


def test_nf7_a_finance_retry_is_idempotent(acme):
    h = acme
    ctx = _funded_unverified(h)
    body = _finance_body(h, ctx, "fin31-retry")
    first = h.ok(_fv(h, ctx, body))
    again = h.ok(_fv(h, ctx, body))
    assert first["finance_verification"] == again["finance_verification"]
    assert len(h.ledger.of_type("client_funds_verified_by_finance_31")) == 1


def test_nf7_the_finance_route_needs_the_service_bearer(acme):
    h = acme
    ctx = _funded_unverified(h)
    headers = {k: v for k, v in h.headers("finance_31").items() if k != "Authorization"}
    r = h.client.post(f"/ooh/v1/tenants/acme/campaigns/{ctx['cid']}/finance-verifications",
                      json=_finance_body(h, ctx, "fin31-nobearer"), headers=headers)
    assert r.status_code == 401
    assert h.view("acme", ctx["cid"])["finance_verification"] is None


@pytest.mark.parametrize("field,value", [("verified_amount", "lots"), ("verified_amount", "-5.00"),
                                         ("finance_ref", "fin 31 ref"), ("finance_ref", "fin31/ref"),
                                         ("cleared", False), ("currency", "usd")])
def test_nf7_a_malformed_finance_body_is_refused(acme, field, value):
    h = acme
    ctx = _funded_unverified(h)
    body = _finance_body(h, ctx, "fin31-malformed") | {field: value}
    assert _fv(h, ctx, body).status_code == 422


# ---------------------------------------------------------------------------- NF-7: holds, avails, faces, contracts

def test_nf7_andre_can_reject_a_hold_while_the_vendor_says_unavailable(acme):
    h = acme
    cid, uid, fid = requested_unit(h, "nf7-rej")
    h.ok(h.move("acme", cid, uid, "HOLD_REQUESTED"))
    h.ok(h.hold_evidence("acme", cid, uid), 201)
    h.ok(avails(h, fid, "UNAVAILABLE"), 201)
    h.refused(h.hold_decide("acme", cid, uid), 409, "AVAILS_UNAVAILABLE")
    u = h.ok(h.hold_decide("acme", cid, uid, decision="REJECT", reject_reason="EVIDENCE_INSUFFICIENT"))
    assert u["hold_evidence"][-1]["status"] == "REJECTED_BY_ANDRE"


@pytest.mark.parametrize("start,end", [("2026-11-01", "2026-11-15"), ("2026-11-15", "2026-11-30")])
def test_nf7_an_available_answer_must_cover_both_ends_of_the_flight(acme, start, end):
    h = acme
    cid, uid, fid = requested_unit(h, f"nf7-cover-{start}")
    flight = h.unit_get("acme", cid, uid)["flight"]
    assert flight == ["2026-11-01", "2026-11-30"]
    h.ok(avails(h, fid, "UNAVAILABLE", dated=at(days=-2)), 201)
    h.ok(avails(h, fid, "AVAILABLE", start=start, end=end, dated=at(days=-1)), 201)
    h.refused(h.move("acme", cid, uid, "HOLD_REQUESTED"), 409, "AVAILS_UNAVAILABLE")


def test_nf7_an_all_zero_face_ref_is_one_face(acme):
    h = acme
    h.ok(h.post("/faces", face_body(h, ref="LA-000")), 201)
    h.refused(h.post("/faces", face_body(h, ref="la-0")), 409, "FACE_EXISTS")


def test_nf7_superseding_one_contract_leaves_another_contracts_authorization(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    u1, q1 = h.held_unit("acme", cid, amount="2000.00")
    u2, q2 = h.held_unit("acme", cid, amount="3000.00")
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    sp = h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[q1, q2]), 201)
    h.ok(h.price("acme", cid, "APPROVED_BUY_LIMIT", "5000.00", andre=True), 201)
    h.ok(h.go("acme", cid, "QUOTES_VALIDATED"))
    h.ok(h.go("acme", cid, "PROPOSAL_SENT", andre=True))
    ctx = {"tid": "acme", "cid": cid, "uid": u1, "qid": q1, "sell_id": sp["price_id"], "sell": "6000.00",
           "amount": "2000.00"}
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, "6000.00"))
    c = h.view("acme", cid)
    ctx.update(approval_id=c["client_approval"]["approval_id"], terms_id=c["commercial"]["terms_id"])
    h.finance_verify(ctx)
    k1 = h.contract(ctx, quotes=[q1], total="2000.00")
    a1 = h.ok(h.authorize(ctx, contract_id=k1), 201)["authorization_id"]
    k2 = h.contract(ctx, quotes=[q2], total="3000.00", approve=False)
    k3 = h.contract(ctx, quotes=[q2], total="3000.00", approve=False)     # supersedes k2, which has no authorization
    assert h.svc.contracts[k2]["status"] == "VOID" and h.svc.contracts[k3]["status"] == "DRAFT"
    assert h.svc.contracts[k1]["status"] == "TERMS_APPROVED_BY_ANDRE"
    assert h.svc.authorizations[a1]["status"] == "AUTHORIZED"
