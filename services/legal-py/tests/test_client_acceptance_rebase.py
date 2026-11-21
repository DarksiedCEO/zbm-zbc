"""AEGIS review of the rebase onto feat/compliance-gate-deploy (aegis-client-acceptance-rebase.md @e5eb8008).

- RB-L2: no caller records ANY client's acceptance on the generic route, a party-bound filled SOW included: every
  non-portal caller allowed on ``POST /legal/v1/acceptances`` gets 409 and nothing is recorded.
- RB-L3: Legal's /health never softens a worse status to ``warning`` because an optional caller is unset.
- RB-I1: the portal routes' own check answers 503 to ANY token, another configured caller's included, while
  ``client_portal`` is unset (the generic optional-caller rule alone would answer that caller 403).
"""

from __future__ import annotations

import json

import pytest

from helpers import CALLERS, Harness, rid
from test_client_acceptance_portal import CLIENT, determine_cq19, msa, sow_fill


@pytest.fixture
def filled():
    x = Harness()
    x.approve_rules()
    determine_cq19(x)
    m, s = msa(x), sow_fill(x)
    return x, m, s


@pytest.mark.parametrize("caller", ["onboarding", "hub", "clipper_network"])
@pytest.mark.parametrize("which", ["sow", "msa"])
def test_no_non_portal_caller_records_a_clients_acceptance(filled, caller, which):
    x, m, s = filled
    doc = s if which == "sow" else m
    doc_id = "sow_seo_audit" if which == "sow" else "client_msa"
    before = len(x.svc.acceptances)
    body = {"request_id": rid("acc"), "party_ref": CLIENT, "signer_identity_ref": CLIENT.split(":", 1)[1],
            "doc_id": doc_id, "version": doc["version"], "doc_sha256": doc["sha256"], "presented_sha256": doc["sha256"],
            "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept", "affirmative_act": True}
    r = x.post("/legal/v1/acceptances", body, caller=caller)
    assert r.status_code == 409, r.text
    r = r.json()
    assert any(i["code"] == "ACCEPTANCE_NOT_INVITED" for i in r["reasons"]), r
    assert len(x.svc.acceptances) == before


def _unset(name):
    return Harness(env={"LEGAL_CALLER_TOKENS": json.dumps(dict(CALLERS, **{name: ""}))})


@pytest.mark.parametrize("name", ["client_portal", "compliance_38"])
def test_a_degraded_health_is_never_softened_to_warning(name, monkeypatch):
    x = _unset(name)
    x.approve_rules()
    assert x.client.get("/health").json()["status"] == "warning"
    monkeypatch.setattr(x.svc, "unacknowledged_quarantine", lambda: [{"line": 1}])
    body = x.client.get("/health").json()
    assert body["status"] == "degraded", body
    assert body["warnings"] == [f"optional_caller_unset:{name}"]


@pytest.mark.parametrize("who", ["onboarding", "finance_31", "compliance_38"])
def test_the_portal_routes_answer_503_to_every_token_while_client_portal_is_unset(who):
    x = _unset("client_portal")
    x.approve_rules()
    for path, body in (("/legal/v1/portal/invitations/resolve", {"token": "A" * 66}),
                       ("/legal/v1/portal/receipts", {"token": "A" * 66, "request_id": "pa-1",
                                                      "form_sha256": "0" * 64}),
                       ("/legal/v1/portal/acceptances", {"request_id": "pa-1"})):
        r = x.post(path, body, caller=who)
        assert r.status_code == 503 and "client_portal" in r.text, (who, path, r.status_code, r.text)
