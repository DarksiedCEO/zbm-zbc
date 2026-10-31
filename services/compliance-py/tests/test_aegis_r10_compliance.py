"""AEGIS r10 R10-3 and R9-4: Compliance hands the sanctions signal to Andre only.

- ``GET /compliance/v1/holds``: Andre's token sees every hold in full; Finance (``finance_31``, its payout gate needs
  only which subject has an open hold) sees each hold as a generic ``compliance_hold`` with no detail or obligation;
  every other caller is refused.
- ``GET /compliance/v1/audit/export`` and ``/audit/evidence``: Andre's token only.
- The generic activation answer (SEO, Revenue Recovery, onboarding) is byte-identical for every refusal: a sanctions
  match, a client outside the service area and a client never activated (no jurisdiction, no valid_until, no ruling).
- An activation ruling answered to a non-Andre caller (``POST /compliance/v1/rule``, ``/gates/activation``,
  ``GET /rulings/{id}``) carries the screening (HR-14) and hold items as one generic item: the OFAC result, the hold
  kind and its id go to Andre only.
"""

from __future__ import annotations

import json

import pytest

from helpers import ANDRE_TOKEN
from test_service_area_gate import activate, attest, harness, status, web_client

NON_ANDRE = ("seo_02", "revenue_recovery", "onboarding", "scheduler", "creative_production", "legal_37")


def _sanctioned(h, client="client-web"):
    activate(h, client)
    a = attest(h, client, result="potential_match")             # an OFAC hit after activation: a sanctions match
    assert a.status_code == 200, a.text


def _get(h, path, caller=None, andre=None, **params):
    return h.client.get(path, headers=h.headers(caller, andre=andre), params=params)


def test_holds_are_andre_only_in_full_and_generic_for_finance():
    h = harness()
    _sanctioned(h)
    full = _get(h, "/compliance/v1/holds", andre=ANDRE_TOKEN)
    assert full.status_code == 200 and "sanctions_match" in full.text and "potential_match" in full.text, full.text
    fin = _get(h, "/compliance/v1/holds", caller="finance_31")
    assert fin.status_code == 200, fin.text
    rows = fin.json()
    assert rows and all(r["kind"] == "compliance_hold" and "detail" not in r and r.get("obligation_id") is None
                        for r in rows), rows
    assert {r["subject_id"] for r in rows} == {"client-web"} and all(r["status"] == "open" for r in rows)
    low = fin.text.lower()
    assert "sanction" not in low and "ofac" not in low and "match" not in low, fin.text
    for caller in NON_ANDRE:
        r = _get(h, "/compliance/v1/holds", caller=caller)
        assert r.status_code == 403, (caller, r.status_code, r.text[:200])


@pytest.mark.parametrize("path", ["/compliance/v1/audit/export", "/compliance/v1/audit/evidence"])
def test_the_audit_routes_are_andre_only(path):
    h = harness()
    _sanctioned(h)
    for caller in NON_ANDRE + ("finance_31",):
        r = _get(h, path, caller=caller)
        assert r.status_code == 403, (caller, r.status_code, r.text[:200])
    r = _get(h, path, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text


def test_the_generic_refusal_is_byte_identical_whatever_the_reason():
    worlds = {}
    h = harness()
    _sanctioned(h)
    worlds["sanctions"] = h
    h = harness()
    activate(h, "client-web", country="CA", region="CA-ON")      # outside the service area
    worlds["area"] = h
    worlds["never_activated"] = harness()
    for caller in ("seo_02", "revenue_recovery", "onboarding"):
        answers = {k: json.dumps(status(w, caller=caller), sort_keys=True) for k, w in worlds.items()}
        assert len(set(answers.values())) == 1, (caller, answers)
        d = json.loads(answers["sanctions"])
        assert d["decision"] == "REFUSE" and d["jurisdiction"] is None and d["valid_until"] is None
        assert d["ruling_id"] is None and d["evaluated_at"] is None


def test_finance_and_andre_still_read_the_full_refusal():
    h = harness()
    _sanctioned(h)
    full = status(h, caller="finance_31")
    assert full["sanctions_grounded"] is True and full["jurisdiction"] == "US-CA"


def _hidden(view: dict) -> None:
    text = json.dumps(view).lower()
    assert "sanction" not in text and "ofac" not in text and "potential_match" not in text and "hold-" not in text, \
        view
    assert all(u["obligation_id"] != "HR-14" and not u["code"].startswith("hold_open") for u in view["unmet"]), view


def test_an_activation_ruling_shows_onboarding_no_screening_result_or_hold():
    h = harness()
    f = web_client(h, "client-web", attest_first=False)
    a = attest(h, "client-web", result="potential_match").json()
    f["sanctions_attestation_id"] = a["attestation_id"]
    r = h.rule("client-web", "client", f)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["allowed"] is False
    _hidden(j)
    assert any(u["code"] == "andre_review" for u in j["unmet"]), j["unmet"]
    stored = h.svc.rulings[j["ruling_id"]]["unmet"]               # Andre's record keeps every item
    assert any(u["obligation_id"] == "HR-14" for u in stored)
    for caller in ("onboarding", "finance_31", "creative_production"):
        g = _get(h, f"/compliance/v1/rulings/{j['ruling_id']}", caller=caller)
        assert g.status_code == 200, g.text
        _hidden(g.json())


def test_a_missing_screening_reads_the_same_as_a_hit_to_onboarding():
    out = []
    for result in ("potential_match", None):
        h = harness()
        f = web_client(h, "client-web", attest_first=False)
        if result:
            f["sanctions_attestation_id"] = attest(h, "client-web", result=result).json()["attestation_id"]
        j = h.rule("client-web", "client", f).json()
        out.append(sorted((u["code"], u["obligation_id"], u["message"]) for u in j["unmet"]))
    assert out[0] == out[1], out


def test_c4_the_expiry_warning_day_boundaries_are_inclusive():
    """AEGIS r10 mutant c4: a rule expiring in exactly 30 / 14 / 7 days is in that window; one day more is not."""
    from datetime import datetime, timezone
    expires = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)                  # US-OFAC-04
    for d in (30, 14, 7):
        for extra, inside in ((0, True), (1, False)):
            from datetime import timedelta
            h = harness(at=expires - timedelta(days=d + extra))
            w = h.get("/compliance/v1/readiness").json()["expiry_warnings"]
            assert ("US-OFAC-04" in w[f"within_{d}_days"]) is inside, (d, extra, w[f"within_{d}_days"])
            assert ("US-OFAC-04" in w["client_activation_rules"][f"within_{d}_days"]) is inside
