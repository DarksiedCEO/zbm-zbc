"""Leak fuzz: no document, memo, clause, question, paper, evidence or variable text ever reaches the ledger, the local
log, the audit export or an error body. Text lives only in the content-addressed blob store (and a memo's quoted
excerpt goes to Compliance as the spec requires)."""

from __future__ import annotations

import json
import random
import string

from builders import MSA_TEXT, MSA_VARS, playbook
from helpers import COUNSEL_REF, Harness, b64, rid, sha

CANARY = "CANARY"


def _c(tag: str) -> str:
    return f"{CANARY}-{tag}-7f3a9c"


def test_no_text_in_ledger_log_audit_or_errors():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    errors = []
    # document text, template fill variables (a client name), counsel question and proposed edit
    tpl = MSA_TEXT + " " + _c("doctext")
    x.approve_doc("client_msa", tpl, "1.0", "zbm", [("MSA-RENEW-01", "standard")], MSA_VARS)
    x.fill("client_msa", {"client_name": _c("clientname"), "end_date": "2027-01-01"}, "client:acme", expect="1.1")
    x.ok(x.apost("/legal/v1/documents/client_msa/versions/1.1/counsel-review",
                 {"request_id": rid(), "question_text": _c("question"), "proposed_edit_text": _c("edit"),
                  "facts": {"k": _c("facts")}}))
    # memo content, answers excerpt (goes to Compliance only), playbook clause texts
    mm = x.memo(content=_c("memobody").encode(), cites={"cq_ids": ["CQ-22"]},
                answers=[{"cq_id": "CQ-22", "resolution": "verified_rule", "quoted_excerpt": _c("excerpt")}])
    x.memo_proposals(mm["memo_id"], [{"kind": "supersede", "target_id": "CQ-22", "quoted_excerpt": _c("excerpt"),
                                      "proposed_row": {"id": "CQ-22-M",
                                                       "source_url": f"urn:legal37:memos:{mm['memo_id']}"}}])
    from builders import MSA_CLAUSES, clause
    cl = [dict(c) for c in MSA_CLAUSES] + [clause("MSA-LEAK-01", _c("clausetext"), _c("fallback"))]
    playbook(x, clauses=cl)
    # counterparty paper and positions, acceptance evidence, countersignature, error bodies echoing input
    x.ok(x.post("/legal/v1/playbooks/client_msa/reviews", {"request_id": rid(), "counterparty_paper_text": _c("paper"),
            "counterparty_positions": [{"clause_id": "MSA-LEAK-01", "text": _c("position")}]}, caller="onboarding"))
    v = x.approve_doc("clipper_agreement", "clipper " + _c("clippertext"))
    x.verify_cq("CQ-19")
    ev = _c("evidence").encode()
    x.clickwrap("clipper_agreement", "1.0", v["sha256"], evidence_ref={"kind": "session_ref", "sha256": sha(ev),
                                                                       "content_b64": b64(ev)})
    errors.append(x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                                "kind": _c("badkind")}, caller="hub"))
    errors.append(x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email",
                                                "requester_ref": _c("requester"), "kind": "question"}, caller="hub"))
    errors.append(x.post("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "party_ref": "client:b",
                         "entity": "zbm", "variables": {"client_name": "you must " + _c("advice"),
                                                        "end_date": "2027-01-01"}}, caller="scheduler"))
    errors.append(x.apost("/legal/v1/memos", {"request_id": rid(), "counsel_ref": COUNSEL_REF, "memo_date": "2026-10-01",
                                              "content_b64": b64(_c("uncited").encode()), "cites": {"cq_ids": ["CQ-21"]},
                                              "answers": [{"cq_id": "CQ-19", "resolution": "verified_rule",
                                                           "quoted_excerpt": _c("uncitedexcerpt")}]}))
    errors.append(x.post("/legal/v1/acceptances", {"request_id": rid(), "user_agent": _c("ua")}, caller="hub"))
    errors.append(x.post("/legal/v1/signoffs", {"request_id": rid(), "topic": _c("topic"), "subject_id": "s",
                                                "facts": {"x": _c("signofffacts")}}, caller="creative_production"))
    text = x.all_text()
    assert CANARY not in text, text[text.find(CANARY) - 200: text.find(CANARY) + 60]
    for r in errors:
        assert r.status_code in (200, 201, 409, 422) and CANARY not in r.text, r.text[:300]
    # the excerpt reached Compliance (by design) and only there; every blob is where the text lives
    assert any(_c("excerpt") in json.dumps(b) for _, b in x.compliance.proposals)
    assert x.svc.blobs.get(sha(ev)) == ev


def test_random_text_fuzz_never_echoes(hr):
    rnd = random.Random(37)
    alphabet = string.ascii_letters + string.digits + " .,;:!?-_/\n\t'\"<>{}[]()ÄßЖ中😀"
    for i in range(150):
        payload = CANARY + "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 300)))
        target = rnd.choice(["requester_ref", "channel", "kind", "text", "question_text", "custodian"])
        body = {"request_id": rid(), "channel": "email", "requester_ref": "r", "kind": "question", target: payload}
        r = hr.post("/legal/v1/requests", body, caller="hub")
        assert CANARY not in r.text
        r = hr.apost("/legal/v1/documents/sow/versions", {"request_id": rid(), "version": f"{i + 1}.0", "entity": "zbm",
                                                          "text": payload})
        assert CANARY not in r.text
    assert CANARY not in hr.all_text()

