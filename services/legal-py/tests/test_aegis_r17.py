"""AEGIS round 17 findings on legal-py (N17-4, -5, -6, -7, -8, -14) and the N17-3 sweep of Legal's ports. Each
test failed on integration-2026-09-24 @ 680c289 (evidence in the fix-18 report) and passes after the fix."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from advice import default_guard
from builders import MSA_TEXT, MSA_VARS, executed_msa, playbook
from helpers import COUNSEL_REF, Harness, b64, rid, sha
from ports import ComplianceRow, Delivery, EnvelopeAnswer, ProposalAnswer

TESTS = Path(__file__).resolve().parent


def _accept(x, version, doc_sha, party, doc_id="client_msa", caller="onboarding", **over):
    body = {"request_id": rid("acc"), "party_ref": party, "signer_identity_ref": party.split(":", 1)[1],
            "doc_id": doc_id, "version": version, "doc_sha256": doc_sha, "presented_sha256": doc_sha,
            "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept", "affirmative_act": True, **over}
    return x.post("/legal/v1/acceptances", body, caller=caller)


def _approve_version(x, doc_id, version, v):
    memo = x.memo(cites={"doc_versions": [f"{doc_id}@{version}"]})
    x.ok(x.apost(f"/legal/v1/documents/{doc_id}/versions/{version}/counsel-signoff",
                 {"request_id": rid("so"), "counsel_ref": COUNSEL_REF, "signed_on": "2026-10-01",
                  "doc_sha256": v["sha256"], "memo_id": memo["memo_id"], "memo_sha256": memo["memo_sha256"]}))
    x.ok(x.apost(f"/legal/v1/documents/{doc_id}/versions/{version}/decision",
                 {"request_id": rid("ap"), "decision": "approve", "version_sha256": v["sha256"]}))


# ================================================================== N17-4 acceptance bound to the party's instance

def test_n17_4_a_party_cannot_accept_another_partys_filled_instance():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    f11, _ = executed_msa(x, client="acme")
    r = _accept(x, "1.1", f11["sha256"], "client:globex")
    assert r.status_code == 409 and r.json()["detail"] == "INSTANCE_NOT_BOUND", r.text
    assert x.ledger.of_type("acceptance_refused")
    assert all(a["party_ref"] == "client:acme" for a in x.svc.acceptances.values())


def test_n17_4_a_template_with_unfilled_placeholders_can_never_be_accepted():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    executed_msa(x, client="acme")
    tv = x.ok(x.get("/legal/v1/documents/client_msa/versions/1.0"))
    r = _accept(x, "1.0", tv["sha256"], "client:initech")
    assert r.status_code == 409 and r.json()["detail"] == "TEMPLATE_NOT_ACCEPTABLE"


def test_n17_4_a_fill_must_name_its_party_and_is_recorded_bound():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    playbook(x)
    x.approve_doc("client_msa", MSA_TEXT, "1.0", "zbm", [("MSA-RENEW-01", "standard")], MSA_VARS)
    r = x.post("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm",
               "variables": {"client_name": "Acme", "end_date": "2027-09-30"}}, caller="scheduler")
    assert r.status_code == 422
    f = x.fill("client_msa", {"client_name": "Acme", "end_date": "2027-09-30"}, "client:acme", expect="1.1")
    assert x.svc.doc_versions["client_msa@1.1"]["party_ref"] == "client:acme"
    assert x.svc.doc_versions["client_msa@1.1"]["unresolved_fields"] is False
    assert f["sha256"] != x.svc.doc_versions["client_msa@1.0"]["sha256"]


def test_n17_4_evidence_is_never_sufficient_for_an_unbound_instance():
    import intelligences.i03_acceptance as i03
    rec = {"party_ref": "client:a", "signer_identity_ref": "a", "doc_id": "client_msa", "version": "1.1",
           "doc_sha256": "a" * 64, "presented_sha256": "a" * 64, "accepted_at": "2026-10-01T00:00:00Z",
           "method": "clickwrap_unticked_box", "presentation": "inline", "affirmative_act": True}
    assert i03.clickwrap_sufficient(rec, True, False, True) is True
    assert i03.clickwrap_sufficient(rec, True, False, False) is False


def test_n17_4_a_standard_form_bound_to_no_party_is_accepted_by_any_party(he):
    v = he.approve_doc("clipper_agreement", "clipper agreement text")
    he.verify_cq("CQ-19")
    for who in ("clipper:c1", "clipper:c2"):
        a = he.clickwrap("clipper_agreement", "1.0", v["sha256"], party=who)
        assert a["evidence_sufficient"] is True


def test_n17_4_an_envelope_for_another_partys_instance_is_refused():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    executed_msa(x, client="acme")
    r = x.apost("/legal/v1/envelopes", {"request_id": rid(), "doc_id": "client_msa", "version": "1.1",
                                        "party_ref": "client:globex", "signer_refs": ["globex-signer"]})
    assert r.status_code == 409 and r.json()["detail"] == "INSTANCE_NOT_BOUND"


# ================================================================== N17-5 server-assigned version numbers

def test_n17_5_a_caller_cannot_freeze_a_document_by_burning_the_version_space():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    playbook(x)
    x.approve_doc("client_msa", MSA_TEXT, "1.0", "zbm", [("MSA-RENEW-01", "standard")], MSA_VARS)
    r = x.post("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "version": "9999.9999", "entity": "zbm",
               "party_ref": "client:acme", "variables": {"client_name": "Acme", "end_date": "2027-09-30"}},
               caller="scheduler")
    assert r.status_code == 422                                   # no caller-supplied number at all
    f = x.fill("client_msa", {"client_name": "Acme", "end_date": "2027-09-30"}, "client:acme")
    assert f["version"] == "1.1"
    up = x.upload("client_msa", "MASTER SERVICES AGREEMENT v2 for {{client_name}}. Term ends {{end_date}}.", "2.0",
                  "zbm", template_variables=MSA_VARS)
    assert up["version"] == "2.0"                                # Andre is never locked out


def test_n17_5_only_andre_uploads_a_document_version():
    x = Harness(wired=True)
    x.approve_rules()
    for caller in ("scheduler",):
        r = x.post("/legal/v1/documents/sow/versions", {"request_id": rid(), "entity": "zbm", "text": "SOW text"},
                   caller=caller)
        assert r.status_code == 403
    for caller in ("onboarding", "hub", "clipper_network"):
        r = x.post("/legal/v1/documents/sow/versions", {"request_id": rid(), "entity": "zbm", "text": "SOW text"},
                   caller=caller)
        assert r.status_code == 403
    assert x.svc.doc_versions == {}


def test_n17_5_numbers_are_monotonic_and_never_reused_after_a_withdrawal(he):
    a = he.upload("sow", "one", "1.0", "zbm")
    he.ok(he.apost("/legal/v1/documents/sow/versions/1.0/decision", {"request_id": rid(), "decision": "withdraw",
                                                                      "version_sha256": a["sha256"]}))
    assert he.upload("sow", "two", "1.1", "zbm")["version"] == "1.1"
    assert he.upload("sow", "three", "2.0", "zbm")["version"] == "2.0"
    assert he.upload("sow", "four", "2.1", "zbm")["version"] == "2.1"


# ================================================================== N17-6 IP addresses anywhere

IPS = ["10.0.0.1", "fe80::1", "client:192.168.1.1", "ip-10-0-0-1", "r-10.0.0.1", "10.0.0.1:8080", "[2001:db8::1]:443",
       "client%3A192%2E168%2E1%2E1", "::ffff:1.2.3.4", "2001:0db8:85a3:0000:0000:8a2e:0370:7334"]


@pytest.mark.parametrize("value", IPS)
def test_n17_6_an_ip_address_in_any_string_field_of_a_write_route_is_422(he, value):
    v = he.approve_doc("clipper_agreement", "clipper agreement text")
    base = {"request_id": rid("acc"), "party_ref": "clipper:c1", "signer_identity_ref": "c1",
            "doc_id": "clipper_agreement", "version": "1.0", "doc_sha256": v["sha256"], "presented_sha256": v["sha256"],
            "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept", "affirmative_act": True}
    for field in ("signer_identity_ref", "request_id"):
        r = he.post("/legal/v1/acceptances", {**base, field: value}, caller="clipper_network")
        assert r.status_code == 422, (field, value, r.text)
    r = he.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "label-1",
                                       "kind": "question", "subject_refs": [f"x:{value}"]}, caller="hub")
    assert r.status_code == 422
    assert he.svc.acceptances == {}


def test_n17_6_timestamps_versions_and_ids_are_not_ips(he):
    v = he.approve_doc("clipper_agreement", "clipper agreement text 2026-10-01T17:00:00Z v1.2")
    assert v["version"] == "1.0"


# ================================================================== N17-7 advice: structure first, phrases second

MISSED_R17 = ["Usted debe firmar este contrato", "Sie müssen den Vertrag unterschreiben", "Vous devez signer",
              "你应该签署这份合同", "If I were you I would file a counter-notice", "It would be wise to file a counter-notice",
              "Best to sign right away", "Sign it now.", "File a counter-notice within 10 days.",
              "Your best option is to file a counter-notice", "The clause will not hold up in court",
              "One should sign this", "Clients should sign this", "The creator should file a counter-notice",
              "You'd be smart to sign", "It is advisable that the clipper responds", "You will want to sign before Friday",
              "Recommendation: sign the agreement", "we'd suggest you sign", "it's in your interest to sign",
              "You are within your rights to refuse", "There is no legal risk in signing",
              "This clause is perfectly fine to accept", "Accepting these terms is safe", "Counter-notice recommended",
              "A counter-notice is the right call", "you might want to sign", "Consider filing a counter-notice",
              "I'd recommend filing", "Ignore the takedown"]


@pytest.mark.parametrize("phrase", MISSED_R17)
def test_n17_7_the_round_17_phrasings_are_blocked(phrase):
    assert default_guard().scan(phrase), phrase


def test_n17_7_a_sow_fill_needs_counsel_signoff_on_its_exact_hash_no_sow_exception():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    x.approve_doc("sow", "STATEMENT OF WORK. Scope: {{scope}}", "1.0", "zbm", [],
                  {"scope": {"type": "string", "max_length": 300}})
    f = x.fill("sow", {"scope": "twelve short clips"}, "client:acme", expect="1.1")
    r = x.apost("/legal/v1/documents/sow/versions/1.1/decision", {"request_id": rid(), "decision": "approve",
                                                                  "version_sha256": f["sha256"]})
    assert r.status_code == 409 and r.json()["detail"] == "COUNSEL_RECORD_MISSING"
    assert x.ok(x.get("/legal/v1/documents/sow/current"))["current_version"] == "1.0"


def test_n17_7_non_andre_answers_never_echo_caller_free_text(he):
    marker = "ZZQMARK you should sign this now"
    he.approve_doc("sow", "STATEMENT OF WORK. Scope: {{scope}}", "1.0", "zbm", [],
                   {"scope": {"type": "string", "max_length": 300}})
    answers = []
    answers.append(he.post("/legal/v1/documents/sow/versions", {"request_id": rid(), "entity": "zbm",
                                                                "party_ref": "client:acme",
                                                                "variables": {"scope": marker}}, caller="scheduler"))
    answers.append(he.post("/legal/v1/documents/sow/versions", {"request_id": rid(), "entity": "zbm",
                                                                "party_ref": "client:acme",
                                                                "variables": {"scope": "ZZQMARK twelve clips"}},
                           caller="scheduler"))
    answers.append(he.post("/legal/v1/signoffs", {"request_id": rid(), "topic": marker, "subject_id": "c1",
                                                  "facts": {"x": marker}}, caller="creative_production"))
    answers.append(he.post("/legal/v1/playbooks/client_msa/reviews",
                           {"request_id": rid(), "counterparty_paper_text": marker,
                            "counterparty_positions": [{"clause_id": "MSA-PAY-01", "text": marker}],
                            "facts": {}}, caller="onboarding"))
    answers.append(he.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "ZZQMARK",
                                                  "kind": "question"}, caller="hub"))
    for r in answers:
        assert "ZZQMARK" not in r.text, r.text[:400]
    for path in ("/legal/v1/documents/sow", "/legal/v1/documents/sow/versions/1.1", "/legal/v1/register",
                 "/legal/v1/obligations"):
        r = he.get(path)
        assert r.status_code == 200 and "ZZQMARK" not in r.text, path


# ================================================================== N17-8 memo -> Compliance proposal

def test_n17_8_memo_id_exists_before_the_row_and_the_row_must_carry_its_urn(he):
    m = he.memo(cites={"cq_ids": ["CQ-11"]}, answers=[{"cq_id": "CQ-11", "resolution": "verified_rule",
                                                        "quoted_excerpt": "Counsel answers CQ-11."}])
    assert m["proposals"] == [] and he.compliance.proposals == []
    bad = he.apost(f"/legal/v1/memos/{m['memo_id']}/compliance-proposals", {"request_id": rid(), "proposals": [
        {"kind": "supersede", "target_id": "CQ-11", "quoted_excerpt": "q",
         "proposed_row": {"id": "CQ-11-M", "source_url": f"legal37://memos/{m['memo_id']}"}}]})
    assert bad.status_code == 422 and he.compliance.proposals == []
    ok = he.memo_proposals(m["memo_id"], [{"kind": "supersede", "target_id": "CQ-11", "quoted_excerpt": "q",
                                           "proposed_row": {"id": "CQ-11-M",
                                                            "source_url": f"urn:legal37:memos:{m['memo_id']}"}}])
    assert ok["proposals"][0]["status"] == "delivered"
    (_rid, body), = he.compliance.proposals
    assert body["evidence"]["source_url"] == f"urn:legal37:memos:{m['memo_id']}"
    assert body["proposed_row"]["source_url"] == f"urn:legal37:memos:{m['memo_id']}"
    r = he.apost(f"/legal/v1/memos/{m['memo_id']}/compliance-proposals", {"request_id": rid(), "proposals": [
        {"kind": "supersede", "target_id": "CQ-19", "quoted_excerpt": "q",
         "proposed_row": {"id": "CQ-19-M", "source_url": f"urn:legal37:memos:{m['memo_id']}"}}]})
    assert r.status_code == 409 and r.json()["detail"] == "MEMO_DOES_NOT_CITE"


def test_n17_8_memo_proposal_end_to_end_against_the_real_compliance_py():
    p = subprocess.run([sys.executable, str(TESTS / "contract_compliance_real.py")], capture_output=True, text=True,
                       timeout=300)
    assert p.returncode == 0, (p.stdout[-2000:], p.stderr[-3000:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["legal_proposal"]["status"] == "delivered" and out["cq11_after"] == "verified"


# ================================================================== N17-14 blob subject refs grow

def test_n17_14_a_hold_on_the_second_writer_of_the_same_bytes_blocks_retention(he):
    v = he.approve_doc("clipper_agreement", "clipper agreement text")
    he.verify_cq("CQ-19")
    ev = b"the same session record"
    for who in ("cn-clp-Y", "cn-clp-X"):                         # Y writes the blob first, X second
        he.clickwrap("clipper_agreement", "1.0", v["sha256"], party=f"clipper:{who}",
                     evidence_ref={"kind": "session_ref", "sha256": sha(ev), "content_b64": b64(ev)})
    assert {"clipper:cn-clp-X", "clipper:cn-clp-Y"} <= set(he.svc.blob_meta[sha(ev)]["subject_refs"])
    he.ok(he.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "label-1",
                                         "kind": "demand_letter", "subject_refs": ["clipper:cn-clp-X"]}, caller="hub"), 201)
    he.memo(cites={"retention_classes": ["acceptance_records"]}, retention_periods={"acceptance_records": "P1D"})
    he.clock.advance(days=2)
    s = he.job("retention")["summary"]
    assert s["blocked_by_hold"] >= 1
    assert he.svc.blobs.get(sha(ev)) is not None                 # held through its SECOND subject


def test_n17_14_refs_and_classes_survive_a_restart(tmp_path):
    d = str(tmp_path / "d")
    x = Harness(data_dir=d, wired=True)
    x.approve_rules()
    x.engage()
    v = x.approve_doc("clipper_agreement", "clipper agreement text")
    x.verify_cq("CQ-19")
    ev = b"shared bytes"
    for who in ("a", "b"):
        x.clickwrap("clipper_agreement", "1.0", v["sha256"], party=f"clipper:{who}",
                    evidence_ref={"kind": "session_ref", "sha256": sha(ev), "content_b64": b64(ev)})
    y = Harness(data_dir=d, ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert {"clipper:a", "clipper:b"} <= set(y.svc.blob_meta[sha(ev)]["subject_refs"])


# ================================================================== N17-3 swept into Legal's ports

@pytest.mark.parametrize("bad", [ProposalAnswer("created", ["p"], 201), ProposalAnswer("created", "p", "201"),
                                 ProposalAnswer("maybe", "p", 201), ProposalAnswer(True, None, None), "created"])
def test_n17_3_malformed_compliance_answer_is_never_created(he, bad):
    he.compliance.create_proposal = lambda request_id, body: bad
    m = he.memo(cites={"cq_ids": ["CQ-11"]}, answers=[{"cq_id": "CQ-11", "resolution": "verified_rule",
                                                        "quoted_excerpt": "q"}])
    out = he.memo_proposals(m["memo_id"], [{"kind": "supersede", "target_id": "CQ-11", "quoted_excerpt": "q",
                                            "proposed_row": {"id": "CQ-11-M",
                                                             "source_url": f"urn:legal37:memos:{m['memo_id']}"}}])
    assert out["proposals"][0]["status"] == "pending_delivery"
    assert he.ledger.of_type("adapter_answer_refused")


def test_n17_3_malformed_row_answer_never_verifies_a_question(he):
    m = he.memo(cites={"cq_ids": ["CQ-11"]}, answers=[{"cq_id": "CQ-11", "resolution": "verified_rule",
                                                        "quoted_excerpt": "q"}])
    he.memo_proposals(m["memo_id"], [{"kind": "supersede", "target_id": "CQ-11", "quoted_excerpt": "q",
                                      "proposed_row": {"id": "CQ-11-M",
                                                       "source_url": f"urn:legal37:memos:{m['memo_id']}"}}])
    he.compliance.row = lambda oid: ComplianceRow(True, oid, ["verified"], 2)
    he.clock.advance(days=1)
    assert he.job("proposal-delivery")["summary"]["confirmed"] == 0
    assert he.ok(he.get("/legal/v1/register/CQ-11"))["status"] == "unverified"


def test_n17_3_malformed_port_answers_are_refused_and_recorded():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    x.ports.counsel.deliver = lambda package: Delivery("yes")
    x.ports.esign.create_envelope = lambda *a: EnvelopeAnswer(True, ["env"])
    x.upload("clipper_agreement", "draft")
    r = x.ok(x.apost("/legal/v1/documents/clipper_agreement/versions/1.0/counsel-review",
                     {"request_id": rid(), "question_text": "Is the clause list complete?"}))
    assert r["delivered"] is False
    v = x.approve_doc("clipper_agreement", "clipper agreement text v2", "1.1")
    e = x.ok(x.apost("/legal/v1/envelopes", {"request_id": rid(), "doc_id": "clipper_agreement", "version": "1.1",
                                             "party_ref": "clipper:c1", "signer_refs": ["c1"]}))
    assert e["created"] is False
    kinds = {ev["payload"]["port"] for ev in x.ledger.of_type("adapter_answer_refused")}
    assert {"counsel_channel", "esign_provider"} <= kinds
    _ = v


# ================================================================== wave-17 class sweep: huge / drip / malformed thin-client answers

def test_fix18_compliance_client_huge_drip_and_malformed_answers_are_never_created_or_verified():
    import time
    import httpx
    from compliance_client import HttpCompliance

    def client(handler, timeout=10.0):
        return HttpCompliance("http://c.test", "s", "c", transport=httpx.MockTransport(handler), timeout=timeout)
    huge = b'{"proposal": {"proposal_id": "p"}, "pad": "' + b"x" * (1024 * 1024 + 10) + b'"}'
    assert client(lambda r: httpx.Response(201, content=huge)).create_proposal("r", {}).status == "unavailable"

    def drip(req):
        def gen():
            for _ in range(40):
                time.sleep(0.05)
                yield b" "
        return httpx.Response(201, content=gen())
    t0 = time.monotonic()
    assert client(drip, timeout=0.5).create_proposal("r", {}).status == "unavailable"
    assert time.monotonic() - t0 < 1.5
    for bad in ({"register_version": True, "row": {"id": "CQ-11-M", "effective_status": "verified"}},
                {"register_version": 3, "row": {"id": "CQ-11-M", "effective_status": ["verified"]}},
                {"register_version": 3, "row": {"id": ["CQ-11-M"], "effective_status": "verified"}},
                {"register_version": 3, "row": "verified"}):
        assert client(lambda r, b=bad: httpx.Response(200, json=b)).row("CQ-11-M").available is False
    for bad in ({"proposal": {"proposal_id": ["p"]}}, {"proposal": {"proposal_id": 7}}, {"proposal": ["p"]}):
        assert client(lambda r, b=bad: httpx.Response(201, json=b)).create_proposal("r", {}).status == "unavailable"
