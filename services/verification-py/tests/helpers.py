"""Test harness (no network: every port is a fake from fakes.py, the ledger is FakeLedgerClient)."""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock, iso
from fakes import (FakeAdapter, FakeAgeProvider, FakeClipperNetwork, FakeCompliance, FakeFinance, FakeHasher,
                   FakeLedgerClient, FakeLegal, FakeMediaIntake, FakeOEmbed, FakePeople, FakeVault)
from platforms import CERTIFIABLE
from service import Ports

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
SERVICE_TOKEN = "test-vi-service-token-do-not-use-0123"
ANDRE_TOKEN = "test-andre-approval-token-vi-do-not-use-01"
CALLERS = {n: f"test-vi-caller-{n}-0123456789abcdefghij" for n in config_mod.CALLER_NAMES}
REVIEWERS = {"rev_amy": "test-vi-reviewer-amy-0123456789abcdefghij"}
JOB_ORDER = ("liveness", "metrics", "revisions", "anomaly", "certify", "retention")
_ids = itertools.count(1)
HARNESSES: list = []            # every harness built (a restart closes the old one on its data dir)


def rid(prefix: str = "r") -> str:
    return f"{prefix}-{next(_ids)}"


def base_env(**over) -> dict:
    env = {"VI_SERVICE_TOKEN": SERVICE_TOKEN, "VI_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN,
           "VI_CALLER_TOKENS": json.dumps(CALLERS), "VI_REVIEWER_TOKENS": json.dumps(REVIEWERS),
           "VI_TT_COVER_PDQ": "1"}
    env.update({k: v for k, v in over.items() if v is not None})
    return {k: v for k, v in env.items() if v != "__unset__"}


class Harness:
    def __init__(self, env: Optional[dict] = None, data_dir: Optional[str] = None, clock: Optional[FixedClock] = None,
                 ledger: Optional[FakeLedgerClient] = None, passing: bool = True, fakes: Optional[dict] = None):
        self.clock = clock or FixedClock(NOW)
        self.ledger = ledger if ledger is not None else FakeLedgerClient()
        f = fakes or {}
        self.vault = f.get("vault") or FakeVault()
        self.adapters = f.get("adapters") or {p: FakeAdapter(p) for p in CERTIFIABLE}
        self.hasher = f.get("hasher") or FakeHasher()
        self.media = f.get("media") or FakeMediaIntake()
        self.age = f.get("age") or FakeAgeProvider()
        self.compliance = f.get("compliance") or FakeCompliance()
        self.legal = f.get("legal") or FakeLegal()
        self.finance = f.get("finance") or FakeFinance()
        self.people = f.get("people") or FakePeople()
        self.cn = f.get("cn") or FakeClipperNetwork()
        self.oembed = f.get("oembed") or FakeOEmbed()
        self.fakes = {"vault": self.vault, "adapters": self.adapters, "hasher": self.hasher, "media": self.media,
                      "age": self.age, "compliance": self.compliance, "legal": self.legal, "finance": self.finance,
                      "people": self.people, "cn": self.cn, "oembed": self.oembed}
        ports = Ports(vault=self.vault, adapters=self.adapters, oembed=self.oembed, hasher=self.hasher,
                      media=self.media, age=self.age, compliance=self.compliance, legal=self.legal,
                      finance=self.finance, people=self.people, clipper_network=self.cn) if passing else Ports()
        e = base_env(**(env or {}))
        if data_dir:
            e["VI_DATA_DIR"] = data_dir
        self.env = e
        if data_dir:
            # a new harness on a data directory is a restart: the old instance stops first (bug sweep C, E-5/F-3: the
            # single-writer claim refuses a second live instance on one directory)
            import os
            for old in list(HARNESSES):
                svc = getattr(old, "svc", None)
                if svc is not None and getattr(old, "_data_dir", None) and \
                        os.path.realpath(old._data_dir) == os.path.realpath(data_dir):
                    svc.close()
        self._data_dir = data_dir
        self.settings = config_mod.load(e)
        self.svc = api.build_service(self.settings, self.clock, ports, self.ledger)
        HARNESSES.append(self)
        self.app = api.create_app(self.svc, self.settings)
        self.client = TestClient(self.app)

    # --- raw HTTP ---------------------------------------------------------------------------
    def headers(self, caller=None, andre=None, reviewer=None, bearer=SERVICE_TOKEN):
        hd = {}
        if bearer is not None:
            hd["Authorization"] = f"Bearer {bearer}"
        if caller:
            hd["X-VI-Caller-Token"] = CALLERS.get(caller, caller)
        if andre is not None:
            hd["X-Andre-Approval-Token"] = andre
        if reviewer is not None:
            hd["X-VI-Reviewer-Token"] = REVIEWERS.get(reviewer, reviewer)
        return hd

    def post(self, path, body, caller=None, andre=None, reviewer=None, bearer=SERVICE_TOKEN):
        return self.client.post(path, json=body, headers=self.headers(caller, andre, reviewer, bearer))

    def get(self, path, caller="scheduler", **params):
        return self.client.get(path, headers=self.headers(caller), params=params)

    def ok(self, r, code=200):
        assert r.status_code == code, (r.status_code, r.text[:2000])
        return r.json()

    # --- rules ----------------------------------------------------------------------------------
    def rules(self):
        return self.ok(self.get("/vi/v1/rules"))

    def approve_rules(self):
        seed = [p for p in self.rules()["open_proposals"] if p["kind"] == "seed"][0]
        return self.ok(self.post("/vi/v1/rules/decisions", {"request_id": rid("dec"), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]},
            andre=ANDRE_TOKEN))

    # --- jobs and time ----------------------------------------------------------------------------
    def job(self, name):
        return self.ok(self.post(f"/vi/v1/jobs/{name}/run", {"request_id": rid("job")}, caller="scheduler"))

    def run_day(self, jobs=JOB_ORDER):
        return {j: self.job(j)["summary"] for j in jobs}

    def advance(self, days=0, hours=0, run=True):
        """Advance day by day, running the scheduler's jobs each day."""
        for _ in range(days):
            self.clock.advance(days=1)
            if run:
                self.run_day()
        if hours:
            self.clock.advance(hours=hours)

    # --- onboarding a clipper -------------------------------------------------------------------------
    def connect(self, clipper, platform="tiktok", account_id=None, code="good-code", **acct):
        if hasattr(self.adapters[platform], "next_account"):
            self.adapters[platform].next_account(account_id or f"acct-{clipper}-{platform}", **acct)
        s = self.ok(self.post("/vi/v1/connections/start", {"request_id": rid("cs"), "clipper_id": clipper,
                                                             "platform": platform,
                                                             "redirect_uri": "https://zbc.example/oauth/cb"},
                              caller="clipper_network"))
        if not s["started"]:
            return s
        state = s["authorization_url"].split("state=")[1].split("&")[0]
        return self.ok(self.post("/vi/v1/connections/complete", {"request_id": rid("cc"), "state": state, "code": code},
                                 caller="clipper_network"))

    def identity(self, clipper, email=None):
        return self.ok(self.post("/vi/v1/identity/checks", {"request_id": rid("id"), "clipper_id": clipper,
                                                             "email": email or f"{clipper}@example.com"},
                                 caller="clipper_network"))

    def age_check(self, subject, dob="1990-05-05", method="photo_id_match", neutral=True, caller="clipper_network"):
        return self.post("/vi/v1/age/checks", {"request_id": rid("age"), "subject_id": subject, "dob": dob,
                                               "dob_field_neutral": neutral, "method": method,
                                               "provider_session_ref": "sess-1"}, caller=caller)

    def onboard(self, clipper, platform="tiktok", account_id=None, **acct):
        c = self.connect(clipper, platform, account_id, **acct)
        self.identity(clipper)
        self.ok(self.age_check(clipper))
        return c

    # --- clips ---------------------------------------------------------------------------------------------
    def post_video(self, platform, post_ref, video_id=None, create_time=None, views=1000, likes=100, caption="a caption #ad",
                   **kw):
        v = {"video_id": video_id or f"vid-{post_ref[-12:]}", "create_time": int((create_time or self.clock.now()).timestamp()),
             "values": {"views": views, "likes": likes, "comments": 5, "shares": 2}, "caption": caption,
             "cover": b"cover-" + post_ref.encode(), **kw}
        if hasattr(self.adapters[platform], "videos"):
            self.adapters[platform].videos[post_ref] = v
        return v

    def register(self, sid, clipper, platform="tiktok", post_ref=None, posted_at=None, min_days_live=7,
                 media_ref="media-default", **kw):
        body = {"request_id": rid("sub"), "submission_id": sid, "campaign_id": kw.pop("campaign_id", "camp-1"),
                "rulebook_version": kw.pop("rulebook_version", 1), "clipper_id": clipper, "platform": platform,
                "post_ref": post_ref or f"https://www.tiktok.com/@c/video/{sid}", "posted_at":
                    iso(posted_at or self.clock.now()), "min_days_live": min_days_live,
                "collab_permitted": kw.pop("collab_permitted", False)}
        if media_ref is not None:
            body["media_ref"] = f"{media_ref}-{sid}"
        body.update(kw)
        return self.post("/vi/v1/submissions", body, caller="creative_production")

    def approve(self, sid):
        return self.ok(self.post(f"/vi/v1/submissions/{sid}/approval", {"request_id": rid("ap")},
                                 caller="creative_production"))

    def cert(self, sid):
        return self.ok(self.get(f"/vi/v1/submissions/{sid}/certification", caller="finance_31"))

    def release_holds(self, subject=None):
        for hold in self.ok(self.get("/vi/v1/holds")):
            if hold["status"] == "open" and (subject is None or hold["subject_id"] == subject):
                self.ok(self.post(f"/vi/v1/holds/{hold['hold_id']}/decision",
                                  {"request_id": rid("hd"), "decision": "release", "reason": "reviewed"},
                                  andre=ANDRE_TOKEN))

    def clean_clip(self, sid="sub-1", clipper="clip-a", platform="tiktok", min_days_live=7, views=5000, likes=500,
                   onboard=True, **kw):
        """Register + approve a clean clip posted now; the clipper onboarded and the stolen check released."""
        if onboard:
            self.onboard(clipper, platform)
        post_ref = kw.pop("post_ref", f"https://www.tiktok.com/@c/video/{sid}")
        self.post_video(platform, post_ref, views=views, likes=likes)
        self.ok(self.register(sid, clipper, platform, post_ref=post_ref, min_days_live=min_days_live, **kw), 201)
        self.approve(sid)
        return post_ref

    def all_text(self) -> str:
        """Every byte V&I produced: ledger payloads, local log, side store, audit export (for A5-style scans)."""
        parts = [json.dumps(self.ledger.events, default=str)]
        parts += [json.dumps(r) for r in self.svc.log.records]
        parts.append(json.dumps(self.svc.side.entries))
        cur = 0
        while cur is not None:
            page = self.ok(self.get("/vi/v1/audit/export", cursor=cur))
            parts.append(json.dumps(page))
            cur = page["next_cursor"]
        return "\n".join(parts)
