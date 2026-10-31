"""AEGIS on ADR 0028 (aegis-client-acceptance.md @55023ba3): M-1, M-2, M-5 and the Legal-side Lows.

- M-1: an empty or unresolved ``client_portal`` entry in LEGAL_CALLER_TOKENS never stops Legal starting (the first
  deploy sets the portal's token after Legal restarts); the portal routes answer 503 until it is set. Every other
  caller entry is still checked strictly.
- M-2: a retry after success (a lost response, a refresh) is answered with the receipt of THAT submission:
  ``POST /legal/v1/portal/receipts`` with the token and the submission's request id; anything else is the 404.
- M-5: Legal renders the page's text itself (clickwrap text v1 Parts B/C, from the bound documents and the display
  block) and refuses a submission whose page text is not that text, or whose typed company is not the client's legal
  name on file (case, spaces and punctuation ignored).
- L-1: bidi and invisible characters (Unicode Cc/Cf/Cs/Zl/Zp, after NFKC) are refused in the typed fields.
- L-2(e): a SOW names the MSA version it is made under (``msa_version``); a link refuses a SOW whose ``msa_version``
  is neither the MSA in the same link nor the client's counted MSA acceptance.
- L-3: Andre's approval of a newer fill of a client document revokes that client's open links to older fills.
- L-4: no RTM-13 single-document invitation is issued for a client party.
- L-5: the test helper never silently routes a client's acceptance to the page: ``clickwrap`` refuses that call.
"""

from __future__ import annotations

import hashlib
import json

import pytest

import clickwrap_page as CP
import config as config_mod
from helpers import ANDRE_TOKEN, CALLERS, Harness, b64, rid
from test_client_acceptance_portal import (CLIENT, DISPLAY, SIGNATORY, accept, accept_body, determine_cq19, docs_of,
                                           founder_approve, invite, msa, resolve, sow_fill)


@pytest.fixture
def pair():
    x = Harness()
    x.approve_rules()
    determine_cq19(x)
    m, s = msa(x), sow_fill(x)
    return x, m, s, docs_of(m, s)


# --- M-1 -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "${{client-portal.PORTAL_LEGAL_CALLER_TOKEN}}", "{{x}}", None])
def test_an_unset_portal_caller_never_stops_legal_and_its_routes_answer_503(value):
    callers = {k: v for k, v in CALLERS.items() if k != "client_portal"}
    if value is not None:
        callers["client_portal"] = value
    s = config_mod.load({"LEGAL_SERVICE_TOKEN": "s" * 40, "LEGAL_CALLER_TOKENS": json.dumps(callers)})
    assert "client_portal" not in s.caller_tokens and s.callers_not_configured == ("client_portal",)
    x = Harness(env={"LEGAL_CALLER_TOKENS": json.dumps(callers)})
    x.approve_rules()
    h = x.ok(x.client.get("/health"))      # rebased on ADR 0024: /health warns about each unset optional caller
    assert h["status"] == "warning" and h["warnings"] == ["optional_caller_unset:client_portal"], h
    for path, body in (("/legal/v1/portal/invitations/resolve", {"token": "A" * 43}),
                       ("/legal/v1/portal/receipts", {"token": "A" * 43, "request_id": "pa-1",
                                                      "form_sha256": "0" * 64}),
                       ("/legal/v1/portal/acceptances", {"request_id": "pa-1"})):
        r = x.post(path, body, caller=CALLERS["client_portal"])
        assert r.status_code == 503 and "client_portal" in r.text, (path, r.text)


@pytest.mark.parametrize("caller", ["finance_31", "onboarding"])          # compliance_38 is optional too (ADR 0024)
def test_every_other_caller_entry_is_still_strict(caller):
    callers = dict(CALLERS, **{caller: ""})
    with pytest.raises(RuntimeError):
        config_mod.load({"LEGAL_SERVICE_TOKEN": "s" * 40, "LEGAL_CALLER_TOKENS": json.dumps(callers)})


def test_a_short_portal_token_is_still_refused():
    callers = dict(CALLERS, client_portal="short")
    with pytest.raises(RuntimeError):
        config_mod.load({"LEGAL_SERVICE_TOKEN": "s" * 40, "LEGAL_CALLER_TOKENS": json.dumps(callers)})


# --- M-2 -----------------------------------------------------------------------------------------------------------

def receipt(h, token, request_id, caller="client_portal", form_sha256=None):
    form = form_sha256 or hashlib.sha256(f"form {token}".encode()).hexdigest()
    return h.post("/legal/v1/portal/receipts", {"token": token, "request_id": request_id, "form_sha256": form},
                  caller=caller)


def test_the_receipt_answers_only_the_submission_that_used_the_link(pair):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    assert receipt(x, tok, "pa-anything").status_code == 404                          # not used yet
    body = accept_body(tok, docs, h=x)
    done = x.ok(x.post("/legal/v1/portal/acceptances", body, caller="client_portal"), 201)
    r = x.ok(receipt(x, tok, body["request_id"]))
    assert r["invitation_id"] == done["invitation_id"] and r["accepted_at"] == done["accepted_at"]
    assert [a["acceptance_id"] for a in r["acceptances"]] == [a["acceptance_id"] for a in done["acceptances"]]
    assert r["display"] == DISPLAY and [d["doc_id"] for d in r["documents"]] == ["client_msa", "sow_seo_audit"]
    assert all(hashlib.sha256(d["text"].encode()).hexdigest() == d["doc_sha256"] for d in r["documents"])
    x.clock.advance(days=1)                                       # beyond Legal's 15-minute idempotency window
    assert receipt(x, tok, body["request_id"]).status_code == 200
    for bad_tok, bad_rid in ((tok, "pa-other"), ("B" * 43, body["request_id"]), (tok[:-1] + "x", body["request_id"])):
        r = receipt(x, bad_tok, bad_rid)
        assert r.status_code == 404 and r.json() == {"detail": "not found"}
    for who in ("onboarding", "finance_31"):
        assert receipt(x, tok, body["request_id"], caller=who).status_code == 403


def test_a_revoked_link_has_no_receipt(pair):
    x, m, s, docs = pair
    out = invite(x, docs)
    x.ok(x.apost(f"/legal/v1/client-acceptance-invitations/{out['invitation_id']}/revoke", {"request_id": rid()}))
    assert receipt(x, out["token"], "pa-x").status_code == 404


# --- M-5 -----------------------------------------------------------------------------------------------------------

def test_legal_renders_the_page_and_the_portal_gets_its_blocks_and_hash(pair):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    r = x.ok(resolve(x, tok))
    text = CP.page_text(r["blocks"])
    assert hashlib.sha256(text.encode()).hexdigest() == r["page_sha256"]
    assert m["text"] in text and "Review and accept your agreement with Z Best Media" in text
    assert ("I have read the Z Best Media Master Services Agreement, version 1.0, and I agree to it on behalf of "
            "Acme LLC.") in text
    assert [d["label"] for d in r["documents"]] == ["Z Best Media Master Services Agreement",
                                                    "Statement of Work SOW-2026-001: SEO and AI Visibility Audit"]


@pytest.mark.parametrize("page", [b"anything at all", "doctored".encode()])
def test_a_page_text_legal_did_not_render_is_refused(pair, page):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    body = accept_body(tok, docs, page_text_b64=b64(page), page_sha256=hashlib.sha256(page).hexdigest())
    r = x.post("/legal/v1/portal/acceptances", body, caller="client_portal")
    assert r.status_code == 409 and "PRESENTED_TEXT_MISMATCH" in r.text
    assert not x.svc.acceptances
    x.ok(accept(x, tok, docs), 201)


@pytest.mark.parametrize("company,ok", [("Totally Different Inc", False), ("Acme", False), ("acme, l.l.c.", True),
                                        ("  ACME LLC ", True)])
def test_the_typed_company_must_be_the_clients_legal_name(pair, company, ok):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    r = accept(x, tok, docs, signatory={**SIGNATORY, "company": company})
    assert r.status_code == (201 if ok else 409), r.text
    if not ok:
        assert "COMPANY_MISMATCH" in r.text and not x.svc.acceptances


# --- L-1 -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Pat‮ CLL emcA", "Pat​Q", "Pat⁦x⁩", "Pat­Q", "Pat﻿"])
def test_bidi_and_invisible_characters_are_refused_in_the_signatory(pair, name):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    assert accept(x, tok, docs, signatory={**SIGNATORY, "full_name": name}).status_code == 422
    assert accept(x, tok, docs, signatory={**SIGNATORY, "title": name}).status_code == 422
    assert not x.svc.acceptances


# --- L-2(e): the SOW's MSA version -----------------------------------------------------------------------------------

SOW_VARS = {"client_legal_name": {"type": "string", "max_length": 200}, "msa_version": {"type": "string",
                                                                                         "max_length": 12}}


def _sow_citing(x, msa_version, party=CLIENT, doc_id="sow_revenue_recovery"):
    if x.svc._template(doc_id) is None:
        t = x.upload(doc_id, "SOW for {{client_legal_name}} under MSA version {{msa_version}}.", "1.0", "zbm",
                     template_variables=SOW_VARS)
        founder_approve(x, doc_id, "1.0", t["sha256"])
    f = x.ok(x.apost(f"/legal/v1/documents/{doc_id}/versions",
                     {"request_id": rid(), "entity": "zbm", "party_ref": party,
                      "variables": {"client_legal_name": "Acme LLC", "msa_version": msa_version}}), 201)
    founder_approve(x, doc_id, f["version"], f["sha256"], rationale=f"{party} SOW under MSA {msa_version}, approved.")
    return {**f, "doc_id": doc_id}


def test_a_sow_with_the_msa_of_the_same_link_must_cite_that_msa_version(pair):
    x, m, s, docs = pair
    invite(x, docs_of(m, _sow_citing(x, "1.0")))
    r = invite(x, docs_of(m, _sow_citing(x, "2.0")), code=409)       # (each the client's latest fill: AEGIS r2 N-L2)
    assert "MSA_VERSION_MISMATCH" in [i["code"] for i in r["reasons"]]


def test_a_sow_alone_must_cite_the_msa_version_the_client_accepted(pair):
    x, m, s, docs = pair
    x.ok(accept(x, invite(x, docs)["token"], docs), 201)              # the client accepted MSA 1.0
    invite(x, docs_of(_sow_citing(x, "1.0")))
    r = invite(x, docs_of(_sow_citing(x, "1.1")), code=409)
    assert "MSA_VERSION_MISMATCH" in [i["code"] for i in r["reasons"]]


# --- L-3: a newer fill revokes older open links -----------------------------------------------------------------

def test_approving_a_newer_fill_revokes_the_clients_open_links_to_older_fills(pair):
    x, m, s, docs = pair
    old = invite(x, docs)
    other_client = invite(x, docs_of(m, sow_fill(x, party="client:other-9", fee="700.00")), party="client:other-9")
    newer = sow_fill(x, fee="1700.00")                                # Andre approves a newer fill for the client
    v = x.ok(x.client.get(f"/legal/v1/client-acceptance-invitations/{old['invitation_id']}",
                          headers=x.headers(andre=ANDRE_TOKEN)))
    assert v["status"] == "revoked"
    assert resolve(x, old["token"]).status_code == 404
    assert resolve(x, other_client["token"]).status_code == 200      # another client's link is untouched
    assert x.ledger.of_type("client_acceptance_link_revoked")[-1]["payload"]["reason"] == "newer_fill_approved"
    invite(x, docs_of(m, newer))


# --- L-4 / L-5 -----------------------------------------------------------------------------------------------------

def test_no_rtm13_invitation_is_issued_for_a_client(pair):
    x, m, s, docs = pair
    r = x.apost("/legal/v1/acceptance-invitations", {"request_id": rid(), "doc_id": "client_msa", "version": "1.0",
                                                      "party_ref": CLIENT})
    assert r.status_code == 409 and "ACCEPTANCE_NOT_INVITED" in r.text
    assert not x.svc.invitations


def test_the_helper_never_reroutes_a_clients_acceptance(pair):
    x, m, s, docs = pair
    with pytest.raises(AssertionError, match="client_page_accept"):
        x.clickwrap("client_msa", "1.0", m["sha256"], party=CLIENT, caller="onboarding")
