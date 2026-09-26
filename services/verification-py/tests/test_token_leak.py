"""A5: a canary access token, refresh token, authorization code, client secret and PKCE verifier returned by
the fake vault/adapters appear in no response, log line, exception text, ledger payload or summary, local
log line, platform-data store or audit export (byte scan), across a seeded random fuzz of operations and
failure modes (adapters raising exceptions that quote the token, vault errors quoting it, rate limits)."""

from __future__ import annotations

import json
import logging
import random

import pytest

from clock import iso
from fakes import CANARIES, CANARY_ACCESS, CANARY_CODE
from helpers import NOW, Harness, rid


class Recorder:
    def __init__(self, h: Harness):
        self.h = h
        self.texts: list[str] = []

    def post(self, path, body, **kw):
        r = self.h.post(path, body, **kw)
        self.texts.append(r.text)
        return r

    def get(self, path, **kw):
        r = self.h.get(path, **kw)
        self.texts.append(r.text)
        return r


def _secrets(h: Harness) -> list[str]:
    out = list(CANARIES)
    for ref in list(h.vault.refs.values()):
        out.append(ref["verifier"])
    out += [p["verifier"] for p in h.svc.pending_oauth.values()]
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_a5_no_secret_anywhere(tmp_path, caplog, seed):
    caplog.set_level(logging.DEBUG)
    rng = random.Random(seed)
    h = Harness(data_dir=str(tmp_path / "d"))
    rec = Recorder(h)
    verifiers = []
    h.approve_rules()
    clippers = [f"clip-{i}" for i in range(3)]
    for i, c in enumerate(clippers):
        h.adapters["tiktok"].next_account(f"acct-{c}", bio=f"bio {CANARY_ACCESS}")
        s = rec.post("/vi/v1/connections/start", {"request_id": rid(), "clipper_id": c, "platform": "tiktok",
                                                  "redirect_uri": "https://zbc.example/cb"}, caller="clipper_network").json()
        state = s["authorization_url"].split("state=")[1].split("&")[0]
        verifiers.append(h.svc.pending_oauth[__import__("service").sha_text(state)]["verifier"])
        code = "good-" + CANARY_CODE if i != 1 else "bad-" + CANARY_CODE
        rec.post("/vi/v1/connections/complete", {"request_id": rid(), "state": state, "code": code},
                 caller="clipper_network")
        rec.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": c, "email": f"{c}@example.com"},
                 caller="clipper_network")
        rec.post("/vi/v1/age/checks", {"request_id": rid(), "subject_id": c, "dob": "1991-02-03",
                                       "dob_field_neutral": True, "method": "photo_id_match",
                                       "provider_session_ref": "s"}, caller="clipper_network")
    for n in range(4):
        c = clippers[n % 3]
        ref = f"https://www.tiktok.com/@c/video/{seed}{n}"
        h.post_video("tiktok", ref, caption=f"cap {CANARY_ACCESS}" if n == 2 else "cap #ad")
        rec.post("/vi/v1/submissions", {"request_id": rid(), "submission_id": f"s{n}", "campaign_id": "camp",
                                        "rulebook_version": 1, "clipper_id": c, "platform": "tiktok", "post_ref": ref,
                                        "posted_at": iso(NOW), "min_days_live": 7, "collab_permitted": False,
                                        "media_ref": f"m{n}"}, caller="creative_production")
        rec.post(f"/vi/v1/submissions/s{n}/approval", {"request_id": rid()}, caller="creative_production")
    ad = h.adapters["tiktok"]
    for day in range(1, 17):
        h.clock.advance(days=1)
        mode = rng.choice(["ok", "ok", "explode", "rate", "down"])
        ad.explode_with_token = mode == "explode"
        ad.rate_limited = mode == "rate"
        ad.available = mode != "down"
        for job in ("liveness", "metrics", "revisions", "anomaly", "certify", "retention"):
            rec.post(f"/vi/v1/jobs/{job}/run", {"request_id": rid()}, caller="scheduler")
        if day == 9:
            cons = [c for c in h.svc.connections.values() if c["status"] == "active"]
            rec.post(f"/vi/v1/connections/{cons[0]['connection_id']}/revoke", {"request_id": rid()},
                     caller="clipper_network")   # later fetches hit a destroyed vault ref: its error quotes the token
    ad.explode_with_token = ad.rate_limited = False
    ad.available = True
    for n in range(4):
        rec.post("/vi/v1/clips/hr13", {"request_id": rid(), "submission_id": f"s{n}", "post_ref": "x",
                                       "platform": "tiktok", "posted_at": iso(NOW), "settlement_lag_days": 14},
                 caller="compliance_38")
        rec.get(f"/vi/v1/submissions/s{n}/certification", caller="finance_31")
    for path, caller in (("/vi/v1/connections?clipper_id=clip-0", "clipper_network"), ("/vi/v1/holds", "scheduler"),
                         ("/vi/v1/findings", "scheduler"), ("/vi/v1/rules", "scheduler"),
                         ("/vi/v1/clawbacks", "finance_31"), ("/vi/v1/strikes", "clipper_network"),
                         ("/vi/v1/feed/verified-results", "creative_production"), ("/vi/v1/integrity", "scheduler")):
        rec.get(path, caller=caller)
    assert ad.tokens_seen and all(t == CANARY_ACCESS for t in ad.tokens_seen)   # the adapter DID get the token
    corpus = "\n".join(rec.texts) + "\n" + h.all_text() + "\n" + "\n".join(r.getMessage() for r in caplog.records)
    corpus += "\n" + (tmp_path / "d" / "vi_log.jsonl").read_text()
    side = tmp_path / "d" / "vi_platform_data.json"
    if side.exists():
        corpus += side.read_text()
    corpus += json.dumps([{k: e[k] for k in ("summary", "payload")} for e in h.ledger.events], default=str)
    for secret in _secrets(h) + verifiers:
        assert secret not in corpus, f"secret leaked: {secret[:12]}..."
