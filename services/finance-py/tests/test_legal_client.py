"""Finance's Legal (37) thin client (launch hardening, Oct 5 2026): the contract check every invoice approval needs.

The answers below have legal-py's exact shapes (``version_view`` and ``acceptance_view`` in legal-py
``src/service.py``); the cross-service run against a real legal-py process is in legal-py ``devtools/live_run.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
