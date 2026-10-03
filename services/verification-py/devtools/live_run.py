"""
Live HTTP run for verification-py: real ledger-rust binaries, real processes, real sockets, real tokens.

Leg A (production entrypoint, day one): ledger A + compliance-py (its production entrypoint; seed approved by
Andre) + verification-py via ``cd src && python3 -m api`` wired to compliance-py through the V&I → Compliance
thin client (VI_COMPLIANCE_*). Every other dependency is its fail-closed stand-in: Andre approves the V&I rules,
and still nothing connects or certifies (the day-one effect), with HR-13 read live from compliance-py.

Leg B (devtools/live_server.py: the test fakes + a settable clock): ledger B + verification-py walking clips
through 32 simulated days: every ruling type, a metric revision → clawback record, anomaly holds (released and
upheld), strikes, a Clipper Network + Andre ban approval, age and identity outcomes, retention purges, the
Compliance/Creative/Onboarding protocol answers, then a RESTART on the same data directory and ledger (anchors
verified at start), and GET /ledger/verify on both ledgers.

usage: LEDGER_BIN=/path/to/ledger-rust/server python3 devtools/live_run.py [--ports 19300,19301,19302,19303,19304]
Kills only the PIDs it started. Exit 0 only when both ledgers verify valid and every checked expectation held.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]
SERVICES = HERE.parents[2]
PORTS = [int(p) for p in (sys.argv[sys.argv.index("--ports") + 1].split(",") if "--ports" in sys.argv else
                          ["19300", "19301", "19302", "19303", "19304"])]
LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
VI_TOKEN = "live-vi-service-token-" + "s" * 16
ANDRE = "live-vi-andre-approval-token-" + "a" * 12
CMP_TOKEN = "live-compliance-service-token-" + "s" * 12
CMP_ANDRE = "live-compliance-andre-token-" + "a" * 14
CALLERS = {n: f"live-vi-caller-{n}-" + "c" * 24 for n in ("compliance_38", "creative_production", "onboarding",
                                                           "clipper_network", "finance_31", "scheduler")}
CMP_CALLERS = {"verification_integrity": "live-cmp-caller-vi-" + "v" * 24, "scheduler": "live-cmp-caller-sched-" + "x" * 20}
LOG: list[str] = []
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool]] = []
JOBS = ("liveness", "metrics", "revisions", "anomaly", "certify", "retention")


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


# Fix wave 26b (scout C6-2): the run's work dir holds the ledger and service logs (the run's evidence). It was made
# with mkdtemp and never removed, so every local run left one behind. Now LIVE_WORK_DIR=<dir> (the name every
# live_run.py reads) KEEPS the run's dir inside <dir> and the end of the run prints where it is — CI's live-runs job
# asks for $RUNNER_TEMP/live-work, then lists and removes it; unset, the dir is made in the temp dir and removed when
# the run ends, passed or failed (the run's narration is on stdout either way).
def make_work_dir(prefix: str) -> tuple[Path, bool]:
    keep_in = os.environ.get("LIVE_WORK_DIR") or None
    return Path(tempfile.mkdtemp(prefix=prefix, dir=keep_in)), keep_in is not None


def finish_work_dir(work: Path, keep: bool) -> None:
    if keep:
        print(f"work dir kept (LIVE_WORK_DIR): {work}", flush=True)
    else:
        shutil.rmtree(work)
        print(f"work dir {work} removed (set LIVE_WORK_DIR=<dir> to keep the run's logs)", flush=True)


def check(name: str, ok: bool) -> None:
    CHECKS.append((name, bool(ok)))
    say(f"  CHECK {'PASS' if ok else 'FAIL'}: {name}")


def wait_health(url: str, timeout: float = 25.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def start(cmd, env, cwd, name, logdir) -> subprocess.Popen:
    out = open(logdir / f"{name}.log", "wb")
    p = subprocess.Popen(cmd, env={**os.environ, **env}, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
    PROCS.append(p)
    say(f"started {name} pid={p.pid}")
    return p


def stop(p: subprocess.Popen, name: str) -> None:
    p.terminate()
    p.wait(timeout=10)
    say(f"stopped {name} pid={p.pid}")


class Api:
    def __init__(self, base: str):
        self.base = base
        self.c = httpx.Client(timeout=60)
        self.n = 0

    def rid(self) -> str:
        self.n += 1
        return f"live-{self.n}"

    def h(self, caller=None, andre=None):
        d = {"Authorization": f"Bearer {VI_TOKEN}"}
        if caller:
            d["X-VI-Caller-Token"] = CALLERS[caller]
        if andre:
            d["X-Andre-Approval-Token"] = andre
        return d

    def post(self, path, body, caller=None, andre=None):
        return self.c.post(self.base + path, json=body, headers=self.h(caller, andre))

    def get(self, path, caller="scheduler", **params):
        return self.c.get(self.base + path, headers=self.h(caller), params=params)

    def dev(self, path, body):
        r = self.c.post(self.base + "/devtools/" + path, json=body, headers=self.h())
        assert r.status_code == 200, r.text
        return r.json()

    def approve_rules(self):
        seed = [p for p in self.get("/vi/v1/rules").json()["open_proposals"] if p["kind"] == "seed"][0]
        return self.post("/vi/v1/rules/decisions", {"request_id": self.rid(), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]},
            andre=ANDRE)

    def jobs(self):
        out = {}
        for j in JOBS:
            r = self.post(f"/vi/v1/jobs/{j}/run", {"request_id": self.rid()}, caller="scheduler")
            assert r.status_code == 200, (j, r.status_code, r.text[:300])
            out[j] = r.json()["summary"]
        return out

    def cert(self, sid):
        return self.get(f"/vi/v1/submissions/{sid}/certification", caller="finance_31").json()

    def connect(self, clipper, platform, account_id, **acct):
        self.dev("account", {"platform": platform, "account_id": account_id, **acct})
        s = self.post("/vi/v1/connections/start", {"request_id": self.rid(), "clipper_id": clipper, "platform": platform,
                                                   "redirect_uri": "https://zbc.example/oauth/cb"},
                      caller="clipper_network").json()
        if not s.get("started"):
            return s
        state = s["authorization_url"].split("state=")[1].split("&")[0]
        return self.post("/vi/v1/connections/complete", {"request_id": self.rid(), "state": state, "code": "good-live-code"},
                         caller="clipper_network").json()

    def onboard(self, clipper, platform="tiktok", email=None):
        c = self.connect(clipper, platform, f"acct-{clipper}")
        idc = self.post("/vi/v1/identity/checks", {"request_id": self.rid(), "clipper_id": clipper,
                                                   "email": email or f"{clipper}@example.com"},
                        caller="clipper_network").json()
        age = self.post("/vi/v1/age/checks", {"request_id": self.rid(), "subject_id": clipper, "dob": "1994-06-01",
                                              "dob_field_neutral": True, "method": "photo_id_match",
                                              "provider_session_ref": "sess-live"}, caller="clipper_network").json()
        return c, idc, age

    def clip(self, sid, clipper, platform="tiktok", views=5000, likes=500, **kw):
        post_ref = kw.pop("post_ref", f"https://www.tiktok.com/@{clipper}/video/{abs(hash(sid)) % 10**12}")
        self.dev("video", {"platform": platform, "post_ref": post_ref, "patch": {"values": {"views": views, "likes": likes}}})
        now = self.c.get(self.base + "/devtools/now", headers=self.h()).json()["now"]
        body = {"request_id": self.rid(), "submission_id": sid, "campaign_id": "camp-live", "rulebook_version": 1,
                "clipper_id": clipper, "platform": platform, "post_ref": post_ref, "posted_at": now,
                "min_days_live": kw.pop("min_days_live", 7), "collab_permitted": False, "media_ref": f"media-{sid}", **kw}
        r = self.post("/vi/v1/submissions", body, caller="creative_production")
        assert r.status_code == 201, r.text
        if platform in ("tiktok", "youtube", "instagram"):
            self.post(f"/vi/v1/submissions/{sid}/approval", {"request_id": self.rid()}, caller="creative_production")
        return post_ref, now


def codes(x):
    return sorted({r["code"] for r in x.get("reasons", [])})


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pla, pcmp, pvia, plb, pvib = PORTS
    LA, CMP, VIA, LB, VIB = (f"http://127.0.0.1:{p}" for p in PORTS)
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    try:
        # ------------------------------------------------------------------ leg A: production entrypoint
        (work / "la").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pla),
                             "LEDGER_LOG_PATH": str(work / "la" / "ledger.jsonl")}, str(work / "la"), "ledger_a", work)
        say(f"ledger A health: {wait_health(LA + '/health')}")
        start([sys.executable, "-m", "api"], {"COMPLIANCE_SERVICE_TOKEN": CMP_TOKEN, "COMPLIANCE_ANDRE_APPROVAL_TOKEN": CMP_ANDRE,
                                              "COMPLIANCE_CALLER_TOKENS": json.dumps(CMP_CALLERS), "LEDGER_SERVICE_URL": LA,
                                              "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "COMPLIANCE_PORT": str(pcmp),
                                              "COMPLIANCE_DATA_DIR": str(work / "cmp")},
              str(SERVICES / "compliance-py" / "src"), "compliance", work)
        say(f"compliance-py health: {wait_health(CMP + '/health')}")
        ch = {"Authorization": f"Bearer {CMP_TOKEN}", "X-Compliance-Caller-Token": CMP_CALLERS["scheduler"]}
        seed = [p for p in httpx.get(CMP + "/compliance/v1/inbox", headers=ch, timeout=60).json() if p["kind"] == "seed"][0]
        r = httpx.post(CMP + "/compliance/v1/register/decisions", timeout=60,
                       headers={"Authorization": f"Bearer {CMP_TOKEN}", "X-Andre-Approval-Token": CMP_ANDRE},
                       json={"request_id": "cmp-seed", "decisions": [{"proposal_id": seed["proposal_id"],
                                                                      "content_sha256": seed["content_sha256"],
                                                                      "decision": "approve"}]})
        say(f"compliance seed approved by Andre -> {r.status_code} register_version={r.json().get('register_version')}")
        common = {"VI_SERVICE_TOKEN": VI_TOKEN, "VI_ANDRE_APPROVAL_TOKEN": ANDRE, "VI_CALLER_TOKENS": json.dumps(CALLERS),
                  "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
        start([sys.executable, "-m", "api"], {**common, "LEDGER_SERVICE_URL": LA, "VI_PORT": str(pvia),
                                              "VI_DATA_DIR": str(work / "via"), "VI_COMPLIANCE_URL": CMP,
                                              "VI_COMPLIANCE_TOKEN": CMP_TOKEN,
                                              "VI_COMPLIANCE_CALLER_TOKEN": CMP_CALLERS["verification_integrity"]},
              str(SVC / "src"), "vi_prod", work)
        say(f"V&I (production entrypoint) health: {wait_health(VIA + '/health')}")
        a = Api(VIA)
        na = {"Authorization": "Bearer t\xf6ken".encode("latin-1")}
        say(f"no bearer -> {a.c.get(VIA + '/vi/v1/rules').status_code}; non-ASCII bearer -> "
            f"{a.c.get(VIA + '/vi/v1/rules', headers=na).status_code}; /docs -> {a.c.get(VIA + '/docs').status_code}; "
            f"/openapi.json -> {a.c.get(VIA + '/openapi.json').status_code}")
        check("leg A: auth refuses without/with a bad bearer, docs off",
              a.c.get(VIA + "/vi/v1/rules").status_code == 401 and a.c.get(VIA + "/vi/v1/rules", headers=na).status_code == 401
              and a.c.get(VIA + "/docs").status_code == 404)
        s = a.post(
            "/vi/v1/connections/start", {"request_id": a.rid(), "clipper_id": "clip-a", "platform": "tiktok",
                                         "redirect_uri": "https://zbc.example/cb"}, caller="clipper_network").json()
        say(f"connection start before rule approval: started={s['started']} codes={codes(s)}")
        check("leg A: before Andre approves, RULES_NOT_IN_FORCE", codes(s) == ["RULES_NOT_IN_FORCE"])
        r = a.post("/vi/v1/rules/decisions", {"request_id": a.rid(), "decisions": [{"proposal_id": "x",
                   "content_sha256": "0" * 64, "decision": "approve"}]}, andre=VI_TOKEN)
        say(f"rules approval with the service token as Andre's token -> {r.status_code}")
        r = a.approve_rules()
        say(f"Andre approves the V&I rules -> {r.status_code} rules_version={r.json()['rules_version']}")
        s = a.post("/vi/v1/connections/start", {"request_id": a.rid(), "clipper_id": "clip-a", "platform": "tiktok",
                                                "redirect_uri": "https://zbc.example/cb"}, caller="clipper_network").json()
        say(f"day one: connection start -> started={s['started']} codes={codes(s)}")
        check("leg A: day one, the vault stand-in refuses every connection", codes(s) == ["DEPENDENCY_UNAVAILABLE"])
        prod_posted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        r = a.post("/vi/v1/submissions", {"request_id": a.rid(), "submission_id": "prod-1", "campaign_id": "camp",
                                          "rulebook_version": 1, "clipper_id": "clip-a", "platform": "tiktok",
                                          "post_ref": "https://www.tiktok.com/@a/video/1", "posted_at": prod_posted,
                                          "min_days_live": 7,
                                          "collab_permitted": False, "media_ref": "m1"}, caller="creative_production")
        say(f"submission registered -> {r.status_code} stolen_check={r.json()['stolen_check']}")
        a.jobs()
        c = a.cert("prod-1")
        say(f"day one certification: status={c['status']} settlement_source={c['settlement_source']} "
            f"compliance_register_version={c['compliance_register_version']} codes={codes(c)}")
        for line in c["reason_lines"][:8]:
            say(f"    {line[:170]}")
        check("leg A: HR-13 read live from compliance-py (settlement_source compliance_hr13)",
              c["settlement_source"] == "compliance_hr13" and c["compliance_register_version"] == 1)
        check("leg A: nothing certifies on day one", c["status"] != "certified")
        h13 = a.post("/vi/v1/clips/hr13", {"request_id": a.rid(), "submission_id": "prod-1",
                                            "post_ref": "https://www.tiktok.com/@a/video/1", "platform": "tiktok",
                                            "posted_at": prod_posted, "settlement_lag_days": 14},
                     caller="compliance_38").json()
        say(f"HR-13 attestation (Compliance protocol) -> verified_views={h13['verified_views']} "
            f"copyright_strike={h13['copyright_strike']} rules_pinned={h13['rules_pinned']}")
        # ------------------------------------------------------------------ leg B: fakes + settable clock
        (work / "lb").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(plb),
                             "LEDGER_LOG_PATH": str(work / "lb" / "ledger.jsonl")}, str(work / "lb"), "ledger_b", work)
        say(f"ledger B health: {wait_health(LB + '/health')}")
        envb = {**common, "LEDGER_SERVICE_URL": LB, "VI_PORT": str(pvib), "VI_DATA_DIR": str(work / "vib"),
                "VI_DEVTOOLS_STATE_FILE": str(work / "vib-devtools-state.json"), "VI_TT_COVER_PDQ": "1"}
        pb = start([sys.executable, str(SVC / "devtools" / "live_server.py")], envb, str(SVC), "vi_fakes", work)
        say(f"V&I (devtools fakes) health: {wait_health(VIB + '/health')}")
        b = Api(VIB)
        say(f"rules approved -> {b.approve_rules().json()['rules_version']}")
        for who in ("alice", "bob", "carl"):
            c_, i_, g_ = b.onboard(who)
            say(f"onboard {who}: connection={c_['connection']['status']} identity={i_['status']} age={g_['result']}")
        g = b.connect("gina", "tiktok", "acct-alice")
        say(f"gina connects alice's TikTok account -> {g['connection']['status']} {codes(g['connection'])}")
        check("account shared across identities is refused", "ACCOUNT_SHARED" in codes(g["connection"]))
        f = b.post("/vi/v1/identity/checks", {"request_id": b.rid(), "clipper_id": "frank", "email": "Alice@example.com"},
                   caller="clipper_network").json()
        say(f"frank's identity check with alice's email -> {f['status']} findings={len(f['findings'])}")
        check("duplicate identity finding", f["status"] == "finding")
        m = b.post("/vi/v1/age/checks", {"request_id": b.rid(), "subject_id": "dana", "dob": "2011-02-02",
                                         "dob_field_neutral": True, "method": "photo_id_match",
                                         "provider_session_ref": "s"}, caller="onboarding").json()
        b.dev("age", {"result": "adult", "estimated_age_low": 22})
        e = b.post("/vi/v1/age/checks", {"request_id": b.rid(), "subject_id": "erin", "dob": "2003-02-02",
                                         "dob_field_neutral": True, "method": "facial_age_estimation",
                                         "provider_session_ref": "s"}, caller="onboarding").json()
        b.dev("age", {"result": "adult"})
        say(f"age: dana (DOB under 18) -> {m['result']} {codes(m)}; erin (FAE estimate 22) -> {e['result']} {codes(e)}")
        check("minor hard block and FAE buffer", m["result"] == "minor" and e["result"] == "inconclusive")
        refs, posted = {}, {}
        for sid, who, kw in (("a1", "alice", {"views": 9000, "likes": 900}), ("a2", "alice", {}), ("a3", "alice", {}),
                             ("a4", "alice", {}), ("b1", "bob", {"views": 50000, "likes": 10}),
                             ("c1", "carl", {"views": 60000, "likes": 5})):
            refs[sid], t0 = b.clip(sid, who, **kw)
            posted[sid] = t0
        r = b.post("/vi/v1/submissions", {"request_id": b.rid(), "submission_id": "a5", "campaign_id": "camp-live",
                                          "rulebook_version": 1, "clipper_id": "alice", "platform": "snapchat",
                                          "post_ref": "https://snap.example/a5", "posted_at": t0, "min_days_live": 7,
                                          "collab_permitted": False}, caller="creative_production")
        c_, _, _ = b.onboard("yuri", "youtube")
        refs["y1"], _ = b.clip("y1", "yuri", "youtube", post_ref="https://www.youtube.com/shorts/abcdefghijk")
        say(f"registered clips: {sorted(refs)} + a5 (snapchat) at {t0}")
        for day in range(1, 33):
            b.dev("advance", {"days": 1})
            if day == 5:
                b.dev("video", {"platform": "tiktok", "post_ref": refs["a2"], "patch": {"gone": True}})
            if day == 4:
                b.dev("video", {"platform": "tiktok", "post_ref": refs["a3"], "patch": {"caption": "caption"}})
                b.dev("video", {"platform": "tiktok", "post_ref": refs["a4"], "patch": {"video_id": "swapped-video"}})
            if day == 20:
                b.dev("video", {"platform": "tiktok", "post_ref": refs["a1"], "patch": {"values": {"views": 8700}}})
            if day == 22:
                b.dev("video", {"platform": "tiktok", "post_ref": refs["a1"], "patch": {"values": {"views": 12000}}})
            if day == 10:
                refs["a6"], _ = b.clip("a6", "alice")
            summary = b.jobs()
            if day in (2, 14, 15, 20, 22, 32):
                say(f"day {day}: " + json.dumps({k: v for k, v in summary.items() if v}, sort_keys=True)[:300])
            if day == 2:
                holds = [x for x in b.get("/vi/v1/holds").json() if x["status"] == "open" and x["cause"] == "anomaly"]
                say(f"day 2 anomaly holds: {[(x['subject_id'], x['reasons'][0]['code']) for x in holds]}")
                for x in [x for x in holds if x["subject_id"] in ("b1", "c1")]:     # y1's YouTube hold stays open
                    d = "release" if x["subject_id"] == "b1" else "uphold"
                    r = b.post(f"/vi/v1/holds/{x['hold_id']}/decision", {"request_id": b.rid(), "decision": d,
                                                                           "reason": "live-run review"}, andre=ANDRE)
                    say(f"  Andre {d}s hold on {x['subject_id']} -> {r.status_code}")
            if day == 14:
                c = b.cert("a1")
                say(f"a1 at day 14: status={c['status']} certified_views={c['certified_views']} settle_at={c['window']['settle_at']}")
                check("a1 certified with the settlement value", c["status"] == "certified" and c["certified_views"] == 9000)
            if day == 20:
                c = b.cert("a1")
                cb = b.get("/vi/v1/clawbacks", caller="finance_31").json()["items"]
                say(f"a1 at day 20: status={c['status']} certified_views={c['certified_views']} clawbacks={[(x['clawback_id'], x['views_delta'], x['rule_id']) for x in cb]}")
                check("metric revision down → revised + clawback record -300", c["status"] == "revised" and
                      c["certified_views"] == 8700 and [x["views_delta"] for x in cb] == [-300])
        for sid in ("a1", "a2", "a3", "a4", "a5", "a6", "b1", "c1", "y1"):
            c = b.cert(sid)
            say(f"final {sid}: status={c['status']} views={c['certified_views']} codes={codes(c)}")
        check("revised up on day 22 changed nothing", b.cert("a1")["certified_views"] == 8700)
        check("deleted before min live", "DELETED_BEFORE_MIN_LIVE" in codes(b.cert("a2")))
        check("caption changed", "CAPTION_CHANGED" in codes(b.cert("a3")))
        check("hash mismatch", "HASH_MISMATCH" in codes(b.cert("a4")))
        check("snapchat not payable", "PLATFORM_NOT_PAYABLE" in codes(b.cert("a5")))
        check("a6 (registered on day 10) certified at its own day 14", b.cert("a6")["status"] == "certified")
        check("anomaly hold released by Andre → certified", b.cert("b1")["status"] == "certified")
        check("anomaly upheld → bought engagement", "BOUGHT_ENGAGEMENT" in codes(b.cert("c1")))
        check("YouTube derived signals off → hold", "YT_DERIVED_USE_UNRESOLVED" in codes(b.cert("y1")))
        strikes = b.get("/vi/v1/strikes", caller="clipper_network").json()["items"]
        say(f"strikes: {sorted((s['clipper_id'], s['class'], s['rule_id']) for s in strikes)}")
        integ = b.get("/vi/v1/clippers/carl/integrity", caller="clipper_network").json()
        say(f"carl integrity: S3={integ['strikes_active']['S3']} ban_recommended={integ['ban_recommended']} banned={integ['banned']}")
        ban = {"request_id": b.rid(), "clipper_id": "carl", "cn_decision_id": "cn-disc-live-1",
               "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        r1 = b.post("/vi/v1/bans", ban, caller="clipper_network")
        r2 = b.post("/vi/v1/bans", ban, caller="clipper_network", andre=ANDRE)
        say(f"ban without Andre's token -> {r1.status_code}; with CN caller + Andre -> {r2.status_code} "
            f"{ {k: r2.json()['ban'][k] for k in ('recommended_by_vi', 'approved_by')} } blocked={len(r2.json()['ban']['blocked'])}")
        check("ban only with Andre's approval", r1.status_code == 403 and r2.status_code == 200)
        again = b.connect("carl-2", "tiktok", "acct-carl")
        say(f"carl's banned account under a new clipper id -> {again['connection']['status']} {codes(again['connection'])}")
        # protocol answers
        h13 = b.post("/vi/v1/clips/hr13", {"request_id": b.rid(), "submission_id": "b1", "post_ref": refs["b1"],
                                            "platform": "tiktok", "posted_at": posted["b1"], "settlement_lag_days": 14},
                     caller="compliance_38").json()
        say(f"HR-13 attestation b1 -> verified_views={h13['verified_views']} still_live={h13['still_live_at_minimum_period']} "
            f"anomaly_screen_passed={h13['anomaly_screen_passed']} purchased={h13['purchased_engagement']} "
            f"copyright_strike={h13['copyright_strike']} ({codes(h13)})")
        check("HR-13 attestation positive for a certified clip", h13["verified_views"] is True)
        bad = b.post("/vi/v1/clips/hr13", {"request_id": b.rid(), "submission_id": "b1", "post_ref": refs["b1"],
                                            "platform": "tiktok", "posted_at": posted["b1"], "settlement_lag_days": 7},
                     caller="compliance_38").json()
        say(f"HR-13 attestation with lag 7 -> verified_views={bad['verified_views']} {codes(bad)}")
        ca = b.post("/vi/v1/clips/attest", {"request_id": b.rid(), "submission_id": "b1", "facts": {
            "campaign_id": "camp-live", "rulebook_version": 1, "post_ref": refs["b1"], "clipper_id": "bob",
            "posted_at": posted["b1"]}}, caller="creative_production").json()
        ra = b.post("/vi/v1/results/attest", {"request_id": b.rid(), "result_id": "res-live", "facts": {
            "result_id": "res-live", "campaign_id": "camp-live", "submission_id": "b1", "vertical": "beauty",
            "platform": "tiktok", "angle_id": "a", "hook": "h", "source": "platform_export",
            "reported_views": 123456789}}, caller="creative_production").json()
        say(f"Creative attest_clip b1 -> verified={ca['verified']} ({ca['reason'][:60]}); attest_result -> verified="
            f"{ra['verified']} verified_views={ra['checks']['verified_views']} (reported_views 123456789 ignored)")
        check("attest_result returns the certified count", ra["checks"]["verified_views"] == 50000)
        check("Creative attest_clip verified for a certified clip", ca["verified"] is True)
        ages = b.get("/vi/v1/age/subjects/alice", caller="onboarding").json()
        danas = b.get("/vi/v1/age/subjects/dana", caller="onboarding").json()
        say(f"Onboarding age_verified_18_plus: alice allowed={ages['allowed']}; dana allowed={danas['allowed']} {danas['unmet'][:1]}")
        side_before = json.loads((work / "vib" / "vi_platform_data.json").read_text())["entries"]
        say(f"platform-data side store now holds {len(side_before)} raw value(s); a1 raw post_ref present: "
            f"{'a1:post_ref' in side_before}")
        check("a1 raw platform data purged after revision_watch_end (only hashes remain)", "a1:post_ref" not in side_before)
        ex = b.get("/vi/v1/audit/export").json()
        say(f"audit export page: {len(ex['records'])} records, next_cursor={ex['next_cursor']}")
        integ = b.get("/vi/v1/integrity").json()
        say(f"integrity before restart: {integ['status']} {integ['problems'][:2]}")
        # ------------------------------------------------------------------ restart leg B on the same dir + ledger
        stop(pb, "vi_fakes")
        pb2 = start([sys.executable, str(SVC / "devtools" / "live_server.py")], envb, str(SVC), "vi_fakes_restart", work)
        hb = wait_health(VIB + "/health")
        say(f"V&I restarted on the same data dir and ledger (anchors verified at start): {hb}")
        integ = b.get("/vi/v1/integrity").json()
        say(f"integrity after restart: {integ['status']} log_lines={integ['log_lines']} problems={integ['problems'][:2]}")
        check("restart: anchors match, integrity green", integ["status"] == "green")
        c = b.cert("a1")
        check("restart: certification state replayed", c["status"] == "revised" and c["certified_views"] == 8700)
        r = b.post("/vi/v1/identity/checks", {"request_id": b.rid(), "clipper_id": "zoe", "email": "zoe@example.com"},
                   caller="clipper_network")
        say(f"write after restart (identity check) -> {r.status_code} {r.json()['status']}")
        del pb2
        # ------------------------------------------------------------------ ledgers
        for name, L in (("A", LA), ("B", LB)):
            ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
            vi = [x for x in ents if x.get("department") == "verification_integrity"]
            say(f"ledger {name}: {len(ents)} entries, {len(vi)} from verification_integrity; "
                f"{len({x['event_type'] for x in vi})} event types")
            if name == "B":
                say("  event types: " + ", ".join(sorted({x["event_type"] for x in vi})))
            v = httpx.get(L + "/ledger/verify", headers=lh, timeout=120)
            say(f"GET /ledger/verify (ledger {name}) -> {v.status_code} {v.text}")
            check(f"ledger {name} verifies valid", v.status_code == 200 and v.json().get("valid") is True)
        failed = [n for n, ok in CHECKS if not ok]
        say(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
        return 0 if not failed else 1
    finally:
        for p in PROCS:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                say(f"stopped pid={p.pid}")
        (work / "live_run.txt").write_text("\n".join(LOG) + "\n")


def main() -> int:
    work, keep = make_work_dir("vi-live-")
    try:
        return _main(work)
    finally:
        finish_work_dir(work, keep)


if __name__ == "__main__":
    sys.exit(main())
