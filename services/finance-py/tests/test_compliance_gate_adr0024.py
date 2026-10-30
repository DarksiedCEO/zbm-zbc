"""ADR 0024 (founder decisions, Oct 30, 2026): Compliance (38) is a hard gate before Finance approves or issues an
invoice, reports billing readiness or makes a Stripe page, and Finance's thin client reads the activation status,
the jurisdiction resolver and the sanctions status for real (the hard-coded "unavailable" answers are gone). The
per-jurisdiction sales report flags any state/territory at 75% of its economic-nexus threshold.

The HTTP answers below have compliance-py's exact shapes (``activation_status``, ``resolve``, ``sanctions_status``,
``register_row`` in compliance-py ``src/service.py``); compliance-py's own suite tests the routes themselves.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from clients import HttpCompliance
from helpers import ANDRE_TOKEN, Harness, rid
from ports import ClientActivation, JurisdictionAnswer, RegisterParameters, SanctionsAnswer
from test_legal_client import ACC, SHA
from test_media_billing import issue

NEXUS = {"alert_ratio": "0.75", "periods": ["trailing_12_months", "calendar_year"],
         "default": {"sales_usd": "100000.00", "transactions": None},
         "overrides": {"US-CA": {"sales_usd": "500000.00", "transactions": None, "source": "https://cdtfa"},
                       "US-HI": {"sales_usd": "100000.00", "transactions": 200, "source": "https://k"},
                       "US-NV": {"sales_usd": "100000.00", "transactions": 200, "source": "https://k"},
                       "PR": {"sales_usd": "100000.00", "transactions": 200, "source": "https://k"}},
         "default_source": "https://k",
         "check_before_first_client": {c: "local gross receipts/business tax - check before first client"
                                       for c in ("GU", "VI", "AS", "MP")}}


def draft(hr, client="zbm-client-1", amount="100.00"):
    body = {"request_id": rid(), "entity": "zbm", "client_id": client, "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": amount}],
            "payment_methods": ["ach"], "legal_ref": {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA,
                                                      "acceptance_id": ACC}}
    return hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]


def approve(hr, inv):
    return hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                   {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"},
                   andre=ANDRE_TOKEN)


# --- invoice approval / issuance --------------------------------------------------------------------------------------

def test_an_invoice_is_issued_only_with_a_current_allow_and_records_the_jurisdiction():
    hr = Harness().ready()
    inv = issue(hr, draft(hr))
    assert inv["status"] == "issued" and inv["client_jurisdiction"] == "US-CA"
    assert inv["compliance_ruling_id"] == "cmp-rul-zbm-client-1"
    assert ("client_activation", "zbm-client-1") in hr.f["compliance"].calls


def test_compliance_down_refuses_the_invoice():
    hr = Harness().ready()
    hr.f["compliance"].activation_available = False
    r = approve(hr, draft(hr))
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:compliance_38" in r.text
    assert hr.get(f"/fin/v1/invoices/{draft(hr)['invoice_id']}", caller="scheduler").json()["status"] == "draft"


def test_a_refused_or_lapsed_activation_refuses_the_invoice():
    hr = Harness().ready()
    hr.f["compliance"].activations = {"zbm-client-fr": {"allowed": False, "jurisdiction": None,
                                                        "reasons": ["the latest activation ruling refused this client"]}}
    r = approve(hr, draft(hr, "zbm-client-fr"))
    assert r.status_code == 409 and "COMPLIANCE_NOT_ACTIVATED" in r.text and "fin/FIN-08/" in r.text


def test_the_production_stand_in_refuses_every_invoice():
    hr = Harness().ready()
    from ports import NotWiredCompliance
    hr.svc.ports.compliance = NotWiredCompliance()
    r = approve(hr, draft(hr))
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:compliance_38" in r.text


def test_rejecting_a_draft_needs_no_activation():
    hr = Harness().ready()
    hr.f["compliance"].activation_available = False
    inv = draft(hr)
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "reject"}, andre=ANDRE_TOKEN)
    assert r.status_code == 200 and r.json()["invoice"]["status"] == "void"


def test_billing_readiness_needs_the_activation():
    hr = Harness().ready()
    hr.ok(hr.put("/fin/v1/clients/zbm-client-1/billing-profile",
                 {"request_id": rid(), "entity": "zbm", "payment_method": "ach",
                  "msa": {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA, "acceptance_id": ACC}},
                 andre=ANDRE_TOKEN))
    ready = hr.ok(hr.get("/fin/v1/clients/zbm-client-1/billing-readiness", caller="onboarding"))
    assert "COMPLIANCE_NOT_ACTIVATED" not in json.dumps(ready)
    hr.f["compliance"].activation_available = False
    r = hr.ok(hr.get("/fin/v1/clients/zbm-client-1/billing-readiness", caller="onboarding"))
    assert r["allowed"] is False and any(x["code"] == "DEPENDENCY_UNAVAILABLE:compliance_38" for x in r["reasons"])
    hr.f["compliance"].activation_available = True
    hr.f["compliance"].activations = {"zbm-client-1": {"allowed": False, "reasons": ["HR-14: re-screen required"]}}
    r = hr.ok(hr.get("/fin/v1/clients/zbm-client-1/billing-readiness", caller="onboarding"))
    assert r["allowed"] is False and any(x["code"] == "COMPLIANCE_NOT_ACTIVATED" for x in r["reasons"])


# --- the thin client against compliance-py's answers --------------------------------------------------------------------

def cmp_client(routes: dict, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((req.method, req.url.path, dict(req.url.params), req.content))
        for prefix, (status, body) in routes.items():
            if req.url.path.startswith(prefix):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": "no"})
    return HttpCompliance("http://compliance.railway.internal:8380", "s" * 40, "c" * 40,
                          transport=httpx.MockTransport(handler), timeout=2)


def activation_doc(**over):
    d = {"client_id": "zbm-client-1", "lane": "client", "decision": "ALLOW", "current": True,
         "ruling_id": "cmp-rul-abc", "evaluated_at": "2026-10-30T12:00:00Z", "register_version": 1,
         "jurisdiction": "US-CA", "valid_until": "2027-01-28T12:00:00Z", "reasons": [], "seed_pinned": True}
    d.update(over)
    return d


def test_client_activation_is_allowed_only_for_a_current_pinned_allow():
    path = "/compliance/v1/clients/zbm-client-1/activation"
    a = cmp_client({path: (200, activation_doc())}).client_activation("zbm-client-1")
    assert a == ClientActivation(True, True, "cmp-rul-abc", "US-CA", ())
    for over in ({"decision": "REFUSE", "current": False, "reasons": ["x"]}, {"current": False},
                 {"seed_pinned": False}, {"jurisdiction": None}):
        a = cmp_client({path: (200, activation_doc(**over))}).client_activation("zbm-client-1")
        assert a.available is True and a.allowed is False, over
    for over in ({"client_id": "someone-else"}, {"lane": "zbc_brand"}, {"jurisdiction": "CA-ON"}):
        assert cmp_client({path: (200, activation_doc(**over))}).client_activation("zbm-client-1").available is False
    for status in (401, 403, 404, 500, 503):
        assert cmp_client({path: (status, {})}).client_activation("zbm-client-1").available is False
    assert cmp_client({path: (200, activation_doc())}).client_activation("bad id").available is False


def test_the_jurisdiction_lookup_calls_the_resolver():
    seen = []
    ans = {"resolution_id": "jur-x", "register_version": 1, "note": None, "ledger_event_ids": [],
           "answers": [{"who": "person", "code": "US-PR", "class": "operate", "reason": "", "cites": ["HR-05"],
                        "missing": []}]}
    c = cmp_client({"/compliance/v1/jurisdictions/resolve": (200, ans)}, seen)
    assert c.jurisdiction("US", "US-PR") == JurisdictionAnswer(True, "operate")
    method, path, _params, content = seen[0]
    body = json.loads(content)
    assert method == "POST" and body["person"] == {"declared_country": "US", "declared_region": "US-PR",
                                                   "attested": True, "attestation_ref": "finance-payee-record"}
    ans["answers"][0].update(code="CA-ON", **{"class": "refuse"})
    assert cmp_client({"/compliance/v1/jurisdictions/resolve": (200, ans)}).jurisdiction("CA", "CA-ON").status == \
        "blocked"
    ans["answers"][0].update(**{"class": "missing"})
    assert cmp_client({"/compliance/v1/jurisdictions/resolve": (200, ans)}).jurisdiction("US", None).status == \
        "unknown"
    assert cmp_client({"/compliance/v1/jurisdictions/resolve": (503, {})}).jurisdiction("US", "US-CA").available \
        is False
    assert cmp_client({}).jurisdiction("usa", None).available is False


def test_the_sanctions_status_is_read():
    doc = {"subject_id": "payee-1", "role": "payee", "screen_id": "scr-1", "result": "clear", "list_version": "L1",
           "list_current": True, "screened_at": "2026-10-30T12:00:00Z", "fresh": True, "problem": None}
    seen = []
    a = cmp_client({"/compliance/v1/sanctions/status": (200, doc)}, seen).sanctions_status("payee-1", "payee")
    assert a == SanctionsAnswer(True, "scr-1", "clear", "L1", True, "2026-10-30T12:00:00Z", True)
    assert seen[0][2] == {"subject_id": "payee-1", "role": "payee"}
    for over in ({"subject_id": "x"}, {"result": "unavailable"}, {"result": "maybe"}):
        assert cmp_client({"/compliance/v1/sanctions/status": (200, {**doc, **over})}).sanctions_status(
            "payee-1", "payee").available is False
    none = {**doc, "screen_id": None, "result": None, "fresh": False}
    a = cmp_client({"/compliance/v1/sanctions/status": (200, none)}).sanctions_status("payee-1", "payee")
    assert a.available is True and a.fresh is False and a.screen_id is None


def test_row_parameters_are_read():
    row = {"register_version": 1, "history": [],
           "row": {"id": "US-SALESTAX-NEXUS", "parameters": NEXUS, "effective_status": "unverified",
                   "source_url": "https://k"}}
    a = cmp_client({"/compliance/v1/register/US-SALESTAX-NEXUS": (200, row)}).row_parameters("US-SALESTAX-NEXUS")
    assert a == RegisterParameters(True, "US-SALESTAX-NEXUS", "unverified", NEXUS, "https://k")
    assert cmp_client({}).row_parameters("US-SALESTAX-NEXUS").available is False


# --- Stripe checkout --------------------------------------------------------------------------------------------------

def test_no_stripe_page_without_a_current_allow():
    hr = Harness().ready()
    inv = issue(hr, draft(hr))
    hr.f["compliance"].activation_available = False
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:compliance_38" in r.text


# --- per-jurisdiction totals and the 75% flag ---------------------------------------------------------------------------

def nexus(hr):
    return hr.ok(hr.get("/fin/v1/readiness/sales-tax-nexus", caller="onboarding"))


def test_nexus_totals_per_jurisdiction_flag_at_75_percent():
    hr = Harness().ready()
    hr.f["compliance"].nexus_params = NEXUS
    hr.f["compliance"].activations = {"tx-1": {"allowed": True, "jurisdiction": "US-TX"},
                                      "ca-1": {"allowed": True, "jurisdiction": "US-CA"},
                                      "pr-1": {"allowed": True, "jurisdiction": "PR"}}
    issue(hr, draft(hr, "tx-1", "40000.00"))
    issue(hr, draft(hr, "tx-1", "34999.99"))
    r = nexus(hr)
    tx = next(j for j in r["jurisdictions"] if j["jurisdiction"] == "US-TX")
    assert tx["trailing_12_months"]["sales_usd"] == "74999.99" and tx["flagged"] is False
    issue(hr, draft(hr, "tx-1", "0.01"))
    issue(hr, draft(hr, "ca-1", "300000.00"))
    r = nexus(hr)
    tx = next(j for j in r["jurisdictions"] if j["jurisdiction"] == "US-TX")
    ca = next(j for j in r["jurisdictions"] if j["jurisdiction"] == "US-CA")
    assert tx["flagged"] is True and tx["trailing_12_months"]["sales_ratio"] == "0.7500"
    assert tx["calendar_year"]["transactions"] == 3
    assert ca["flagged"] is False and ca["threshold"]["sales_usd"] == "500000.00"   # California: $500,000
    assert r["flagged"] == ["US-TX"] and r["thresholds"]["available"] is True and r["memo_ref"] == \
        "MEMO-FIN-CQ-11-2026-10-30"


def test_nexus_counts_transactions_where_the_threshold_has_them():
    hr = Harness().ready()
    hr.f["compliance"].nexus_params = {**NEXUS, "overrides": {**NEXUS["overrides"],
                                                              "PR": {"sales_usd": "100000.00", "transactions": 4,
                                                                     "source": "https://k"}}}
    hr.f["compliance"].activations = {"pr-1": {"allowed": True, "jurisdiction": "PR"}}
    for _ in range(3):
        issue(hr, draft(hr, "pr-1", "10.00"))
    pr = next(j for j in nexus(hr)["jurisdictions"] if j["jurisdiction"] == "PR")
    assert pr["flagged"] is True and pr["trailing_12_months"]["transactions_ratio"] == "0.7500"


def test_nexus_without_thresholds_flags_every_jurisdiction_with_sales_and_unknowns():
    hr = Harness().ready()
    issue(hr, draft(hr))
    r = nexus(hr)
    assert r["thresholds"]["available"] is False and r["flagged"] == ["US-CA"]
    inv = hr.svc.db["invoices"]
    any_id = next(iter(inv))
    inv[any_id] = {**inv[any_id], "client_jurisdiction": None}       # an invoice issued before ADR 0024
    hr.f["compliance"].nexus_params = NEXUS
    r = nexus(hr)
    assert "unknown" in r["flagged"]


def test_nexus_trailing_window_and_calendar_year():
    hr = Harness().ready()
    hr.f["compliance"].nexus_params = NEXUS
    issue(hr, draft(hr, amount="80000.00"))           # California: 80,000 of 500,000 -> not flagged
    hr.clock.at = hr.clock.at + timedelta(days=400)
    r = nexus(hr)
    ca = next(j for j in r["jurisdictions"] if j["jurisdiction"] == "US-CA")
    assert ca["trailing_12_months"]["sales_usd"] == "0.00" and ca["calendar_year"]["sales_usd"] == "0.00"


def test_nexus_is_andre_or_the_onboarding_caller():
    hr = Harness().ready()
    assert hr.get("/fin/v1/readiness/sales-tax-nexus", caller="seo_02").status_code == 403
    assert hr.client.get("/fin/v1/readiness/sales-tax-nexus",
                         headers={**hr.headers(None), "X-Andre-Approval-Token": ANDRE_TOKEN}).status_code == 200


def test_territories_without_a_verified_threshold_get_a_check_not_a_number():
    """Founder update (12:51, Oct 30): GU, VI, AS and MP are served; no verified threshold source exists for them, so the
    report names the local gross receipts/business tax check and invents no number."""
    hr = Harness().ready()
    hr.f["compliance"].nexus_params = NEXUS
    hr.f["compliance"].activations = {"vi-1": {"allowed": True, "jurisdiction": "VI"},
                                      "as-1": {"allowed": True, "jurisdiction": "AS"}}
    issue(hr, draft(hr, "vi-1", "10.00"))
    issue(hr, draft(hr, "as-1", "10.00"))
    r = nexus(hr)
    assert sorted(r["thresholds"]["check_before_first_client"]) == ["AS", "GU", "MP", "VI"]
    for code in ("VI", "AS"):
        j = next(x for x in r["jurisdictions"] if x["jurisdiction"] == code)
        assert j["threshold"] is None and j["trailing_12_months"]["sales_ratio"] is None
        assert j["flagged"] is True and j["flag_reasons"] == ["local gross receipts/business tax - check before first client"]
    assert sorted(r["flagged"]) == ["AS", "VI"]


# --- AEGIS F-5: a page already open stops being payable when the activation lapses -----------------------------------

def _stripe_hr(tmp_path):
    """The Stripe-incoming harness (real adapter, simulated Stripe) of test_stripe_incoming, with card prepayments."""
    import os

    from test_stripe_incoming import make

    def secret(name, value):
        p = tmp_path / name
        p.write_text(value + "\n")
        os.chmod(p, 0o600)
        return str(p)
    from stripe_sim import TEST_KEY, TEST_WHSEC
    from test_stripe_incoming import CANCEL, SUCCESS
    return make({"FIN_STRIPE_INCOMING": "1", "FIN_STRIPE_SECRET_KEY_FILE": secret("sk", TEST_KEY),
                 "FIN_STRIPE_WEBHOOK_SECRET_FILE": secret("wh", TEST_WHSEC),
                 "FIN_STRIPE_SUCCESS_URL": SUCCESS, "FIN_STRIPE_CANCEL_URL": CANCEL}, FIN_CARD_PREPAYMENTS="1")


def test_an_ofac_hit_after_the_page_is_made_closes_it_and_no_replay_hands_it_back(tmp_path):
    from test_stripe_incoming import rr_invoice
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    other = rr_invoice(hr, "300.00")                       # a second open page of the same client
    req = {"request_id": rid()}
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", req, caller="onboarding"))["checkout"]
    cs2 = hr.ok(hr.post(f"/fin/v1/invoices/{other['invoice_id']}/stripe-checkout", {"request_id": rid()},
                        caller="onboarding"))["checkout"]
    assert hr.sim.sessions[cs["session_id"]]["status"] == "open"
    # Andre records an OFAC potential match: Compliance now answers REFUSE for this client
    hr.f["compliance"].activations = {"zbm-client-7": {"allowed": False, "jurisdiction": "US-CA",
                                                       "reasons": ["hold open: sanctions_match (hold-x)"]}}
    # the SAME request (idempotent replay) is gated first: refused, never the cached page
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", req, caller="onboarding")
    assert r.status_code == 409 and "COMPLIANCE_NOT_ACTIVATED" in r.text, r.text
    # every open page of the client is closed at Stripe and recorded expired here
    for sid in (cs["session_id"], cs2["session_id"]):
        assert hr.sim.sessions[sid]["status"] == "expired", sid
        assert hr.svc.db["stripe_sessions"][sid]["status"] == "expired", sid
    # a new request is refused too, and the client cannot pay on the closed page
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 409 and "COMPLIANCE_NOT_ACTIVATED" in r.text
    with pytest.raises(Exception):
        hr.sim.pay(cs["session_id"])


def test_compliance_unreachable_on_replay_refuses_without_closing_pages(tmp_path):
    """Unknown is not REFUSE: the page is not handed back, but nothing is closed on an outage."""
    from test_stripe_incoming import rr_invoice
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    req = {"request_id": rid()}
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", req, caller="onboarding"))["checkout"]
    hr.f["compliance"].activation_available = False
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", req, caller="onboarding")
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:compliance_38" in r.text
    assert hr.sim.sessions[cs["session_id"]]["status"] == "open"


def test_the_hourly_stripe_sessions_job_closes_pages_of_clients_no_longer_allowed(tmp_path):
    from test_stripe_incoming import rr_invoice
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    run = lambda: hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))
    assert run()["summary"]["closed"] == 0                       # still ALLOW: the page stays
    hr.f["compliance"].activation_available = False              # an outage is not a REFUSE: nothing is closed
    s = run()["summary"]
    assert s["closed"] == 0 and s["activation_unknown"] == 1
    assert hr.sim.sessions[cs["session_id"]]["status"] == "open"
    hr.f["compliance"].activation_available = True
    hr.f["compliance"].activations = {"zbm-client-7": {"allowed": False, "reasons": ["jurisdiction US-CA is refuse"]}}
    s = run()["summary"]
    assert s["closed"] == 1 and s["refused_clients"] == 1, s
    assert hr.sim.sessions[cs["session_id"]]["status"] == "expired"
    assert hr.svc.db["stripe_sessions"][cs["session_id"]]["status"] == "expired"


def test_nexus_reports_the_previous_calendar_year_too():
    """AEGIS F-11: most states test the current OR the previous calendar year; a year-old peak still flags."""
    from datetime import datetime, timezone
    hr = Harness().ready()
    hr.f["compliance"].nexus_params = NEXUS
    hr.f["compliance"].activations = {"tx-1": {"allowed": True, "jurisdiction": "US-TX"}}
    issue(hr, draft(hr, "tx-1", "76000.00"))                          # 2026
    hr.clock.at = datetime(2027, 11, 15, 12, 0, tzinfo=timezone.utc)
    r = nexus(hr)
    assert r["periods"]["previous_calendar_year"] == {"from": "2026-01-01", "to": "2026-12-31"}
    tx = next(j for j in r["jurisdictions"] if j["jurisdiction"] == "US-TX")
    assert tx["trailing_12_months"]["sales_usd"] == "0.00" and tx["calendar_year"]["sales_usd"] == "0.00"
    assert tx["previous_calendar_year"]["sales_usd"] == "76000.00" and tx["flagged"] is True
    assert any(x.startswith("previous_calendar_year:") for x in tx["flag_reasons"])


# --- AEGIS r2 N-2: a page is recorded expired only when Stripe confirms it ----------------------------------------------

def test_a_page_stripe_does_not_confirm_closed_stays_open_and_the_refusal_says_so(tmp_path):
    from test_stripe_incoming import rr_invoice
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    hr.f["compliance"].activations = {"zbm-client-7": {"allowed": False, "reasons": ["hold open: sanctions_match"]}}
    hr.sim.fail_next = [500] * 10                                  # Stripe cannot expire the page
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 409 and "Stripe did not confirm 1 open page(s)" in r.text, r.text
    assert hr.svc.db["stripe_sessions"][cs["session_id"]]["status"] == "open"
    assert hr.sim.sessions[cs["session_id"]]["status"] == "open"
    hr.sim.fail_next = []
    s = hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))["summary"]
    assert s["closed"] == 1 and hr.svc.db["stripe_sessions"][cs["session_id"]]["status"] == "expired"


# --- AEGIS r2 N-4: bounded work per request and per job run ------------------------------------------------------------

def test_a_refused_checkout_closes_at_most_the_cap_and_the_job_closes_the_rest(tmp_path, monkeypatch):
    import svc_stripe
    from test_stripe_incoming import rr_invoice
    monkeypatch.setattr(svc_stripe, "MAX_REQUEST_CLOSES", 2)
    hr = _stripe_hr(tmp_path)
    invs = [rr_invoice(hr, f"{100 + i}.00") for i in range(3)]
    pages = [hr.ok(hr.post(f"/fin/v1/invoices/{i['invoice_id']}/stripe-checkout", {"request_id": rid()},
                           caller="onboarding"))["checkout"]["session_id"] for i in invs]
    hr.f["compliance"].activations = {"zbm-client-7": {"allowed": False, "reasons": ["x"]}}
    calls_before = sum(1 for c in hr.f["compliance"].calls if c[0] == "client_activation")
    r = hr.post(f"/fin/v1/invoices/{invs[0]['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="onboarding")
    # AEGIS r3 N-13: pages over the cap are "not attempted", never reported as unconfirmed by Stripe
    assert r.status_code == 409 and "1 open page(s) of this client not attempted (cap of 2 per request)" in r.text
    assert "Stripe did not confirm" not in r.text
    assert sum(1 for c in hr.f["compliance"].calls if c[0] == "client_activation") == calls_before + 1
    statuses = [hr.svc.db["stripe_sessions"][p]["status"] for p in pages]
    assert statuses.count("expired") == 2 and statuses.count("open") == 1
    hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))
    assert all(hr.svc.db["stripe_sessions"][p]["status"] == "expired" for p in pages)


def test_the_job_reads_compliance_for_at_most_the_cap_rotating_through_clients(tmp_path, monkeypatch):
    import svc_stripe
    from test_stripe_incoming import rr_invoice
    monkeypatch.setattr(svc_stripe, "MAX_JOB_ACTIVATION_CHECKS", 2)
    hr = _stripe_hr(tmp_path)
    for cid in ("c-a", "c-b", "c-c"):
        hr.f["compliance"].activations = {**(hr.f["compliance"].activations or {}),
                                          cid: {"allowed": True, "jurisdiction": "US-CA"}}
    pages = {}
    for cid in ("c-a", "c-b", "c-c"):
        inv = rr_invoice(hr)
        hr.svc.db["invoices"][inv["invoice_id"]] = {**hr.svc.db["invoices"][inv["invoice_id"]], "client_id": cid}
        pages[cid] = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                                   caller="onboarding"))["checkout"]["session_id"]
    run = lambda: hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))["summary"]
    hr.f["compliance"].calls.clear()
    assert run()["activation_checked"] == 2
    first = [c[1] for c in hr.f["compliance"].calls if c[0] == "client_activation"]
    assert first == ["c-a", "c-b"]
    hr.f["compliance"].calls.clear()
    run()
    assert [c[1] for c in hr.f["compliance"].calls if c[0] == "client_activation"] == ["c-c", "c-a"]


# --- AEGIS r3 N-9: a page made while the client turned REFUSE is closed at once; a payment from a refused client -----
# --- is booked truthfully but held ---------------------------------------------------------------------------------

def _flip_after(hr, n_allowed, then):
    """Compliance answers ALLOW for the first ``n_allowed`` reads, then ``then`` ("refuse" or "down")."""
    fc = hr.f["compliance"]
    real = fc.client_activation
    seen = {"n": 0}

    def answer(client_id):
        seen["n"] += 1
        if seen["n"] <= n_allowed:
            return real(client_id)
        if then == "down":
            fc.activation_available = False
        else:
            fc.activations = {client_id: {"allowed": False, "reasons": ["hold open: sanctions_match"]}}
        return real(client_id)

    fc.client_activation = answer


@pytest.mark.parametrize("then", ["refuse", "down"])
def test_a_page_made_while_the_client_turned_refused_or_unknown_is_closed_at_once(tmp_path, then):
    from test_stripe_incoming import rr_invoice
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    _flip_after(hr, 1, then)                      # the gate read says ALLOW; the read after the page is made does not
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 409, r.text
    assert ("COMPLIANCE_NOT_ACTIVATED" if then == "refuse" else "DEPENDENCY_UNAVAILABLE:compliance_38") in r.text
    sessions = list(hr.svc.db["stripe_sessions"].values())
    assert len(sessions) == 1 and sessions[0]["status"] == "expired"
    assert hr.sim.sessions[sessions[0]["session_id"]]["status"] == "expired"


def test_a_payment_from_a_client_refused_at_booking_is_booked_and_held(tmp_path):
    from test_stripe_incoming import rr_invoice, sent
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    pi = hr.sim.pay(cs["session_id"])
    hr.f["compliance"].activations = {"zbm-client-7": {"allowed": False, "reasons": ["hold open: sanctions_match"]}}
    assert sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})["status"] \
        == "matched_held_compliance"
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid"                                       # the money was received: recorded truthfully
    assert i["held_compliance"]["reason"].startswith("no current Compliance activation")
    bid = i["held_compliance"]["break_id"]
    b = hr.svc.db["breaks"][bid]
    assert b["owner"] == "andre" and b["leg"] == "compliance_hold" and b["status"] == "open" and "refund" in b["note"]
    rct = next(r for r in hr.svc.db["receipts"].values() if r["stripe"]["payment_intent"] == pi)
    assert rct["status"] == "matched"
    assert not [c for c in hr.svc.db["client_receipts"].values() if c["invoice_id"] == inv["invoice_id"]]


def test_a_payment_booked_while_compliance_is_down_opens_a_break_for_andre(tmp_path):
    from test_stripe_incoming import rr_invoice, sent
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    hr.sim.pay(cs["session_id"])
    hr.f["compliance"].activation_available = False
    assert sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})["status"] \
        == "matched"
    assert "held_compliance" not in hr.svc.db["invoices"][inv["invoice_id"]]
    assert any(b["leg"] == "compliance_unverified" and b["status"] == "open" for b in hr.svc.db["breaks"].values())


def test_a_held_media_prepayment_never_pays_the_vendor(tmp_path):
    from test_media_billing import buy, create, issue, pay_vendor
    from test_stripe_incoming import sent
    hr = _stripe_hr(tmp_path)
    m = create(hr)
    inv = issue(hr, m["invoice"])
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    hr.sim.pay(cs["session_id"])
    hr.f["compliance"].activations = {inv["client_id"]: {"allowed": False, "reasons": ["jurisdiction US-CA is refuse"]}}
    sent(hr, "checkout.session.completed", {"id": cs["session_id"]})
    hr.clock.advance(days=4)
    r = pay_vendor(hr, m["media_buy"]["buy_id"], "10000.00", paid_on="2026-10-06")
    assert r.status_code == 409 and "Compliance" in r.text, r.text
