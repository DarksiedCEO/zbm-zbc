"""Finance's Legal (37) thin client (launch hardening, Oct 5 2026): the contract check every invoice approval needs.

The answers below have legal-py's exact shapes (``version_view`` and ``acceptance_view`` in legal-py
``src/service.py``); the cross-service run against a real legal-py process is in legal-py ``devtools/live_run.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import answers as A
import config as config_mod
from clients import HttpLegal
from helpers import ANDRE_TOKEN, Harness, rid
from ports import LegalAnswer
from test_media_billing import issue

SHA = "c" * 64
ACC = "lg-acc-0123456789ABCDEFGHJKMNPQRS"
NOW = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)


def version_doc(**over):
    v = {"doc_id": "client_msa", "version": "1.1", "entity": "zbm", "sha256": SHA, "status": "approved",
         "clause_ids": [], "template_variables": None, "template_ref": {"version": "1.0"}, "counsel_signoff": None,
         "approved_by": "andre", "approved_at": "2026-09-30T00:00:00+00:00",
         "effective_at": "2026-10-01T00:00:00+00:00", "review_by": "2027-10-01T00:00:00+00:00",
         "supersedes": None, "created_at": "2026-09-30T00:00:00+00:00", "created_by": "scheduler",
         "review_label": "counsel_memo:m1"}
    v.update(over)
    return v


def acceptance_doc(**over):
    a = {"acceptance_id": ACC, "doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA,
         "accepted_at": "2026-10-01T12:00:00+00:00", "method": "clickwrap_unticked_box", "evidence_sufficient": True,
         "party_ref": "client:zbm-client-1", "rules_pinned": True}
    a.update(over)
    return a


def client(version=None, acceptance=None, status=200, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((req.url.path, dict(req.headers)))
        if status != 200:
            return httpx.Response(status, json={"detail": "x"})
        if "/versions/" in req.url.path:
            return httpx.Response(200, json=version if version is not None else version_doc())
        if "/acceptances/" in req.url.path:
            return httpx.Response(200, json=acceptance if acceptance is not None else acceptance_doc())
        return httpx.Response(404, json={})
    return HttpLegal("http://legal.internal", "s" * 40, "c" * 40, transport=httpx.MockTransport(handler),
                     timeout=2, now=lambda: NOW)


def ask(c, version="1.1", party="client:zbm-client-1", entity="zbm", acc=ACC, sha=SHA):
    return c.document_status("client_msa", version, sha, acc, party, entity)


def test_a_contract_in_force_accepted_by_this_client_passes():
    seen = []
    ans = ask(client(seen=seen))
    assert (ans.available, ans.current, ans.acceptance_matches) == (True, True, True)
    assert A.check(ans, LegalAnswer(False)) is ans
    assert [p for p, _ in seen] == ["/legal/v1/documents/client_msa/versions/1.1", f"/legal/v1/acceptances/{ACC}"]
    assert seen[0][1]["x-legal-caller-token"] == "c" * 40 and seen[0][1]["authorization"] == "Bearer " + "s" * 40


@pytest.mark.parametrize("over", [{"status": "draft"}, {"status": "retired"}, {"sha256": "d" * 64},
                                  {"entity": "zbc"}, {"effective_at": "2026-10-03T00:00:00+00:00"},
                                  {"review_by": "2026-10-02T17:00:00+00:00"}, {"version": "1.0"},
                                  {"doc_id": "other_msa"}, {"effective_at": None}])
def test_a_version_legal_does_not_hold_in_force_for_this_entity_is_not_current(over):
    ans = ask(client(version=version_doc(**over)))
    assert ans.available and not ans.current


@pytest.mark.parametrize("over", [{"party_ref": "client:someone-else"}, {"evidence_sufficient": False},
                                  {"doc_sha256": "d" * 64}, {"version": "1.0"}, {"doc_id": "other"},
                                  {"acceptance_id": "lg-acc-ZZZZZZZZZZZZZZZZZZZZZZZZZZ"}])
def test_an_acceptance_by_another_party_or_for_another_text_does_not_match(over):
    ans = ask(client(acceptance=acceptance_doc(**over)))
    assert ans.available and ans.current and not ans.acceptance_matches


@pytest.mark.parametrize("status", [404, 409, 500, 503])
def test_legal_that_cannot_confirm_is_unavailable(status):
    assert not ask(client(status=status)).available


def test_a_non_production_legal_or_odd_answers_are_never_a_pass():
    assert not ask(client(acceptance=acceptance_doc(rules_pinned=False))).available
    assert not ask(client(version=["not", "a", "dict"])).available
    assert not ask(client(version=version_doc(effective_at="yesterday"))).available
    a = acceptance_doc()
    del a["evidence_sufficient"]
    assert not ask(client(acceptance=a)).available


def test_ids_never_reach_a_url_unchecked():
    seen = []
    c = client(seen=seen)
    for kw in [{"version": "1"}, {"version": "../../x"}, {"sha": "nothex"}]:
        assert not ask(c, **kw).current
    assert not ask(c, acc="../../documents/x").acceptance_matches
    assert c.document_status("../x", "1.1", SHA, ACC).current is False
    assert all(p.startswith("/legal/v1/documents/client_msa/versions/1.1") for p, _ in seen)


def test_a_whole_number_version_means_n_point_zero():
    seen = []
    ans = ask(client(version=version_doc(version="1.0"), acceptance=acceptance_doc(version="1.0"), seen=seen),
              version=1)
    assert ans.current and ans.acceptance_matches and seen[0][0].endswith("/versions/1.0")


# --- through the service ----------------------------------------------------------------------------------------------

def test_every_contract_check_names_the_client_and_entity():
    hr = Harness().ready()
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-9", "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "100.00"}],
            "payment_methods": ["ach"], "legal_ref": {"doc_id": "client_msa", "version": "1.1",
                                                      "doc_sha256": SHA, "acceptance_id": ACC}}
    inv = hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]
    issue(hr, inv)
    assert hr.f["legal"].last == ("client_msa", "1.1", SHA, ACC, "client:zbm-client-9", "zbm")


def test_without_legal_no_invoice_is_issued():
    hr = Harness().ready()
    hr.f["legal"].available = False
    body = {"request_id": rid(), "entity": "zbm", "client_id": "c1", "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "100.00"}],
            "payment_methods": ["ach"], "legal_ref": {"doc_id": "client_msa", "version": 1, "doc_sha256": SHA,
                                                      "acceptance_id": ACC}}
    inv = hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:legal_37" in r.text


def test_legal_settings_come_as_a_set():
    base = Harness().env
    s = config_mod.load({**base, "FIN_LEGAL_URL": "http://legal.internal:8437", "FIN_LEGAL_TOKEN": "t" * 40,
                         "FIN_LEGAL_CALLER_TOKEN": "k" * 40})
    assert s.legal_url == "http://legal.internal:8437"
    import api
    assert isinstance(api.build_ports(s).legal, HttpLegal)
    with pytest.raises(RuntimeError):
        config_mod.load({**base, "FIN_LEGAL_URL": "http://x", "FIN_LEGAL_TOKEN": "t" * 40})


# --- ADR 0022: a founder-approved contract passes; Finance shows only its renewal date ----------------------------------

FOUNDER_VERSION = {"approval_basis": "founder_approval", "review_label": "founder_approval",
                   "founder_rationale_sha256": "e" * 64}
FOUNDER_ACCEPTANCE = {"evidence_basis": "founder_determination"}


def test_a_founder_approved_contract_passes_and_only_its_renewal_date_is_carried():
    ans = ask(client(version=version_doc(**FOUNDER_VERSION), acceptance=acceptance_doc(**FOUNDER_ACCEPTANCE)))
    assert (ans.available, ans.current, ans.acceptance_matches) == (True, True, True)
    assert (ans.renew_by, ans.renewal_lapsed) == ("2027-10-01T00:00:00+00:00", False)
    assert A.check(ans, LegalAnswer(False)) is ans
    assert set(vars(ans)) == {"available", "current", "acceptance_matches", "reason", "renew_by", "renewal_lapsed",
                              "version_basis_in_force", "acceptance_basis_in_force"}


def test_readiness_and_the_issued_invoice_carry_no_approval_or_evidence_basis():
    hr = Harness().ready()
    hr.svc.ports.legal = client(version=version_doc(**FOUNDER_VERSION), acceptance=acceptance_doc(**FOUNDER_ACCEPTANCE))
    msa = {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA, "acceptance_id": ACC}
    hr.ok(hr.put("/fin/v1/clients/zbm-client-1/billing-profile",
                 {"request_id": rid(), "entity": "zbm", "payment_method": "ach", "msa": msa}, andre=ANDRE_TOKEN))
    rd = hr.ok(hr.get("/fin/v1/clients/zbm-client-1/billing-readiness", caller="onboarding"))
    assert rd["allowed"] is True, rd["unmet"]
    assert rd["contract_renewal"] == {"MSA": {"renew_by": "2027-10-01T00:00:00+00:00", "renewal_due_soon": False,
                                              "renewal_lapsed": False}}
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-1", "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "100.00"}],
            "payment_methods": ["ach"], "legal_ref": msa}
    inv = hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]
    out = issue(hr, inv)
    assert out["status"] == "issued"
    text = str(rd) + str(out)
    for word in ("founder", "approval_basis", "evidence_basis", "review_label"):
        assert word not in text, word


def test_a_lapsed_renewal_date_is_named_plainly_and_a_close_one_is_flagged():
    lapsed = ask(client(version=version_doc(**FOUNDER_VERSION, review_by="2026-10-02T00:00:00+00:00")))
    assert lapsed.available and not lapsed.current and lapsed.renewal_lapsed is True
    hr = Harness().ready()
    msa = {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA, "acceptance_id": ACC}
    hr.ok(hr.put("/fin/v1/clients/zbm-client-1/billing-profile",
                 {"request_id": rid(), "entity": "zbm", "payment_method": "ach", "msa": msa}, andre=ANDRE_TOKEN))
    now = hr.svc._now()
    for renew_by, allowed, soon in ((now.replace(year=now.year + 1), True, False),
                                    (now + timedelta(days=3), True, True),
                                    (now - timedelta(days=1), False, None)):
        hr.svc.ports.legal = HttpLegal("http://legal.internal", "s" * 40, "c" * 40, timeout=2, now=lambda: now,
                                       transport=httpx.MockTransport(lambda req, rb=renew_by: httpx.Response(
                                           200, json=version_doc(**FOUNDER_VERSION, review_by=rb.isoformat())
                                           if "/versions/" in req.url.path else acceptance_doc(**FOUNDER_ACCEPTANCE))))
        rd = hr.ok(hr.get("/fin/v1/clients/zbm-client-1/billing-readiness", caller="onboarding"))
        assert rd["allowed"] is allowed and rd["contract_renewal"]["MSA"]["renewal_due_soon"] is soon, rd
        if not allowed:
            assert rd["contract_renewal"]["MSA"]["renewal_lapsed"] is True
            assert any("renewal date passed on" in u and "Andre renews it at Legal" in u for u in rd["unmet"])


def test_the_sales_tax_row_is_named_neutrally_with_the_same_rule_and_code():
    hr = Harness().ready(("FIN-CQ-01",))                  # FIN-CQ-11 not on file
    hr.svc.ports.legal = client(version=version_doc(**FOUNDER_VERSION), acceptance=acceptance_doc(**FOUNDER_ACCEPTANCE))
    msa = {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA, "acceptance_id": ACC}
    hr.ok(hr.put("/fin/v1/clients/zbm-client-1/billing-profile",
                 {"request_id": rid(), "entity": "zbm", "payment_method": "ach", "msa": msa}, andre=ANDRE_TOKEN))
    rd = hr.ok(hr.get("/fin/v1/clients/zbm-client-1/billing-readiness", caller="onboarding"))
    want = ("sales-tax determination FIN-CQ-11 is not verified: Andre amends FIN-CQ-11 to verified with the memo "
            "reference")
    assert rd["allowed"] is False and rd["unmet"] == [f"fin/FIN-27/TAX_TREATMENT_UNVERIFIED: {want}"]
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-1", "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "100.00"}],
            "payment_methods": ["ach"], "legal_ref": msa}
    inv = hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409
    reasons = r.json()["reasons"]
    assert [(x["code"], x["rule_id"], x["message"]) for x in reasons] == [("TAX_TREATMENT_UNVERIFIED", "FIN-27", want)]


@pytest.mark.parametrize("version_over,acceptance_over", [({"basis_in_force": False}, {}),
                                                          ({}, {"basis_in_force": False}),
                                                          ({"basis_in_force": "yes"}, {}),
                                                          ({}, {"basis_in_force": None})])
def test_aegis_m2_a_basis_legal_no_longer_holds_is_refused_at_invoice_issue(version_over, acceptance_over):
    ans = ask(client(version=version_doc(**FOUNDER_VERSION, **version_over),
                     acceptance=acceptance_doc(**FOUNDER_ACCEPTANCE, **acceptance_over)))
    assert ans.available and not (ans.current and ans.acceptance_matches)
    hr = Harness().ready()
    msa = {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA, "acceptance_id": ACC}
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-1", "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "100.00"}],
            "payment_methods": ["ach"], "legal_ref": msa}
    hr.svc.ports.legal = client(version=version_doc(**FOUNDER_VERSION), acceptance=acceptance_doc(**FOUNDER_ACCEPTANCE))
    inv = hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]
    # Legal changes its answer between drafting and issue: the issue-time check is what counts
    hr.svc.ports.legal = client(version=version_doc(**FOUNDER_VERSION, **version_over),
                                acceptance=acceptance_doc(**FOUNDER_ACCEPTANCE, **acceptance_over))
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "LEGAL_NOT_CURRENT" in r.text
    msgs = [x["message"] for x in r.json()["reasons"] if x["code"] == "LEGAL_NOT_CURRENT"]
    # AEGIS r2 N6: say plainly that Legal's basis is out of force, never a hash/document mismatch that is not there
    assert msgs and all("basis is not in force at Legal" in m for m in msgs), msgs
    assert not any("does not match" in m or "is not current at Legal" in m for m in msgs), msgs
