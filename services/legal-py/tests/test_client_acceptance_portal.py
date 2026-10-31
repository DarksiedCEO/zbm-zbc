"""ADR 0028 (Oct 31 2026): the client acceptance page. Closes the red team's OPS-C1 ("clients have no acceptance page")
and RTM-13 / AEGIS r10-r11 L3's residual (an onboarding token plus any open invitation recorded a client's acceptance
with an arbitrary signer).

- Andre invites ONE client to accept specific document versions (client_msa and/or one SOW), by hash. Legal answers
  with a single-use, 256-bit acceptance token, shown ONCE: only its SHA-256 is stored (never in the log, the ledger,
  the audit export or the idempotency record). 14 days at most; Andre can revoke it.
- The client-portal service (caller ``client_portal``, its only routes) resolves a token to the documents' exact
  registered text, and records the acceptance: one record per document, the signatory's typed name, title and
  company (a blob; only hashes leave Legal), the page text shown (a blob) and its hash, and HMACs of the client's IP
  address and user agent made by the portal with its own key (Legal never sees either value).
- Every refusal to resolve is the same 404: unknown, malformed, expired, revoked, used, or a document no longer usable.
- A client's acceptance is recorded ONLY this way: POST /legal/v1/acceptances refuses every ``client:*`` party, for
  every caller, and an acceptance of a client recorded any other way is never evidence-sufficient.
- ZBM's own entity facts in a template (``{{zbm_*}}`` variables) are filled from LEGAL_ZBM_FACTS at fill time, never
  from the request and never hard-coded.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import re
from datetime import timedelta

import pytest

import config as config_mod
import clickwrap_page as CP
from clock import iso
from helpers import ANDRE_TOKEN, CALLERS, Harness, b64, rid, sha

CLIENT = "client:acme-1"
OTHER = "client:globex-2"
RATIONALE = "Founder approval (ADR 0022): ZBM's standard client documents, approved by me as founder."
SOW_VARS = {"client_legal_name": {"type": "string", "max_length": 200},
            "fee_usd": {"type": "string", "max_length": 20}}
DISPLAY = {"client_legal_name": "Acme LLC", "notice_email": "legal@acme.example", "contact_first_name": "Pat",
           "sow_number": "SOW-2026-001", "sow_title": "SEO and AI Visibility Audit"}
SIGNATORY = {"full_name": "Patricia Q. Example", "title": "Managing Member", "company": "Acme LLC"}
PAGE = "Review and accept your agreement with Z Best Media\n(page text as served)\n".encode("utf-8")
HMAC_IP, HMAC_UA, KEY_ID = "1" * 64, "2" * 64, "0123456789abcdef"


def founder_approve(h, doc_id, version, digest, rationale=RATIONALE):
    return h.ok(h.apost(f"/legal/v1/documents/{doc_id}/versions/{version}/founder-approval",
                        {"request_id": rid("fap"), "version_sha256": digest, "rationale": rationale,
                         "founder_confirms": True}))


def determine_cq19(h):
    return h.ok(h.apost("/legal/v1/register/CQ-19/founder-determination",
                        {"request_id": rid("fdt"), "founder_confirms": True,
                         "rationale": "Founder determination (ADR 0022): B2B clients accept by ticking an unticked box."}))


def msa(h, text="Z Best Media Master Services Agreement v1: the same text for every client."):
    v = h.upload("client_msa", text, "1.0", "zbm")
    founder_approve(h, "client_msa", "1.0", v["sha256"])
    return {**v, "version": "1.0", "text": text}


def sow_fill(h, party=CLIENT, fee="1500.00", doc_id="sow_seo_audit"):
    if h.svc._template(doc_id) is None:
        t = h.upload(doc_id, "SOW for {{client_legal_name}}; fee {{fee_usd}} USD.", "1.0", "zbm",
                     template_variables=SOW_VARS)
        founder_approve(h, doc_id, "1.0", t["sha256"])
    f = h.ok(h.apost(f"/legal/v1/documents/{doc_id}/versions",
                     {"request_id": rid("fill"), "entity": "zbm", "party_ref": party,
                      "variables": {"client_legal_name": "Acme LLC", "fee_usd": fee}}), 201)
    founder_approve(h, doc_id, f["version"], f["sha256"], rationale=f"{party} SOW at {fee} on the approved template.")
    return {**f, "doc_id": doc_id}


def docs_of(*vs):
    return [{"doc_id": v.get("doc_id", "client_msa"), "version": v["version"], "doc_sha256": v["sha256"]} for v in vs]


def invite(h, docs, party=CLIENT, code=201, **over):
    body = {"request_id": rid("cinv"), "party_ref": party, "documents": docs, "display": dict(DISPLAY), **over}
    return h.ok(h.apost("/legal/v1/client-acceptance-invitations", body), code)


def resolve(h, token, caller="client_portal"):
    return h.post("/legal/v1/portal/invitations/resolve", {"token": token}, caller=caller)


def page_of(h, token) -> bytes:
    """The page Legal renders for this link (AEGIS M-5), or a stand-in for a link that no longer resolves."""
    r = resolve(h, token) if h is not None else None
    if r is not None and r.status_code == 200 and r.json().get("blocks"):
        return CP.page_text(r.json()["blocks"]).encode("utf-8")
    return PAGE


def accept_body(token, docs, h=None, **over):
    page = page_of(h, token)
    body = {"request_id": rid("pacc"), "token": token,
            "documents": [{**d, "presented_sha256": d["doc_sha256"], "affirmative_act": True} for d in docs],
            "presentation": "inline", "page_sha256": hashlib.sha256(page).hexdigest(), "page_text_b64": b64(page),
            "form_sha256": hashlib.sha256(f"form {token}".encode()).hexdigest(),
            "signatory": dict(SIGNATORY), "ip_hmac_sha256": HMAC_IP, "user_agent_hmac_sha256": HMAC_UA,
            "hmac_key_id": KEY_ID}
    body.update(over)
    return body


def accept(h, token, docs, caller="client_portal", **over):
    return h.post("/legal/v1/portal/acceptances", accept_body(token, docs, h=h, **over), caller=caller)


@pytest.fixture
def hr():
    x = Harness()
    x.approve_rules()
    determine_cq19(x)
    return x


@pytest.fixture
def pair(hr):
    m, s = msa(hr), sow_fill(hr)
    return hr, m, s, docs_of(m, s)


# --- 1. the invitation -------------------------------------------------------------------------------------------

def test_andre_invites_one_client_and_gets_a_single_use_token_shown_once(pair):
    hr, m, s, docs = pair
    out = invite(hr, docs)
    tok = out["token"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{66}", tok)       # version byte + 32 random bytes + a 16-byte tag (AEGIS r2 N-M1, r3 N3-L4)
    assert out["accept_path"] == f"/accept/{tok}" and out["page"] == "first_engagement"
    assert out["party_ref"] == CLIENT and out["documents"] == docs and out["token_shown_once"] is True
    assert out["expires_at"] == iso(hr.clock.now() + timedelta(days=14))
    inv = hr.svc.client_invitations[out["invitation_id"]]
    assert inv["token_sha256"] == hashlib.sha256(tok.encode()).hexdigest() and "token" not in inv
    # the token is never written anywhere Legal keeps: not the log, the ledger, the audit export or the idem record
    assert tok not in hr.all_text() and tok not in json.dumps(list(hr.svc.idem.values()), default=str)
    # an idempotent replay answers the same invitation WITHOUT the token (it is not stored to be shown again)
    body = {"request_id": "cinv-replay-1", "party_ref": CLIENT, "documents": docs, "display": dict(DISPLAY)}
    first = hr.ok(hr.apost("/legal/v1/client-acceptance-invitations", body), 201)
    again = hr.ok(hr.apost("/legal/v1/client-acceptance-invitations", body), 201)
    assert first["token"] and again["token"] is None and again["accept_path"] is None
    assert again["invitation_id"] == first["invitation_id"]
    # the client's contact details are a blob, never in a record, an event or the export
    assert "legal@acme.example" not in hr.all_text() and "Acme LLC" not in json.dumps(inv)


def test_two_invitations_never_share_a_token(pair):
    hr, _, _, docs = pair
    assert invite(hr, docs)["token"] != invite(hr, docs)["token"]


@pytest.mark.parametrize("who", ["onboarding", "client_portal", "finance_31", "hub"])
def test_only_andre_invites_reads_and_revokes(pair, who):
    hr, _, _, docs = pair
    out = invite(hr, docs)
    body = {"request_id": rid(), "party_ref": CLIENT, "documents": docs, "display": dict(DISPLAY)}
    assert hr.post("/legal/v1/client-acceptance-invitations", body, caller=who).status_code == 403
    assert hr.get(f"/legal/v1/client-acceptance-invitations/{out['invitation_id']}", caller=who).status_code == 403
    assert hr.post(f"/legal/v1/client-acceptance-invitations/{out['invitation_id']}/revoke",
                   {"request_id": rid()}, caller=who).status_code == 403


def test_the_invitation_refuses_documents_the_client_could_not_validly_accept(hr):
    m = msa(hr)
    s = sow_fill(hr)
    other_fill = sow_fill(hr, party=OTHER, fee="900.00")
    tpl = hr.svc._template("sow_seo_audit")
    codes = lambda r: [x["code"] for x in r["reasons"]]                  # noqa: E731
    assert "ACCEPTANCE_HASH_MISMATCH" in codes(invite(hr, [{**docs_of(m)[0], "doc_sha256": "a" * 64}], code=409))
    assert "INSTANCE_NOT_BOUND" in codes(invite(hr, docs_of(m, other_fill), code=409))
    assert "TEMPLATE_NOT_ACCEPTABLE" in codes(
        invite(hr, [{"doc_id": "sow_seo_audit", "version": tpl["version"], "doc_sha256": tpl["sha256"]}], code=409))
    draft = hr.ok(hr.apost("/legal/v1/documents/sow_revenue_recovery/versions",
                           {"request_id": rid(), "entity": "zbm", "text": "Revenue Recovery SOW, no fields."}), 201)
    assert "VERSION_NOT_IN_FORCE" in codes(invite(hr, [{"doc_id": "sow_revenue_recovery", "version": draft["version"],
                                                       "doc_sha256": draft["sha256"]}], code=409))
    assert "DOCUMENT_UNKNOWN" in codes(invite(hr, [{"doc_id": "client_msa", "version": "9.9",
                                                   "doc_sha256": m["sha256"]}], code=409))
    invite(hr, docs_of(m, s), party="clipper:cn-1", code=422)            # clients only
    invite(hr, docs_of(m, m), code=422)                                   # one MSA
    invite(hr, docs_of(s, {**s, "doc_id": "sow"}), code=422)              # one SOW
    invite(hr, [{"doc_id": "dpa", "version": "1.0", "doc_sha256": m["sha256"]}], code=422)   # client contracts only
    invite(hr, [], code=422)
    invite(hr, docs_of(m, s), expires_in_days=15, code=422)
    invite(hr, docs_of(m, s), expires_in_days=0, code=422)
    # the page names the SOW (clickwrap text v1, Part B): its number and title are required with a SOW
    invite(hr, docs_of(m, s), display={"client_legal_name": "Acme LLC", "notice_email": "legal@acme.example"}, code=422)
    invite(hr, docs_of(m, s), display={**DISPLAY, "notice_email": "not-an-email"}, code=422)
    assert not hr.svc.client_invitations


def test_no_invitation_while_clickwrap_evidence_would_be_insufficient():
    x = Harness()
    x.approve_rules()
    m = msa(x)                                                            # no CQ-19 determination, no memo
    r = invite(x, docs_of(m), code=409)
    assert "EVIDENCE_INSUFFICIENT" in [i["code"] for i in r["reasons"]]


# --- 2. resolving a token (client_portal only) -------------------------------------------------------------------

def test_the_portal_resolves_a_token_to_the_exact_registered_texts(pair):
    hr, m, s, docs = pair
    out = invite(hr, docs, expires_in_days=7)
    r = hr.ok(resolve(hr, out["token"]))
    assert r["invitation_id"] == out["invitation_id"] and r["page"] == "first_engagement"
    assert r["expires_at"] == iso(hr.clock.now() + timedelta(days=7))
    assert r["display"] == DISPLAY
    assert [(d["doc_id"], d["version"], d["doc_sha256"]) for d in r["documents"]] == \
        [(d["doc_id"], d["version"], d["doc_sha256"]) for d in docs]
    for d in r["documents"]:
        assert hashlib.sha256(d["text"].encode("utf-8")).hexdigest() == d["doc_sha256"]
    assert r["documents"][0]["text"] == m["text"]
    assert "party_ref" not in r                                           # the page never needs the client id


@pytest.mark.parametrize("who", ["onboarding", "finance_31", "compliance_38", "hub", "scheduler"])
def test_only_the_portal_caller_resolves_or_records(pair, who):
    hr, _, _, docs = pair
    tok = invite(hr, docs)["token"]
    assert resolve(hr, tok, caller=who).status_code == 403
    assert accept(hr, tok, docs, caller=who).status_code == 403
    assert hr.post("/legal/v1/portal/invitations/resolve", {"token": tok}, andre=ANDRE_TOKEN).status_code == 403


def _not_found(r):
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "not found"}


def test_every_unusable_token_is_the_same_404(pair):
    hr, m, s, docs = pair
    good = invite(hr, docs)
    for tok in ("A" * 43, good["token"][:-1] + ("A" if good["token"][-1] != "A" else "B"), "short", "x" * 200,
                good["token"].upper() if good["token"].upper() != good["token"] else "Z" * 43):
        _not_found(resolve(hr, tok))
    # revoked
    rv = invite(hr, docs)
    hr.ok(hr.apost(f"/legal/v1/client-acceptance-invitations/{rv['invitation_id']}/revoke", {"request_id": rid()}))
    _not_found(resolve(hr, rv["token"]))
    _not_found(accept(hr, rv["token"], docs))
    # used
    hr.ok(accept(hr, good["token"], docs), 201)
    _not_found(resolve(hr, good["token"]))
    # expired: the 14th day is the last
    ex = invite(hr, docs)
    hr.clock.advance(days=14)
    _not_found(resolve(hr, ex["token"]))
    _not_found(accept(hr, ex["token"], docs))


def test_a_template_changed_after_the_invitation_makes_the_link_dead(pair):
    hr, m, s, docs = pair
    out = invite(hr, docs)
    tok = out["token"]
    hr.ok(resolve(hr, tok))
    tpl = hr.svc._template("sow_seo_audit")
    hr.ok(hr.apost("/legal/v1/documents/sow_seo_audit/versions/1.0/decision",
                   {"request_id": rid(), "decision": "retire", "version_sha256": tpl["sha256"]}))
    _not_found(resolve(hr, tok))
    r = accept(hr, tok, docs)
    assert r.status_code == 404
    view = hr.ok(hr.client.get(f"/legal/v1/client-acceptance-invitations/{out['invitation_id']}",
                               headers=hr.headers(andre=ANDRE_TOKEN)))
    assert view["status"] == "open" and view["usable"] is False
    assert "TEMPLATE_NOT_IN_FORCE" in [x["code"] for x in view["reasons"]]
    assert not hr.svc.acceptances


def test_a_retired_or_superseded_msa_after_the_invitation_makes_the_link_dead(pair):
    hr, m, s, docs = pair
    tok = invite(hr, docs)["token"]
    v2 = hr.upload("client_msa", "Z Best Media Master Services Agreement v2.", "2.0", "zbm")
    founder_approve(hr, "client_msa", "2.0", v2["sha256"])
    _not_found(resolve(hr, tok))                                          # a newer standard form is current
    tok2 = invite(hr, docs_of(m, s), code=409)
    assert "VERSION_SUPERSEDED" in [x["code"] for x in tok2["reasons"]]
    tok3 = invite(hr, docs_of({**v2, "version": "2.0"}, s))["token"]
    hr.ok(hr.apost("/legal/v1/documents/client_msa/versions/2.0/decision",
                   {"request_id": rid(), "decision": "retire", "version_sha256": v2["sha256"]}))
    _not_found(resolve(hr, tok3))


# --- 3. recording the acceptance --------------------------------------------------------------------------------

def test_the_portal_records_each_document_once_with_its_evidence(pair):
    hr, m, s, docs = pair
    out = invite(hr, docs)
    page = page_of(hr, out["token"])
    r = hr.ok(accept(hr, out["token"], docs), 201)
    assert r["invitation_id"] == out["invitation_id"] and len(r["acceptances"]) == 2
    for a, d in zip(r["acceptances"], docs):
        assert (a["doc_id"], a["version"], a["doc_sha256"]) == (d["doc_id"], d["version"], d["doc_sha256"])
        assert a["party_ref"] == CLIENT and a["via"] == "client_portal" and a["invitation_id"] == out["invitation_id"]
        assert a["evidence_sufficient"] is True and a["evidence_basis"] == "founder_determination"
        assert a["basis_in_force"] is True and a["method"] == "clickwrap_unticked_box"
        # Finance and Compliance read the same answer (FIN-29, HR-15)
        fin = hr.ok(hr.get(f"/legal/v1/acceptances/{a['acceptance_id']}", caller="finance_31"))
        assert fin["evidence_sufficient"] is True and fin["party_ref"] == CLIENT
    rec = hr.svc.acceptances[r["acceptances"][0]["acceptance_id"]]
    pe = rec["portal_evidence"]
    assert hr.svc.blobs.get(pe["page_sha256"]) == page and pe["page_sha256"] == hashlib.sha256(page).hexdigest()
    assert pe["ip_hmac_sha256"] == HMAC_IP and pe["user_agent_hmac_sha256"] == HMAC_UA and pe["hmac_key_id"] == KEY_ID
    sig = json.loads(hr.svc.blobs.get(pe["signatory_sha256"]))
    assert sig == SIGNATORY and rec["signer_identity_ref"] == f"sig:{pe['signatory_sha256'][:40]}"
    assert rec["entry_by"] == "client_portal" and rec["presentation"] == "inline"
    # the signatory, the client's contact and the token never reach a record, an event or the export
    text = hr.all_text()
    for secret in (SIGNATORY["full_name"], SIGNATORY["title"], "legal@acme.example", out["token"]):
        assert secret not in text
    inv = hr.ok(hr.client.get(f"/legal/v1/client-acceptance-invitations/{out['invitation_id']}",
                              headers=hr.headers(andre=ANDRE_TOKEN)))
    assert inv["status"] == "accepted" and sorted(inv["acceptance_ids"]) == sorted(a["acceptance_id"]
                                                                                  for a in r["acceptances"])
    assert [e["payload"]["invitation_id"] for e in hr.ledger.of_type("client_acceptance_link_used")] == \
        [out["invitation_id"]]


def test_a_retry_of_the_same_submission_answers_the_same_and_a_new_one_is_404(pair):
    hr, _, _, docs = pair
    tok = invite(hr, docs)["token"]
    body = accept_body(tok, docs, h=hr)
    first = hr.ok(hr.post("/legal/v1/portal/acceptances", body, caller="client_portal"), 201)
    again = hr.ok(hr.post("/legal/v1/portal/acceptances", body, caller="client_portal"), 201)
    assert again["acceptances"] == first["acceptances"]
    assert len(hr.svc.acceptances) == 2
    _not_found(hr.post("/legal/v1/portal/acceptances", accept_body(tok, docs, h=hr), caller="client_portal"))
    r = hr.post("/legal/v1/portal/acceptances", {**body, "signatory": {**SIGNATORY, "title": "CEO"}},
                caller="client_portal")
    assert r.status_code == 409                                           # same request id, another body
    assert len(hr.svc.acceptances) == 2


def test_concurrent_submissions_consume_the_token_exactly_once(pair):
    hr, _, _, docs = pair
    tok = invite(hr, docs)["token"]
    bodies = [accept_body(tok, docs, h=hr, signatory={**SIGNATORY, "title": f"Title {i}"}) for i in range(8)]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda b: hr.post("/legal/v1/portal/acceptances", b,
                                                  caller="client_portal").status_code, bodies))
    assert codes == [201] + [404] * 7
    assert len(hr.svc.acceptances) == 2


@pytest.mark.parametrize("change", ["hash", "presented", "unticked", "missing_doc", "extra_doc", "version", "page"])
def test_a_tampered_submission_is_refused_and_does_not_use_up_the_link(pair, change):
    hr, m, s, docs = pair
    tok = invite(hr, docs)["token"]
    body = accept_body(tok, docs, h=hr)
    d0 = body["documents"][0]
    if change == "hash":
        d0["doc_sha256"] = d0["presented_sha256"] = "b" * 64
    elif change == "presented":
        d0["presented_sha256"] = "c" * 64
    elif change == "unticked":
        d0["affirmative_act"] = False
    elif change == "missing_doc":
        body["documents"] = body["documents"][1:]
    elif change == "extra_doc":
        body["documents"].append({**body["documents"][1], "doc_id": "sow"})
    elif change == "version":
        d0["version"] = "1.1"
    elif change == "page":
        body["page_sha256"] = "d" * 64
    r = hr.post("/legal/v1/portal/acceptances", body, caller="client_portal")
    assert r.status_code in (409, 422), r.text
    assert not hr.svc.acceptances
    hr.ok(accept(hr, tok, docs), 201)                                     # the client can still accept


def test_a_signatory_field_or_any_string_carrying_an_ip_or_a_forbidden_key_is_refused(pair):
    hr, _, _, docs = pair
    tok = invite(hr, docs)["token"]
    assert accept(hr, tok, docs, signatory={**SIGNATORY, "title": "CEO at 10.0.0.1"}).status_code == 422
    assert accept(hr, tok, docs, ip="203.0.113.9").status_code == 422
    assert accept(hr, tok, docs, signatory={**SIGNATORY, "full_name": ""}).status_code == 422
    assert accept(hr, tok, docs, signatory={**SIGNATORY, "full_name": "x" * 121}).status_code == 422
    assert accept(hr, tok, docs, hmac_key_id="XYZ").status_code == 422
    hr.ok(accept(hr, tok, docs), 201)


def test_new_sow_page_for_a_client_who_accepted_the_msa(pair):
    hr, m, s, docs = pair
    hr.ok(accept(hr, invite(hr, docs_of(m, s))["token"], docs_of(m, s)), 201)
    s2 = sow_fill(hr, fee="2500.00")
    out = invite(hr, docs_of(s2))
    assert out["page"] == "new_sow"
    r = hr.ok(resolve(hr, out["token"]))
    assert r["msa_acceptance"]["version"] == "1.0" and r["msa_acceptance"]["accepted_at"]


def test_a_sow_alone_for_a_client_without_an_msa_acceptance_has_no_page():
    x = Harness()
    x.approve_rules()
    determine_cq19(x)
    s = sow_fill(x)
    out = invite(x, docs_of(s))
    assert out["page"] == "new_sow" and out["warnings"]
    assert x.ok(resolve(x, out["token"]))["msa_acceptance"] is None


# --- 4. no other way to record a client's acceptance -------------------------------------------------------------

@pytest.mark.parametrize("who", ["onboarding", "hub", "clipper_network"])
def test_no_caller_records_a_clients_acceptance_outside_the_page(pair, who):
    hr, m, _, _ = pair
    old = hr.apost("/legal/v1/acceptance-invitations", {"request_id": rid(), "doc_id": "client_msa", "version": "1.0",
                                                         "party_ref": CLIENT})
    body = {"request_id": rid(), "party_ref": CLIENT, "signer_identity_ref": "mallory", "doc_id": "client_msa",
            "version": "1.0", "doc_sha256": m["sha256"], "presented_sha256": m["sha256"],
            "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept", "affirmative_act": True}
    if old.status_code == 201:
        body["invitation_id"] = old.json()["invitation_id"]
    r = hr.post("/legal/v1/acceptances", body, caller=who)
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    assert not hr.svc.acceptances


def test_the_portal_caller_has_no_other_route(pair):
    hr, m, _, docs = pair
    body = {"request_id": rid(), "party_ref": CLIENT, "signer_identity_ref": "x", "doc_id": "client_msa",
            "version": "1.0", "doc_sha256": m["sha256"], "presented_sha256": m["sha256"],
            "method": "clickwrap_unticked_box", "presentation": "inline", "affirmative_act": True}
    assert hr.post("/legal/v1/acceptances", body, caller="client_portal").status_code == 403
    for path in ("/legal/v1/documents/client_msa/current", "/legal/v1/documents/client_msa/versions/1.0",
                 "/legal/v1/rules", "/legal/v1/audit/export"):
        assert hr.get(path, caller="client_portal").status_code == 403, path


def test_a_client_acceptance_recorded_any_other_way_is_never_sufficient(pair):
    hr, m, _, _ = pair
    # a record as an older build wrote it (onboarding path): it stays in the log, it never counts
    aid = "lg-acc-" + "0" * 26
    rec = {"acceptance_id": aid, "party_ref": CLIENT, "signer_identity_ref": "mallory", "doc_id": "client_msa",
           "version": "1.0", "doc_sha256": m["sha256"], "presented_sha256": m["sha256"],
           "accepted_at": hr.clock.now().isoformat(), "method": "clickwrap_unticked_box",
           "presentation": "scroll_to_accept", "affirmative_act": True, "esign_consent": None, "evidence_ref": None,
           "entry_by": "onboarding", "evidence_sufficient": True, "evidence_basis": "founder_determination",
           "evidence_lineage": hr.svc.register["CQ-19"]["founder_determination"].get("lineage")
           or hr.svc.register["CQ-19"]["founder_determination"]["determined_at"]}
    hr.svc.acceptances[aid] = rec
    view = hr.ok(hr.get(f"/legal/v1/acceptances/{aid}", caller="finance_31"))
    assert view["evidence_sufficient"] is False


# --- 5. revoke, Andre's view, restart -----------------------------------------------------------------------------

def test_revoke_is_final_and_an_accepted_link_cannot_be_revoked(pair):
    hr, _, _, docs = pair
    a = invite(hr, docs)
    path = f"/legal/v1/client-acceptance-invitations/{a['invitation_id']}"
    out = hr.ok(hr.apost(f"{path}/revoke", {"request_id": rid()}))
    assert out["status"] == "revoked" and out["revoked_at"]
    assert hr.ok(hr.client.get(path, headers=hr.headers(andre=ANDRE_TOKEN)))["status"] == "revoked"
    b = invite(hr, docs)
    hr.ok(accept(hr, b["token"], docs), 201)
    r = hr.apost(f"/legal/v1/client-acceptance-invitations/{b['invitation_id']}/revoke", {"request_id": rid()})
    assert r.status_code == 409
    assert hr.apost("/legal/v1/client-acceptance-invitations/lg-cin-" + "0" * 26 + "/revoke",
                    {"request_id": rid()}).status_code == 404


def test_andre_sees_the_status_and_expiry(pair):
    hr, _, _, docs = pair
    a = invite(hr, docs)
    path = f"/legal/v1/client-acceptance-invitations/{a['invitation_id']}"
    v = hr.ok(hr.client.get(path, headers=hr.headers(andre=ANDRE_TOKEN)))
    assert v["status"] == "open" and v["usable"] is True and v["reasons"] == [] and v["acceptance_ids"] == []
    assert "token" not in json.dumps(v).replace("token_shown_once", "")
    hr.clock.advance(days=15)
    assert hr.ok(hr.client.get(path, headers=hr.headers(andre=ANDRE_TOKEN)))["status"] == "expired"


def test_a_restart_keeps_the_link_used_and_the_token_is_not_on_disk(tmp_path, pair):
    hr0, _, _, _ = pair
    x = Harness(data_dir=str(tmp_path))
    x.approve_rules()
    determine_cq19(x)
    m, s = msa(x), sow_fill(x)
    docs = docs_of(m, s)
    live, used = invite(x, docs), invite(x, docs)
    x.ok(accept(x, used["token"], docs), 201)
    y = Harness(data_dir=str(tmp_path), ledger=x.ledger, clock=x.clock)
    _not_found(resolve(y, used["token"]))
    y.ok(resolve(y, live["token"]))
    assert len(y.svc.acceptances) == 2
    for p in tmp_path.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert live["token"].encode() not in data and used["token"].encode() not in data, p


# --- 6. ZBM's entity facts come from configuration ---------------------------------------------------------------

ZBM_VARS = {"zbm_legal_entity_name": {"type": "string", "max_length": 200},
            "zbm_street_address": {"type": "string", "max_length": 200}}
FACTS = {"zbm_legal_entity_name": "Z Best Media Example Co.", "zbm_street_address": "1 Example Way"}


def _msa_template(h):
    t = h.upload("client_msa", "Agreement between {{zbm_legal_entity_name}} ({{zbm_street_address}}) and "
                 "{{client_legal_name}}.", "1.0", "zbm",
                 template_variables={**ZBM_VARS, "client_legal_name": {"type": "string", "max_length": 200}})
    founder_approve(h, "client_msa", "1.0", t["sha256"])
    return t


def test_zbm_entity_facts_are_filled_from_configuration():
    x = Harness(env={"LEGAL_ZBM_FACTS": json.dumps(FACTS)})
    x.approve_rules()
    _msa_template(x)
    f = x.ok(x.apost("/legal/v1/documents/client_msa/versions",
                     {"request_id": rid(), "entity": "zbm", "party_ref": CLIENT,
                      "variables": {"client_legal_name": "Acme LLC"}}), 201)
    text = x.svc.blobs.get(f["sha256"]).decode()
    assert text == "Agreement between Z Best Media Example Co. (1 Example Way) and Acme LLC."
    # the request never supplies a ZBM fact
    r = x.apost("/legal/v1/documents/client_msa/versions",
                {"request_id": rid(), "entity": "zbm", "party_ref": CLIENT,
                 "variables": {"client_legal_name": "Acme LLC", "zbm_legal_entity_name": "Someone Else"}})
    assert r.status_code == 422


def test_a_missing_zbm_fact_refuses_the_fill():
    x = Harness(env={"LEGAL_ZBM_FACTS": json.dumps({"zbm_legal_entity_name": "Z Best Media Example Co."})})
    x.approve_rules()
    _msa_template(x)
    r = x.apost("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm", "party_ref": CLIENT,
                                                            "variables": {"client_legal_name": "Acme LLC"}})
    assert r.status_code == 409 and "ZBM_FACT_MISSING" in r.text


@pytest.mark.parametrize("raw", ['{"legal_entity_name": "x"}', '{"zbm_x": ""}', '{"zbm_x": "{{y}}"}', "[]", "{",
                                 '{"zbm_x": "a\\u0000b"}', '{"zbm_x": 5}'])
def test_bad_zbm_facts_refuse_to_start(raw):
    with pytest.raises(RuntimeError):
        config_mod.load({"LEGAL_SERVICE_TOKEN": "s" * 40, "LEGAL_ZBM_FACTS": raw})


# --- 7. Andre reads the evidence back (the confirmation email, a dispute) -----------------------------------------

def test_andre_reads_the_signatory_and_the_evidence_of_a_used_link(pair):
    hr, _, _, docs = pair
    out = invite(hr, docs)
    path = f"/legal/v1/client-acceptance-invitations/{out['invitation_id']}/evidence"
    assert hr.client.get(path, headers=hr.headers(andre=ANDRE_TOKEN)).status_code == 409     # not used yet
    page = page_of(hr, out["token"])
    r = hr.ok(accept(hr, out["token"], docs), 201)
    ev = hr.ok(hr.client.get(path, headers=hr.headers(andre=ANDRE_TOKEN)))
    assert ev["signatory"] == SIGNATORY and ev["display"] == DISPLAY and ev["accepted_at"] == r["accepted_at"]
    assert [a["acceptance_id"] for a in ev["acceptances"]] == [a["acceptance_id"] for a in r["acceptances"]]
    assert base64.b64decode(ev["page_text_b64"]) == page and ev["page_sha256"] == hashlib.sha256(page).hexdigest()
    assert (ev["ip_hmac_sha256"], ev["user_agent_hmac_sha256"], ev["hmac_key_id"]) == (HMAC_IP, HMAC_UA, KEY_ID)
    for who in ("onboarding", "client_portal", "finance_31", "compliance_38"):
        assert hr.get(path, caller=who).status_code == 403
