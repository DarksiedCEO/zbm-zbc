"""Invoice void and credit notes (ADR 0026; red team OPS-H9; MSA 4.10 / 4.12(g); refund policy 5.3 and 8.4).

Driven through the real API and, for the Stripe side, the REAL Stripe adapter against the simulated Stripe, with
signed webhook events. Every scenario ends with the books checked: the journal balances, A/R and 2050 (refunds
payable) per client are what the story says, and 1060 equals what the simulated Stripe holds.
"""

from __future__ import annotations

import pytest

from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i01_journal as J
from test_media_billing import LEGAL, buy_body, create, issue
from test_stripe_incoming import bal, checkout, keys, make, rr_invoice, sent  # noqa: F401  (keys: a fixture)

CLIENT = "zbm-client-7"


@pytest.fixture
def st(keys):  # noqa: F811
    return make(keys, FIN_CARD_PREPAYMENTS="1")


def ach_invoice(hr, price="1000.00", client=CLIENT, code="strategy_services"):
    body = {"request_id": rid(), "entity": "zbm", "client_id": client, "kind": "service",
            "lines": [{"line_code": code, "quantity": 1, "unit_price": price, "description": "SEO audit"}],
            "payment_methods": ["ach"], "legal_ref": LEGAL}
    return issue(hr, hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"])


def void(hr, inv, **over):
    body = {"request_id": rid(), "content_sha256": inv["content_sha256"], "reason_code": "sow_ended_unpaid", **over}
    return hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/void", body, andre=ANDRE_TOKEN)


def credit(hr, inv, amount, reason="sow_ended_unpaid", **kw):
    body = {"request_id": rid(), "content_sha256": inv["content_sha256"], "amount": amount, "reason_code": reason}
    return hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/credit-notes", body, andre=ANDRE_TOKEN, **kw)


def ar(hr, client=CLIENT):
    return hr.bal("1100", f"client:{client}", "zbm")


def owed_back(hr, client=CLIENT):
    return hr.bal("2050", f"client:{client}", "zbm")


def balanced(hr):
    for e in hr.svc.entries:
        d, c = J.totals(e)
        assert d == c and d > 0, e["memo_code"]
    tb = hr.ok(hr.get("/fin/v1/journal/zbm/trial-balance"))
    assert tb["totals"]["debit"] == tb["totals"]["credit"] if "totals" in tb else True


def stripe_paid(hr, price="1200.00"):
    inv = rr_invoice(hr, price)
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method="us_bank_account")
    sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    inv = hr.svc.db["invoices"][inv["invoice_id"]]
    assert inv["status"] == "paid"
    return inv, pi


def dashboard_refund(hr, pi, cents=None):
    hr.sim.refund(pi, cents)
    ch = hr.sim.pis[pi]["latest_charge"]
    return sent(hr, "charge.refunded", {"id": ch, "object": "charge", "payment_intent": pi})


def sim_1060(hr):
    from decimal import Decimal
    return (Decimal(hr.sim.balance_cents()) / 100).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------------------------- void

def test_void_cancels_an_issued_unpaid_invoice_in_full_and_balances():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    assert ar(hr) == J.M.D("1000.00")
    out = hr.ok(void(hr, inv))
    assert out["invoice"]["status"] == "void" and out["invoice"]["void_reason"] == "sow_ended_unpaid"
    e = hr.svc.entries_by_id[out["entry_id"]]
    assert e["memo_code"] == "F11v"
    assert {(l["account"], l["debit"], l["credit"]) for l in e["lines"]} == {("4110", "1000.00", "0.00"),
                                                                             ("1100", "0.00", "1000.00")}
    assert ar(hr) == 0 and hr.bal("4110", None, "zbm") == 0
    balanced(hr)
    assert hr.ledger.of_type("invoice_voided")
    # the client is told (queued with the booking, sent later by the client-mail job)
    q = [m for m in hr.svc.db["mail_outbox"].values() if m["kind"] == "invoice_cancelled"]
    assert len(q) == 1 and q[0]["client_id"] == CLIENT and q[0]["status"] == "pending"
    # nothing can be paid on it any more, and it is not voided twice
    assert void(hr, out["invoice"]).status_code == 409


def test_void_is_andre_only_and_refuses_what_it_must_not_touch():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    for kw in ({"caller": "onboarding"}, {"caller": "scheduler"}, {"andre": "wrong-token-0123456789abcdefghijk"}):
        body = {"request_id": rid(), "content_sha256": inv["content_sha256"], "reason_code": "other"}
        assert hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/void", body, **kw).status_code in (401, 403)
    assert void(hr, {**inv, "content_sha256": "0" * 64}).status_code == 409           # stale read
    draft = hr.ok(hr.post("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": CLIENT,
                                               "kind": "service", "payment_methods": ["ach"], "legal_ref": LEGAL,
                                               "lines": [{"line_code": "strategy_services", "quantity": 1,
                                                          "unit_price": "10.00"}]}, caller="onboarding"), 201)
    assert void(hr, draft["invoice"]).status_code == 409                              # a draft is rejected instead
    paid = ach_invoice(hr, "2000.00")
    hr.receive("zbm", "1010", "2000.00", paid["invoice_id"])
    r = void(hr, paid)
    assert r.status_code == 409 and "credit note" in r.text                          # paid: a credit note, not a void
    buy = create(hr, **{k: v for k, v in buy_body().items() if k != "request_id"})
    media = hr.svc.db["invoices"][buy["media_buy"]["invoice_id"]]
    assert void(hr, media).status_code == 409                                         # media: cancel the buy
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"


def test_void_closes_the_open_stripe_page_first_and_refuses_when_stripe_cannot_confirm(st):
    inv = rr_invoice(st, "800.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.fail_next = [500]
    r = void(st, inv)
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:stripe" in r.text
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    assert st.sim.sessions[cs["session_id"]]["status"] == "open"
    out = st.ok(void(st, inv))
    assert out["invoice"]["status"] == "void"
    assert st.sim.sessions[cs["session_id"]]["status"] == "expired"
    assert st.svc.db["stripe_sessions"][cs["session_id"]]["status"] == "expired"


def test_void_refuses_when_the_client_already_paid_on_the_page(st):
    inv = rr_invoice(st, "800.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.pay(cs["session_id"], settle=False)                                          # ACH still processing
    r = void(st, inv)
    assert r.status_code == 409 and "paid" in r.text
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"


# --------------------------------------------------------------------------------------------- credit notes, unpaid

def test_policy_5_3_an_unpaid_invoice_is_reduced_by_credit_note_and_the_client_pays_the_rest(st):
    """Refund policy 5.3 / MSA 4.12(g): the SOW ends while the invoice is unpaid; within 10 business days ZBM reduces
    it to the work done. The Stripe page then asks for the reduced amount and that payment settles the invoice."""
    inv = rr_invoice(st, "1000.00")
    old = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    out = st.ok(credit(st, inv, "300.00"), 201)
    cn = out["credit_note"]
    assert cn["status"] == "applied" and cn["to_ar"] == "300.00" and cn["to_refund"] == "0.00"
    assert cn["number"].startswith("CN-2026-") and cn["refund_due_by"] is None
    assert out["invoice"]["status"] == "issued" and out["invoice"]["credited"] == "300.00"
    assert ar(st) == J.M.D("700.00")
    assert st.sim.sessions[old["session_id"]]["status"] == "expired"                  # the 1000.00 page is gone
    page = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    assert page["total"] == "700.00"
    assert [r for r in st.sim.requests if r["method"] == "POST" and r["path"] == "/v1/checkout/sessions"][-1][
        "form"]["line_items[0][price_data][unit_amount]"] == "70000"
    pi = st.sim.pay(page["session_id"])
    assert sent(st, "checkout.session.completed", {"id": page["session_id"], "object": "checkout.session"})[
        "status"] == "matched"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid" and ar(st) == 0
    assert st.bal("1060", None, "zbm") == sim_1060(st)
    balanced(st)
    assert pi


def test_a_full_credit_on_an_unpaid_invoice_cancels_it():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    out = hr.ok(credit(hr, inv, "1000.00", reason="cancelled_before_work"), 201)
    assert out["invoice"]["status"] == "void" and ar(hr) == 0
    assert credit(hr, out["invoice"], "0.01").status_code == 409                      # nothing left to credit
    balanced(hr)


def test_a_bank_payment_of_the_reduced_amount_matches_and_the_old_total_does_not():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    hr.ok(credit(hr, inv, "250.00"), 201)
    hr.receive("zbm", "1010", "1000.00", inv["invoice_id"])                             # the old total: not matched
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    hr.receive("zbm", "1010", "750.00", inv["invoice_id"])
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid" and ar(hr) == 0


def test_credit_amount_rules():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    assert credit(hr, inv, "1000.01").status_code == 409
    assert credit(hr, inv, "0.00").status_code == 422
    assert credit(hr, inv, "12.345").status_code == 422
    assert credit(hr, {**inv, "content_sha256": "1" * 64}, "10.00").status_code == 409
    assert credit(hr, inv, "10.00", reason="because").status_code == 422


# --------------------------------------------------------------------------------------------- credit notes, paid

def test_credit_note_on_a_paid_invoice_owes_a_refund_that_the_dashboard_refund_pays(st):
    inv, pi = stripe_paid(st, "1200.00")
    out = st.ok(credit(st, inv, "400.00", reason="nonconforming"), 201)
    cn = out["credit_note"]
    assert cn["to_ar"] == "0.00" and cn["to_refund"] == "400.00" and cn["status"] == "refund_pending"
    assert cn["refund_due_by"] == "2026-10-16"                     # Fri Oct 2 + 10 business days (MSA 4.10)
    e = st.svc.entries_by_id[cn["entry_id"]]
    assert e["memo_code"] == "F11c" and {(l["account"], l["debit"], l["credit"]) for l in e["lines"]} == {
        ("4110", "400.00", "0.00"), ("2050", "0.00", "400.00")}
    assert owed_back(st) == J.M.D("400.00") and ar(st) == 0
    brk = st.svc.db["breaks"][cn["break_id"]]
    assert brk["leg"] == "credit_note_refund_due" and brk["status"] == "open" and brk["due_by"] == "2026-10-16"
    assert out["invoice"]["status"] == "paid"
    # Andre refunds 400.00 in the Stripe Dashboard; charge.refunded links it to the credit note
    r = dashboard_refund(st, pi, 40000)
    assert r["status"] == "refund_booked"
    cn = st.svc.db["credit_notes"][cn["credit_note_id"]]
    assert cn["status"] == "refunded" and cn["refund_remaining"] == "0.00"
    assert cn["refunds"][0]["booked"] == "against_credit_note" and cn["refunds"][0]["amount"] == "400.00"
    f7r = st.svc.entries_by_id[cn["refunds"][0]["entry_id"]]
    assert {(l["account"], l["debit"], l["credit"]) for l in f7r["lines"]} == {("2050", "400.00", "0.00"),
                                                                               ("1060", "0.00", "400.00")}
    assert owed_back(st) == 0 and ar(st) == 0                       # nobody owes anybody
    assert st.svc.db["breaks"][brk["break_id"]]["status"] == "resolved"
    assert not [b for b in st.svc.db["breaks"].values() if b["leg"] == "stripe_refund"]   # an expected refund
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"
    assert st.bal("1060", None, "zbm") == sim_1060(st)
    assert [m["kind"] for m in st.svc.db["mail_outbox"].values()] == ["credit_note", "refund_sent"]
    balanced(st)


def test_full_credit_then_full_refund_leaves_the_invoice_credited_and_the_books_square(st):
    inv, pi = stripe_paid(st, "1200.00")
    out = st.ok(credit(st, inv, "1200.00", reason="audit_not_run"), 201)
    assert out["invoice"]["status"] == "credited"
    assert st.ok(st.get(f"/fin/v1/invoices/{inv['invoice_id']}/status", caller="seo_02"))["status"] == "credited"
    dashboard_refund(st, pi)
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "credited" and i["refunded"] == "1200.00" and i["refund_due"] == "0.00"
    assert ar(st) == 0 and owed_back(st) == 0
    assert st.bal("4110", None, "zbm") == 0
    assert st.bal("1060", None, "zbm") == sim_1060(st)
    balanced(st)


def test_a_dashboard_refund_first_is_absorbed_by_the_credit_note_issued_after_it(st):
    inv, pi = stripe_paid(st, "1200.00")
    dashboard_refund(st, pi)                       # refunded before any credit note: the client "owes it again"
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "issued" and ar(st) == J.M.D("1200.00")
    out = st.ok(credit(st, i, "1200.00", reason="cancelled_before_work"), 201)
    cn = out["credit_note"]
    assert cn["to_ar"] == "1200.00" and cn["to_refund"] == "0.00" and cn["status"] == "applied"
    assert cn["refunds"] and cn["refunds"][0]["booked"] == "before_credit_note"
    assert out["invoice"]["status"] == "credited" and ar(st) == 0 and owed_back(st) == 0
    balanced(st)


def test_partial_refund_then_partial_credit_note(st):
    inv, pi = stripe_paid(st, "1200.00")
    dashboard_refund(st, pi, 20000)
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and ar(st) == J.M.D("200.00")
    out = st.ok(credit(st, i, "500.00", reason="billing_dispute"), 201)
    cn = out["credit_note"]
    assert (cn["to_ar"], cn["to_refund"]) == ("200.00", "300.00")       # absorbs the refund, owes 300 more
    assert ar(st) == 0 and owed_back(st) == J.M.D("300.00")
    dashboard_refund(st, pi, 30000)
    assert st.svc.db["credit_notes"][cn["credit_note_id"]]["status"] == "refunded"
    assert ar(st) == 0 and owed_back(st) == 0
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"
    balanced(st)


def test_a_credit_cannot_promise_back_more_than_zbm_holds(st):
    inv, pi = stripe_paid(st, "1200.00")
    st.ok(credit(st, inv, "700.00"), 201)
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert credit(st, i, "500.01").status_code == 409
    assert credit(st, i, "500.00").status_code == 201


def test_a_refund_owing_credit_note_asks_compliance_and_refuses_sanctions_or_no_answer(st):
    inv, pi = stripe_paid(st, "1200.00")
    st.cmp.activations = {CLIENT: {"allowed": False, "sanctions": True, "ground": "match",
                                   "reasons": ("OFAC SDN match",)}}
    r = credit(st, inv, "100.00")
    assert r.status_code == 409 and "SANCTIONS" in r.text
    st.cmp.activations = None
    st.cmp.activation_available = False
    assert credit(st, inv, "100.00").status_code == 409
    assert not st.svc.db["credit_notes"] and owed_back(st) == 0


def test_an_unpaid_credit_needs_no_compliance_answer():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    hr.cmp.activation_available = False
    assert credit(hr, inv, "100.00").status_code == 201


def test_held_and_disputed_invoices_are_refused(st):
    inv, pi = stripe_paid(st, "1200.00")
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.created", {"id": du, "object": "dispute", "payment_intent": pi})
    r = credit(st, st.svc.db["invoices"][inv["invoice_id"]], "100.00")
    assert r.status_code == 409 and "dispute" in r.text


def test_credit_notes_are_listed_for_andre_only(st):
    inv, _pi = stripe_paid(st, "1200.00")
    cn = st.ok(credit(st, inv, "100.00"), 201)["credit_note"]
    view = st.ok(st.get(f"/fin/v1/invoices/{inv['invoice_id']}/credit-notes", caller=None, andre=ANDRE_TOKEN))
    assert [c["credit_note_id"] for c in view["credit_notes"]] == [cn["credit_note_id"]]
    assert view["credited"] == "100.00" and view["refund_due"] == "100.00"
    assert st.get(f"/fin/v1/invoices/{inv['invoice_id']}/credit-notes", caller="onboarding").status_code in (401, 403)
    assert st.ok(st.get(f"/fin/v1/credit-notes/{cn['credit_note_id']}", caller=None, andre=ANDRE_TOKEN))[
        "amount"] == "100.00"


def test_a_failed_refund_on_a_credited_invoice_is_never_booked_automatically(st):
    inv, pi = stripe_paid(st, "1200.00")
    cn = st.ok(credit(st, inv, "1200.00"), 201)["credit_note"]
    dashboard_refund(st, pi)
    ch = st.sim.charges[st.sim.pis[pi]["latest_charge"]]
    ch["amount_refunded"] = 0                                         # the ACH refund came back
    r = sent(st, "charge.refund.updated", {"id": "re_0001failed", "object": "refund", "payment_intent": pi})
    assert r["status"] == "refund_reversal_break"
    assert any(b["leg"] == "stripe_refund_reversed" for b in st.svc.db["breaks"].values())
    assert st.svc.db["credit_notes"][cn["credit_note_id"]]["status"] == "refunded"   # unchanged until Andre acts


def test_sales_tax_nexus_counts_invoices_net_of_credit_notes():
    hr = Harness().ready()
    a = ach_invoice(hr, "1000.00")
    hr.ok(credit(hr, a, "300.00"), 201)
    b = ach_invoice(hr, "500.00")
    hr.ok(void(hr, b))
    c = ach_invoice(hr, "400.00")
    hr.ok(credit(hr, c, "400.00"), 201)                            # fully credited: not a sale
    rows = {r["jurisdiction"]: r for r in hr.ok(hr.get("/fin/v1/readiness/sales-tax-nexus",
                                                       caller="onboarding"))["jurisdictions"]}
    ca = rows["US-CA"]["trailing_12_months"]
    assert ca["sales_usd"] == "700.00" and ca["transactions"] == 1
