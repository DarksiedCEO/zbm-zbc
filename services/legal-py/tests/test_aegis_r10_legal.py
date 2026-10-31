"""AEGIS r10 R10-L1, L2, L3 (introduced by cf563ce): an acceptance's invitation id never poisons the log, never burns
another client's invitation, and only the onboarding path records a client's acceptance.

- An unknown, used, expired or mismatched ``invitation_id`` is refused 409 ``ACCEPTANCE_NOT_INVITED`` for EVERY caller
  and writes nothing (before cf563ce's fix a bogus id gave 500 and a log line that stopped legal-py from restarting).
- A log line that still updates a missing record (written by the old code) never stops the start: the bad record is
  quarantined, the rest of the line applies, and ``/health`` flags it for Andre.
- ``hub`` and ``clipper_network`` no longer record a ``client:*`` acceptance at all (ADR 0022 / RTM-13: the
  founder/onboarding path only).
"""

from __future__ import annotations

import pytest

from helpers import Harness, rid


def _setup(tmp_path=None):
    x = Harness(wired=True, data_dir=str(tmp_path / "d") if tmp_path else None)
    x.approve_rules()
    x.engage()
    v = x.approve_doc("client_msa", "Client MSA: the same text for every client, no fields to fill.")
    return x, v


def _clipper_doc(x):
    return x.approve_doc("clipper_agreement", "clipper agreement text")


def _accept(x, sha, party, caller="onboarding", doc_id="client_msa", version="1.0", **over):
    body = {"request_id": rid("acc"), "party_ref": party, "signer_identity_ref": party.split(":", 1)[1],
            "doc_id": doc_id, "version": version, "doc_sha256": sha, "presented_sha256": sha,
            "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept", "affirmative_act": True, **over}
    if doc_id == "clipper_agreement":
        body.setdefault("esign_consent", {"disclosure_version": "1.0", "consented_at": "2026-10-01T16:59:00Z",
                                          "access_demonstrated": True})
    return x.post("/legal/v1/acceptances", body, caller=caller)


def _invite(x, party, doc_id="client_msa", version="1.0"):
    r = x.apost("/legal/v1/acceptance-invitations", {"request_id": rid("inv"), "doc_id": doc_id,
                                                      "version": version, "party_ref": party})
    assert r.status_code == 201, r.text
    return r.json()


def _restart(x, tmp_path):
    return Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports)


@pytest.mark.parametrize("caller,party,doc", [("hub", "clipper:cn-clp-1", "clipper_agreement"),
                                              ("clipper_network", "clipper:cn-clp-1", "clipper_agreement"),
                                              ("onboarding", "client:acme", "client_msa")])
def test_a_bogus_invitation_id_is_a_4xx_for_every_caller_and_the_service_restarts(tmp_path, caller, party, doc):
    x, v = _setup(tmp_path)
    sha = _clipper_doc(x)["sha256"] if doc == "clipper_agreement" else v["sha256"]
    lines = len(x.svc.log)
    r = _accept(x, sha, party, caller=caller, doc_id=doc, invitation_id="inv-doesnotexist00000000")
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    assert not x.svc.acceptances
    assert len(x.svc.log) == lines or all(rec["kind"] != "acceptance" for rec in x.svc.log.iter_records())
    y = _restart(x, tmp_path)                                    # never a KeyError on replay
    assert y.ok(y.client.get("/health"))["status"] == "ok"
    assert not y.svc.acceptances


def test_no_caller_can_burn_another_clients_invitation():
    x, v = _setup()
    cv = _clipper_doc(x)
    inv = _invite(x, "client:acme")
    r = _accept(x, cv["sha256"], "clipper:cn-clp-1", caller="clipper_network", doc_id="clipper_agreement",
                invitation_id=inv["invitation_id"])
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    assert x.svc.invitations[inv["invitation_id"]]["used_by"] is None
    ok = _accept(x, v["sha256"], "client:acme", invitation_id=inv["invitation_id"])     # acme's own acceptance
    assert ok.status_code == 201, ok.text


def test_an_invitation_for_another_client_or_document_is_refused_and_stays_unused():
    x, v = _setup()
    inv = _invite(x, "client:acme")
    r = _accept(x, v["sha256"], "client:globex", invitation_id=inv["invitation_id"])
    assert r.status_code == 409
    assert x.svc.invitations[inv["invitation_id"]]["used_by"] is None


@pytest.mark.parametrize("caller", ["hub", "clipper_network"])
def test_only_the_onboarding_path_records_a_clients_acceptance(caller):
    x, v = _setup()
    r = _accept(x, v["sha256"], "client:acme", caller=caller)
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    inv = _invite(x, "client:acme")
    r = _accept(x, v["sha256"], "client:acme", caller=caller, invitation_id=inv["invitation_id"])
    assert r.status_code == 409, r.text                          # not even with a valid invitation
    assert not x.svc.acceptances
    assert x.svc.invitations[inv["invitation_id"]]["used_by"] is None


def test_the_clipper_network_still_records_a_clippers_standard_agreement():
    x, _ = _setup()
    cv = _clipper_doc(x)
    r = _accept(x, cv["sha256"], "clipper:cn-clp-1", caller="clipper_network", doc_id="clipper_agreement")
    assert r.status_code == 201, r.text


def test_l5_the_invitation_names_its_version():
    """AEGIS r10 mutant l5: an invitation for version 1.0 does not admit an acceptance of an EXISTING version 1.1."""
    x, v = _setup()
    v11 = x.approve_doc("client_msa", "Client MSA v1.1: still one text for every client.", version="1.1")
    inv = _invite(x, "client:acme", version="1.0")
    r = _accept(x, v11["sha256"], "client:acme", version="1.1", invitation_id=inv["invitation_id"])
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    assert x.svc.invitations[inv["invitation_id"]]["used_by"] is None


def test_a_poisoned_log_line_never_stops_the_start_it_is_quarantined_and_flagged(tmp_path):
    """A line written by the pre-fix code (an update of an invitation that does not exist, after the acceptance's own
    records) is replayed: the bad record is quarantined, the rest of the line applies, and /health says so."""
    x, v = _setup(tmp_path)
    from service import Op
    svc = x.svc
    op = Op(svc, "acc|poison", "intel_03_clickwrap", "client:acme")
    op.update("invitations", "inv-doesnotexist00000000", {"used_by": "acc-x", "used_at": "2026-10-02T00:00:00Z"})
    svc._check_updates = lambda _op: None                        # the pre-fix commit path: no guard
    with svc.lock, pytest.raises(KeyError):
        svc._commit(op)
    y = _restart(x, tmp_path)
    h = y.ok(y.client.get("/health"))
    assert h["replay_quarantined"] == 1 and h["status"] == "degraded", h
    assert y.svc.replay_quarantine[0]["kind"] == "update" and "invitations" in y.svc.replay_quarantine[0]["detail"]
    # the service works: a real acceptance is recorded after the restart
    inv = _invite(y, "client:acme")
    assert _accept(y, v["sha256"], "client:acme", invitation_id=inv["invitation_id"]).status_code == 201


def test_the_commit_refuses_an_update_of_a_missing_record_before_anything_is_written():
    x, _ = _setup()
    from errors import Conflict
    from service import Op
    op = Op(x.svc, "acc|poison", "intel_03_clickwrap", "client:acme")
    op.update("invitations", "inv-doesnotexist00000000", {"used_by": "acc-x"})
    n = len(x.svc.log)
    with x.svc.lock, pytest.raises(Conflict):
        x.svc._commit(op)
    assert len(x.svc.log) == n
