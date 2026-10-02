"""Record-first: when the ledger (or the local store) cannot record, every write route answers 503
{"issued": false} and NOTHING changed — no local-log line, no state, no platform-data store change."""

from __future__ import annotations

import json

import pytest

from clock import iso
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from test_cert_scenarios import run_to_day


def _state(h):
    s = h.svc
    return json.dumps({"log": len(s.log), "certs": s.certs, "holds": s.holds, "findings": s.findings,
                       "strikes": s.strikes, "subs": s.submissions, "cons": s.connections, "ages": s.ages,
                       "ids": s.identities, "snaps": sorted(s.snapshots), "bans": s.bans, "atts": sorted(s.attestations),
                       "versions": len(s.versions), "props": s.proposals, "side": s.side.entries,
                       "jobs": sorted(map(list, s.job_runs))}, sort_keys=True, default=str)


def _prepared():
    h = Harness()
    h.approve_rules()
    h.clean_clip("n1")
    ref2 = "https://www.tiktok.com/@c/video/n2"
    h.post_video("tiktok", ref2, views=50000, likes=1)
    h.ok(h.register("n2", "clip-a", post_ref=ref2), 201)
    h.approve("n2")
    ref4 = "https://www.tiktok.com/@c/video/n4"
    h.post_video("tiktok", ref4)
    h.ok(h.register("n4", "clip-a", post_ref=ref4), 201)                 # registered, not yet approved
    run_to_day(h, 2)
    return h


def _writes(h):
    hold = [x for x in h.svc.holds.values() if x["status"] == "open"][0]["hold_id"]
    con = [c for c in h.svc.connections.values() if c["status"] == "active"][0]["connection_id"]
    row = dict(h.svc.current.by_id()["VI-15c"], statement="tightened")
    return [
        ("/vi/v1/connections/start", {"request_id": rid(), "clipper_id": "c9", "platform": "tiktok",
                                      "redirect_uri": "https://z.example/cb"}, {"caller": "clipper_network"}),
        (f"/vi/v1/connections/{con}/revoke", {"request_id": rid()}, {"caller": "clipper_network"}),
        ("/vi/v1/submissions", {"request_id": rid(), "submission_id": "n3", "campaign_id": "c", "rulebook_version": 1,
                                "clipper_id": "clip-a", "platform": "tiktok", "post_ref": "https://www.tiktok.com/@c/video/n3",
                                "posted_at": iso(h.clock.now()), "min_days_live": 7, "collab_permitted": False,
                                "media_ref": "m3"}, {"caller": "creative_production"}),
        ("/vi/v1/submissions/n4/approval", {"request_id": rid()}, {"caller": "creative_production"}),
        ("/vi/v1/clips/hr13", {"request_id": rid(), "submission_id": "n1", "post_ref": "x", "platform": "tiktok",
                               "posted_at": iso(NOW), "settlement_lag_days": 14}, {"caller": "compliance_38"}),
        ("/vi/v1/clips/attest", {"request_id": rid(), "submission_id": "n1", "facts": {
            "campaign_id": "camp-1", "rulebook_version": 1, "post_ref": "x", "clipper_id": "clip-a",
            "posted_at": iso(NOW)}}, {"caller": "creative_production"}),
        ("/vi/v1/results/attest", {"request_id": rid(), "result_id": "r1", "facts": {
            "result_id": "r1", "campaign_id": "camp-1", "submission_id": "n1", "vertical": "v", "platform": "tiktok",
            "angle_id": "a", "hook": "h", "source": "platform_export", "reported_views": 1}},
         {"caller": "creative_production"}),
        ("/vi/v1/age/checks", {"request_id": rid(), "subject_id": "k2", "dob": "1990-01-01", "dob_field_neutral": True,
                               "method": "photo_id_match", "provider_session_ref": "s"}, {"caller": "onboarding"}),
        ("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "k2", "email": "k2@example.com"},
         {"caller": "clipper_network"}),
        (f"/vi/v1/holds/{hold}/decision", {"request_id": rid(), "decision": "uphold", "reason": "x"},
         {"andre": ANDRE_TOKEN}),
        ("/vi/v1/bans", {"request_id": rid(), "clipper_id": "clip-a", "cn_decision_id": "d", "approved_at": iso(NOW)},
         {"caller": "clipper_network", "andre": ANDRE_TOKEN}),
        ("/vi/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "VI-15c", "proposed_row": row},
         {"andre": ANDRE_TOKEN}),
    ] + [(f"/vi/v1/jobs/{j}/run", {"request_id": rid()}, {"caller": "scheduler"})
         for j in ("liveness", "metrics", "revisions", "anomaly", "certify")]


@pytest.mark.parametrize("mode", ["ledger", "store"])
def test_every_write_route_has_no_effect_when_recording_fails(mode):
    h = _prepared()
    h.clock.advance(days=1)
    for path, body, kw in _writes(h):
        before = _state(h)
        if mode == "ledger":
            h.ledger.fail_all = True
        else:
            h.svc.log.fail_next_append = True
        r = h.post(path, body, **kw)
        h.ledger.fail_all = False
        h.svc.log.fail_next_append = False
        assert r.status_code == 503, (path, r.status_code, r.text[:300])
        assert r.json()["issued"] is False
        assert _state(h) == before, path


def test_age_answers_and_audit_export_need_the_ledger_too(hr):
    a = hr.ok(hr.age_check("k"))
    hr.ledger.fail_all = True
    assert hr.get(f"/vi/v1/age/attestations/{a['attestation_id']}", caller="compliance_38").status_code == 503
    assert hr.get("/vi/v1/age/subjects/k", caller="onboarding").status_code == 503
    assert hr.get("/vi/v1/audit/export").status_code == 503


def test_founder_refusal_stands_even_when_it_cannot_be_recorded(hr):
    hr.ledger.fail_all = True
    r = hr.post("/vi/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": "p", "content_sha256": "0" * 64, "decision": "approve"}]}, andre="wrong-token")
    assert r.status_code == 403


def test_platform_data_store_write_failure_is_reported_not_hidden(tmp_path):
    h = Harness(data_dir=str(tmp_path))
    h.approve_rules()
    h.svc.side.fail_next_save = True
    h.onboard("clip-a")
    assert h.ok(h.get("/health"))["platform_data_store_degraded"] is True
