"""Paying influencers (ADR 0015 decision 18): a tax REFERENCE only, verified at Finance before paid, payouts through the
Finance (31) port only (never Stripe), at most the deal's cash fee, for live content Andre approved. Nothing is ever paid
here."""

from __future__ import annotations

import pytest

from helpers import FakeFinance, Harness, rid, wired_ports


def test_tax_profile_holds_a_reference_and_its_hash_only(w):
    inf = w.creator()
    conf = w.ok(w.tax(inf), 201)
    assert conf["status"] == "pending" and "payload" not in conf and "email" not in conf
    assert w.svc.influencers[inf["influencer_id"]]["tax"] is None             # nothing before the confirmation
    w.ok(w.confirm(conf["conf_id"]))
    out = w.ok(w.get(f"/influencers/{inf['influencer_id']}"))
    assert out["tax_profile"]["tax_form"] == "w9" and "tax_ref" not in out["tax_profile"]
    ev = w.ledger.of_type("tax_profile_recorded")[0]["_payload"]
    assert "tax_ref" not in ev and len(ev["tax_ref_sha256"]) == 64


@pytest.mark.parametrize("ref", ["123456789", "ssn:123456789", "stripe:short", "http://x", "stripe:acct 1",
                                 "stripe:" + "a" * 121, "vault:ssn123456789", "fin:abcdefgh", "stripe:acct_ATTACKER1",
                                 "stripe:acct_123456789abcdefgh", "vault:12345678-9abc-def0-1234-567890abcdef",
                                 "vault:abcdefghijklmnopqrstuvwxy", "vault:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                 "vault:new-destination-1"])
def test_tax_ref_must_be_an_opaque_reference(w, ref):
    inf = w.creator()
    assert w.tax(inf, ref=ref).status_code == 422
    assert w.svc.influencers[inf["influencer_id"]]["tax"] is None


@pytest.mark.parametrize("form,country,legal,ok", [("w9", "US", "individual", True), ("w9", "US", "entity", True),
                                                    ("w9", "CA", "individual", False),
                                                    ("w8ben", "US", "individual", False),
                                                    ("w8ben", "GB", "individual", True),
                                                    ("w8ben", "GB", "entity", False),
                                                    ("w8bene", "GB", "entity", True),
                                                    ("w8bene", "GB", "individual", False)])
def test_tax_form_matches_country_and_legal_form(w, form, country, legal, ok):
    inf = w.creator()
    r = w.tax(inf, form=form, country=country, legal=legal)
    assert (r.status_code == 201) is ok, r.text


def test_only_the_creator_portal_or_console_records_a_tax_reference(w):
    inf = w.creator()
    r = w.post("/tax-profiles", {"request_id": rid(), "influencer_id": inf["influencer_id"], "tax_form": "w9",
                                 "tax_ref": "stripe:acct_TESTabcdefghijklmnop", "legal_form": "individual", "country": "US"},
               caller="influencer_agent")
    w.code(r, 403, "CALLER_NOT_ALLOWED")


def test_finance_stand_in_nothing_is_verified_or_paid(h):
    inf = h.creator()
    h.tax_confirmed(inf)
    n = len(h.svc.log)
    h.code(h.verify(inf), 503, "FINANCE_UNAVAILABLE")
    assert len(h.svc.log) == n
    assert h.ok(h.get(f"/influencers/{inf['influencer_id']}"))["payee"]["status"] == "none"


def test_verification_needs_a_tax_reference(w):
    inf = w.creator()
    w.code(w.verify(inf), 409, "TAX_PROFILE_REQUIRED")


def test_a_new_tax_reference_resets_verification(w):
    inf, d, content = w.paid_ready()
    conf = w.tax_confirmed(inf, ref="stripe:acct_TESTotherreference")
    assert conf["status"] == "pending_andre"                       # a verified payee: Andre approves the change
    w.ok(w.post(f"/confirmations/{conf['conf_id']}/approve",
                {"request_id": rid(), "content_sha256": conf["content_sha256"]}, andre=True))
    assert w.svc.influencers[inf["influencer_id"]]["payee"]["status"] == "none"
    w.code(w.payout(d, "100.00", [content["content_id"]]), 403, "PAYEE_NOT_VERIFIED")


def test_pending_verification_is_not_verified(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(finance=FakeFinance(status="pending")))
    inf, c, b, d = h.setup_deal()
    h.contract(d)
    content = h.ok(h.content(d), 201)
    h.approve_content(content)
    h.ok(h.live(content))
    h.tax_confirmed(inf)
    assert h.ok(h.verify(inf))["payee"]["status"] == "pending"
    h.code(h.payout(d, "100.00", [content["content_id"]]), 403, "PAYEE_NOT_VERIFIED")


def test_finance_is_asked_again_at_payout_time(w):
    inf, d, content = w.paid_ready()
    w.ports.finance.status = "refused"                     # e.g. the TIN match failed since
    w.code(w.payout(d, "100.00", [content["content_id"]]), 403, "PAYEE_NOT_VERIFIED")
    w.ports.finance.status = "unavailable"
    w.code(w.payout(d, "100.00", [content["content_id"]]), 503, "FINANCE_UNAVAILABLE")
    assert not w.svc.payouts and not w.ports.finance.payouts


def test_payout_rules(w):
    inf, d, content = w.paid_ready(fee="1000.00")
    w.code(w.payout(d, "1000.01", [content["content_id"]]), 409, "PAYOUT_OVER_DEAL")
    w.code(w.payout(d, "10.00", [content["content_id"], content["content_id"]]), 422, "CONTENT_REPEATED")
    w.code(w.payout(d, "10.00", ["if-cnt-" + "0" * 40]), 422, "CONTENT_NOT_IN_DEAL")
    w.code(w.payout(d, "0.00", [content["content_id"]]), 422, "MONEY_INVALID")
    p = w.ok(w.payout(d, "600.00", [content["content_id"]]), 201)
    assert p["status"] == "submitted"
    w.code(w.payout(d, "100.00", [content["content_id"]]), 409, "CONTENT_ALREADY_PAID")
    w.code(w.payout(d, "100.00", [content["content_id"]], caller="hub"), 403, "CALLER_NOT_ALLOWED")


def test_payout_needs_live_content_and_a_contract(w):
    inf, c, b, d = w.setup_deal()
    content = w.ok(w.content(d), 201)
    w.tax_confirmed(inf)
    w.ok(w.verify(inf))
    w.code(w.payout(d, "100.00", [content["content_id"]]), 409, "CONTRACT_NOT_IN_FORCE")
    w.contract(d)
    w.code(w.payout(d, "100.00", [content["content_id"]]), 409, "CONTENT_NOT_LIVE")
    w.approve_content(content)
    w.code(w.payout(d, "100.00", [content["content_id"]]), 409, "CONTENT_NOT_LIVE")


def test_a_payout_finance_cannot_take_now_is_kept_and_retried(tmp_path):
    fin = FakeFinance(payout="unavailable")
    h = Harness(tmp_path, ports=wired_ports(finance=fin))
    inf, d, content = h.paid_ready()
    p = h.ok(h.payout(d, "400.00", [content["content_id"]]), 201)
    assert p["status"] == "pending_finance"
    assert h.ledger.of_type("payout_requested")[0]["_payload"]["amount"] == "400.00"
    out = h.ok(h.job("payout-retry"))
    assert out["pending_finance"] == 1
    fin.payout = "accepted"
    out = h.ok(h.job("payout-retry"))
    assert out["submitted"] == 1 and h.svc.payouts[p["payout_id"]]["status"] == "submitted"
    assert [x[0] for x in fin.payouts] == [p["payout_id"]] * 3          # the same payout id each time (idempotent)


def test_a_refused_payout_releases_its_amount(tmp_path):
    fin = FakeFinance(payout="refused")
    h = Harness(tmp_path, ports=wired_ports(finance=fin))
    inf, d, content = h.paid_ready(fee="500.00")
    p = h.ok(h.payout(d, "500.00", [content["content_id"]]), 201)
    assert p["status"] == "refused_by_finance" and h.svc.deals[d["deal_id"]]["requested"] == "0.00"
    fin.payout = "accepted"
    assert h.ok(h.payout(d, "500.00", [content["content_id"]]), 201)["status"] == "submitted"


def test_a_ledger_outage_records_no_payout_and_calls_no_finance(w):
    inf, d, content = w.paid_ready()
    w.ledger.fail_types.add("payout_requested")
    w.code(w.payout(d, "100.00", [content["content_id"]]), 503, "LEDGER_UNAVAILABLE")
    assert not w.svc.payouts and not w.ports.finance.payouts


def test_a_blocked_influencer_is_never_paid(w):
    inf, d, content = w.paid_ready()
    w.code(w.application(adult=False), 422, "MINOR_REFUSED")
    w.code(w.payout(d, "100.00", [content["content_id"]]), 403, "INFLUENCER_BLOCKED")
    assert not w.ports.finance.payouts


def test_this_service_never_calls_stripe():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    text = "\n".join(p.read_text() for p in src.rglob("*.py"))
    assert "api.stripe.com" not in text and "import stripe" not in text


def test_a_waiting_payout_is_cancelled_when_the_payee_changes(tmp_path):
    fin = FakeFinance(payout="unavailable")
    h = Harness(tmp_path, ports=wired_ports(finance=fin))
    inf, d, content = h.paid_ready(fee="500.00")
    p = h.ok(h.payout(d, "500.00", [content["content_id"]]), 201)
    assert p["status"] == "pending_finance"
    conf = h.tax_confirmed(inf, ref="vault:abcdefghijklmnopqrstuvwxyz")
    h.ok(h.post(f"/confirmations/{conf['conf_id']}/approve",
                {"request_id": rid(), "content_sha256": conf["content_sha256"]}, andre=True))
    fin.payout = "accepted"
    out = h.ok(h.job("payout-retry"))
    assert out["cancelled"] == 1 and h.svc.payouts[p["payout_id"]]["reason"] == "PAYEE_CHANGED"
    assert len(fin.payouts) == 1 and h.svc.deals[d["deal_id"]]["requested"] == "0.00"
