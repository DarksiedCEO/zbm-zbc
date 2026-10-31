"""ADR 0022 (Andre's ruling, 2026-10-30): client commercial contracts -- founder approval (rule LG-20).

- client_msa, the SOWs and zbc_order_form: Andre's token approves a version on its hash with a written rationale;
  the version says ``approval_basis: founder_approval`` (view, ledger, audit export). The existing memo sign-off path
  is unchanged.
- CQ-19: Andre's founder determination makes a clickwrap acceptance of THOSE documents evidence-sufficient; the
  acceptance says ``evidence_basis: founder_determination``; the row's own status and memo list are untouched.
- Unchanged: every other document (clipper/creator, IC, NDA, privacy, terms, DMCA, engagement letter, ...), every
  other CQ row, the hash/version/party/affirmative-act checks on an acceptance, and who may call what.
"""

from __future__ import annotations

import json

import pytest

import config as config_mod
import rules as RU
from builders import terms
from helpers import ANDRE_TOKEN, CALLERS, Harness, rid

RATIONALE = ("Founder approval (ADR 0022): ZBM's standard client MSA for SEO audits and Revenue Recovery; "
             "approved by me as founder.")
CQ19_RATIONALE = ("Founder determination (ADR 0022): B2B clients accept the MSA/SOW by ticking an unticked box "
                  "next to the full text; we keep the exact text hash, time and signer reference.")
CLIENT = "client:zbm-client-1"
NON_CLIENT_DOCS = ("clipper_agreement", "ic_agreement", "nda_mutual", "nda_oneway", "privacy_policy", "terms",
                   "cookie_notice", "dmca_procedure", "engagement_letter", "dpa", "not_legal_advice_v1")


def founder_approve(h, doc_id, version, sha, code=200, rationale=RATIONALE, andre=ANDRE_TOKEN, **over):
    body = {"request_id": rid("fap"), "version_sha256": sha, "rationale": rationale,
            "founder_confirms": True, **over}
    r = h.post(f"/legal/v1/documents/{doc_id}/versions/{version}/founder-approval", body, andre=andre)
    return h.ok(r, code)


def determine_cq19(h, code=200, cq="CQ-19", andre=ANDRE_TOKEN, **over):
    body = {"request_id": rid("fdt"), "rationale": CQ19_RATIONALE, "founder_confirms": True, **over}
    return h.ok(h.post(f"/legal/v1/register/{cq}/founder-determination", body, andre=andre), code)


def client_msa(h, text="ZBM client MSA v1 (standard form, no placeholders)", version="1.0"):
    v = h.upload("client_msa", text, version, "zbm")
    founder_approve(h, "client_msa", version, v["sha256"])
    return v


@pytest.fixture
def hr():
    x = Harness()
    x.approve_rules()
    return x


# --- 1. founder approval makes a client contract version current -------------------------------------------------

def test_founder_approval_makes_a_client_msa_version_current_marked_founder_approval(hr):
    v = hr.upload("client_msa", "ZBM client MSA v1", "1.0", "zbm")
    cur = hr.ok(hr.get("/legal/v1/documents/client_msa/current"))
    assert cur["current_version"] is None                   # before: nothing current
    out = founder_approve(hr, "client_msa", "1.0", v["sha256"])
    assert out["status"] == "approved" and out["approval_basis"] == "founder_approval"
    assert out["review_label"] == "founder_approval" and out["approved_by"] == "andre"
    cur = hr.ok(hr.get("/legal/v1/documents/client_msa/current"))
    assert cur["current_version"] == "1.0" and cur["doc_sha256"] == v["sha256"]
    view = hr.ok(hr.get("/legal/v1/documents/client_msa/versions/1.0", caller="finance_31"))
    assert view["approval_basis"] == "founder_approval" and len(view["founder_rationale_sha256"]) == 64
    doc = hr.ok(hr.get("/legal/v1/documents/client_msa"))
    assert doc["founder_approval_allowed"] is True
    ev = hr.ledger.of_type("document_version_founder_approved")
    assert len(ev) == 1 and ev[0]["payload"]["approval_basis"] == "founder_approval" and ev[0]["actor"] == "andre"
    assert ev[0]["payload"]["approved_by"] == "andre"


@pytest.mark.parametrize("doc_id,entity", [("sow", "zbm"), ("sow_seo_audit", "zbm"), ("sow_revenue_recovery", "zbm"),
                                           ("zbc_order_form", "zbc")])
def test_sow_and_zbc_order_form_take_the_founder_path_too(hr, doc_id, entity):
    v = hr.upload(doc_id, f"{doc_id} standard text v1", "1.0", entity)
    founder_approve(hr, doc_id, "1.0", v["sha256"])
    assert hr.ok(hr.get(f"/legal/v1/documents/{doc_id}/current"))["current_version"] == "1.0"


def test_the_normal_decision_route_is_unchanged_for_client_contracts(hr):
    v = hr.upload("client_msa", "ZBM client MSA v1", "1.0", "zbm")
    r = hr.apost("/legal/v1/documents/client_msa/versions/1.0/decision",
                 {"request_id": rid(), "decision": "approve", "version_sha256": v["sha256"]})
    assert r.status_code == 409 and r.json()["reasons"][0]["rule_id"] == "LG-02"


def test_founder_approval_needs_the_exact_hash_a_rationale_and_the_acknowledgment(hr):
    v = hr.upload("client_msa", "ZBM client MSA v1", "1.0", "zbm")
    r = founder_approve(hr, "client_msa", "1.0", "0" * 64, code=409)
    assert r["reasons"][0]["code"] == "HASH_MISMATCH"
    founder_approve(hr, "client_msa", "1.0", v["sha256"], code=422, rationale="ok")
    body = {"request_id": rid(), "version_sha256": v["sha256"], "rationale": RATIONALE}
    assert hr.apost("/legal/v1/documents/client_msa/versions/1.0/founder-approval", body).status_code == 422
    body["founder_confirms"] = False
    assert hr.apost("/legal/v1/documents/client_msa/versions/1.0/founder-approval", body).status_code == 422
    assert hr.ok(hr.get("/legal/v1/documents/client_msa/current"))["current_version"] is None


@pytest.mark.parametrize("doc_id", NON_CLIENT_DOCS)
def test_every_other_document_keeps_its_existing_gate(hr, doc_id):
    d = hr.ok(hr.get(f"/legal/v1/documents/{doc_id}"))
    assert d["founder_approval_allowed"] is False
    v = hr.upload(doc_id, f"{doc_id} text v1", "1.0", d["entities"][0],
                  [("ENG-AI-01", "standard")] if doc_id == "engagement_letter" else ())
    r = founder_approve(hr, doc_id, "1.0", v["sha256"], code=409)
    assert "FOUNDER_PATH_NOT_ALLOWED" in [x["code"] for x in r["reasons"]]
    assert all(x["rule_id"] == "LG-20" for x in r["reasons"] if x["code"] == "FOUNDER_PATH_NOT_ALLOWED")
    assert hr.ok(hr.get(f"/legal/v1/documents/{doc_id}/current"))["current_version"] is None


# --- 2. CQ-19: founder determination makes client clickwrap sufficient -----------------------------------------

def test_cq19_founder_determination_makes_a_client_clickwrap_sufficient(hr):
    v = client_msa(hr)
    # before: no acceptance link is issued at all while a clickwrap acceptance would be insufficient (ADR 0028)
    r1 = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT, code=409)
    assert "EVIDENCE_INSUFFICIENT" in [x["code"] for x in r1["reasons"]]
    out = determine_cq19(hr)
    assert out["founder_determination"]["status"] == "active" and out["founder_determination"]["renew_by"]
    a2 = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    assert a2["evidence_sufficient"] is True and a2["evidence_basis"] == "founder_determination"
    row = hr.ok(hr.get("/legal/v1/register/CQ-19"))
    assert row["status"] == "unverified" and row["memo_ids"] == []                  # the row itself is untouched
    assert row["founder_determination"]["status"] == "active"
    assert row["founder_determination"]["scope_doc_ids"] == ["client_msa", "sow", "sow_revenue_recovery",
                                                         "sow_seo_audit", "zbc_order_form"]
    read = hr.ok(hr.get(f"/legal/v1/acceptances/{a2['acceptance_id']}", caller="finance_31"))
    assert read["evidence_sufficient"] is True and read["evidence_basis"] == "founder_determination"
    ev = hr.ledger.of_type("register_row_founder_determined")
    assert len(ev) == 1 and ev[0]["actor"] == "andre" and ev[0]["payload"]["rule_id"] == "LG-20"


def test_cq19_determination_does_not_reach_other_documents(he):
    v = he.approve_doc("clipper_agreement", "ZBC clipper agreement v1.0 text")      # the existing path
    determine_cq19(he)
    a = he.clickwrap("clipper_agreement", "1.0", v["sha256"], party="clipper:cn-clp-1")
    assert a["evidence_sufficient"] is False and a["reasons"][0]["cq_id"] == "CQ-19"


def test_only_cq19_takes_a_founder_determination(hr):
    r = determine_cq19(hr, code=409, cq="CQ-20")
    assert r["reasons"][0]["code"] == "FOUNDER_PATH_NOT_ALLOWED"
    assert hr.ok(hr.get("/legal/v1/register/CQ-20"))["founder_determination"] is None


def test_acceptance_still_needs_the_exact_hash_version_affirmative_act_and_its_own_instance(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    r = hr.client_page_accept("client_msa", "1.0", "a" * 64, party=CLIENT, code=409)
    assert "ACCEPTANCE_HASH_MISMATCH" in [x["code"] for x in r["reasons"]]
    r = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT, code=409,
                     presented_sha256="b" * 64)
    assert "PRESENTED_TEXT_MISMATCH" in [x["code"] for x in r["reasons"]]
    r = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT, code=409,
                     affirmative_act=False)
    assert "AFFIRMATIVE_ACT_MISSING" in [x["code"] for x in r["reasons"]]
    d = hr.upload("client_msa", "ZBM client MSA v1.1 draft", "1.1", "zbm")             # not approved
    r = hr.client_page_accept("client_msa", "1.1", d["sha256"], party=CLIENT, code=409)
    assert "VERSION_NOT_IN_FORCE" in [x["code"] for x in r["reasons"]]
    p = hr.upload("client_msa", "ZBM client MSA for client 2", "1.2", "zbm", party_ref="client:zbm-client-2")
    founder_approve(hr, "client_msa", "1.2", p["sha256"])
    r = hr.client_page_accept("client_msa", "1.2", p["sha256"], party=CLIENT, code=409)
    assert "INSTANCE_NOT_BOUND" in [x["code"] for x in r["reasons"]]


def test_compliance_invalidating_cq19_also_ends_the_founder_determination(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    hr.ok(hr.post("/legal/v1/register/CQ-19/invalidate", {"request_id": rid(), "source_ref": "urn:x:1",
                                                          "detected_change_sha256": "c" * 64}, caller="compliance_38"))
    assert hr.ok(hr.get("/legal/v1/register/CQ-19"))["founder_determination"]["status"] == "invalidated"
    r = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT, code=409)
    assert "EVIDENCE_INSUFFICIENT" in [x["code"] for x in r["reasons"]]             # ADR 0028: no link is issued


def test_the_determination_expires_with_the_memo_review_period(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    hr.clock.advance(days=hr.svc.cfg.memo_review_days)
    assert hr.ok(hr.get("/legal/v1/register/CQ-19"))["founder_determination"]["status"] == "expired"
    _ = v


# --- 3. nobody but Andre ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("who", list(config_mod.CALLER_NAMES) + ["bearer_only", "wrong_andre"])
def test_no_caller_but_andre_can_approve_as_founder_or_determine_cq19(hr, who):
    v = hr.upload("client_msa", "ZBM client MSA v1", "1.0", "zbm")
    fa = {"request_id": rid(), "version_sha256": v["sha256"], "rationale": RATIONALE,
          "founder_confirms": True}
    fd = {"request_id": rid(), "rationale": CQ19_RATIONALE, "founder_confirms": True}
    for path, body in (("/legal/v1/documents/client_msa/versions/1.0/founder-approval", fa),
                       ("/legal/v1/register/CQ-19/founder-determination", fd)):
        if who == "bearer_only":
            r = hr.post(path, body)
        elif who == "wrong_andre":
            r = hr.post(path, body, andre="not-andre-" + "x" * 40)
        else:
            r = hr.post(path, body, caller=who)
            assert hr.post(path, body, andre=CALLERS[who]).status_code == 403     # a caller token is never Andre
        assert r.status_code == 403, (who, path, r.status_code)
    assert hr.ok(hr.get("/legal/v1/documents/client_msa/current"))["current_version"] is None
    assert hr.ok(hr.get("/legal/v1/register/CQ-19"))["founder_determination"] is None
    assert hr.ledger.of_type("founder_approval_refused")


# --- 4. the existing memo sign-off path still works -------------------------------------------------------------------

def test_the_memo_signoff_path_still_works(he):
    v = he.approve_doc("client_msa", "ZBM client MSA v1 via the memo path", "1.0", "zbm")
    view = he.ok(he.get("/legal/v1/documents/client_msa/versions/1.0"))
    assert view["status"] == "approved" and "approval_basis" not in view
    assert view["review_label"] != "founder_approval"
    he.verify_cq("CQ-19")
    a = he.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    assert a["evidence_sufficient"] is True and a["evidence_basis"] == "memo"


# --- 5. exports, the ledger and the local log record the founder approval; no rationale text leaves the blob store ---

def test_exports_record_the_founder_approval_and_never_the_rationale_text(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    recs, cur = [], 0
    while cur is not None:
        page = hr.ok(hr.get("/legal/v1/audit/export", cursor=cur))
        recs += page["records"]
        cur = page["next_cursor"]
    upd = [r["record"] for e in recs for r in e["records"]
           if r["kind"] == "update" and r["record"]["coll"] == "doc_versions"
           and r["record"]["fields"].get("approval_basis") == "founder_approval"]
    assert len(upd) == 1 and upd[0]["fields"]["approved_by"] == "andre"
    assert upd[0]["key"] == "client_msa@1.0" and upd[0]["fields"]["founder_rationale_sha256"]
    reg = [r["record"] for e in recs for r in e["records"]
           if r["kind"] == "update" and r["record"]["coll"] == "register" and r["record"]["key"] == "CQ-19"]
    assert reg[-1]["fields"]["founder_determination"]["status"] == "active"
    text = hr.all_text()
    assert RATIONALE not in text and CQ19_RATIONALE not in text
    assert json.dumps(v["sha256"]) in text


# --- 6. LG-20 must be in the rule version in force ------------------------------------------------------------------

def _version_without_lg20(h):
    """What a Legal approved before ADR 0022 holds: a rule version in force without LG-20."""
    cur = h.svc.current
    rows = tuple(r for r in cur.rows if r["rule_id"] != "LG-20")
    h.svc.versions[-1] = RU.Version(cur.version, cur.created_at, cur.approved_by, cur.proposal_ids,
                                    RU.rows_sha256(list(rows)), cur.prev_version_sha256, rows)


def test_a_legal_approved_before_adr_0022_opens_the_path_only_after_andre_adds_lg20(hr):
    _version_without_lg20(hr)
    v = hr.upload("client_msa", "ZBM client MSA v1", "1.0", "zbm")
    r = founder_approve(hr, "client_msa", "1.0", v["sha256"], code=409)
    assert [x["code"] for x in r["reasons"]] == ["FOUNDER_PATH_NOT_IN_FORCE"]
    assert determine_cq19(hr, code=409)["reasons"][0]["code"] == "FOUNDER_PATH_NOT_IN_FORCE"
    row = dict(hr.svc.seed_by_id["LG-20"])
    p = hr.ok(hr.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "add", "proposed_row": row}), 201)
    assert p["proposal"]["weakening"] is False
    hr.ok(hr.apost("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal"]["proposal_id"], "content_sha256": p["proposal"]["content_sha256"],
         "decision": "approve"}]}))
    founder_approve(hr, "client_msa", "1.0", v["sha256"])
    determine_cq19(hr)


def test_andre_can_close_the_path_by_marking_lg20_unverified(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    row = {**hr.svc.current.by_id()["LG-20"], "status": "unverified"}
    p = hr.ok(hr.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "LG-20",
                                                     "proposed_row": row}), 201)
    assert p["proposal"]["weakening"] is False                                      # closing tightens
    hr.ok(hr.apost("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal"]["proposal_id"], "content_sha256": p["proposal"]["content_sha256"],
         "decision": "approve"}]}))
    # AEGIS M2: closing LG-20 also takes the founder-approved version out of force, so no new acceptance is recorded
    r = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT, code=409)
    assert "VERSION_NOT_IN_FORCE" in [x["code"] for x in r["reasons"]]
    assert hr.ok(hr.get("/legal/v1/register/CQ-19"))["founder_determination"]["status"] == "lg20_not_in_force"
    w = hr.upload("client_msa", "ZBM client MSA v1.1", "1.1", "zbm")
    assert founder_approve(hr, "client_msa", "1.1", w["sha256"], code=409)["reasons"][0]["code"] == \
        "FOUNDER_PATH_NOT_IN_FORCE"


def test_lg20_is_founder_locked_and_cannot_be_reworded(hr):
    row = {**hr.svc.current.by_id()["LG-20"], "statement": "Andre may approve any document."}
    r = hr.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "LG-20",
                                               "proposed_row": row})
    assert r.status_code == 422 and "founder-locked" in r.text


def test_a_restart_replays_the_founder_records(tmp_path):
    x = Harness(data_dir=str(tmp_path))
    x.approve_rules()
    v = client_msa(x)
    determine_cq19(x)
    y = Harness(data_dir=str(tmp_path), ledger=x.ledger, clock=x.clock)
    view = y.ok(y.get("/legal/v1/documents/client_msa/versions/1.0"))
    assert view["approval_basis"] == "founder_approval"
    a = y.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    assert a["evidence_basis"] == "founder_determination"


# --- 7. contract drafter's requirements (Oct 30): two SOW templates, filled SOWs, renewal, IP, [BRACKETS] -----------

SOW_VARS = {"client_legal_name": {"type": "string", "max_length": 200}, "fee_usd": {"type": "string", "max_length": 20}}


def sow_template(h, doc_id, label):
    t = h.upload(doc_id, f"{label} SOW for {{{{client_legal_name}}}}; fee {{{{fee_usd}}}} USD.", "1.0", "zbm",
                 template_variables=SOW_VARS)
    founder_approve(h, doc_id, "1.0", t["sha256"])
    return t


def andre_fill(h, doc_id, party, code=201, **values):
    body = {"request_id": rid("fill"), "entity": "zbm", "party_ref": party,
            "variables": {"client_legal_name": "Acme LLC", "fee_usd": "1500.00", **values}}
    return h.ok(h.apost(f"/legal/v1/documents/{doc_id}/versions", body), code)


def test_both_sow_templates_are_live_at_once_and_each_client_fill_is_founder_approved_then_accepted(hr):
    sow_template(hr, "sow_seo_audit", "SEO audit")
    sow_template(hr, "sow_revenue_recovery", "Revenue Recovery")
    for doc_id in ("sow_seo_audit", "sow_revenue_recovery"):
        assert hr.ok(hr.get(f"/legal/v1/documents/{doc_id}/current"))["current_version"] == "1.0"
    determine_cq19(hr)
    for doc_id in ("sow_seo_audit", "sow_revenue_recovery"):
        f = andre_fill(hr, doc_id, CLIENT)
        assert f["version"] == "1.1" and f["status"] == "draft"
        assert f["review_label"] == f"founder_template:{doc_id}@1.0"
        # a fill is not acceptable until Andre approves THAT client's values (lightweight founder approval per fill)
        r = hr.client_page_accept(doc_id, "1.1", f["sha256"], party=CLIENT, code=409)
        assert "VERSION_NOT_IN_FORCE" in [x["code"] for x in r["reasons"]]
        founder_approve(hr, doc_id, "1.1", f["sha256"], rationale="Acme LLC SOW at 1500.00 per the approved template.")
        a = hr.client_page_accept(doc_id, "1.1", f["sha256"], party=CLIENT)
        assert a["evidence_sufficient"] is True and a["evidence_basis"] == "founder_determination"
        # the template itself is still the live one for the next client, and never acceptable
        assert hr.svc._template(doc_id)["version"] == "1.0"
        r = hr.client_page_accept(doc_id, "1.0", hr.svc._template(doc_id)["sha256"], party=CLIENT, code=409)
        assert "TEMPLATE_NOT_ACCEPTABLE" in [x["code"] for x in r["reasons"]]
        # another client cannot accept Acme's fill
        r = hr.client_page_accept(doc_id, "1.1", f["sha256"], party="client:other-1", code=409)
        assert "INSTANCE_NOT_BOUND" in [x["code"] for x in r["reasons"]]


def test_only_andre_or_the_scheduler_fill_and_a_fill_carries_variables_only(hr):
    sow_template(hr, "sow_seo_audit", "SEO audit")
    body = {"request_id": rid(), "entity": "zbm", "party_ref": CLIENT,
            "variables": {"client_legal_name": "Acme LLC", "fee_usd": "1500.00"}}
    assert hr.post("/legal/v1/documents/sow_seo_audit/versions", body, caller="onboarding").status_code == 403
    assert hr.apost("/legal/v1/documents/sow_seo_audit/versions", {**body, "bump": "major"}).status_code == 422
    assert hr.apost("/legal/v1/documents/sow_seo_audit/versions", {**body, "party_ref": None}).status_code == 422
    assert hr.ok(hr.post("/legal/v1/documents/sow_seo_audit/versions", {**body, "request_id": rid()},
                         caller="scheduler"), 201)["version"] == "1.1"


@pytest.mark.parametrize("token", ["[ZBM LEGAL ENTITY NAME]", "[ZBM NOTICE EMAIL]", "[TBD]", "[INCREASED CAP MULTIPLE]"])
def test_an_unfilled_bracket_placeholder_never_becomes_current(he, token):
    v = he.upload("client_msa", f"MSA between {token} and the client.", "1.0", "zbm")
    r = founder_approve(he, "client_msa", "1.0", v["sha256"], code=409)
    assert "UNFILLED_PLACEHOLDER" in [x["code"] for x in r["reasons"]] and token not in json.dumps(r)
    w = None
    with pytest.raises(AssertionError) as exc:                          # the memo sign-off path refuses it too
        w = he.approve_doc("client_msa", f"MSA v2 between {token} and the client.", "2.0", "zbm")
    assert w is None and "UNFILLED_PLACEHOLDER" in str(exc.value)
    assert he.ok(he.get("/legal/v1/documents/client_msa/current"))["current_version"] is None


def test_ordinary_brackets_are_not_placeholders(hr):
    v = hr.upload("client_msa", "Section 4 [a] and note [^1]; see [Exhibit A] and [^2].", "1.0", "zbm")
    founder_approve(hr, "client_msa", "1.0", v["sha256"])


def test_andre_renews_a_founder_approved_version_and_live_acceptances_stay_valid(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    a = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    first = hr.ok(hr.get("/legal/v1/documents/client_msa/versions/1.0"))["review_by"]
    hr.clock.advance(days=hr.svc.cfg.memo_review_days - 5)
    out = founder_approve(hr, "client_msa", "1.0", v["sha256"], rationale="Re-reviewed the MSA; terms unchanged.")
    assert out["review_by"] > first and out["status"] == "approved"
    assert hr.ledger.of_type("document_version_founder_renewed")
    hr.clock.advance(days=10)                                     # past the first review_by
    view = hr.ok(hr.get("/legal/v1/documents/client_msa/versions/1.0"))
    assert view["approval_basis"] == "founder_approval" and hr.svc._in_force(hr.svc._version("client_msa", "1.0"))
    assert hr.ok(hr.get(f"/legal/v1/acceptances/{a['acceptance_id']}", caller="finance_31"))["evidence_sufficient"]


def test_a_memo_path_version_is_not_renewed_by_the_founder_path(he):
    v = he.approve_doc("client_msa", "ZBM client MSA v1 via the memo path", "1.0", "zbm")
    r = founder_approve(he, "client_msa", "1.0", v["sha256"], code=409)
    assert "VERSION_NOT_APPROVABLE" in [x["code"] for x in r["reasons"]]


@pytest.mark.parametrize("field,value", [("signer_identity_ref", "203.0.113.7"), ("signer_identity_ref", "acme:10.0.0.1"),
                                         ("ip_address", "203.0.113.7")])
def test_a_client_acceptance_still_refuses_any_ip_address(hr, field, value):
    v = client_msa(hr)
    determine_cq19(hr)
    r = hr.post("/legal/v1/acceptances", {"request_id": rid(), "party_ref": CLIENT, "signer_identity_ref": "s-1",
                                          "doc_id": "client_msa", "version": "1.0", "doc_sha256": v["sha256"],
                                          "presented_sha256": v["sha256"], "method": "clickwrap_unticked_box",
                                          "presentation": "inline", "affirmative_act": True, field: value},
                caller="onboarding")
    assert r.status_code == 422 and not hr.svc.acceptances



# --- 8. AEGIS review of e8ab1af (H1, M1-M5, L1-L3) ------------------------------------------------------------------

AEGIS_PLACEHOLDERS = ["[FEE]", "[Client Name]", "[client name]", "［CLIENT NAME］", "[CLIENT’S NAME]",
                      "[CLIENTNAME]", "[$_____]", "[TBD: price]", "[XX]", "[CLIENT NAME]", "<<CLIENT NAME>>",
                      "«CLIENT NAME»", "[___]", "fee: TBD", "TODO fill the fee", "[ZBM]"]


@pytest.mark.parametrize("token", AEGIS_PLACEHOLDERS)
def test_h1_every_placeholder_form_is_refused(hr, token):
    v = hr.upload("client_msa", f"MSA between ZBM and the client. Fee: {token}.", "1.0", "zbm")
    r = founder_approve(hr, "client_msa", "1.0", v["sha256"], code=409)
    assert "UNFILLED_PLACEHOLDER" in [x["code"] for x in r["reasons"]]
    assert hr.ok(hr.get("/legal/v1/documents/client_msa/current"))["current_version"] is None


def test_h1_an_undeclared_curly_field_in_a_template_is_refused_declared_ones_are_not(hr):
    vars_ = {"client_name": {"type": "string"}}
    ok = hr.upload("sow_seo_audit", "SOW for {{client_name}}.", "1.0", "zbm", template_variables=vars_)
    founder_approve(hr, "sow_seo_audit", "1.0", ok["sha256"])
    bad = hr.upload("sow_revenue_recovery", "SOW for {{client_name}}; fee {{ Fee Amount }}.", "1.0", "zbm",
                    template_variables=vars_)
    r = founder_approve(hr, "sow_revenue_recovery", "1.0", bad["sha256"], code=409)
    assert "UNFILLED_PLACEHOLDER" in [x["code"] for x in r["reasons"]]


@pytest.mark.parametrize("boiler", ["[SIGNATURE PAGE FOLLOWS]", "[REMAINDER OF PAGE INTENTIONALLY LEFT BLANK]",
                                    "[Intentionally Omitted]", "[EXHIBIT A]", "[Exhibit B]", "[Schedule 1]",
                                    "[SCHEDULE 12]", "[N/A]", "[n/a]", "［SIGNATURE PAGE FOLLOWS］"])
def test_m1_standard_boilerplate_is_not_a_placeholder(he, boiler):
    v = he.upload("client_msa", f"MSA text. {boiler} End.", "1.0", "zbm")
    founder_approve(he, "client_msa", "1.0", v["sha256"])
    w = he.approve_doc("client_msa", f"MSA v2 text. {boiler} End.", "2.0", "zbm")      # the memo sign-off path too
    assert w["sha256"]


def test_m1_the_allowlist_is_exact_not_a_pattern(hr):
    for near in ("[SIGNATURE PAGE FOLLOWS HERE]", "[EXHIBIT A-1 FEE]", "[N/A: price]"):
        v = hr.ok(hr.apost("/legal/v1/documents/client_msa/versions",
                           {"request_id": rid(), "entity": "zbm", "text": f"MSA text {near}."}), 201)
        r = founder_approve(hr, "client_msa", v["version"], v["sha256"], code=409)
        assert "UNFILLED_PLACEHOLDER" in [x["code"] for x in r["reasons"]], near


def _close_lg20(h):
    row = {**h.svc.current.by_id()["LG-20"], "status": "unverified"}
    p = h.ok(h.apost("/legal/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "LG-20",
                                                   "proposed_row": row}), 201)["proposal"]
    h.ok(h.apost("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}]}))


def test_m2_closing_lg20_stops_existing_founder_versions_and_acceptances_counting(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    a = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    assert hr.ok(hr.get(f"/legal/v1/acceptances/{a['acceptance_id']}", caller="finance_31"))["evidence_sufficient"]
    _close_lg20(hr)
    view = hr.ok(hr.get("/legal/v1/documents/client_msa/versions/1.0", caller="finance_31"))
    assert view["basis_in_force"] is False
    assert hr.ok(hr.get("/legal/v1/documents/client_msa/current"))["current_version"] is None
    acc = hr.ok(hr.get(f"/legal/v1/acceptances/{a['acceptance_id']}", caller="finance_31"))
    assert acc["evidence_sufficient"] is False and acc["basis_in_force"] is False


def test_m2_invalidating_cq19_stops_existing_founder_acceptances_counting(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    a = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    hr.ok(hr.post("/legal/v1/register/CQ-19/invalidate", {"request_id": rid(), "source_ref": "urn:x:2",
                                                          "detected_change_sha256": "d" * 64}, caller="compliance_38"))
    acc = hr.ok(hr.get(f"/legal/v1/acceptances/{a['acceptance_id']}", caller="finance_31"))
    assert acc["evidence_sufficient"] is False and acc["basis_in_force"] is False
    view = hr.ok(hr.get("/legal/v1/documents/client_msa/versions/1.0", caller="finance_31"))
    assert view["basis_in_force"] is True                       # the version itself stays approved


def test_m2_an_expired_determination_does_not_cut_off_recorded_acceptances(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    a = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    hr.clock.advance(days=hr.svc.cfg.memo_review_days + 1)
    acc = hr.ok(hr.get(f"/legal/v1/acceptances/{a['acceptance_id']}", caller="finance_31"))
    assert acc["evidence_sufficient"] is True and acc["basis_in_force"] is True


def test_m3_a_fill_of_a_template_no_longer_in_force_is_not_approvable(hr):
    t = sow_template(hr, "sow_seo_audit", "SEO audit")
    f = andre_fill(hr, "sow_seo_audit", CLIENT)
    hr.ok(hr.apost("/legal/v1/documents/sow_seo_audit/versions/1.0/decision",
                   {"request_id": rid(), "decision": "retire", "version_sha256": t["sha256"]}))
    r = founder_approve(hr, "sow_seo_audit", f["version"], f["sha256"], code=409)
    assert "TEMPLATE_NOT_IN_FORCE" in [x["code"] for x in r["reasons"]]


def test_m4_andre_reads_back_his_rationales_and_nobody_else_can(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    p1 = "/legal/v1/documents/client_msa/versions/1.0/founder-approval/rationale"
    p2 = "/legal/v1/register/CQ-19/founder-determination/rationale"
    hd = hr.headers(andre=ANDRE_TOKEN)
    r1 = hr.ok(hr.client.get(p1, headers=hd))
    assert r1["rationale"] == RATIONALE and r1["rationale_sha256"] == hr.ok(hr.get(
        "/legal/v1/documents/client_msa/versions/1.0"))["founder_rationale_sha256"]
    assert hr.ok(hr.client.get(p2, headers=hd))["rationale"] == CQ19_RATIONALE
    for who in config_mod.CALLER_NAMES:
        for path in (p1, p2):
            assert hr.client.get(path, headers=hr.headers(caller=who)).status_code == 403
            assert hr.client.get(path, headers=hr.headers(andre=CALLERS[who])).status_code == 403
    assert hr.client.get(p1, headers=hr.headers()).status_code == 403
    w = hr.upload("client_msa", "MSA v1.1 draft", "1.1", "zbm")
    assert hr.client.get(p1.replace("1.0", "1.1"), headers=hd).status_code == 404


def test_m5_current_never_returns_another_clients_fill(hr):
    sow_template(hr, "sow_seo_audit", "SEO audit")
    f = andre_fill(hr, "sow_seo_audit", CLIENT)
    founder_approve(hr, "sow_seo_audit", f["version"], f["sha256"], rationale="Acme LLC at 1500.00 on the template.")
    for who in ("onboarding", "finance_31", "scheduler"):
        cur = hr.ok(hr.get("/legal/v1/documents/sow_seo_audit/current", caller=who))
        assert cur["current_version"] == "1.0" and cur["doc_sha256"] != f["sha256"]


def test_l1_renewal_of_a_superseded_version_needs_an_explicit_flag(hr):
    v = client_msa(hr)
    w = hr.upload("client_msa", "ZBM client MSA v1.1", "1.1", "zbm")
    founder_approve(hr, "client_msa", "1.1", w["sha256"])
    r = founder_approve(hr, "client_msa", "1.0", v["sha256"], code=409)
    assert "VERSION_SUPERSEDED" in [x["code"] for x in r["reasons"]]
    founder_approve(hr, "client_msa", "1.0", v["sha256"], renew_superseded=True)


def test_l2_renewal_reruns_the_document_checks(hr):
    v = client_msa(hr)
    hr.svc.documents["client_msa"]["founder_approval_allowed"] = False          # a future seed un-flags it
    r = founder_approve(hr, "client_msa", "1.0", v["sha256"], code=409)
    assert "FOUNDER_PATH_NOT_ALLOWED" in [x["code"] for x in r["reasons"]]
    hr.svc.documents["client_msa"]["founder_approval_allowed"] = True
    hr.svc.documents["client_msa"]["approval_blocked_by"] = ["CQ-17"]           # and a block added later
    r = founder_approve(hr, "client_msa", "1.0", v["sha256"], code=409)
    assert "CQ_UNVERIFIED" in [x["code"] for x in r["reasons"]]


def test_l3_the_determination_covers_only_the_documents_it_recorded(hr):
    determine_cq19(hr)
    hr.svc.documents["dpa"]["founder_approval_allowed"] = True                  # a future seed flags another document
    v = hr.upload("dpa", "DPA v1", "1.0", "zbm")
    founder_approve(hr, "dpa", "1.0", v["sha256"])
    # ADR 0028: the client acceptance page serves only the client contracts (no link for a DPA), and no other route
    # records a client's acceptance
    hr.client_page_accept("dpa", "1.0", v["sha256"], party=CLIENT, code=422)
    r = hr.clickwrap("dpa", "1.0", v["sha256"], party=CLIENT, caller="hub", code=409)
    assert r["detail"] == "ACCEPTANCE_NOT_INVITED"
    assert hr.svc._clickwrap_basis("dpa") is None                                  # and its evidence would not count



# --- 9. AEGIS re-review r2 of c7030a7 (N1-N9) -----------------------------------------------------------------------

def _guard_refuses(h, text):
    v = h.ok(h.apost("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm", "text": text}),
             201)
    r = founder_approve(h, "client_msa", v["version"], v["sha256"], code=409)
    return "UNFILLED_PLACEHOLDER" in [x["code"] for x in r["reasons"]]


def _guard_allows(h, text):
    v = h.ok(h.apost("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm", "text": text}),
             201)
    body = {"request_id": rid("fap"), "version_sha256": v["sha256"], "rationale": RATIONALE, "founder_confirms": True}
    r = h.post(f"/legal/v1/documents/client_msa/versions/{v['version']}/founder-approval", body, andre=ANDRE_TOKEN)
    return r.json() if r.status_code == 200 else {"status": r.status_code, "body": r.text[:600]}


R2_PLACEHOLDERS = [
    # N1: blank-like and empty brackets, long drafting notes, wrapped fields, bare blanks, X-amounts and dates
    "[●]", "[•]", "[ ]", "[   ]", "[]", "[#]", "[X]", "[x]", "[*]", "[?]", "[-]", "[—]", "[...]",
    "[…]", "$[0.00]", "[00/00/0000]",
    "[Insert the client's full legal name, as it appears in its certificate of formation or equivalent "
    "registration document, together with its state of formation, here]",
    "[FEE" + " " * 130 + "]", "[CLIENT\nNAME]", "[CLIENT\r\nNAME]", "$__________", "Client: ____________________",
    "$X,XXX", "$XXX", "$XX", "XX/XX/XXXX", "[ZBM LLC]", "[USD 1,000]", "[SECTION 2.1]",
    # N2: a bracket followed by '(' is a link only with a URL or an existing #anchor target
    '[CLIENT NAME](the "Client")', "[FEE]()", "[CLIENT NAME](#)", "[[FEE](x)]", "[fee](#nowhere)",
    "[CLIENT NAME](https://zbestmedia.com)",
    # N3: zero-width and soft-hyphen characters inside a field
    "[F​E​E]", "[F‍E‍E]", "[F⁠E⁠E]", "[F­E­E]", "[﻿FEE]",
    "[C​L​I​E​N​T N​A​M​E]",
    # N4: other bracket families, single braces/angles, HTML entities, lower-case tokens
    "【FEE】", "〔FEE〕", "「FEE」", "⟦FEE⟧", "‹FEE›", "≪FEE≫",
    "{FEE}", "{CLIENT NAME}", "<FEE>", "<CLIENT NAME>", "&#91;FEE&#93;", "&lt;&lt;FEE&gt;&gt;", "&lt;FEE&gt;",
    "fee tbd", "todo: fee", "tbc", "T.B.D.",
]


@pytest.mark.parametrize("token", R2_PLACEHOLDERS)
def test_n1_to_n4_every_placeholder_form_is_refused(hr, token):
    assert _guard_refuses(hr, f"MSA between ZBM and the client.\n\nFee: {token}.\n")


R2_ALLOWED = [
    "[Signature page follows.]", "[Remainder of page intentionally left blank.]", "[EXHIBIT A-1]", "[Exhibit b-12]",
    "[SIGNATURE PAGE FOLLOWS]", "[N/A]", "See [our site](https://zbestmedia.com/terms) and [fees](#fees).",
    "Section 4 [a] and note [^1]; footnote [^2].", "clause 3([b])", "§4[c]",
]


@pytest.mark.parametrize("text", R2_ALLOWED)
def test_n9_and_n2_boilerplate_links_and_markers_are_not_placeholders(hr, text):
    r = _guard_allows(hr, f"# Client MSA\n\n## Fees\n\nMSA text. {text}\n\nBy: ________________\nName: ________\n")
    assert r.get("status") == "approved", r


def test_n1_a_mid_text_single_letter_is_a_field_but_a_list_marker_is_not(hr):
    assert _guard_refuses(hr, "The fee is [b] per month.")
    assert _guard_allows(hr, "Terms:\n[a] first\n[b] second\n").get("status") == "approved"


def test_n1_a_paragraph_break_ends_a_bracket(hr):
    # two separate ordinary brackets far apart are not one huge field; a bracket left open is not a field either
    assert _guard_allows(hr, "Note [^1] here.\n\nAnother paragraph [^2].").get("status") == "approved"


def test_n5_a_new_determination_does_not_revive_acceptances_recorded_under_an_invalidated_one(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    a1 = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    hr.ok(hr.post("/legal/v1/register/CQ-19/invalidate", {"request_id": rid(), "source_ref": "urn:x:3",
                                                          "detected_change_sha256": "e" * 64}, caller="compliance_38"))
    hr.clock.advance(days=1)
    determine_cq19(hr)
    acc1 = hr.ok(hr.get(f"/legal/v1/acceptances/{a1['acceptance_id']}", caller="finance_31"))
    assert acc1["evidence_sufficient"] is False and acc1["basis_in_force"] is False
    a2 = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    acc2 = hr.ok(hr.get(f"/legal/v1/acceptances/{a2['acceptance_id']}", caller="finance_31"))
    assert acc2["evidence_sufficient"] is True and acc2["basis_in_force"] is True


def test_n5_a_renewed_determination_keeps_covering_what_it_covered(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    a1 = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    hr.clock.advance(days=hr.svc.cfg.memo_review_days + 1)
    determine_cq19(hr)                               # Andre renews an expired (never invalidated) determination
    acc1 = hr.ok(hr.get(f"/legal/v1/acceptances/{a1['acceptance_id']}", caller="finance_31"))
    assert acc1["evidence_sufficient"] is True


def _retire(h, doc_id, version, sha):
    h.ok(h.apost(f"/legal/v1/documents/{doc_id}/versions/{version}/decision",
                 {"request_id": rid(), "decision": "retire", "version_sha256": sha}))


def test_n7_retiring_a_template_stops_new_acceptances_and_renewals_of_its_fills(hr):
    determine_cq19(hr)
    t = sow_template(hr, "sow_seo_audit", "SEO audit")
    f = andre_fill(hr, "sow_seo_audit", CLIENT)
    founder_approve(hr, "sow_seo_audit", f["version"], f["sha256"], rationale="Acme LLC at 1500.00 on the template.")
    a = hr.client_page_accept("sow_seo_audit", f["version"], f["sha256"], party=CLIENT)
    _retire(hr, "sow_seo_audit", "1.0", t["sha256"])
    r = hr.client_page_accept("sow_seo_audit", f["version"], f["sha256"], party=CLIENT, code=409)
    assert "TEMPLATE_NOT_IN_FORCE" in [x["code"] for x in r["reasons"]]
    r = founder_approve(hr, "sow_seo_audit", f["version"], f["sha256"], code=409,
                        rationale="Renewing the Acme fill for another period.")
    assert "TEMPLATE_NOT_IN_FORCE" in [x["code"] for x in r["reasons"]]
    # the signed acceptance stays valid until Andre withdraws the fill itself
    acc = hr.ok(hr.get(f"/legal/v1/acceptances/{a['acceptance_id']}", caller="finance_31"))
    assert acc["evidence_sufficient"] is True
    view = hr.ok(hr.get(f"/legal/v1/documents/sow_seo_audit/versions/{f['version']}", caller="finance_31"))
    assert view["status"] == "approved" and view["basis_in_force"] is True


def test_n8_closing_lg20_unsigns_stored_contract_terms(hr):
    v = client_msa(hr)
    determine_cq19(hr)
    a = hr.client_page_accept("client_msa", "1.0", v["sha256"], party=CLIENT)
    ex = {"doc_id": "client_msa", "version": "1.0", "doc_sha256": v["sha256"], "acceptance_id": a["acceptance_id"]}
    path = "/legal/v1/contracts/zbm-client-1/terms"
    hr.ok(hr.put(path, {"request_id": rid(), "terms": terms("zbm-client-1", ccpa=False), "executed": ex},
                 caller="onboarding"))
    assert hr.ok(hr.get(path, caller="onboarding"))["signed"] is True
    _close_lg20(hr)
    got = hr.ok(hr.get(path, caller="onboarding"))
    assert got["signed"] is False and got["signed_at"] is None
    r = hr.put(path, {"request_id": rid(), "terms": terms("zbm-client-1", ccpa=False), "executed": ex},
               caller="onboarding")
    assert r.status_code == 409 and r.json()["detail"] == "NO_SUFFICIENT_ACCEPTANCE"


def test_n8_andre_upload_with_text_and_variables_is_refused(hr):
    sow_template(hr, "sow_seo_audit", "SEO audit")
    r = hr.apost("/legal/v1/documents/sow_seo_audit/versions",
                 {"request_id": rid(), "entity": "zbm", "text": "SOW for Acme LLC; fee 1500.00 USD.",
                  "party_ref": CLIENT, "variables": {"client_legal_name": "Acme LLC", "fee_usd": "1500.00"}})
    # refused as an UPLOAD carrying variables (the first gate), not only by the fill branch's own text check
    assert r.status_code == 422 and "carries the document text" in r.text, r.text


# --- 10. AEGIS r3 of 5931490 (C1, C2/R1, R2, R3, surviving guard mutants) -----------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from intelligences import i01_documents as i01  # noqa: E402

R3_PLACEHOLDERS = [
    # C2/R1: blanks outside an exact signature line; short blanks; date blanks
    "Date: __________   Fee: __________", "Name of Client: ____________________",
    "Its monthly fee shall be __________ per month.", "By __________ the Client shall pay", "Title: Monthly fee ______",
    "Date of first payment: __________", "Signed fee: __________", "Fee ___ per month", "starting __/__/____",
    "$___",
    # R2: bracketed numbers are open commercial points, not citations; footnotes are [^<number>] only
    "within [30] days", "$[500]", "[30,000]", "[5]%", "see [1]", "[^FEE]", "[^INSERT_CLIENT_NAME]", "[^note]",
    # R3: single-brace / angle identifiers, TBA, unclosed brackets, every Unicode Ps/Pe pair
    "{client_name}", "{company_name}", "{client name}", "<client_name>", "TBA", "fee tba",
    "[INSERT FEE per month.", "[INSERT\n\nFEE]",
    "❲FEE❳", "❨FEE❩", "⁅FEE⁆", "⌈FEE⌉", "｟FEE｠", "⦋FEE⦌",
    "⸢FEE⸣",
    # surviving r3 mutants: g01 (Cf outside a bracket), g10 ([x] as a marker), g31 (double-encoded entity),
    # g18/g19/g20 (each single-brace criterion on its own)
    "T​BD", "\n[x] item", "&amp;#91;FEE&amp;#93;", "{ }", "{ZQ-9}", "{the fee.}", "{foo_bar}",
]


@pytest.mark.parametrize("token", R3_PLACEHOLDERS)
def test_r3_every_listed_form_is_refused(hr, token):
    assert _guard_refuses(hr, f"MSA between ZBM and the client.\n\nFee: {token}.\n")


@pytest.mark.parametrize("line", ["By: ______________", "Name: ________", "Title:  ______ ", "Date: ______",
                                  "Signature: __________", "Its: _____", "Signed: _______", "Initials: ____",
                                  "  by:______"])
def test_c2_only_an_exact_signature_line_keeps_its_blank(hr, line):
    r = _guard_allows(hr, f"# Client MSA\n\nMSA text. Footnote [^1].\n\n{line}\n")
    assert r.get("status") == "approved", r


def test_r3_bare_capitals_and_parenthesised_fields_are_documented_as_not_detected():
    # ADR 0022 lists these as NOT detected: the guard is a backstop to Andre's own reading, not a guarantee
    assert i01.bracket_placeholders("Between ZBM and CLIENT NAME (CLIENT NAME), the client.") == 0


STRESS = {
    "markers mid-line": "x [a] " * 180_000,
    "signature lines": "By: " + "_____ " * 180_000,
    "open brackets": "[" * 1_000_000,
    "angle pairs": "<<" * 500_000,
    "guillemets": "«" * 1_000_000,
    "section refs": "Section 4 [b] " * 75_000,
    "entities": "&amp;" * 200_000,
    "long line words": ("word " * 200_000) + "]",
}


@pytest.mark.parametrize("name", sorted(STRESS))
def test_c1_the_guard_is_linear_a_megabyte_checks_in_under_two_seconds(name):
    text = STRESS[name]
    assert len(text) >= 900_000
    # CPU time, not wall-clock: a loaded CI host cannot make it flake, and quadratic work still shows (r3 measured
    # 23 s for 30 KB of mid-line markers before the fix)
    t0 = time.process_time()
    i01.bracket_placeholders(text)
    assert time.process_time() - t0 < 2.0, name


def test_c1_the_guard_runs_outside_the_service_lock(hr, monkeypatch):
    v = hr.upload("client_msa", "ZBM client MSA v1 (standard form, no placeholders)", "1.0", "zbm")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real = i01.bracket_placeholders

    def slow(*a, **k):
        entered.set()
        release.wait(5)
        finished.set()
        return real(*a, **k)

    monkeypatch.setattr(i01, "bracket_placeholders", slow)
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", founder_approve(hr, "client_msa", "1.0", v["sha256"])))
    worker.start()
    try:
        assert entered.wait(5), "the guard never ran"
        assert hr.get("/legal/v1/register").status_code == 200
        # no clock: had the guard held the service lock, this GET could only return after the guard finished
        assert not finished.is_set(), "a Legal route waited for the placeholder guard"
    finally:
        release.set()
        worker.join(10)
    assert out["r"]["status"] == "approved"


@pytest.mark.parametrize("line", ["Payment due by: ______", "Client Name: ______", "Fee Date: ______",
                                  "By: ______ Fee: ______", "By: Acme ______", "Bylaws: ______"])
def test_c2_a_line_that_only_ends_like_a_signature_line_keeps_counting(hr, line):
    assert _guard_refuses(hr, f"MSA text. Footnote [^1].\n\n{line}\n")


# --- 11. AEGIS r4 of 741f591 (C4/R6, m02, m10, m13, $[^n]) --------------------------------------------------------

R4_STRESS = {
    "signature line, long run then a letter": "By: " + "_" * 200_000 + "x",
    "signature line, run, spaces, letter": "Date: ___" + " " * 200_000 + "x",
    "signature line, alternating": "Name: " + "___ " * 100_000 + "x",
}


@pytest.mark.parametrize("name", sorted(R4_STRESS))
def test_c4_a_failing_signature_line_is_linear(name):
    t0 = time.process_time()                    # CPU time (no wall-clock bound, hygiene rule L1)
    assert i01.bracket_placeholders(R4_STRESS[name]) >= 1
    assert time.process_time() - t0 < 2.0, name


def test_c4_a_split_signature_blank_stays_exempt(hr):
    assert _guard_allows(hr, "MSA text.\n\nBy: ___ ___\nDate: ____  ____ \n").get("status") == "approved"


@pytest.mark.parametrize("token", ["starting __/__/__", "[Insert client name](https://zbestmedia.com)",
                                   "[to do: insert fee](#fees)", "$[^500]", "€[^12]"])
def test_r4_more_forms_are_refused(hr, token):
    assert _guard_refuses(hr, f"# Fees\n\nMSA between ZBM and the client.\n\nFee: {token}.\n")


def test_r4_a_numbered_footnote_after_a_word_still_passes(hr):
    assert _guard_allows(hr, "MSA text with a note[^1] and another [^2].\n\n[^1]: one.\n").get("status") == "approved"


def test_m10_the_memo_signoff_decision_also_counts_outside_the_lock(he, monkeypatch):
    v = he.upload("client_msa", "ZBM client MSA v2 (standard form, no placeholders)", "1.0", "zbm")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real = i01.bracket_placeholders

    def slow(*a, **k):
        entered.set()
        release.wait(5)
        finished.set()
        return real(*a, **k)

    monkeypatch.setattr(i01, "bracket_placeholders", slow)
    body = {"request_id": rid("ap"), "decision": "approve", "version_sha256": v["sha256"]}
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", he.apost(
        "/legal/v1/documents/client_msa/versions/1.0/decision", body)))
    worker.start()
    try:
        assert entered.wait(5), "the guard never ran"
        assert he.get("/legal/v1/register").status_code == 200
        assert not finished.is_set(), "a Legal route waited for the placeholder guard"
    finally:
        release.set()
        worker.join(10)
    assert out["r"].status_code in (200, 409)


def test_c4_a_replayed_request_is_answered_without_counting_again(hr, monkeypatch):
    v = hr.upload("client_msa", "ZBM client MSA v1 (standard form, no placeholders)", "1.0", "zbm")
    body = {"request_id": rid("fap"), "version_sha256": v["sha256"], "rationale": RATIONALE, "founder_confirms": True}
    path = "/legal/v1/documents/client_msa/versions/1.0/founder-approval"
    first = hr.ok(hr.post(path, body, andre=ANDRE_TOKEN))
    calls = []
    real = i01.bracket_placeholders
    monkeypatch.setattr(i01, "bracket_placeholders", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert hr.ok(hr.post(path, body, andre=ANDRE_TOKEN)) == first
    assert calls == []


# --- 12. Money red team RTM-04 on af10a0b: 21 more forms -----------------------------------------------------------

RTM04_FORMS = [
    "XX/XX/2026", "USD XXX", "(insert fee)", "(INSERT CLIENT NAME)", "%%FEE%%", "{{ fee }}", "__FEE__", "#FEE#",
    "Lorem ipsum dolor sit amet", "Fee: ??", "Fee: N/A (to be agreed)", "████",
    "Effective Date: the date of", "Client name here", "PLACEHOLDER", "Fee: $0.00 (to be updated)", "XXXX",
    "{{{FEE}}}", "Fee: —", "Fee: TK", "Fee: TKTK",
]


def _never_in_force(h, text):
    """Either Legal refuses the upload itself (422: an undeclared {{...}} with no template schema) or the approval
    is refused with UNFILLED_PLACEHOLDER. Both leave no version in force."""
    r = h.apost("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm", "text": text})
    if r.status_code == 422:
        return "upload"
    v = h.ok(r, 201)
    a = founder_approve(h, "client_msa", v["version"], v["sha256"], code=409)
    assert "UNFILLED_PLACEHOLDER" in [x["code"] for x in a["reasons"]], a
    return "approval"


@pytest.mark.parametrize("token", RTM04_FORMS)
def test_rtm04_every_red_team_form_never_comes_into_force(hr, token):
    _never_in_force(hr, f"MSA between ZBM and the client.\n\nFee: {token}.\n")
    assert hr.ok(hr.get("/legal/v1/documents/client_msa/current"))["current_version"] is None


@pytest.mark.parametrize("token", ["{{ fee }}", "{{{FEE}}}"])
def test_rtm04_undeclared_double_braces_are_refused_at_upload(hr, token):
    assert _never_in_force(hr, f"Fee: {token}.") == "upload"


@pytest.mark.parametrize("token", ["XX/XX/2026", "XXXX", "(insert fee)", "%%FEE%%", "__FEE__", "#FEE#", "Fee: ??",
                                   "Fee: TK", "Lorem ipsum dolor sit amet", "████"])
def test_rtm04_the_guard_itself_counts_them(token):
    assert i01.bracket_placeholders(f"MSA text.\n\nFee: {token}.\n") >= 1


@pytest.mark.parametrize("text", [
    "This is __important__ and **bold**.", "## Fees ##\n\nText.", "Payment on a date to be agreed by the parties.",
    "■ First item\n■ Second item", "The services the Client agreed to.", "Fees -- see Section 4.",
    "Term: 12 months — renewable.", "Invoices are due 30 days after the invoice date.", "Total: $0.00 setup fee.",
    "Version 2.0 between ZBM and the Client.\n\nThe parties agree.", "Hashtag #growth and #SEO in posts.",
    "This Agreement is made by and between\nZ Best Media LLC and the Client, effective as of\nthe date of signature.",
    "This Agreement is made by and between:\n\n1. Z Best Media LLC", "XXII. Miscellaneous",
])
def test_rtm04_ordinary_contract_text_still_passes(hr, text):
    assert _guard_allows(hr, f"# Client MSA\n\n{text}\n").get("status") == "approved", text


RTM04_STRESS = {
    "dash line spaces": "Fee: —" + " " * 1_000_000 + "x",
    "date-of spaces": "the date of" + " " * 1_000_000 + "x",
    "hash runs": "#A" * 500_000,
    "percent runs": "%%A" * 333_334,
    "dunder runs": "__A" * 333_334,
    "paren insert": "(insert " * 125_000,
}


@pytest.mark.parametrize("name", sorted(RTM04_STRESS))
def test_rtm04_the_new_forms_stay_linear(name):
    t0 = time.process_time()                    # CPU time (no wall-clock bound, hygiene rule L1)
    i01.bracket_placeholders(RTM04_STRESS[name])
    assert time.process_time() - t0 < 2.0, name
