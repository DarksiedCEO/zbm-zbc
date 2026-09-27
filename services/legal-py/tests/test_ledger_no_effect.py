"""Record-first on every write route: a ledger outage, an anchor failure or a local-store failure -> 503 and
NOTHING took effect (the local log, every collection and every answer are unchanged)."""

from __future__ import annotations

import json

import pytest

from builders import playbook
from helpers import Harness, b64, rid

POST = "f" * 64
ELEMENTS = {e: True for e in ("signature", "work_identified", "material_located", "contact", "good_faith_statement",
                              "perjury_statement")}


def world():
    x = Harness()
    x.approve_rules()
    x.engage()
    v = x.approve_doc("clipper_agreement", "clipper agreement text")
    x.verify_cq("CQ-19")
    playbook(x)
    d = x.upload("client_msa", "draft msa", entity="zbm")
    n = x.ok(x.post("/legal/v1/takedowns", {"request_id": rid(), "target": {"kind": "platform_post",
                    "post_ref_sha256": POST, "platform": "tiktok"}, "elements": ELEMENTS}, caller="hub"), 201)
    m = x.ok(x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                           "kind": "agency_letter", "custodians": ["andre"]}, caller="hub"), 201)
    f = x.ok(x.apost("/legal/v1/filings", {"request_id": rid(), "entity": "zbc", "kind": "tm_application"}), 201)
    memo = x.memo(content=b"spare memo", cites={"doc_versions": ["client_msa@1.0"]})
    return x, {"clip_sha": v["sha256"], "msa_sha": d["sha256"], "notice": n["notice_id"], "hold": m["hold_ids"][0],
               "matter": m["matter_id"], "filing": f["filing_id"], "memo": memo}


OPS = {
    "upload": lambda x, c: x.apost("/legal/v1/documents/sow/versions", {"request_id": "w1", "version": "1.0",
                                                                         "entity": "zbm", "text": "sow"}),
    "counsel_review": lambda x, c: x.apost("/legal/v1/documents/client_msa/versions/1.0/counsel-review",
                                           {"request_id": "w2", "question_text": "question"}),
    "signoff": lambda x, c: x.apost("/legal/v1/documents/client_msa/versions/1.0/counsel-signoff",
                                    {"request_id": "w3", "counsel_ref": "eng-counsel-1", "signed_on": "2026-10-01",
                                     "doc_sha256": c["msa_sha"], "memo_id": c["memo"]["memo_id"],
                                     "memo_sha256": c["memo"]["memo_sha256"]}),
    "withdraw": lambda x, c: x.apost("/legal/v1/documents/client_msa/versions/1.0/decision",
                                     {"request_id": "w4", "decision": "withdraw", "version_sha256": c["msa_sha"]}),
    "acceptance": lambda x, c: x.post("/legal/v1/acceptances", {
        "request_id": "w5", "party_ref": "clipper:z", "signer_identity_ref": "z", "doc_id": "clipper_agreement",
        "version": "1.0", "doc_sha256": c["clip_sha"], "presented_sha256": c["clip_sha"],
        "method": "clickwrap_unticked_box", "presentation": "link", "affirmative_act": True,
        "esign_consent": {"disclosure_version": "1.0", "consented_at": "2026-10-01T16:00:00Z", "access_demonstrated": True}},
        caller="clipper_network"),
    "memo": lambda x, c: x.apost("/legal/v1/memos", {"request_id": "w6", "counsel_ref": "eng-counsel-1",
                                                     "memo_date": "2026-10-01", "content_b64": b64(b"new memo"),
                                                     "cites": {"cq_ids": ["CQ-21"]},
                                                     "answers": [{"cq_id": "CQ-21", "resolution": "verified_rule"}]}),
    "review": lambda x, c: x.post("/legal/v1/playbooks/client_msa/reviews", {"request_id": "w7",
                                  "counterparty_positions": [{"clause_id": "MSA-PAY-01", "text": "x"}]}, caller="onboarding"),
    "intake": lambda x, c: x.post("/legal/v1/requests", {"request_id": "w8", "channel": "email", "requester_ref": "r",
                                                         "kind": "subpoena", "deadlines": {"return_date": "2026-11-01"}},
                                  caller="hub"),
    "takedown": lambda x, c: x.post("/legal/v1/takedowns", {"request_id": "w9", "target": {"kind": "platform_post",
                                    "post_ref_sha256": "e" * 64, "platform": "tiktok"}, "elements": ELEMENTS}, caller="hub"),
    "counter": lambda x, c: x.post(f"/legal/v1/takedowns/{c['notice']}/counter-notice", {"request_id": "w10"}, caller="hub"),
    "ack": lambda x, c: x.post(f"/legal/v1/holds/{c['hold']}/acknowledgments", {"request_id": "w11", "custodian": "andre"},
                               caller="hub"),
    "release": lambda x, c: x.apost(f"/legal/v1/holds/{c['hold']}/release", {"request_id": "w12",
                                                                            "memo_id": c["memo"]["memo_id"]}),
    "filing": lambda x, c: x.apost("/legal/v1/filings", {"request_id": "w13", "entity": "zbc",
                                                        "kind": "dmca_agent_designation", "filed_on": "2026-10-01"}),
    "filed": lambda x, c: x.apost(f"/legal/v1/filings/{c['filing']}/filed", {"request_id": "w14", "filed_on": "2026-10-02",
                                                                            "reference": "SN-1"}),
    "music": lambda x, c: x.post("/legal/v1/music/rulings", {"request_id": "w15", "subject_kind": "zbc_clip",
                                 "subject_id": "c", "platform": "tiktok", "paid": True,
                                 "music": {"present": False, "source": "none"}, "reposted_or_reedited_by_zbc": False,
                                 "music_changed_since_approval": False}, caller="creative_production"),
    "signoff_topic": lambda x, c: x.post("/legal/v1/signoffs", {"request_id": "w16", "topic": "ai_generative_fill",
                                                                "subject_id": "s", "facts": {"asset_ids": ["a"]}},
                                         caller="creative_production"),
    "invalidate": lambda x, c: x.post("/legal/v1/register/CQ-19/invalidate", {"request_id": "w17", "source_ref": "w",
                                                                              "detected_change_sha256": "a" * 64},
                                      caller="compliance_38"),
    "job": lambda x, c: x.post("/legal/v1/jobs/holds-renotice/run", {"request_id": "w18"}, caller="scheduler"),
    "rule_proposal": lambda x, c: x.apost("/legal/v1/rules/proposals", {"request_id": "w19", "kind": "amend",
                                          "target_id": "LG-16", "proposed_row": dict(x.svc.current.by_id()["LG-16"],
                                                                                     title="DSAR clock (secondary)")}),
}


def _state(x):
    s = x.svc
    return json.dumps([len(s.log), s.doc_versions, s.acceptances, s.memos, s.cproposals, s.matters, s.holds, s.takedowns,
                       s.filings, s.rulings, s.signoffs, s.register, s.reviews, s.packages, len(s.proposals),
                       s.job_runs], sort_keys=True, default=str)


@pytest.mark.parametrize("name", sorted(OPS))
@pytest.mark.parametrize("failure", ["ledger_down", "anchor_fails", "store_fails"])
def test_failure_means_no_effect(name, failure):
    x, c = world()
    before = _state(x)
    if failure == "ledger_down":
        x.ledger.fail_all = True
    elif failure == "anchor_fails":
        x.ledger.fail_on_type = "local_log_appended"
    else:
        x.svc.log.fail_next_append = True
    r = OPS[name](x, c)
    assert r.status_code == 503, (name, failure, r.status_code, r.text[:300])
    assert r.json()["issued"] is False
    x.ledger.fail_all, x.ledger.fail_on_type, x.svc.log.fail_next_append = False, None, False
    assert _state(x) == before, name
    # and it works once the dependency is back (the same request id is not burnt)
    r2 = OPS[name](x, c)
    assert r2.status_code in (200, 201), (name, r2.status_code, r2.text[:300])


def test_blob_store_failure_means_no_effect():
    x, c = world()
    before = _state(x)
    x.svc.blobs.fail_next_put = True
    r = OPS["upload"](x, c)
    assert r.status_code == 503 and _state(x) == before

