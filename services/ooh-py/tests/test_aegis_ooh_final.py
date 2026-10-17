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


def test_nf7_short_method_words():
    assert sc.funds_ref_parts("cc-4242")[0] == {"card"}
    assert sc.funds_ref_parts("cc4242")[0] == set()           # accepted limit (NF-2): a bank prefix like CC4491
    assert sc.funds_ref_parts("visa4242")[0] == {"card"}
    for ref, rail in (("mo5", "money_order"), ("btc1", "crypto"), ("eth1", "crypto"), ("wu77", "money_transfer"),
                      ("ltc3", "crypto"), ("boa-mo123", "money_order")):
        assert sc.funds_ref_parts(ref)[0] == {rail}, ref
    assert sc.funds_ref_parts("mo5x")[0] == set() and sc.funds_ref_parts("moab1")[0] == set()
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


@pytest.mark.parametrize("variant", ["FIN31-NF6-REF", "fin31.nf6.ref", "Fin31_Nf6_Ref", "FIN31NF6REF"])
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


# ---------------------------------------------------------------------------- delta red team (988725f): DF-1 .. DF-8

@pytest.mark.parametrize("ref", ["3530111333300000", "36227206271667", "5018000000000009", "2223000048400011",
                                 "6445644564456445", "4242-4242-4242-4242-1228", "4242.4242.4242.4242.0",
                                 "x-12-4242-4242-4242-4242", "money-orders-1", "giftcards-1", "credit-cards-1",
                                 "litecoin-1", "dogecoin-1", "mo5", "btc1"])
def test_df1_df5_cards_of_every_network_and_plural_instruments_are_refused(acme, ref):
    ctx = _approved(acme)
    acme.refused(acme.funds(ctx, ctx["sell"], ref=ref), 422, "FUNDS_METHOD_CONFLICT")
    assert not acme.ledger.of_type("client_funds_verified_by_andre")


def test_df1_a_date_inside_a_reference_is_never_joined_into_a_card():
    for y, m, d, n in ((2026, 10, 17, 12345678), (2025, 1, 2, 87654321), (2027, 12, 31, 40000000)):
        for seq in range(200):
            assert not sc.names_a_card(f"ach-{y}-{m:02d}-{d:02d}-{n + seq}")


@pytest.mark.parametrize("text", ["jane@163.com", "jane@126.com", "contact jane@example .com",
                                  "contact jane@example. com", "Rep 48,310 555 1234", "jane&#x40;example&#46;com",
                                  "jane&commat;example&period;com", "JANE&COMMAT;EXAMPLE&PERIOD;COM",
                                  "jane{at}example{dot}com", "jane@example{.}com", "(555) 123-4567",
                                  "+1 (555) 123-4567", "5551234567"])
def test_df2_df8_these_addresses_and_numbers_are_personal_data(acme, text):
    assert pii.has_personal_data(text)
    acme.refused(_text_attr(acme, f"df2-{abs(hash(text)) % 10 ** 6}", text), 422, "ATTRIBUTE_VALUE_PERSONAL_DATA")


def test_df2_vendor_text_with_an_address_is_redacted(acme):
    cid = acme.campaign()
    uid = acme.unit("acme", cid, ref="df2b", text="Rep: jane@163.com, lit 06:00-24:00")
    vt = acme.unit_get("acme", cid, uid)["vendor_text"]
    assert vt["personal_data_redacted"] is True and "163" not in vt["text"] and "06:00-24:00" in vt["text"]


@pytest.mark.parametrize("text", ["$12,500.00 per 4 weeks", "+1 234 567", "GPS 34.0522, -118.2437"])
def test_df3_df8_ordinary_text_is_accepted(acme, text):
    assert not pii.has_personal_data(text)
    acme.ok(_text_attr(acme, f"df3-{abs(hash(text)) % 10 ** 6}", text))


@pytest.mark.parametrize("text", ["Daypart plays: 360, 720, 1440", "Panels 101, 205, 1450 on Sunset",
                                  "GPS 34.0522342, -118.2436849", "Sunset@Vine. Faces east", "Face ID LA-101-2025"])
def test_e3_the_screen_errs_toward_refusing_these_documented_false_positives(text):
    """A false positive is refused and never stored; a miss would keep personal data (ADR 0022)."""
    assert pii.has_personal_data(text)


@pytest.mark.parametrize("text", ["Rep 310-5551234", "310 5551234", "310555 1234", "310.5551234", "310, 555, 1234",
                                  "+1 310 555-1234", "1-310.555 1234", "(310)5551234", "1(555)123-4567"])
def test_e3_a_phone_with_mixed_separators_is_refused(acme, text):
    acme.refused(_text_attr(acme, f"e3-{abs(hash(text)) % 10 ** 6}", text), 422, "ATTRIBUTE_VALUE_PERSONAL_DATA")


def test_e3_every_us_phone_layout_is_caught():
    import itertools
    seps = ["", " ", "-", ".", "/", ",", ", ", " - ", "  "]
    for a, b in itertools.product(seps, seps):
        for pre in ("", "+1 ", "1-"):
            assert pii.has_personal_data(f"Rep {pre}310{a}555{b}1234"), (pre, a, b)


@pytest.mark.parametrize("text", ["IG @lamar/jane@example.com", "Sunset@Vine;jane@example.com",
                                  "https://www.lamar.com/la/inventory/bulletins/contact-form?rep=jane@example.com",
                                  "x" * 70 + "jane@example.com", "Rep: bob@lamar.com,jane@lamar.com"])
def test_e6_an_address_after_another_at_sign_or_a_long_token_is_refused(acme, text):
    assert pii.has_personal_data(text)
    out = pii.redact(text)
    assert "jane" not in out and not pii.has_personal_data(out)


@pytest.mark.parametrize("ref", ["BOA-ACH-20261017-123456788", "ACH-021000021-000123457", "1000000000000008",
                                 "ach-2026101700000005", "WF-ACH-02100002100002"])
def test_e2_bank_references_with_dates_or_routing_numbers_are_not_cards(acme, ref):
    assert not sc.names_a_card(ref)
    ctx = _approved(acme)
    assert acme.ok(acme.funds(ctx, ctx["sell"], ref=ref))["state"] == "FUNDS_VERIFIED"


def test_e2_a_digit_run_that_cannot_start_a_card_is_never_one():
    import random
    rnd = random.Random(7)
    for _ in range(3000):
        for first in ("0", "1", "7", "80", "83", "89", "90", "95"):
            assert not sc.names_a_card(first + "".join(rnd.choice("0123456789") for _ in range(16 - len(first))))
        assert not sc.names_a_card(f"2{rnd.randint(0, 199):03d}" + "".join(rnd.choice("0123456789") for _ in range(12)))


def test_df8_an_international_number_needs_eight_digits():
    assert pii.has_personal_data("+44 2079 46") and not pii.has_personal_data("+44 2079 4")   # 8 vs 7 digits


def test_df7_redaction_leaves_no_detectable_number_behind():
    for text in ("[redacted]+44.09587946 x", "call 555-123-4567 555-123-4567", "5551234567@example.com"):
        out = pii.redact(text)
        assert not pii.has_personal_data(out) and "[redacted]" in out, (text, out)


def test_df4_e4_finance_references_that_differ_only_in_separators_are_one(acme):
    """Over-merging refuses (fails closed); it never lets one reference verify two campaigns (E4)."""
    h = acme
    a, b, c = _funded_unverified(h), _funded_unverified(h), _funded_unverified(h)
    h.ok(_fv(h, a, _finance_body(h, a, "FIN31-ABC")))
    h.refused(_fv(h, b, _finance_body(h, b, "FIN31ABC")), 409, "FUNDS_EVIDENCE_REUSED")
    h.refused(_fv(h, c, _finance_body(h, c, "-")), 422, "INVALID")    # no letter or digit: no reference
    assert sc.finance_ref_key("FIN31.Ab_c") == sc.finance_ref_key("fin31-ab-c") == "fin31abc"


@pytest.mark.parametrize("card", ["351000000000005", "361000000000003", "381000000000009", "501000000000004",
                                  "561000000000001"])
def test_e8_fifteen_digit_jcb_diners_and_maestro_numbers_are_cards(card):
    assert _luhn(card) and sc.names_a_card(card)


@pytest.mark.parametrize("ref", ["411-111-111-111-1111", "4242---4242---4242---4242", "4242 / 4242 / 4242 / 4242"])
def test_e8_a_card_in_three_digit_groups_or_behind_long_separators_is_a_card(ref):
    assert sc.names_a_card(ref)


def test_e8_a_short_leading_group_is_never_joined_into_a_card():
    assert _luhn("4201000000000009") and not sc.names_a_card("ACH-42-01000000000009")


def _card_with_prefix(prefix: str, n: int = 16) -> str:
    base = (prefix + "0" * n)[:n - 1]
    return next(base + x for x in "0123456789" if _luhn(base + x))


@pytest.mark.parametrize("prefix", ["2200", "2204", "2221", "2230", "2699", "2700", "2720", "8100", "8200", "8600",
                                    "9792", "9860", "3000", "3900", "5000", "5800", "6200", "4000"])
def test_f1_f4_every_card_network_prefix_is_a_card(prefix):
    assert sc.names_a_card(_card_with_prefix(prefix))


@pytest.mark.parametrize("prefix", ["2205", "2220", "2721", "2800", "8000", "8300", "8601", "9791", "9861", "7000",
                                    "1000", "0210", "5900"])
def test_f1_f4_a_run_outside_every_card_network_is_not_a_card(prefix):
    assert not sc.names_a_card(_card_with_prefix(prefix))


def test_f1_a_mir_card_is_refused_and_never_recorded(acme):
    ctx = _approved(acme)
    acme.refused(acme.funds(ctx, ctx["sell"], ref="2200-1022-4807-8782"), 422, "FUNDS_METHOD_CONFLICT")
    assert not acme.ledger.of_type("client_funds_verified_by_andre")


@pytest.mark.parametrize("ref", ["ACH-256074974-93982757", "ACH-322271627-72979298", "ACH-314074269-35735457"])
def test_f2_a_routing_and_account_reference_is_not_a_card(acme, ref):
    assert not sc.names_a_card(ref)
    ctx = _approved(acme)
    assert acme.ok(acme.funds(ctx, ctx["sell"], ref=ref))["state"] == "FUNDS_VERIFIED"


def test_f2_amex_grouping_4_6_5_is_still_a_card():
    assert sc.names_a_card("3782-822463-10005")


def test_f3_a_long_local_part_before_the_word_at_is_caught():
    assert pii.has_personal_data("a" * 65 + " at x1.org")


@pytest.mark.parametrize("ref", ["42424242-42424242", "4242-424242424242", "424242424242-4242", "625094-1006528599016",
                                 "3782-82246310005"])
def test_g1_a_card_split_around_a_long_group_is_a_card(acme, ref):
    assert sc.names_a_card(ref)
    ctx = _approved(acme)
    acme.refused(acme.funds(ctx, ctx["sell"], ref=ref), 422, "FUNDS_METHOD_CONFLICT")


def test_g3_a_routing_number_is_never_joined_but_a_non_routing_nine_digit_group_is():
    assert sc._aba_routing("256074974") and sc._aba_routing("021000021") and not sc._aba_routing("424242424")
    assert not sc.names_a_card("ACH-256074974-4242")             # routing + short account: never joined
    assert sc.names_a_card("424242424-2424242")                  # nine digits that are no routing number: joined
    import random
    rnd = random.Random(11)
    for _ in range(3000):
        acct = str(rnd.randint(10 ** 2, 10 ** 12))
        for routing in ("256074974", "322271627", "314074269", "021000021", "121000358"):
            assert not sc.names_a_card(f"ACH-{routing}-{acct}"), (routing, acct)


def test_h3_the_routing_checksum_is_modulo_ten():
    assert not sc._aba_routing("100000002")                     # weighted sum ends in 5: not a routing number
    assert sc.names_a_card("4000-100000002") == _luhn("4000100000002")


def test_h3_a_routing_number_in_second_place_is_not_joined_either():
    first = next(f"4{n:03d}" for n in range(1000) if _luhn(f"4{n:03d}256074974"))
    assert _luhn(first + "256074974") and not sc.names_a_card(f"ACH-{first}-256074974")
