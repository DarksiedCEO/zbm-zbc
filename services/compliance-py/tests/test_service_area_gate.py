"""Founder decisions of Oct 30, 2026 (ADR 0024): the service area (every inhabited US jurisdiction: the 50 states, DC,
PR, GU, VI, AS, MP; update of 12:51), the client
sanctions screening and legal documents required at activation, the client activation status other departments
read, the readiness report (expired rules and the gates they affect) and the in-process daily scheduler.

Every test runs on the real seed, approved by Andre, with the clock on Oct 30, 2026 or later.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import helpers as H
from clock import FixedClock, iso
from fakes import FakeLedgerClient, PassingLegal
from helpers import ANDRE_TOKEN, Harness, client_facts, jur, rid, unmet_codes, unmet_ids

OCT30 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
STATES = ["AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "IA", "ID", "IL", "IN", "KS", "KY", "LA",
          "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK",
          "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY"]
MSA = {"doc_id": "client_msa", "version": "1.0", "doc_sha256": "e" * 64, "acceptance_id": "lg-acc-01J0000000000000000000000A"}


def harness(at: datetime = OCT30, legal=None, ledger=None) -> Harness:
    clock = FixedClock(at)
    ports = H.passing_ports(clock)
    ports.legal = legal if legal is not None else PassingLegal()
    h = Harness(ports=ports, clock=clock, ledger=ledger)
    h.approve_seed()
    h.run_controls()
    return h


def attest(h: Harness, subject: str, result: str = "no_match", searched_at: datetime | None = None,
           lists=("SDN", "Consolidated"), lane: str = "client", andre=ANDRE_TOKEN):
    body = {"request_id": rid("att"), "subject_id": subject, "lane": lane,
            "searched_at": iso(searched_at or h.clock.at), "list_version": "SDN list updated 2026-10-29",
            "lists": list(lists), "tool": "ofac_sanctions_list_search", "result": result,
            "evidence_sha256": hashlib.sha256(f"ev-{subject}".encode()).hexdigest(),
            "searched_name_sha256": hashlib.sha256(f"name-{subject}".encode()).hexdigest()}
    return h.post("/compliance/v1/sanctions/attestations", body, andre=andre)


def default_ownership(h: Harness, subject: str, lane: str = "client") -> None:
    """ADR 0027: every client activation needs an ownership record; the default is an entity that attested nobody
    owns 25% or more of it (owner screening has its own tests: test_owner_screening.py)."""
    if (lane, subject) in h.svc.ownerships:
        return
    body = {"request_id": rid("own"), "lane": lane, "client_kind": "entity", "owners": [], "no_owner_25": True,
            "client_attestation": {"attested_at": iso(h.clock.at), "method": "signed_form", "evidence_sha256": "c" * 64,
                                   "attestation_ref": "ownership-form"}}
    r = h.client.put(f"/compliance/v1/clients/{subject}/ownership", json=body, headers=h.headers(None, ANDRE_TOKEN))
    assert r.status_code == 200, r.text


def web_client(h: Harness, subject: str = "client-web", country: str = "US", region: str | None = "US-CA",
               targets=("US",), platforms=("web",), docs=(MSA,), attest_first: bool = True, ownership: bool = True,
               **over) -> dict:
    f = client_facts(targets=targets, platforms=platforms, country=country, region=region)
    if ownership and attest_first:
        default_ownership(h, subject)
    if attest_first:
        a = attest(h, subject)
        assert a.status_code == 200, a.text
        f["sanctions_attestation_id"] = a.json()["attestation_id"]
    if docs is not None:
        f["legal_documents"] = [dict(d) for d in docs]
    f.update(over)
    return f


def activate(h: Harness, subject: str = "client-web", **kw):
    r = h.rule(subject, "client", web_client(h, subject, **kw))
    assert r.status_code == 200, r.text
    return r.json()


def status(h: Harness, client: str = "client-web", caller: str = "finance_31"):
    r = h.get(f"/compliance/v1/clients/{client}/activation", caller=caller)
    assert r.status_code == 200, r.text
    return r.json()


# --- the allowed set is ONE reviewed rule parameter ------------------------------------------------------------------

def test_the_service_area_is_hr05_operate_exactly():
    hr05 = next(r for r in H.SEED_ROWS if r["id"] == "HR-05")
    want = sorted([f"US-{s}" for s in STATES] + ["US-DC", "US-PR", "US-GU", "US-VI", "US-AS", "US-MP",
                                                  "PR", "GU", "VI", "AS", "MP"])
    assert sorted(hr05["parameters"]["operate"]) == want
    assert hr05["parameters"]["target_countries"] == ["US"]
    assert hr05["status"] == "verified" and hr05["verified_at"] == "2026-10-30"
    for code in ("US", "GB", "CA", "FR", "MX", "US-UM", "UM"):
        assert code not in hr05["parameters"]["operate"]
    hr06 = next(r for r in H.SEED_ROWS if r["id"] == "HR-06")
    assert hr06["status"] == "superseded"           # the EU kit is outside the service area; kept for history


def test_no_service_area_text_in_the_register_names_uk_or_canada_as_served():
    for r in H.SEED_ROWS:
        if r["status"] == "superseded":
            assert r["effective_note"].startswith("Superseded Oct 30, 2026: outside the US service area")
            continue
        text = (r["title"] + " " + r["obligation"]).lower()
        assert "outside quebec" not in text and "operate in the united kingdom" not in text, r["id"]
        assert "eu kit" not in text or r["id"] == "HR-06", r["id"]


# --- geofence refusals and admissions ----------------------------------------------------------------------------------

@pytest.mark.parametrize("country,region", [("FR", None), ("CA", "CA-ON"), ("CA", "CA-QC"), ("CA", None),
                                            ("GB", None), ("GB", "GB-EDH"), ("DE", None), ("MX", None),
                                            ("US", "US-UM"), ("UM", None), ("FR", "FR-75C")])
def test_clients_outside_the_service_area_are_refused(country, region):
    h = harness()
    r = h.rule("client-x", "client", web_client(h, "client-x", country=country, region=region))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["allowed"] is False
    assert ("HR-07", "jurisdiction_class") in unmet_codes(j) or ("HR-05", "jurisdiction_class") in unmet_codes(j), j


@pytest.mark.parametrize("country,region", [("US", "US-PR"), ("PR", None), ("US", "US-GU"), ("GU", None),
                                            ("US", "US-VI"), ("VI", None), ("US", "US-AS"), ("AS", None),
                                            ("US", "US-MP"), ("MP", None),
                                            ("US", "US-DC"), ("US", "US-NY"), ("US", "US-AK"), ("US", "US-HI")])
def test_clients_in_the_service_area_are_allowed(country, region):
    h = harness()
    targets = (region or country,) if country in ("PR", "GU", "VI", "AS", "MP") else ("US",)
    j = activate(h, "client-ok", country=country, region=region, targets=targets)
    assert j["allowed"] is True, j["unmet_lines"]


def test_a_campaign_target_outside_the_service_area_is_refused():
    h = harness()
    for target in ("CA-ON", "GB", "FR", "US-UM", "DE", "MX"):
        j = h.rule(f"client-t-{target}", "client", web_client(h, f"client-t-{target}", targets=("US", target))).json()
        assert j["allowed"] is False and "HR-07" in unmet_ids(j), (target, j["unmet_lines"])


def test_missing_jurisdiction_is_refused():
    h = harness()
    f = web_client(h, "client-nj")
    del f["jurisdiction"]
    j = h.rule("client-nj", "client", f).json()
    assert j["allowed"] is False and ("HR-03", "fact_missing:jurisdiction") in unmet_codes(j)
    f = web_client(h, "client-ns", region=None)          # a bare US needs its state
    j = h.rule("client-ns", "client", f).json()
    assert j["allowed"] is False and ("HR-05", "fact_missing:jurisdiction.declared_region") in unmet_codes(j)
    f = web_client(h, "client-na")
    f["jurisdiction"]["attested"] = False
    j = h.rule("client-na", "client", f).json()
    assert j["allowed"] is False


def test_ofac_jurisdictions_still_cite_us_ofac_04():
    h = harness()
    j = h.rule("client-ir", "client", web_client(h, "client-ir", country="IR", region=None)).json()
    assert {"HR-07", "US-OFAC-04"} <= unmet_ids(j)


def test_resolve_route_answers_the_service_area_for_finance():
    h = harness()
    r = h.post("/compliance/v1/jurisdictions/resolve",
               {"request_id": rid("res"), "person": jur("US", "US-PR"), "targets": ["US", "CA-ON"]}, caller="finance_31")
    assert r.status_code == 200, r.text
    classes = [(a["code"], a["class"]) for a in r.json()["answers"]]
    assert classes == [("US-PR", "operate"), ("US", "operate"), ("CA-ON", "refuse")]


# --- sanctions screening recorded (HR-14) --------------------------------------------------------------------------

def test_sanctions_screening_missing_is_refused():
    h = harness()
    j = h.rule("client-ns1", "client", web_client(h, "client-ns1", attest_first=False)).json()
    assert j["allowed"] is False and ("HR-14", "fact_missing:sanctions_attestation_id") in unmet_codes(j)


def test_sanctions_attestation_of_another_subject_is_refused():
    h = harness()
    other = attest(h, "client-other").json()["attestation_id"]
    j = h.rule("client-ns2", "client", web_client(h, "client-ns2", attest_first=False,
                                                  sanctions_attestation_id=other)).json()
    assert j["allowed"] is False and ("HR-14", "client_sanctions_attested") in unmet_codes(j)


def test_a_potential_match_or_match_refuses_and_holds_the_subject_permanently():
    h = harness()
    for result in ("potential_match", "match"):
        subject = f"client-{result}"
        a = attest(h, subject, result=result)
        assert a.status_code == 200 and a.json()["hold_id"], a.text
        f = web_client(h, subject, attest_first=False, sanctions_attestation_id=a.json()["attestation_id"])
        j = h.rule(subject, "client", f).json()
        assert j["allowed"] is False and ("HR-14", "client_sanctions_attested") in unmet_codes(j)
        rel = h.post(f"/compliance/v1/holds/{a.json()['hold_id']}/release",
                     {"request_id": rid("rel"), "reason": "looked fine"}, andre=ANDRE_TOKEN)
        assert rel.status_code == 409          # never released through the API: refuse, do not investigate around it


def test_a_stale_screening_is_refused():
    h = harness()
    a = attest(h, "client-old", searched_at=OCT30 - timedelta(days=31)).json()
    j = h.rule("client-old", "client", web_client(h, "client-old", attest_first=False,
                                                  sanctions_attestation_id=a["attestation_id"])).json()
    assert j["allowed"] is False and ("HR-14", "client_sanctions_attested") in unmet_codes(j)


def test_a_screening_missing_a_list_or_in_the_future_is_refused():
    h = harness()
    r = attest(h, "client-fut", searched_at=OCT30 + timedelta(hours=1))
    assert r.status_code == 422
    a = attest(h, "client-sdn", lists=("SDN",)).json()
    j = h.rule("client-sdn", "client", web_client(h, "client-sdn", attest_first=False,
                                                  sanctions_attestation_id=a["attestation_id"])).json()
    assert j["allowed"] is False and ("HR-14", "client_sanctions_attested") in unmet_codes(j)


def test_only_andre_records_a_sanctions_attestation_and_it_is_on_the_ledger_first():
    h = harness()
    assert attest(h, "client-a", andre=None).status_code == 403
    assert attest(h, "client-a", andre="not-andre").status_code == 403
    r = h.post("/compliance/v1/sanctions/attestations", {"request_id": rid("att")}, caller="onboarding")
    assert r.status_code in (403, 422)
    ok = attest(h, "client-a")
    assert ok.status_code == 200
    assert h.ledger.of_type("sanctions_attestation_recorded")
    ledger = FakeLedgerClient()
    h2 = harness(ledger=ledger)
    ledger.fail_all = True
    r = attest(h2, "client-b")
    assert r.status_code == 503 and r.json()["issued"] is False


# --- legal documents current (HR-15, Legal 37) --------------------------------------------------------------------------

class _Legal(PassingLegal):
    def __init__(self, available=True, current=True, accepted=True):
        super().__init__()
        self.answer = (available, current, accepted)
        self.asked: list[tuple] = []

    def client_document(self, doc_id, version, doc_sha256, acceptance_id, party_ref):
        from ports import ClientDocument
        self.asked.append((doc_id, version, doc_sha256, acceptance_id, party_ref))
        a, c, acc = self.answer
        return ClientDocument(a, c if a else False, acc if a else False)


def test_legal_documents_must_be_current_and_accepted_by_this_client():
    legal = _Legal()
    h = harness(legal=legal)
    assert activate(h, "client-docs")["allowed"] is True
    assert legal.asked[-1] == ("client_msa", "1.0", "e" * 64, MSA["acceptance_id"], "client:client-docs")
    for answer, code in (((False, False, False), "dependency_unavailable:legal_37"),
                         ((True, False, True), "client_docs_current"), ((True, True, False), "client_docs_current")):
        legal.answer = answer
        j = h.rule("client-docs", "client", web_client(h, "client-docs")).json()
        assert j["allowed"] is False and ("HR-15", code) in unmet_codes(j), (answer, j["unmet_lines"])


def test_legal_documents_missing_is_refused():
    h = harness()
    j = h.rule("client-nodoc", "client", web_client(h, "client-nodoc", docs=None)).json()
    assert j["allowed"] is False and ("HR-15", "fact_missing:legal_documents") in unmet_codes(j)
    j = h.rule("client-nomsa", "client", web_client(h, "client-nomsa", docs=({**MSA, "doc_id": "sow"},))).json()
    assert j["allowed"] is False and ("HR-15", "fact_missing:legal_documents") in unmet_codes(j)


def test_production_legal_stand_in_refuses_activation():
    clock = FixedClock(OCT30)
    ports = H.passing_ports(clock)
    from ports import NotBuiltLegal37
    ports.legal = NotBuiltLegal37()
    h = Harness(ports=ports, clock=clock)
    h.approve_seed()
    h.run_controls()
    j = h.rule("client-sb", "client", web_client(h, "client-sb")).json()
    assert j["allowed"] is False and ("HR-15", "dependency_unavailable:legal_37") in unmet_codes(j)


# --- rule expiry: irrelevant rules do not block a US web client -----------------------------------------------------------

def test_expired_platform_rules_do_not_block_a_us_web_client_but_block_tiktok():
    h = harness()
    j = activate(h, "client-web")
    assert j["allowed"] is True, j["unmet_lines"]
    j = h.rule("client-tt", "client", web_client(h, "client-tt", platforms=("tiktok",))).json()
    assert j["allowed"] is False and ("PLT-TT-02", "rule_not_in_force") in unmet_codes(j)


def test_on_dec_25_the_us_ofac_rows_block_until_re_verified():
    h = harness(at=datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc))
    j = h.rule("client-dec", "client", web_client(h, "client-dec")).json()
    assert j["allowed"] is False and ("US-OFAC-04", "rule_not_in_force") in unmet_codes(j)


# --- gates closed until C-11 has passed once ------------------------------------------------------------------------------

def test_gates_stay_closed_until_c11_has_passed_once():
    clock = FixedClock(OCT30)
    ports = H.passing_ports(clock)
    ports.legal = PassingLegal()
    h = Harness(ports=ports, clock=clock)
    h.approve_seed()
    j = h.rule("client-c11", "client", web_client(h, "client-c11")).json()
    assert j["allowed"] is False and any(c.startswith("control_red:C-11") for _, c in unmet_codes(j))
    assert status(h, "client-c11")["decision"] == "REFUSE"


# --- the activation status other departments read ---------------------------------------------------------------------------

def test_activation_status_allow_only_while_current():
    h = harness()
    assert status(h, "client-none") == {**status(h, "client-none"), "decision": "REFUSE", "current": False}
    activate(h, "client-web")
    s = status(h)
    assert s["decision"] == "ALLOW" and s["current"] is True and s["jurisdiction"] == "US-CA"
    assert s["ruling_id"] and s["valid_until"] == iso(OCT30 + timedelta(days=90))
    for caller in ("seo_02", "revenue_recovery", "onboarding"):
        assert status(h, caller=caller)["decision"] == "ALLOW"
    # a refused re-activation voids it
    h.rule("client-web", "client", web_client(h, "client-web", platforms=("tiktok",)))
    assert status(h)["decision"] == "REFUSE"


def test_activation_status_lapses_with_the_screening_the_controls_and_rule_expiry():
    h = harness()
    activate(h, "client-web")
    h.clock.at = OCT30 + timedelta(days=91)
    s = status(h)
    assert s["decision"] == "REFUSE" and any("re-screen" in x for x in s["reasons"])
    h2 = harness()
    activate(h2, "client-web")
    h2.clock.at = OCT30 + timedelta(hours=25)          # C-11 not re-run within its 24 h SLA
    assert status(h2)["decision"] == "REFUSE"
    h2.run_controls()
    assert status(h2)["decision"] == "ALLOW"
    h2.clock.at = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)
    h2.run_controls()
    s = status(h2)
    assert s["decision"] == "REFUSE" and any("US-OFAC-04" in x for x in s["reasons"])


def test_activation_status_needs_a_caller_and_bearer():
    h = harness()
    assert h.client.get("/compliance/v1/clients/c/activation").status_code == 401
    assert h.client.get("/compliance/v1/clients/c/activation",
                        headers={"Authorization": f"Bearer {H.SERVICE_TOKEN}"}).status_code == 403
    assert h.get("/compliance/v1/clients/bad%20id/activation", caller="finance_31").status_code == 422


# --- readiness: exactly which rules are expired and which gates they affect -----------------------------------------------

def test_readiness_names_expired_rules_their_gates_and_the_us_web_client_set():
    h = harness()
    r = h.get("/compliance/v1/readiness")
    assert r.status_code == 200, r.text
    j = r.json()
    expired = {e["id"]: e for e in j["expired"]}
    assert set(expired) == {"US-STRIPE-KYC", "PLT-YT-01", "PLT-YT-02", "PLT-TT-01", "PLT-TT-02", "PLT-TT-03",
                            "PLT-TT-04", "PLT-X-01", "PLT-X-02", "PLT-SNAP-01"}
    assert expired["PLT-TT-02"]["gates"] == ["activation", "payout", "publish"]
    assert all(e["blocks_us_web_client_activation"] is False for e in expired.values())
    web = j["us_web_client_activation"]
    assert web["blocking_now"] == []
    assert {"US-OFAC-04", "HR-05", "HR-07", "HR-14", "HR-15"} <= set(web["rows"])
    soon = {e["id"]: e for e in web["expiring"]}
    assert soon["US-OFAC-04"]["expires_at"] == "2026-12-25"
    assert set(j["expiring_within_60_days"]) >= {"US-OFAC-01", "US-OFAC-02", "US-OFAC-03", "US-OFAC-04"}
    assert j["gate_blockers"] == []


def test_readiness_before_the_seed_and_before_c11():
    clock = FixedClock(OCT30)
    h = Harness(ports=H.passing_ports(clock), clock=clock)
    j = h.get("/compliance/v1/readiness").json()
    assert j["register_version"] is None and "register_not_in_force" in j["gate_blockers"]
    h.approve_seed()
    j = h.get("/compliance/v1/readiness").json()
    assert any(b.startswith("control_red:C-11") for b in j["gate_blockers"])


def test_readiness_lines_are_printed_for_the_operator():
    h = harness()
    import readiness
    lines = readiness.summary_lines(h.svc.readiness())
    text = "\n".join(lines)
    assert "expired: 10 rule(s)" in text and "PLT-TT-02 (expired 2026-10-26; gates activation, payout, publish)" in text
    assert "US web client activation: not blocked by any rule" in text
    assert "US-OFAC-04 expires 2026-12-25" in text


# --- the daily scheduler --------------------------------------------------------------------------------------------------

def test_scheduler_runs_controls_then_opens_the_gates():
    clock = FixedClock(OCT30)
    ports = H.passing_ports(clock)
    ports.legal = PassingLegal()
    h = Harness(ports=ports, clock=clock)
    h.approve_seed()
    import scheduler
    lines: list[str] = []
    s = scheduler.Scheduler(h.svc, interval_s=21600, watcher=False, clock=clock, out=lines.append)
    assert s.due_controls() is True
    s.tick()
    assert s.due_controls() is False and any("controls run" in x for x in lines)
    assert activate(h, "client-sched")["allowed"] is True
    clock.at = OCT30 + timedelta(hours=6, seconds=1)
    assert s.due_controls() is True


def test_scheduler_runs_the_watcher_once_a_day_when_enabled():
    clock = FixedClock(OCT30)
    h = Harness(ports=H.passing_ports(clock), clock=clock, env={"COMPLIANCE_WATCHER_ENABLED": "1"})
    h.approve_seed()
    import scheduler
    s = scheduler.Scheduler(h.svc, interval_s=21600, watcher=True, clock=clock, out=lambda _x: None)
    assert s.due_watcher() is True
    s.tick()
    assert s.due_watcher() is False
    clock.at = OCT30 + timedelta(hours=24, seconds=1)
    assert s.due_watcher() is True


def test_scheduler_settings_fail_closed():
    import config
    env = H.base_env(COMPLIANCE_SCHEDULER_ENABLED="2")
    with pytest.raises(RuntimeError):
        config.load(env)
    with pytest.raises(RuntimeError):
        config.load(H.base_env(COMPLIANCE_SCHEDULER_INTERVAL_SECONDS="10"))
    s = config.load(H.base_env(COMPLIANCE_SCHEDULER_ENABLED="1"))
    assert s.scheduler_enabled is True and s.scheduler_interval_s == 21600


def test_the_nexus_thresholds_are_rule_parameters_with_sources():
    row = next(r for r in H.SEED_ROWS if r["id"] == "US-SALESTAX-NEXUS")
    p = row["parameters"]
    assert p["alert_ratio"] == "0.75" and p["default"] == {"sales_usd": "100000.00", "transactions": None}
    assert p["overrides"]["US-CA"]["sales_usd"] == "500000.00"
    for code in ("US-HI", "US-NV", "PR"):
        assert p["overrides"][code]["transactions"] == 200 and p["overrides"][code]["source"].startswith("https://")
    assert row["source_url"].startswith("https://") and row["status"] == "unverified" and row["gates"] == ["control"]


# --- Finance's reads: the sanctions status route (finding M1) -------------------------------------------------------

def test_sanctions_status_route_for_finance():
    h = harness()
    r = h.get("/compliance/v1/sanctions/status", caller="finance_31", subject_id="clipper-9", role="payee")
    assert r.status_code == 200 and r.json()["screen_id"] is None and r.json()["fresh"] is False
    s = h.screen("clipper-9")
    j = h.get("/compliance/v1/sanctions/status", caller="finance_31", subject_id="clipper-9", role="payee").json()
    assert j["screen_id"] == s["screen_id"] and j["result"] == "clear" and j["fresh"] is True and j["list_current"]
    assert h.get("/compliance/v1/sanctions/status", caller="seo_02", subject_id="clipper-9",
                 role="payee").status_code == 403


# --- the re-verification helper Andre runs (devtools/reverify_proposal.py) ---------------------------------------------

def test_the_reverify_helper_builds_a_proposal_the_register_accepts():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reverify_proposal", Path(__file__).resolve().parents[1] / "devtools" / "reverify_proposal.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    at = datetime(2026, 12, 20, 15, 0, tzinfo=timezone.utc)
    h = harness(at=at)
    row = h.get("/compliance/v1/register/US-OFAC-04").json()
    body = tool.build(row["row"], b"<html><p>Comprehensive sanctions: Cuba, Iran, North Korea</p></html>",
                      "Comprehensive sanctions: Cuba, Iran, North Korea", at)
    p = h.post("/compliance/v1/register/proposals", body, andre=ANDRE_TOKEN)
    assert p.status_code == 201, p.text
    h.approve(p.json()["proposal"])
    h.clock.at = datetime(2026, 12, 26, 12, 0, tzinfo=timezone.utc)
    h.run_controls()
    j = h.rule("client-after", "client", web_client(h, "client-after")).json()
    assert j["allowed"] is True, j["unmet_lines"]           # re-verified on Dec 20: in force until Mar 20, 2027
    assert h.get("/compliance/v1/register/US-OFAC-04").json()["row"]["expires_at"] == "2027-03-20"
    with pytest.raises(SystemExit):
        tool.build(next(r for r in H.SEED_ROWS if r["id"] == "HR-05"), b"x", "x", at)


# --- configuration -----------------------------------------------------------------------------------------------

def test_unresolved_references_and_partial_legal_settings_refuse_to_start():
    import config
    callers = dict(H.CALLERS)
    callers["seo_02"] = "${{seo.SEO_COMPLIANCE_CALLER_TOKEN}}"
    with pytest.raises(RuntimeError, match="unresolved"):
        config.load(H.base_env(COMPLIANCE_CALLER_TOKENS=json.dumps(callers)))
    with pytest.raises(RuntimeError, match="together"):
        config.load(H.base_env(COMPLIANCE_LEGAL_URL="http://legal.railway.internal:8420"))
    with pytest.raises(RuntimeError):
        config.load(H.base_env(COMPLIANCE_LEGAL_URL="http://evil.example:8420", COMPLIANCE_LEGAL_TOKEN="t" * 40,
                               COMPLIANCE_LEGAL_CALLER_TOKEN="c" * 40))
    s = config.load(H.base_env(COMPLIANCE_LEGAL_URL="http://legal.railway.internal:8420",
                               COMPLIANCE_LEGAL_TOKEN="t" * 40, COMPLIANCE_LEGAL_CALLER_TOKEN="c" * 40))
    assert "t" * 40 not in repr(s)
    import api
    from service import Ports
    svc = api.build_service(s, FixedClock(OCT30), Ports(), FakeLedgerClient())
    assert type(svc.ports.legal).__name__ == "HttpLegal37" and "t" * 40 not in repr(svc.ports.legal)
    svc.close()


# --- the Legal (37) thin client against legal-py's answer shapes ------------------------------------------------------

def _legal(current=None, acceptance=None, status=200, seen=None):
    import httpx
    from legal_client import HttpLegal37
    cur = {"doc_id": "client_msa", "available": True, "current_version": "1.0", "doc_sha256": "e" * 64,
           "effective_at": "2026-10-01T00:00:00Z", "review_by": "2027-10-01T00:00:00Z", "rules_pinned": True,
           "reason": "current"}
    acc = {"acceptance_id": MSA["acceptance_id"], "doc_id": "client_msa", "version": "1.0", "doc_sha256": "e" * 64,
           "accepted_at": "2026-10-29T12:00:00Z", "method": "clickwrap_unticked_box", "evidence_sufficient": True,
           "party_ref": "client:client-web", "rules_pinned": True}

    def handler(req):
        if seen is not None:
            seen.append(req)
        if status != 200:
            return httpx.Response(status, json={"detail": "x"})
        if req.url.path.endswith("/current"):
            return httpx.Response(200, json={**cur, **(current or {})})
        return httpx.Response(200, json={**acc, **(acceptance or {})})
    return HttpLegal37("http://legal.railway.internal:8420", "s" * 40, "c" * 40,
                       transport=httpx.MockTransport(handler), timeout=2)


def _ask(c):
    return c.client_document("client_msa", "1.0", "e" * 64, MSA["acceptance_id"], "client:client-web")


def test_legal_client_reads_current_version_and_acceptance():
    seen = []
    a = _ask(_legal(seen=seen))
    assert (a.available, a.current, a.accepted) == (True, True, True)
    assert [r.url.path for r in seen] == ["/legal/v1/documents/client_msa/current",
                                          f"/legal/v1/acceptances/{MSA['acceptance_id']}"]
    assert seen[0].headers["x-legal-caller-token"] == "c" * 40 and seen[0].headers["authorization"] == "Bearer " + "s" * 40
    a = _ask(_legal(current={"current_version": "1.1", "doc_sha256": "f" * 64}))
    assert (a.available, a.current, a.accepted) == (True, False, True)
    for acc in ({"party_ref": "client:someone-else"}, {"evidence_sufficient": False}, {"version": "0.9"}):
        a = _ask(_legal(acceptance=acc))
        assert a.available and not a.accepted, acc
    for bad in ({"rules_pinned": False}, {"available": False}, {"doc_id": "terms"}):
        assert _ask(_legal(current=bad)).available is False, bad
    assert _ask(_legal(acceptance={"rules_pinned": False})).available is False
    for status in (401, 403, 404, 500, 503):
        assert _ask(_legal(status=status)).available is False


def test_territories_report_their_own_code_and_are_not_foreign_payees():
    """Founder update (12:51): a US Virgin Islands client declared either way is reported as VI; a VI, AS or MP creator
    is not a foreign payee (no W-8 rows apply)."""
    h = harness()
    activate(h, "client-vi", country="US", region="US-VI", targets=("US",))
    assert status(h, "client-vi")["jurisdiction"] == "VI"
    activate(h, "client-mp", country="MP", region=None, targets=("MP",))
    assert status(h, "client-mp")["jurisdiction"] == "MP"
    from intelligences import i02_activation_gate as i02
    for c in ("VI", "AS", "MP", "PR", "GU"):
        ctx, _ = i02.build("zbc_creator", "clipper-x", H.creator_facts("scr-x", country=c, region=None),
                           h.svc.rows_by_id(), h.svc.seed_by_id, None, OCT30)
        assert ctx.flags["foreign_payee"] is False, c
