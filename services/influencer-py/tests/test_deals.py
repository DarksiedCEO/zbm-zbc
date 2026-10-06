"""Deals (ADR 0015 decision 15; Andre: any influencer deal over $5,000 total goes to him): exact Decimal money, the
three aggregates (the deal, the influencer's open deals, the influencer's deals in the campaign), Andre's approval by
hash, material-connection records."""

from __future__ import annotations

import pytest

from helpers import Harness, rid, wired_ports


@pytest.mark.parametrize("fee,product,auto", [("5000.00", "0.00", True), ("4999.99", "0.01", True),
                                              ("5000.01", "0.00", False), ("4000.00", "1000.01", False),
                                              ("0.00", "5000.01", False), ("0.01", "0.00", True)])
def test_the_5000_line_is_exact(w, fee, product, auto):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    d = w.ok(w.deal(inf, c, b, fee=fee, product=product), 201)
    assert (d["status"] == "approved") is auto
    assert d["needs_andre"] == ([] if auto else ["DEAL_OVER_LIMIT", "INFLUENCER_TOTAL_OVER_LIMIT",
                                                 "CAMPAIGN_TOTAL_OVER_LIMIT"])


def test_a_zero_deal_is_refused(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    w.code(w.deal(inf, c, b, fee="0.00"), 422, "MONEY_INVALID")


def test_a_split_deal_in_one_campaign_still_reaches_andre(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    first = w.ok(w.deal(inf, c, b, fee="3000.00"), 201)
    second = w.ok(w.deal(inf, c, b, fee="2500.00"), 201)
    assert first["status"] == "approved"
    assert second["status"] == "pending_andre"
    assert second["needs_andre"] == ["INFLUENCER_TOTAL_OVER_LIMIT", "CAMPAIGN_TOTAL_OVER_LIMIT"]


def test_a_split_across_campaigns_and_brands_is_caught_by_the_person_total(w):
    inf = w.creator()
    c1, c2 = w.campaign(), w.campaign(brand="zbc")
    b1, b2 = w.brief(c1), w.brief(c2)
    w.ok(w.deal(inf, c1, b1, fee="4000.00"), 201)
    d2 = w.ok(w.deal(inf, c2, b2, fee="1500.00"), 201)
    assert d2["needs_andre"] == ["INFLUENCER_TOTAL_OVER_LIMIT"]


def test_pending_deals_count_toward_the_person_total(w):
    inf = w.creator()
    c1, c2 = w.campaign(), w.campaign()
    b1, b2 = w.brief(c1), w.brief(c2)
    big = w.ok(w.deal(inf, c1, b1, fee="6000.00"), 201)
    assert big["status"] == "pending_andre"
    small = w.ok(w.deal(inf, c2, b2, fee="10.00"), 201)
    assert small["needs_andre"] == ["INFLUENCER_TOTAL_OVER_LIMIT"]


def test_completed_deals_count_for_life(w):
    inf, d, content = w.paid_ready(fee="4000.00")
    w.ok(w.payout(d, "4000.00", [content["content_id"]]), 201)
    assert w.svc.deals[d["deal_id"]]["status"] == "completed"
    c = w.svc.campaigns[d["campaign_id"]]
    b = w.svc.briefs[d["brief_id"]]
    again = w.ok(w.deal(inf, c, b, fee="1500.00"), 201)
    assert again["needs_andre"] == ["INFLUENCER_TOTAL_OVER_LIMIT", "CAMPAIGN_TOTAL_OVER_LIMIT"]
    other = w.campaign()
    ob = w.brief(other)
    assert w.ok(w.deal(inf, other, ob, fee="900.00"), 201)["needs_andre"] == ["INFLUENCER_TOTAL_OVER_LIMIT"]


def test_cancelled_and_rejected_deals_do_not_count(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    a = w.ok(w.deal(inf, c, b, fee="4000.00"), 201)
    w.ok(w.post(f"/deals/{a['deal_id']}/cancel", {"request_id": rid()}, caller="influencer_agent"))
    big = w.ok(w.deal(inf, c, b, fee="5000.01"), 201)
    w.ok(w.post(f"/deals/{big['deal_id']}/reject", {"request_id": rid()}, andre=True))
    assert w.ok(w.deal(inf, c, b, fee="5000.00"), 201)["status"] == "approved"


def test_a_lower_configured_limit(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_AUTO_APPROVE_MAX="1000.00")
    inf = h.creator()
    c = h.campaign()
    b = h.brief(c)
    assert h.ok(h.deal(inf, c, b, fee="1000.01"), 201)["status"] == "pending_andre"


def test_andre_approves_the_exact_deal(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    d = w.ok(w.deal(inf, c, b, fee="7500.00"), 201)
    assert not w.svc.material
    url = f"/deals/{d['deal_id']}/approve"
    w.code(w.post(url, {"request_id": rid(), "content_sha256": d["content_sha256"]}, caller="influencer_agent",
                  andre=True), 403, "CALLER_NOT_ALLOWED")
    w.code(w.post(url, {"request_id": rid(), "content_sha256": d["content_sha256"]}), 403,
           "ANDRE_APPROVAL_REQUIRED")
    w.code(w.post(url, {"request_id": rid(), "content_sha256": "a" * 64}, andre=True), 409, "CONTENT_HASH_MISMATCH")
    w.svc.deals[d["deal_id"]]["fee"] = "1.00"                         # tampered after it was recorded
    w.code(w.post(url, {"request_id": rid(), "content_sha256": d["content_sha256"]}, andre=True), 409,
           "CONTENT_HASH_MISMATCH")
    w.svc.deals[d["deal_id"]]["fee"] = "7500.00"
    out = w.ok(w.post(url, {"request_id": rid(), "content_sha256": d["content_sha256"]}, andre=True))
    assert out["status"] == "approved" and out["approved_by"] == "andre"
    mc = list(w.svc.material.values())
    assert len(mc) == 1 and mc[0]["fee"] == "7500.00" and mc[0]["disclosure"] == "#ad"
    w.code(w.post(url, {"request_id": rid(), "content_sha256": d["content_sha256"]}, andre=True), 409,
           "DEAL_NOT_PENDING")


def test_a_deal_needs_an_approved_brief_of_its_campaign(w):
    inf = w.creator()
    c1, c2 = w.campaign(), w.campaign()
    b1 = w.brief(c1, approve=False)
    w.code(w.deal(inf, c1, b1), 409, "BRIEF_NOT_APPROVED")
    b2 = w.brief(c2)
    w.code(w.deal(inf, c1, b2), 422, "BRIEF_NOT_IN_CAMPAIGN")


def test_a_closed_campaign_takes_no_deal(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    w.ok(w.post(f"/campaigns/{c['campaign_id']}/close", {"request_id": rid()}, caller="influencer_agent"))
    w.code(w.deal(inf, c, b), 409, "CAMPAIGN_CLOSED")


def test_a_blocked_influencer_takes_no_deal_and_its_pending_deal_cannot_be_approved(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    d = w.ok(w.deal(inf, c, b, fee="9000.00"), 201)
    w.code(w.application(adult=False), 422, "MINOR_REFUSED")
    w.code(w.deal(inf, c, b), 403, "INFLUENCER_BLOCKED")
    w.code(w.post(f"/deals/{d['deal_id']}/approve", {"request_id": rid(), "content_sha256": d["content_sha256"]},
                  andre=True), 403, "INFLUENCER_BLOCKED")


def test_material_connection_for_free_product(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c, disclosure="Paid partnership with Z Best Media")
    d = w.ok(w.deal(inf, c, b, fee="100.00", product="250.00"), 201)
    mc = w.ok(w.get("/material-connections", caller="compliance_38"))
    assert mc[0]["kinds"] == ["payment", "free_product"] and mc[0]["deal_id"] == d["deal_id"]
    assert mc[0]["disclosure"] == "Paid partnership with Z Best Media"
    w.code(w.get("/material-connections", caller="influencer_agent"), 403, "CALLER_NOT_ALLOWED")


def test_campaigns_and_co_marketing_partners(h):
    h.code(h.post("/campaigns", {"request_id": rid(), "brand": "zbm", "name": "Co", "kind": "co_marketing"},
                  caller="influencer_agent"), 422, "PARTNER_REQUIRED")
    h.code(h.post("/campaigns", {"request_id": rid(), "brand": "zbm", "name": "I", "kind": "influencer",
                                 "partner": {"name": "Acme", "ref": "p-1"}}, caller="influencer_agent"), 422,
           "PARTNER_NOT_ALLOWED")
    c = h.campaign(kind="co_marketing", partner={"name": "Acme Agency", "ref": "p-1"})
    assert c["kind"] == "co_marketing" and c["partner"]["ref"] == "p-1"
    assert h.ledger.of_type("campaign_created")[-1]["_payload"]["partner_ref"] == "p-1"
    r = h.post("/campaigns", {"request_id": rid(), "brand": "zbm", "name": "C", "kind": "referral"},
               caller="influencer_agent")
    assert r.status_code == 422                     # referral / alliance commissions are department 12's
