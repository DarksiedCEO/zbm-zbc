"""Price books, quotes, proposals and hand-offs (ADR 0013 decisions 14-15, 19)."""

from __future__ import annotations

import pytest

from helpers import CALLERS, FakeHandoff, FakeLegal, Harness, rid, wired_ports


def propose(h, opp, lines, **extra):
    return h.post("/sales/v1/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                          "lines": lines, **extra}, "sales_agent")


def send(h, p, ref="msa-1"):
    return h.post(f"/sales/v1/proposals/{p['proposal_id']}/send", {"request_id": rid(), "contract_kind": "client_msa",
                                                                    "contract_ref": ref}, "sales_agent")


RR = "zbm.revenue_recovery_engagement"
SOCIAL = "zbm.social_management_monthly"
TV = "zbm.media_buy_tv"


def test_every_line_exists_with_no_price(h):
    for brand in ("zbm", "zbc"):
        book = h.ok(h.get(f"/sales/v1/pricebook/{brand}", "sales_agent"))
        assert book and all(x["approved"] is None and x["version"] == 0 for x in book)
    assert {x["line_id"] for x in h.ok(h.get("/sales/v1/pricebook/zbm"))} >= {RR, SOCIAL, TV, "zbm.media_buy_ooh",
                                                                              "zbm.media_buy_radio",
                                                                              "zbm.media_buy_digital"}


def test_no_approved_price_no_quote(w):
    opp = w.opportunity()
    w.refused(propose(w, opp, [{"line_id": RR}]), 409, "PRICE_NOT_APPROVED")


def test_only_andre_approves_prices(w):
    body = {"request_id": rid(), "version": 1, "price": "2500.00"}
    w.refused(w.post(f"/sales/v1/pricebook/zbm/lines/{RR}/approve", body, "dashboard"), 403,
              "ANDRE_APPROVAL_REQUIRED")
    w.refused(w.post(f"/sales/v1/pricebook/zbm/lines/{RR}/approve", body, "sales_agent", CALLERS["sales_agent"]),
              403, "CALLER_NOT_ALLOWED")
    assert w.ledger.of_type("price_approved") == []


def test_price_approval_is_versioned_and_bound(w):
    x = w.ok(w.price(RR, price="2500.00"))
    assert x["approved"]["version"] == 1 and len(x["approved"]["binding_sha256"]) == 64
    w.refused(w.price(RR, price="3000.00", version=1), 409, "PRICE_VERSION_STALE")
    w.refused(w.price(RR, price="3000.00", version=3), 409, "PRICE_VERSION_STALE")
    assert w.ok(w.price(RR, price="3000.00", version=2))["approved"]["price"] == "3000.00"
    ev = w.ledger.of_type("price_approved")
    assert len(ev) == 2


@pytest.mark.parametrize("price", [2500.0, 2500, "2500", "2500.001", "-1.00", "0.00", "1e3", " 10.00"])
def test_money_is_a_canonical_string_never_a_float(w, price):
    r = w.andre(f"/sales/v1/pricebook/zbm/lines/{RR}/approve", {"request_id": rid(), "version": 1, "price": price})
    assert r.status_code == 422


def test_media_lines_take_a_markup_not_a_price(w):
    w.refused(w.price(TV, price="100.00"), 422, "MARKUP_REQUIRED")
    w.refused(w.price(RR, markup="15.00"), 422, "PRICE_REQUIRED")
    w.refused(w.price(TV, markup="15.5"), 422, "MONEY_INVALID")
    assert w.ok(w.price(TV, markup="15.00"))["approved"]["markup_pct"] == "15.00"


def test_small_list_price_proposal_auto_approved_and_states_card_for_revenue_recovery(w):
    w.ok(w.price(RR, price="2500.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR, "quantity": 2}]), 201)
    assert (p["status"], p["total"], p["approved_by"]) == ("approved", "5000.00", "auto_rule")
    assert p["payment_methods"] == ["ach", "card"]
    assert w.ledger.of_type("proposal_approved")
    assert w.ok(send(w, p))["status"] == "sent"


def test_card_only_up_to_5000_and_only_revenue_recovery(w):
    w.ok(w.price(RR, price="2500.01"))
    w.ok(w.price(SOCIAL, price="1000.00"))
    assert w.ok(propose(w, w.opportunity(), [{"line_id": RR, "quantity": 2}]), 201)["payment_methods"] == ["ach"]
    opp = w.opportunity(email="b@shop-b.test", phone=None, account={"name": "B"})
    assert w.ok(propose(w, opp, [{"line_id": SOCIAL}]), 201)["payment_methods"] == ["ach"]


def test_over_ten_thousand_waits_for_andre_and_never_sends(w):
    w.ok(w.price(RR, price="10000.01"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR}]), 201)
    assert p["status"] == "pending_andre" and p["needs_andre"] == ["OVER_AUTO_APPROVE_MAX"]
    w.refused(send(w, p), 409, "PROPOSAL_NOT_APPROVED")


def test_exactly_ten_thousand_is_auto(w):
    w.ok(w.price(RR, price="5000.00"))
    assert w.ok(propose(w, w.opportunity(), [{"line_id": RR, "quantity": 2}]), 201)["status"] == "approved"


def test_lower_configured_max_is_honoured(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), SALES_AUTO_APPROVE_MAX="1000.00")
    h.ok(h.price(RR, price="1500.00"))
    assert h.ok(propose(h, h.opportunity(), [{"line_id": RR}]), 201)["status"] == "pending_andre"


def test_any_media_buy_line_waits_for_andre_even_when_small(w):
    w.ok(w.price(TV, markup="15.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": TV, "media_cost": "1000.00"}]), 201)
    assert p["status"] == "pending_andre" and "MEDIA_BUY_LINE" in p["needs_andre"]
    line = p["lines"][0]
    assert (line["media_cost"], line["markup_pct"], line["amount"]) == ("1000.00", "15.00", "1150.00")
    assert p["payment_methods"] == ["ach"]
    w.refused(send(w, p), 409, "PROPOSAL_NOT_APPROVED")


def test_per_deal_markup_is_flagged(w):
    w.ok(w.price(TV, markup="15.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": TV, "media_cost": "333.33", "markup_pct": "12.50"}]), 201)
    assert "PER_DEAL_MARKUP" in p["needs_andre"] and p["lines"][0]["amount"] == "375.00"   # 333.33 + 41.67


@pytest.mark.parametrize("extra,reason", [({"discount": "100.00"}, "DISCOUNT"),
                                          ({"custom_terms": "Net 60"}, "CUSTOM_TERMS")])
def test_discount_or_custom_terms_wait_for_andre(w, extra, reason):
    w.ok(w.price(RR, price="2500.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR}], **extra), 201)
    assert p["status"] == "pending_andre" and p["needs_andre"] == [reason]


def test_andre_approves_the_exact_proposal_then_it_can_be_sent(w):
    w.ok(w.price(TV, markup="15.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": TV, "media_cost": "50000.00"}]), 201)
    w.refused(w.andre(f"/sales/v1/proposals/{p['proposal_id']}/approve",
                      {"request_id": rid(), "content_sha256": "f" * 64}), 409, "PROPOSAL_HASH_MISMATCH")
    w.refused(w.post(f"/sales/v1/proposals/{p['proposal_id']}/approve",
                     {"request_id": rid(), "content_sha256": p["content_sha256"]}, "dashboard"), 403,
              "ANDRE_APPROVAL_REQUIRED")
    a = w.ok(w.andre(f"/sales/v1/proposals/{p['proposal_id']}/approve",
                     {"request_id": rid(), "content_sha256": p["content_sha256"]}))
    assert a["status"] == "approved" and a["approved_by"] == "andre"
    assert w.ok(send(w, p))["status"] == "sent"


def test_proposal_tampered_after_build_is_refused(w):
    w.ok(w.price(RR, price="20000.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR}]), 201)
    w.svc.proposals[p["proposal_id"]]["total"] = "1.00"
    w.refused(w.andre(f"/sales/v1/proposals/{p['proposal_id']}/approve",
                      {"request_id": rid(), "content_sha256": p["content_sha256"]}), 409, "PROPOSAL_HASH_MISMATCH")


def test_price_changed_after_the_proposal_was_built_blocks_sending(w):
    w.ok(w.price(RR, price="2500.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR}]), 201)
    w.ok(w.price(RR, price="2600.00", version=2))
    w.refused(send(w, p), 409, "PRICE_CHANGED")


def test_withdrawn_price_blocks_quotes(w):
    w.ok(w.price(RR, price="2500.00"))
    w.ok(w.andre(f"/sales/v1/pricebook/zbm/lines/{RR}/withdraw", {"request_id": rid(), "version": 1}))
    w.refused(propose(w, w.opportunity(), [{"line_id": RR}]), 409, "PRICE_NOT_APPROVED")


def test_legal_stand_in_refuses_sending(tmp_path):
    ports = wired_ports()
    ports.legal = __import__("ports").NotWiredLegal()
    h = Harness(tmp_path, ports=ports)
    h.ok(h.price(RR, price="2500.00"))
    p = h.ok(propose(h, h.opportunity(), [{"line_id": RR}]), 201)
    h.refused(send(h, p), 503, "LEGAL_UNAVAILABLE")
    assert h.ok(h.get(f"/sales/v1/proposals/{p['proposal_id']}"))["status"] == "approved"


def test_contract_not_in_force_refuses_sending(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(legal=FakeLegal("not_in_force")))
    h.ok(h.price(RR, price="2500.00"))
    p = h.ok(propose(h, h.opportunity(), [{"line_id": RR}]), 201)
    h.refused(send(h, p), 403, "CONTRACT_NOT_IN_FORCE")


@pytest.mark.parametrize("lines,code", [([{"line_id": "zbc.clipping_campaign_setup"}], "LINE_UNKNOWN"),
                                        ([{"line_id": RR}, {"line_id": RR}], "LINE_REPEATED"),
                                        ([{"line_id": TV}], "MEDIA_COST_REQUIRED"),
                                        ([{"line_id": RR, "media_cost": "10.00"}], "MEDIA_FIELDS_ON_SERVICE_LINE")])
def test_quote_shape_refusals(w, lines, code):
    w.ok(w.price(RR, price="100.00"))
    w.ok(w.price(TV, markup="15.00"))
    w.ok(w.price("zbc.clipping_campaign_setup", price="100.00"))
    w.refused(propose(w, w.opportunity(), lines), 422, code)


def test_discount_larger_than_subtotal_refused(w):
    w.ok(w.price(RR, price="100.00"))
    w.refused(propose(w, w.opportunity(), [{"line_id": RR}], discount="100.01"), 422, "DISCOUNT_TOO_LARGE")


def test_float_quantity_or_amount_refused(w):
    w.ok(w.price(TV, markup="15.00"))
    opp = w.opportunity()
    assert propose(w, opp, [{"line_id": TV, "media_cost": 1000.0}]).status_code == 422
    assert propose(w, opp, [{"line_id": RR, "quantity": 1.0}]).status_code == 422


def test_won_hands_off_to_onboarding_and_finance(w):
    w.ok(w.price(RR, price="2500.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR}]), 201)
    w.refused(w.post(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid()}, "sales_agent"), 409,
              "PROPOSAL_NOT_SENT")
    w.ok(send(w, p))
    won = w.ok(w.post(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid()}, "sales_agent"))
    assert sorted(x["kind"] for x in won["handoffs"]) == ["finance_invoice_draft", "onboarding_create_client"]
    assert all(x["status"] == "delivered" for x in won["handoffs"])
    fin = w.ports.finance.calls[0][1]
    assert fin["total"] == "2500.00" and fin["payment_methods"] == ["ach", "card"]
    assert w.ok(w.get(f"/sales/v1/opportunities/{p['opportunity_id']}"))["stage"] == "closed_won"


def test_handoff_stand_ins_stay_pending_and_the_job_retries(tmp_path):
    ports = wired_ports(onboarding=FakeHandoff("unavailable"), finance=FakeHandoff("unavailable"))
    h = Harness(tmp_path, ports=ports)
    h.ok(h.price(RR, price="2500.00"))
    p = h.ok(propose(h, h.opportunity(), [{"line_id": RR}]), 201)
    h.ok(send(h, p))
    won = h.ok(h.post(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid()}, "sales_agent"))
    assert all(x["status"] == "pending_delivery" for x in won["handoffs"])
    assert h.ok(h.job("handoff-retry"))["pending_delivery"] == 2
    ports.onboarding.status = ports.finance.status = "delivered"
    assert h.ok(h.job("handoff-retry"))["delivered"] == 2
    assert all(x["status"] == "delivered" for x in h.ok(h.get("/sales/v1/handoffs")))
    assert h.ok(h.job("handoff-retry"))["delivered"] == 0


def test_default_stand_ins_leave_handoffs_pending(h):
    assert h.ok(h.get("/sales/v1/status"))["handoffs_pending"] == 0
    h.ok(h.price(RR, price="2500.00"))
    p = h.ok(propose(h, h.opportunity(), [{"line_id": RR}]), 201)
    h.refused(send(h, p), 503, "LEGAL_UNAVAILABLE")


def test_lost_proposal(w):
    w.ok(w.price(RR, price="20000.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR}]), 201)
    assert w.ok(w.post(f"/sales/v1/proposals/{p['proposal_id']}/lost", {"request_id": rid(),
                                                                        "reason_code": "price"}, "sales_agent"))[
        "status"] == "lost"
    w.refused(w.andre(f"/sales/v1/proposals/{p['proposal_id']}/approve",
                      {"request_id": rid(), "content_sha256": p["content_sha256"]}), 409, "PROPOSAL_NOT_PENDING")


def test_expired_proposal_cannot_be_sent(w):
    w.ok(w.price(RR, price="2500.00"))
    p = w.ok(propose(w, w.opportunity(), [{"line_id": RR}]), 201)
    w.clock.advance(days=31)
    w.refused(send(w, p), 409, "PROPOSAL_EXPIRED")


def test_proposal_create_idempotent(w):
    w.ok(w.price(RR, price="2500.00"))
    opp = w.opportunity()
    body = {"request_id": rid(), "opportunity_id": opp["opportunity_id"], "lines": [{"line_id": RR}]}
    a = w.ok(w.post("/sales/v1/proposals", body, "sales_agent"), 201)
    b = w.ok(w.post("/sales/v1/proposals", body, "sales_agent"), 201)
    assert a["proposal_id"] == b["proposal_id"] and len(w.svc.proposals) == 1
    w.refused(w.post("/sales/v1/proposals", {**body, "lines": [{"line_id": RR, "quantity": 3}]}, "sales_agent"), 409,
              "REQUEST_ID_REUSED")


def test_ledger_down_no_price_and_no_proposal_approval(w):
    w.ledger.fail = True
    w.refused(w.price(RR, price="2500.00"), 503, "LEDGER_UNAVAILABLE")
    w.ledger.fail = False
    assert w.ok(w.get("/sales/v1/pricebook/zbm"))[0]["approved"] is None
