"""Founder decision Oct 30 2026: the SEO Technical Audit may be paid by card as well as ACH, with no surcharge; USD only
and Adaptive Pricing off (RTM-03). And the ONE list of Stripe webhook events (red team OPS-C4): the code, the runbook,
the README and the registration helper agree, every event is one Finance handles, and a Dashboard refund
(``charge.refunded``) reaches the books."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import ANDRE_TOKEN, rid
from stripe_webhooks import WEBHOOK_EVENTS
from svc_stripe import event_kind
from test_media_billing import LEGAL, issue
from test_stripe_incoming import checkout, keys, make, sent  # noqa: F401  (keys: a fixture)

FIN = Path(__file__).resolve().parents[1]
REPO = FIN.parents[1]


@pytest.fixture
def st(keys):  # noqa: F811
    return make(keys, FIN_CARD_PREPAYMENTS="1")


def seo_invoice(hr, price="1500.00", code="seo_audit_services", methods=("ach", "card")):
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-seo-9", "kind": "service",
            "lines": [{"line_code": code, "quantity": 1, "unit_price": price, "description": "SEO Technical Audit"}],
            "payment_methods": list(methods), "legal_ref": LEGAL}
    r = hr.post("/fin/v1/invoices", body, caller="onboarding")
    return r if r.status_code != 201 else issue(hr, r.json()["invoice"])


def test_the_seo_audit_is_offered_card_and_ach_in_usd_with_adaptive_pricing_off_and_no_surcharge(st):
    inv = seo_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    assert cs["methods"] == ["us_bank_account", "card"] and cs["total"] == "1500.00"
    f = [r for r in st.sim.requests if r["method"] == "POST" and r["path"] == "/v1/checkout/sessions"][-1]["form"]
    assert f["allowed_payment_method_types[0]"] == "us_bank_account" and f["allowed_payment_method_types[1]"] == "card"
    assert f["line_items[0][price_data][currency]"] == "usd" and f["adaptive_pricing[enabled]"] == "false"
    assert f["line_items[0][price_data][unit_amount]"] == "150000"                    # the fee, nothing added
    assert not any(re.search(r"surcharge|fee|convenience", k + v, re.I) for k, v in f.items()
                   if k != "line_items[0][price_data][product_data][name]")
    # a card payment of exactly the fee settles it
    st.sim.pay(cs["session_id"], method="card")
    assert sent(st, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})[
        "status"] == "matched"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"


def test_other_strategy_work_stays_ach_and_cards_stay_capped_and_switchable(st, keys):  # noqa: F811
    r = seo_invoice(st, code="strategy_services")
    assert r.status_code == 422 and "CARD_NOT_ALLOWED" in r.text                       # card refused at draft
    inv = seo_invoice(st, code="strategy_services", methods=("ach",))
    assert st.ok(checkout(st, inv["invoice_id"]))["checkout"]["methods"] == ["us_bank_account"]
    assert seo_invoice(st, price="5000.01").status_code == 422                          # the founder M10 cap
    off = make(keys)                                                                   # FIN_CARD_PREPAYMENTS unset
    assert seo_invoice(off).status_code == 422


def test_the_seo_audit_line_reads_as_such_on_the_client_receipt(st):
    inv = seo_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.pay(cs["session_id"], method="card")
    sent(st, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    rec = [r for r in st.svc.db["client_receipts"].values() if r["invoice_id"] == inv["invoice_id"]][0]
    assert rec["what_you_bought"] == "SEO Technical Audit" and rec["method"] == "card (Stripe)"


# --------------------------------------------------------------------------------------------- the webhook event list

def _listed(text: str, anchor: str) -> list[str]:
    """The backticked event names of the sentence that starts at ``anchor`` (up to the first line ending in '.')."""
    lines = text[text.index(anchor):].splitlines()
    out = []
    for ln in lines:
        out += re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)`", ln)
        if ln.rstrip().endswith("."):
            break
    return out


def test_one_canonical_list_and_every_event_is_handled():
    assert len(WEBHOOK_EVENTS) == len(set(WEBHOOK_EVENTS))
    for ev in WEBHOOK_EVENTS:
        assert event_kind(ev) is not None, ev
    for must in ("charge.refunded", "charge.refund.updated", "charge.dispute.created", "charge.dispute.closed",
                 "checkout.session.completed", "checkout.session.expired", "payment_intent.succeeded"):
        assert must in WEBHOOK_EVENTS
    assert {e.rsplit(".", 1)[0] for e in WEBHOOK_EVENTS if e.startswith("charge.dispute.")} == {"charge.dispute"}


def test_the_runbook_the_readme_and_the_helper_show_exactly_the_canonical_list():
    runbook = (REPO / "docs" / "deploy" / "railway.md").read_text()
    readme = (FIN / "README.md").read_text()
    assert _listed(runbook, "WEBHOOK EVENTS (services/finance-py/src/stripe_webhooks.py") == list(WEBHOOK_EVENTS)
    assert _listed(readme, "Webhook events to subscribe") == list(WEBHOOK_EVENTS)
    out = subprocess.run([sys.executable, str(FIN / "devtools" / "stripe_webhook_events.py")], capture_output=True,
                         text=True, check=True, env={"PYTHONDONTWRITEBYTECODE": "1"}).stdout.split()
    assert out == list(WEBHOOK_EVENTS)


def test_a_dashboard_refund_reaches_the_books_through_charge_refunded(st):
    inv = seo_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"], method="us_bank_account")
    sent(st, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    st.sim.refund(pi)
    ch = st.sim.pis[pi]["latest_charge"]
    assert sent(st, "charge.refunded", {"id": ch, "object": "charge", "payment_intent": pi})["status"] == \
        "refund_booked"
    assert [e["memo_code"] for e in st.svc.entries if e["entity"] == "zbm"][-1] == "F7r"
    assert st.ok(st.get(f"/fin/v1/invoices/{inv['invoice_id']}", caller=None, andre=ANDRE_TOKEN))["refunded"] == \
        "1500.00"
