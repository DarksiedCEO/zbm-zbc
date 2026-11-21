"""Money red team RTM-13 (Oct 30-31 2026): the onboarding token may record a client's acceptance of a standard form
(a version bound to no party) only with an acceptance invitation Andre issued for THAT client, that document and that
version (single use, 14 days). A version bound to a party keeps its own binding; the clipper network's standard
clipper agreement path is unchanged. (The client-facing acceptance page will carry the invitation id.)
"""

from __future__ import annotations

from helpers import Harness, rid


def _setup():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    v = x.approve_doc("client_msa", "Client MSA: the same text for every client, no fields to fill.")
    return x, v


def _accept(x, v, party, caller="onboarding", **over):
    body = {"request_id": rid("acc"), "party_ref": party, "signer_identity_ref": party.split(":", 1)[1],
            "doc_id": "client_msa", "version": "1.0", "doc_sha256": v["sha256"], "presented_sha256": v["sha256"],
            "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept", "affirmative_act": True, **over}
    return x.post("/legal/v1/acceptances", body, caller=caller)


def _invite(x, party, doc_id="client_msa", version="1.0", code=201):
    r = x.apost("/legal/v1/acceptance-invitations", {"request_id": rid("inv"), "doc_id": doc_id,
                                                      "version": version, "party_ref": party})
    assert r.status_code == code, r.text
    return r.json()


def _clipper(x):
    return x.approve_doc("clipper_agreement", "clipper agreement text")


def _accept_clipper(x, cv, party, **over):
    body = {"request_id": rid("acc"), "party_ref": party, "signer_identity_ref": party.split(":", 1)[1],
            "doc_id": "clipper_agreement", "version": cv.get("version", "1.0"), "doc_sha256": cv["sha256"],
            "presented_sha256": cv["sha256"], "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept",
            "affirmative_act": True, "esign_consent": {"disclosure_version": "1.0", "consented_at":
                                                       "2026-10-01T16:59:00Z", "access_demonstrated": True}, **over}
    return x.post("/legal/v1/acceptances", body, caller="clipper_network")


def test_onboarding_cannot_record_a_standard_form_acceptance_without_an_invitation():
    x, v = _setup()
    r = _accept(x, v, "client:someone-else")
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    assert not x.svc.acceptances


def test_an_invitation_binds_the_acceptance_to_one_party_once():
    """AEGIS L-4 (ADR 0028): no RTM-13 invitation is issued for a client any more (the client acceptance page is the
    client's path); for every other party the binding is unchanged: one party, one document version, once."""
    x, v = _setup()
    cv = _clipper(x)
    _invite(x, "client:acme", code=409)
    inv = _invite(x, "clipper:cn-a", doc_id="clipper_agreement")
    assert _accept_clipper(x, cv, "clipper:cn-b", invitation_id=inv["invitation_id"]).status_code == 409
    r = _accept_clipper(x, cv, "clipper:cn-a", invitation_id=inv["invitation_id"])
    assert r.status_code == 201, r.text
    assert r.json()["invitation_id"] == inv["invitation_id"]
    again = _accept_clipper(x, cv, "clipper:cn-a", invitation_id=inv["invitation_id"])
    assert again.status_code == 409 and again.json()["detail"] == "ACCEPTANCE_NOT_INVITED"           # single use


def test_an_invitation_expires_and_names_its_document_version():
    from datetime import timedelta
    x, v = _setup()
    cv = _clipper(x)
    inv = _invite(x, "clipper:cn-a", doc_id="clipper_agreement")
    x.clock.at = x.clock.at + timedelta(days=15)
    assert _accept_clipper(x, cv, "clipper:cn-a", invitation_id=inv["invitation_id"]).status_code == 409
    _invite(x, "clipper:cn-a", doc_id="clipper_agreement", version="9.9", code=409)   # no such version


def test_only_andre_issues_invitations():
    x, v = _setup()
    r = x.post("/legal/v1/acceptance-invitations", {"request_id": rid("inv"), "doc_id": "client_msa",
                                                     "version": "1.0", "party_ref": "client:acme"},
               caller="onboarding")
    assert r.status_code in (401, 403)
