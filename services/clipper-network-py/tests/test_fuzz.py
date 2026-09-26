"""No-500 fuzz: every write route, random bodies of wrong shapes and types, with every identity. Nothing may
answer 500 (a crash), and nothing that was refused may change state."""

from __future__ import annotations

import copy
import random

from helpers import ANDRE_TOKEN, Harness

VALUES = [None, True, False, 0, -1, 10**20, 1.5, float("inf") if False else 2.5, "", "x" * 300, "\u0000", "\ud800x"[1:],
          "CN-01", "cn-clp-x", [], [1, "a"], {}, {"a": {"b": []}}, "2026-09-28T00:00:00Z", "1990-01-01", "a@b.co",
          "+15551234567", "US", "US-CA", "T3", "approve", "email_opt_in"]


def rand_body(rnd, fields):
    body = {}
    for f in fields:
        if rnd.random() < 0.8:
            body[f] = rnd.choice(VALUES)
    if rnd.random() < 0.3:
        body[rnd.choice(["extra", "age_verified", "guardian"])] = True
    return body


FIELDS = ["request_id", "email", "display_name", "declared_country", "declared_region", "channel", "clipper_id",
          "version", "facts", "decisions", "kind", "target_id", "rule", "template", "memo", "trigger", "outcome", "note",
          "proposal_id", "decision", "platform", "redirect_uri", "state", "code", "dob", "method", "statement",
          "subject_kind", "subject_ref", "notice_message_id", "recipients", "min_tier", "platforms",
          "rate_card_ref", "view_terms", "opens_at", "closes_at", "nominate", "head_sha256", "void_lines"]


def test_no_route_answers_500_to_garbage():
    h = Harness().ready()
    cid = h.admitted_clipper()
    routes = [(sorted(r.methods - {"HEAD"})[0], r.path) for r in h.app.routes if hasattr(r, "methods")]
    rnd = random.Random(99)
    for method, path in routes:
        if method not in ("POST", "PUT"):
            continue
        url = path.replace("{clipper_id}", cid).replace("{campaign_id}", "camp-1").replace("{recruit_id}", "cn-rcr-x") \
            .replace("{enrolment_id}", "cn-enr-x").replace("{dispute_id}", "cn-dsp-x")
        for i in range(25):
            body = rand_body(rnd, rnd.sample(FIELDS, rnd.randint(0, 12)))
            for ident in ({"caller": "hub"}, {"caller": "scheduler"}, {"andre": ANDRE_TOKEN}, {"caller": "creative_production"}):
                before = copy.deepcopy(h.svc.st)
                r = h.client.request(method, url, json=body, headers=h.headers(**ident))
                assert r.status_code < 500, (method, url, body, r.status_code, r.text[:200])
                if r.status_code >= 400:
                    assert h.svc.st == before, (method, url, body)
