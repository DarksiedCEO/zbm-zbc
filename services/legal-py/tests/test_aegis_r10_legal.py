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

import json

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


def test_no_caller_can_burn_another_partys_invitation():
    x, v = _setup()
    cv = _clipper_doc(x)
    assert x.apost("/legal/v1/acceptance-invitations", {"request_id": rid("inv"), "doc_id": "client_msa",
                                                         "version": "1.0", "party_ref": "client:acme"}).status_code == 409
    inv = _invite(x, "clipper:cn-clp-1", doc_id="clipper_agreement")
    r = _accept(x, cv["sha256"], "clipper:cn-clp-2", caller="clipper_network", doc_id="clipper_agreement",
                invitation_id=inv["invitation_id"])
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    assert x.svc.invitations[inv["invitation_id"]]["used_by"] is None
    ok = _accept(x, cv["sha256"], "clipper:cn-clp-1", caller="clipper_network", doc_id="clipper_agreement",
                 invitation_id=inv["invitation_id"])
    assert ok.status_code == 201, ok.text


def test_an_invitation_for_another_party_or_document_is_refused_and_stays_unused():
    x, v = _setup()
    cv = _clipper_doc(x)
    inv = _invite(x, "clipper:cn-clp-1", doc_id="clipper_agreement")
    r = _accept(x, cv["sha256"], "clipper:cn-clp-9", caller="clipper_network", doc_id="clipper_agreement",
                invitation_id=inv["invitation_id"])
    assert r.status_code == 409
    assert x.svc.invitations[inv["invitation_id"]]["used_by"] is None


@pytest.mark.parametrize("caller", ["hub", "clipper_network"])
def test_only_the_onboarding_path_records_a_clients_acceptance(caller):
    x, v = _setup()
    r = _accept(x, v["sha256"], "client:acme", caller=caller)
    assert r.status_code == 409 and r.json()["detail"] == "ACCEPTANCE_NOT_INVITED", r.text
    # AEGIS L-4 (ADR 0028): no invitation is even issued for a client on this path
    assert x.apost("/legal/v1/acceptance-invitations", {"request_id": rid("inv"), "doc_id": "client_msa",
                                                         "version": "1.0", "party_ref": "client:acme"}).status_code == 409
    assert not x.svc.acceptances and not x.svc.invitations


def test_the_clipper_network_still_records_a_clippers_standard_agreement():
    x, _ = _setup()
    cv = _clipper_doc(x)
    r = _accept(x, cv["sha256"], "clipper:cn-clp-1", caller="clipper_network", doc_id="clipper_agreement")
    assert r.status_code == 201, r.text


def test_l5_the_invitation_names_its_version():
    """AEGIS r10 mutant l5: an invitation for version 1.0 does not admit an acceptance of an EXISTING version 1.1."""
    x, v = _setup()
    _clipper_doc(x)
    v11 = x.approve_doc("clipper_agreement", "clipper agreement text v1.1", version="1.1")
    inv = _invite(x, "clipper:cn-clp-1", doc_id="clipper_agreement", version="1.0")
    r = _accept(x, v11["sha256"], "clipper:cn-clp-1", caller="clipper_network", doc_id="clipper_agreement",
                version="1.1", invitation_id=inv["invitation_id"])
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
    # the service works: a real acceptance is recorded after the restart (a clipper's standard agreement; a client's
    # acceptance is recorded only on the client acceptance page, ADR 0028)
    cv = _clipper_doc(y)
    assert _accept(y, cv["sha256"], "clipper:cn-clp-1", caller="clipper_network",
                   doc_id="clipper_agreement").status_code == 201


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


# --- AEGIS r11 R11-6: replay never resurrects a write the caller saw fail; Andre reviews and clears the quarantine -----

def _poison_line(svc, ops):
    """Append a line the pre-fix code could write (anchored, the runtime apply then failing)."""
    from service import Op
    op = Op(svc, f"acc|poison|{len(svc.log)}", "intel_03_clickwrap", "client:acme")
    for kind, rec in ops:
        if kind == "update":
            op.update(*rec)
        else:
            op.add(kind, rec)
    svc._check_updates = lambda _op: None                        # the pre-fix commit path: no guard
    with svc.lock:
        try:
            svc._commit(op)
        except KeyError:
            pass


_BAD = ("update", ("invitations", "inv-doesnotexist00000000", {"used_by": "acc-poison"}))
_ACC = ("acceptance", {"acceptance_id": "acc-poison", "party_ref": "client:acme", "doc_id": "client_msa",
                       "version": "1.0"})


@pytest.mark.parametrize("order", ["update_first", "acceptance_first"])
def test_r11_6_lp1_a_failed_line_is_never_applied_on_replay(tmp_path, order):
    x, v = _setup(tmp_path)
    _poison_line(x.svc, [_BAD, _ACC] if order == "update_first" else [_ACC, _BAD])
    y = _restart(x, tmp_path)
    assert "acc-poison" not in y.svc.acceptances                 # the caller was told it failed
    h = y.ok(y.client.get("/health"))
    assert h["status"] == "degraded" and h["replay_quarantined"] == 1, h
    assert any("update" in q["kinds"] and "acceptance" in q["kinds"] for q in y.svc.replay_quarantine)


def test_r11_6_andre_reviews_and_clears_the_quarantine_and_health_returns_to_ok(tmp_path):
    from helpers import ANDRE_TOKEN
    x, v = _setup(tmp_path)
    _poison_line(x.svc, [_BAD, _ACC])
    y = _restart(x, tmp_path)
    assert y.client.get("/legal/v1/replay-quarantine", headers=y.headers(caller="hub")).status_code in (401, 403)
    lst = y.ok(y.client.get("/legal/v1/replay-quarantine", headers=y.headers(andre=ANDRE_TOKEN)))
    seqs = sorted({q["seq"] for q in lst["quarantined"]})
    assert seqs and lst["unacknowledged"] == len(lst["quarantined"])
    r = y.post("/legal/v1/replay-quarantine/acknowledgements", {"request_id": rid("qa"), "seqs": seqs}, caller="hub")
    assert r.status_code in (401, 403)
    y.ok(y.post("/legal/v1/replay-quarantine/acknowledgements", {"request_id": rid("qa"), "seqs": seqs},
                andre=ANDRE_TOKEN))
    assert y.ok(y.client.get("/health"))["status"] == "ok"
    z = _restart(y, tmp_path)                                    # the acknowledgement is durable
    h = z.ok(z.client.get("/health"))
    assert h["status"] == "ok" and h["replay_quarantined"] == 0, h
    assert "acc-poison" not in z.svc.acceptances


def test_r11_6_a_runtime_apply_failure_is_marked_durably_and_skipped_on_replay(tmp_path):
    x, v = _setup(tmp_path)
    svc = x.svc
    real = svc._apply
    calls = {"n": 0}

    def flaky(kind, r):                                          # an apply that fails after the line is appended
        if kind == "acceptance" and r.get("acceptance_id") == "acc-flaky":
            raise TypeError("boom")
        return real(kind, r)
    svc._apply = flaky
    from service import Op
    op = Op(svc, "acc|flaky", "intel_03_clickwrap", "client:acme")
    op.add("acceptance", {"acceptance_id": "acc-flaky", "party_ref": "client:acme", "doc_id": "client_msa",
                          "version": "1.0"})
    with svc.lock, pytest.raises(TypeError):
        svc._commit(op)
    svc._apply = real
    assert any(rec["kind"] == "apply_failed" for rec in svc.log.iter_records())
    y = _restart(x, tmp_path)
    assert "acc-flaky" not in y.svc.acceptances


# --- AEGIS r12 R12-6 and the surviving guards l5, l7, l8 -----------------------------------------------------------------

def _flaky_line(x, fail_id, ids):
    from service import Op
    svc = x.svc
    real = svc._apply

    def flaky(kind, r):
        if kind == "acceptance" and r.get("acceptance_id") == fail_id:
            raise TypeError("boom")
        return real(kind, r)
    svc._apply = flaky
    op = Op(svc, f"acc|{'-'.join(ids)}", "intel_03_clickwrap", "client:acme")
    for i in ids:
        op.add("acceptance", {"acceptance_id": i, "party_ref": "client:acme", "doc_id": "client_msa", "version": "1.0"})
    with svc.lock, pytest.raises(TypeError):
        svc._commit(op)
    svc._apply = real


def test_r12_6_lp3_a_failed_line_leaves_nothing_in_memory_and_later_writes_survive_a_restart(tmp_path):
    from errors import Conflict
    from service import Op
    x, v = _setup(tmp_path)
    _flaky_line(x, "acc-B", ["acc-A", "acc-B"])
    assert "acc-A" not in x.svc.acceptances                         # rolled back: the caller was told it failed
    h = x.ok(x.client.get("/health"))
    assert h["status"] == "degraded", h                             # l7: apply_fault degrades until a restart
    op2 = Op(x.svc, "acc|A-upd", "intel_03_clickwrap", "client:acme")
    op2.update("acceptances", "acc-A", {"note": "later"})
    with x.svc.lock, pytest.raises(Conflict):                       # nothing can build on the failed write
        x.svc._commit(op2)
    op3 = Op(x.svc, "acc|C", "intel_03_clickwrap", "client:acme")   # an unrelated later write succeeds ...
    op3.add("acceptance", {"acceptance_id": "acc-C", "party_ref": "client:acme", "doc_id": "client_msa",
                           "version": "1.0"})
    with x.svc.lock:
        x.svc._commit(op3)
    y = _restart(x, tmp_path)                                       # ... and survives the restart
    assert "acc-C" in y.svc.acceptances and "acc-A" not in y.svc.acceptances
    assert not [q for q in y.svc.replay_quarantine if "acc-C" in json.dumps(q)]


def test_l5_acknowledging_a_line_that_is_not_quarantined_is_refused(tmp_path):
    from helpers import ANDRE_TOKEN
    x, v = _setup(tmp_path)
    r = x.post("/legal/v1/replay-quarantine/acknowledgements", {"request_id": rid("qa"), "seqs": [999]},
               andre=ANDRE_TOKEN)
    assert r.status_code == 409, r.text


def test_l8_the_replay_fallback_stops_a_line_at_its_first_failing_record(tmp_path):
    from service import Op
    x, v = _setup(tmp_path)
    svc = x.svc
    real = svc._apply
    svc._apply = lambda kind, r: None if kind == "job_run" else real(kind, r)   # runtime skips the odd record
    op = Op(svc, "acc|odd", "intel_03_clickwrap", "client:acme")
    op.add("acceptance", {"acceptance_id": "acc-1", "party_ref": "client:acme", "doc_id": "client_msa", "version": "1.0"})
    op.add("job_run", {"malformed": True})                           # replay's _apply raises KeyError on it
    op.add("acceptance", {"acceptance_id": "acc-2", "party_ref": "client:acme", "doc_id": "client_msa", "version": "1.0"})
    with svc.lock:
        svc._commit(op)
    svc._apply = real
    y = _restart(x, tmp_path)
    assert "acc-1" in y.svc.acceptances and "acc-2" not in y.svc.acceptances
    q = [q for q in y.svc.replay_quarantine if q["kinds"][:1] == ["job_run"]]
    assert q and q[0]["kinds"] == ["job_run", "acceptance"]
