"""AEGIS reviews of OOH-2..4 (Oct 16 2026): the security HIGH (an id collision letting a preparer overwrite Andre's
record), the red team's breaks on OOH-2, the planning and personal-data bypasses, and the mutation gaps the red team
named. Each test fails if its fix is removed."""

from __future__ import annotations

import pytest

from helpers import at, rid


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


# ---------------------------------------------------------------------------------------------- security F1 (HIGH)

def test_sec_f1_a_preparer_reusing_andres_request_id_never_overwrites_his_buy_limit(acme):
    h = acme
    cid = h.campaign()
    req = rid()
    base = f"/tenants/acme/campaigns/{cid}/prices"
    lim = h.ok(h.post(base, {"request_id": req, "price_type": "APPROVED_BUY_LIMIT", "amount": "1000.00",
                             "currency": "USD"}, andre=True), 201)
    est = h.ok(h.post(base, {"request_id": req, "price_type": "VENDOR_COST_ESTIMATE", "amount": "9999.00",
                             "currency": "USD"}), 201)
    assert est["price_id"] != lim["price_id"]
    after = h.svc.prices[lim["price_id"]]
    assert after["amount"] == "1000.00" and after["price_type"] == "APPROVED_BUY_LIMIT"
    assert after["recorded_by"] == "andre"


def test_sec_f1_a_quote_under_an_approved_contract_cannot_be_rewritten(acme):
    h = acme
    ctx = h.funded()
    q = h.svc.prices[ctx["qid"]]
    before = dict(q)
    # the agent tries the quote's own request id again, with other terms
    req = next(r for (actor, r), v in h.svc.requests.items() if v[1] == ctx["qid"])
    r = h.post(f"/tenants/acme/campaigns/{ctx['cid']}/prices",
               {"request_id": req.split("|")[-1], "price_type": "VENDOR_QUOTE", "amount": "1.00", "currency": "USD",
                "vendor": "lamar", "inventory_ids": [ctx["uid"]], "valid_until": at(days=5)})
    assert r.status_code in (201, 409, 422)
    assert h.svc.prices[ctx["qid"]] == before


def test_sec_f1_every_created_id_includes_the_caller(acme):
    h = acme
    req = rid()
    a = h.ok(h.post("/tenants/acme/campaigns", {"request_id": req, "name": "Same Id", "markets": ["los-angeles"]}),
             201)
    b = h.ok(h.post("/tenants/acme/campaigns", {"request_id": req, "name": "Same Id", "markets": ["los-angeles"]},
                    caller="dashboard"), 201)
    assert a["campaign_id"] != b["campaign_id"] and len(h.svc.campaigns) == 2


def test_sec_f1_an_existing_id_is_never_reused(acme):
    with pytest.raises(Exception) as e:
        acme.svc._fresh({"x": 1}, "x")
    assert getattr(e.value, "reason", "") == "REQUEST_ID_REUSED"


# ---------------------------------------------------------------------------------------------- red team (OOH-2)

@pytest.mark.parametrize("ref", ["card-4242", "visa-1234-5678", "cash-100", "zelle-778", "VENMO:991", "paypal-1"])
def test_rt_f2_card_cash_and_app_references_are_not_ach_money(acme, ref):
    h = acme
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.refused(h.funds(ctx, ctx["sell"], ref=ref), 422, "FUNDS_METHOD_CONFLICT")
    assert h.view("acme", ctx["cid"])["funds"] == []


def test_rt_f3_leading_zeros_do_not_make_a_second_face(acme):
    h = acme
    h.ok(h.post("/faces", {"request_id": rid(), "source_id": h.source("lamar"), "vendor_face_ref": "LA-123",
                           "format": "STATIC_BILLBOARD", "market": "los-angeles"}), 201)
    for variant in ("LA-0123", "la_00123", "L-A-0-1-2-3"):
        h.refused(h.post("/faces", {"request_id": rid(), "source_id": h.source("lamar"), "vendor_face_ref": variant,
                                    "format": "STATIC_BILLBOARD", "market": "los-angeles"}), 409, "FACE_EXISTS")


def test_rt_f3_one_board_on_two_tenants_open_orders_is_refused_whatever_the_zeros(acme):
    h = acme
    h.tenant("globex")
    g = h.authorized("globex", ref="LA-123")
    h.ok(h.submit(g))
    ctx = h.funded("acme", ref="LA-0123", flight=("2026-11-15", "2026-12-14"))
    h.refused(h.authorize(ctx), 409, "FACE_ALREADY_ORDERED")


def test_rt_f4_the_purchase_gate_reads_the_vendors_avails(acme):
    h = acme
    ctx = h.authorized()
    fid = h.svc.inventory[ctx["uid"]]["face_id"]
    h.ok(h.post(f"/faces/{fid}/avails", {"request_id": rid(), "source_id": h.source("lamar", kind="VENDOR_QUOTE"),
                                         "flight_start": "2026-11-01", "flight_end": "2026-11-30",
                                         "answer": "UNAVAILABLE"}), 201)
    h.refused(h.submit(ctx), 409, "AVAILS_UNAVAILABLE")
    h.refused(h.authorize(ctx), 409, "AVAILS_UNAVAILABLE")


def test_rt_f1_a_later_answer_for_other_days_never_lifts_a_partial_unavailable(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    uid = h.unit("acme", cid, ref="pa-1")
    for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
        h.ok(h.move("acme", cid, uid, to))
    h.quote("acme", cid, [uid])
    h.ok(h.move("acme", cid, uid, "QUOTED"))
    fid = h.svc.inventory[uid]["face_id"]
    for start, end, ans, days in (("2026-11-01", "2026-11-10", "UNAVAILABLE", -3),
                                  ("2026-11-20", "2026-11-30", "AVAILABLE", -1)):
        h.ok(h.post(f"/faces/{fid}/avails", {"request_id": rid(),
                                             "source_id": h.source("lamar", kind="VENDOR_QUOTE", dated=at(days=days)),
                                             "flight_start": start, "flight_end": end, "answer": ans}), 201)
    h.refused(h.move("acme", cid, uid, "HOLD_REQUESTED"), 409, "AVAILS_UNAVAILABLE")


# ---------------------------------------------------------------------------------------------- security F2 (plan)

def test_sec_f2_an_empty_plan_never_reaches_quotes_validated(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)                                     # approved empty (allowed: nothing is bought from it)
    assert h.svc.campaigns[cid]["state"] == "PLAN_APPROVED"
    uid = h.unit("acme", cid, ref="late-1")                 # added after approval, cost unknown
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid))
    r = h.refused(h.go("acme", cid, "QUOTES_VALIDATED"), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] in ("NO_CURRENT_SELL_PRICE", "PLAN_COST_UNKNOWN")
    assert uid


def test_sec_f2_plan_guards_rerun_at_quotes_validated(acme):
    h = acme
    cid = h.campaign()
    h.ok(h.brief("acme", cid, budget_cap="6000.00"))
    h.plan("acme", cid)
    u1, q1 = h.held_unit("acme", cid, amount="4000.00")
    u2, q2 = h.held_unit("acme", cid, amount="3000.00")                # 7000.00 > 6000.00, added after approval
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "0.00"))
    h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "7000.00", andre=True, quote_refs=[q1, q2]), 201)
    r = h.refused(h.go("acme", cid, "QUOTES_VALIDATED"), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "PLAN_OVER_BUDGET"


def test_sec_f2_a_multi_unit_quote_counts_in_full_while_any_unit_is_live(acme):
    h = acme
    cid = h.campaign()
    a, b = h.unit("acme", cid, ref="mq-a"), h.unit("acme", cid, ref="mq-b")
    for uid in (a, b):
        for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
            h.ok(h.move("acme", cid, uid, to))
    h.quote("acme", cid, [a, b], "20000.00")
    h.ok(h.move("acme", cid, b, "WITHDRAWN"))
    p = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))
    assert p["totals"]["quoted"] == "20000.00"


def test_sec_f2_the_empty_plan_guard_names_itself(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    c = h.svc.campaigns[cid]
    assert h.svc._guard(c, "QUOTES_VALIDATED") == "NO_COMMERCIAL_MODEL"
    h.ok(h.commercial("acme", cid))
    c["current_sell_price_id"] = "x"                       # pretend a current, valid sell price (memory only)
    h.svc.prices["x"] = {"quote_refs": [], "valid_until": None}
    assert h.svc._guard(c, "QUOTES_VALIDATED") == "PLAN_EMPTY"


# ---------------------------------------------------------------------------------------------- security F3 (PII)

def _face(h, ref):
    return h.ok(h.post("/faces", {"request_id": rid(), "source_id": h.source("lamar"), "vendor_face_ref": ref,
                                  "format": "STATIC_BILLBOARD", "market": "los-angeles"}), 201)["face_id"]


@pytest.mark.parametrize("text", ["jane at example.com",
                                  "555/123/4567", "555_123_4567", "jane＠example.com", "jane [at] example [dot] com",
                                  "jane@ example.com", "(213) 555.0100", "２１３５５５０１００"])
def test_sec_f3_personal_data_spellings_are_caught(acme, text):
    h = acme
    fid = _face(h, f"pii-{abs(hash(text)) % 100000}")
    f = h.ok(h.get(f"/faces/{fid}"))
    r = h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                            "attribute": "location_description", "value": text,
                                            "source_id": h.source("lamar")})
    h.refused(r, 422, "ATTRIBUTE_VALUE_PERSONAL_DATA")


def test_sec_f3_vendor_text_is_redacted(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="vt-1", text="Rep: jane@example.com, cell 213-555-0100. Lit until midnight.")
    vt = h.svc.inventory[uid]["vendor_text"]
    assert vt["personal_data_redacted"] is True
    assert "jane" not in vt["text"].lower() and "0100" not in vt["text"] and "Lit until midnight" in vt["text"]


# ---------------------------------------------------------------------------------------------- mutation gaps

@pytest.mark.parametrize("write", ["source", "face", "attribute", "avails", "supplier"])
def test_mut_the_registry_kill_stops_every_registry_write(acme, write):
    h = acme
    sid = h.source("lamar")
    fid = _face(h, f"kill-{write}")
    h.ok(h.switch("capability:registry"))
    f = h.ok(h.get(f"/faces/{fid}"))
    v = h.ok(h.get("/vendors/lamar"))
    calls = {
        "source": lambda: h.post("/sources", {"request_id": rid(), "kind": "RATE_CARD", "vendor": "lamar",
                                              "doc_ref": "k", "dated": at(days=-1)}),
        "face": lambda: h.post("/faces", {"request_id": rid(), "source_id": sid, "vendor_face_ref": "kill-new",
                                          "format": "STATIC_BILLBOARD", "market": "los-angeles"}),
        "attribute": lambda: h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                                                 "attribute": "width_ft", "value": "10",
                                                                 "source_id": sid}),
        "avails": lambda: h.post(f"/faces/{fid}/avails", {"request_id": rid(), "source_id": sid,
                                                          "flight_start": "2026-11-01", "flight_end": "2026-11-30",
                                                          "answer": "AVAILABLE"}),
        "supplier": lambda: h.post("/vendors/lamar/attributes", {"request_id": rid(), "expected_version": v["version"],
                                                                 "attribute": "payment_terms_days", "value": "30",
                                                                 "source_id": sid}),
    }
    h.refused(calls[write](), 403, "KILLED_CAPABILITY")


@pytest.mark.parametrize("missing", ["confirmed_by", "confirmation_ref", "determination"])
def test_mut_a_determination_needs_every_field(h, missing):
    h.tenant("acme")
    body = {"request_id": rid(), "determination": "AGENT", "confirmed_by": "CPA", "confirmation_ref": "cpa-9"}
    body.pop(missing)
    assert h.post("/principal-agent-determinations", body, andre=True).status_code == 422
    assert h.svc.pa_determination["determination"] == "PRINCIPAL"


@pytest.mark.parametrize("caller", ["hub", "scheduler", "finance_31", "compliance_38"])
def test_mut_registry_writes_refuse_other_callers(acme, caller):
    h = acme
    sid = h.source("lamar")
    tenant = "acme" if caller == "hub" else None
    bodies = {"/sources": {"request_id": rid(), "kind": "RATE_CARD", "vendor": "lamar", "doc_ref": "c",
                           "dated": at(days=-1)},
              "/faces": {"request_id": rid(), "source_id": sid, "vendor_face_ref": "cw-1", "format": "STATIC_BILLBOARD",
                         "market": "los-angeles"},
              "/inventory-port/sync": {"request_id": rid(), "vendor": "lamar"}}
    for path, body in bodies.items():
        r = h.post(path, body, caller=caller, tenant=tenant)
        assert r.status_code == 403, (path, caller, r.text)
    v = h.ok(h.get("/vendors/lamar"))
    r = h.post("/vendors/lamar/attributes", {"request_id": rid(), "expected_version": v["version"],
                                             "attribute": "payment_terms_days", "value": "30", "source_id": sid},
               caller=caller, tenant=tenant)
    assert r.status_code == 403
    if caller != "compliance_38":                          # compliance may lower a flag (the safe direction)
        r = h.post("/markets/ventura/readiness", {"request_id": rid(), "flag": "RESEARCH_COVERAGE", "value": "NOT_MET",
                                                  "evidence_ref": "x"}, caller=caller, tenant=tenant)
        assert r.status_code == 403


@pytest.mark.parametrize("path", ["/markets", "/faces", "/vendors/lamar", "/inventory-port", "/sources/ooh-src-" + "0" * 40])
@pytest.mark.parametrize("caller", ["hub", "scheduler", "finance_31"])
def test_mut_registry_reads_refuse_other_callers(acme, path, caller):
    r = acme.get(path, caller=caller, tenant="acme" if caller == "hub" else None)
    assert r.status_code == 403, (path, caller, r.text)


def test_mut_face_filters(acme):
    h = acme
    _face(h, "flt-1")
    h.ok(h.post("/faces", {"request_id": rid(), "source_id": h.source("outfront"), "vendor_face_ref": "flt-2",
                           "format": "DIGITAL_BILLBOARD", "market": "san-diego"}), 201)
    assert [f["vendor_face_ref"] for f in h.ok(h.get("/faces", params={"market": "san-diego"}))] == ["flt-2"]
    assert [f["vendor_face_ref"] for f in h.ok(h.get("/faces", params={"format": "DIGITAL_BILLBOARD"}))] == ["flt-2"]
    assert [f["vendor_face_ref"] for f in h.ok(h.get("/faces", params={"vendor": "lamar"}))] == ["flt-1"]
    assert h.ok(h.get("/faces", params={"vendor": "lamar", "vendor_face_ref": "FLT_2"})) == []


def test_mut_registry_retries_answer_once(acme):
    h = acme
    body = {"request_id": rid(), "kind": "RATE_CARD", "vendor": "lamar", "doc_ref": "rt-1", "dated": at(days=-1)}
    a = h.ok(h.post("/sources", body), 201)
    b = h.ok(h.post("/sources", body), 201)
    assert a == b and sum(1 for s in h.svc.sources.values() if s["doc_ref"] == "rt-1") == 1
    h.refused(h.post("/sources", {**body, "doc_ref": "rt-2"}), 409, "REQUEST_ID_REUSED")


def test_sec_f2_a_multi_unit_quote_counts_once_however_many_units_are_live(acme):
    h = acme
    cid = h.campaign()
    a, b = h.unit("acme", cid, ref="mq2-a"), h.unit("acme", cid, ref="mq2-b")
    for uid in (a, b):
        for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
            h.ok(h.move("acme", cid, uid, to))
    h.quote("acme", cid, [a, b], "20000.00")
    p = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))
    assert p["totals"]["quoted"] == "20000.00" and len(p["lines"]) == 2
    assert all(ln["cost"]["split_evenly_from_multi_unit_quote"] for ln in p["lines"])


# ---------------------------------------------------------------------------------------------- re-verification (70e6962)

@pytest.mark.parametrize("text", ["jane at example dot com", "jane @ example . com", "jane%40example.com",
                                  "ja​ne@exam​ple.com", "jose@ejemplo-señal.mx", "1 213 555 0100"])
def test_rv_n1_the_remaining_email_and_phone_spellings_are_caught(acme, text):
    h = acme
    fid = _face(h, f"rv-{abs(hash(text)) % 100000}")
    f = h.ok(h.get(f"/faces/{fid}"))
    r = h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                            "attribute": "location_description", "value": text,
                                            "source_id": h.source("lamar")})
    h.refused(r, 422, "ATTRIBUTE_VALUE_PERSONAL_DATA")


# OOH2-PII-1 digit-cluster rule: GPS, ZIP+4 and the like moved to test_ooh2_pii1_cluster_rule.py
@pytest.mark.parametrize("text", ["Posting 2026-11-01 to 2026-11-30", "Lit 06:00-24:00 daily",
                                  "1,250,000 weekly impressions (vendor-stated)", "I-405 N at Wilshire Blvd, exit 55",
                                  "12345 Ventura Blvd, Studio City CA 91604"])
def test_rv_n3_ordinary_ooh_text_passes_the_screen(acme, text):
    h = acme
    fid = _face(h, f"ok-{abs(hash(text)) % 100000}")
    f = h.ok(h.get(f"/faces/{fid}"))
    h.ok(h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                             "attribute": "location_description", "value": text,
                                             "source_id": h.source("lamar")}))


def test_rv_n3_redaction_keeps_the_case_and_the_flight_dates(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="vt-2", text="Flight 2026-11-01 to 2026-11-30. Call Jane at 213-555-0100.")   # "..": one digit cluster now
    t = h.svc.inventory[uid]["vendor_text"]["text"]
    assert "2026-11-01" in t and "Flight" in t and "Jane" in t and "0100" not in t


def test_rv_n1_a_campaign_name_with_personal_data_is_refused(acme):
    r = acme.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "Jane Doe 555-123-4567",
                                              "markets": ["los-angeles"]})
    acme.refused(r, 422, "ATTRIBUTE_VALUE_PERSONAL_DATA")


@pytest.mark.parametrize("ref", ["money-order-5", "MO-778", "crypto-1", "btc-9a", "usdt-12", "cc-4242", "mc-881",
                                 "creditcard-1", "applepay-7", "giftcard-55", "4111-1111-1111-1111", "4111111111111111"])
def test_rv_n2_more_non_ach_instruments_and_card_numbers_are_refused(acme, ref):
    h = acme
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.refused(h.funds(ctx, ctx["sell"], ref=ref), 422, "FUNDS_METHOD_CONFLICT")


def test_rv_n4_one_finance_reference_verifies_one_campaign(acme):
    h = acme
    h.tenant("globex")
    a = h.funded("acme")
    b = h.to_proposal_sent("globex")
    h.ok(h.approve(b))
    h.ok(h.funds(b, b["sell"]))
    ref = h.view("acme", a["cid"])["finance_verification"]["finance_ref"]
    ap = h.view("globex", b["cid"])["client_approval"]
    r = h.post(f"/tenants/globex/campaigns/{b['cid']}/finance-verifications",
               {"request_id": rid(), "approval_id": ap["approval_id"], "verified_amount": ap["amount"],
                "currency": "USD", "finance_ref": ref, "cleared": True}, caller="finance_31")
    h.refused(r, 409, "FUNDS_EVIDENCE_REUSED")



@pytest.mark.parametrize("ref", ["boa-ach-cc4491", "CC4491", "mc3f0a", "boa-ach-mc77"])
def test_rv_n2_short_instrument_words_count_only_as_whole_tokens(acme, ref):
    """An ACH reference that merely starts with "cc" or "mc" before a digit is not a card (a random funds reference
    made this test suite flaky before). "mo5", "btc1", "eth1" are refused (delta red team: test_aegis_ooh_final)."""
    h = acme
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, ctx["sell"], ref=ref))



def _with_luhn_digit(body: str) -> str:
    for d in "0123456789":
        n = body + d
        total = sum((int(c) * 2 - 9 if int(c) > 4 else int(c) * 2) if i % 2 else int(c)
                    for i, c in enumerate(reversed(n)))
        if total % 10 == 0:
            return n
    raise AssertionError


def test_rv_n2_ach_trace_numbers_that_pass_luhn_are_not_mistaken_for_cards(acme):
    """15-digit ACH trace numbers (routing prefixes 02 and 12) that happen to pass the Luhn check are not cards;
    a Visa-prefixed Luhn number still is."""
    from svc_campaigns import _luhn_card
    traces = [_with_luhn_digit("02100002100001"), _with_luhn_digit("12104288000000")]
    assert all(not _luhn_card(t) for t in traces)
    assert _luhn_card(_with_luhn_digit("411111111111111"))
    h = acme
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, ctx["sell"], ref=f"trace-{traces[0]}"))
