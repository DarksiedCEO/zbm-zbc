"""
Live HTTP run: the real ledger-rust binary + clipper-network-py.

  A: the production entrypoint (``cd src && python3 -m api``) with every department a fail-closed stand-in, on
     ledger A: auth on the wire, seed approval, an application, admission BLOCKED with one
     DEPENDENCY_UNAVAILABLE per stand-in; restart (the disk log is verified against the ledger's anchors).
  B: devtools/live_server.py (the same app and launcher, test fakes behind a fixture file) on its OWN ledger B
     (one disk-backed instance per ledger): admitted → tiered → enrolled → kit delivered + acknowledged →
     S3 strike mirrored → suspension + ban proposal → Andre's ban approval (V&I ban called) → offboarding →
     exit closed after the retention period; restart with the anchor check.
  Both ledgers end with GET /ledger/verify.

Every narrated behaviour is ASSERTED (AEGIS N16-10): a mismatch is printed as ``MISMATCH: ...`` and the run exits
non-zero; the last line says how many expectations held.

usage: LEDGER_BIN=/path/to/server python3 devtools/live_run.py [--ports 19350,19351,19352,19353]
(ledger A, CN A, CN B, ledger B). Kills only the PIDs it started.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]
PORTS = [int(p) for p in (sys.argv[sys.argv.index("--ports") + 1].split(",") if "--ports" in sys.argv else
                          ["19350", "19351", "19352", "19353"])]
LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
SVC_TOKEN = "live-cn-service-token-" + "s" * 16
HMAC_KEY = "live-cn-identity-hmac-key-" + "k" * 12
ANDRE = "live-andre-approval-token-" + "a" * 16
CALLERS = {n: f"live-cn-caller-{n}-" + "c" * 24 for n in ("hub", "onboarding", "creative_production", "scheduler",
                                                           "verification_integrity")}
LOG: list[str] = []
PROCS: list[subprocess.Popen] = []
EXPECTED: list[str] = []
MISMATCHES: list[str] = []


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


def expect(what: str, ok: bool, observed=None) -> None:
    """Assert one narrated behaviour; a mismatch is logged and fails the run (exit 1)."""
    EXPECTED.append(what)
    if not ok:
        MISMATCHES.append(what)
        say(f"MISMATCH: {what} (observed: {observed!r})"[:1500])


def wait_health(url: str, proc: subprocess.Popen, timeout: float = 25.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"{url}: process exited with {proc.returncode}")
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


def daytime_zone() -> str:
    now = datetime.now(timezone.utc)
    for z in ("America/Los_Angeles", "America/New_York", "Europe/London", "Asia/Kolkata", "Asia/Tokyo",
              "Pacific/Auckland", "America/Sao_Paulo", "Europe/Berlin"):
        if 9 <= now.astimezone(ZoneInfo(z)).hour < 18:
            return z
    return "Etc/GMT-14"


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, pa, pb, plb = PORTS
    L, A, B, LB = (f"http://127.0.0.1:{p}" for p in (pl, pa, pb, plb))
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    common = {"CN_SERVICE_TOKEN": SVC_TOKEN, "CN_IDENTITY_HMAC_KEY": HMAC_KEY, "CN_ANDRE_APPROVAL_TOKEN": ANDRE,
              "CN_CALLER_TOKENS": json.dumps(CALLERS), "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN,
              "CN_POSTAL_ADDRESS": "Z Best Media, 100 Example St, Los Angeles CA 90001",
              "CN_OPT_OUT_URL": "https://zbc.example/opt-out"}
    try:
        (work / "la").mkdir()
        led_a = start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                                     "LEDGER_LOG_PATH": str(work / "la" / "ledger.jsonl")}, str(work / "la"), "ledger_a", work)
        say(f"ledger A health: {wait_health(L + '/health', led_a)}")
        env_a = {**common, "LEDGER_SERVICE_URL": L, "CN_PORT": str(pa), "CN_DATA_DIR": str(work / "a")}
        cn_a = start([sys.executable, "-m", "api"], env_a, str(SVC / "src"), "cn_a", work)
        say(f"CN A (production entrypoint, stand-ins) health: {wait_health(A + '/health', cn_a)}")
        c = httpx.Client(timeout=30)
        n = iter(range(1, 100_000))

        def rid():
            return f"live-{next(n)}"

        def h(caller=None, andre=None):
            d = {"Authorization": f"Bearer {SVC_TOKEN}"}
            if caller:
                d["X-CN-Caller-Token"] = CALLERS[caller]
            if andre:
                d["X-Andre-Approval-Token"] = andre
            return d

        non_ascii = {"Authorization": "Bearer tök".encode("utf-8")}
        wire = (c.get(A + '/cn/v1/rules').status_code, c.get(A + '/cn/v1/rules', headers=non_ascii).status_code,
                c.get(A + '/docs').status_code,
                c.post(A + '/cn/v1/clippers/x/ban-decision', headers=h(andre=SVC_TOKEN),
                       json={'request_id': rid(), 'proposal_id': 'p', 'decision': 'approve', 'note': 'n'}).status_code)
        say(f"wire auth: no bearer -> {wire[0]}; non-ASCII bearer -> {wire[1]}; /docs -> {wire[2]}; "
            f"ban-decision with the service token as Andre -> {wire[3]}")
        expect("wire auth 401/401/404/403", wire == (401, 401, 404, 403), wire)
        tz = daytime_zone()
        app_body = {"display_name": "Live Clipper", "email": "live.clipper@example.com", "declared_country": "US",
                    "declared_region": "US-CA", "jurisdiction_attested": True, "time_zone": tz, "channel": "inbound_form",
                    "declared_18_plus": True, "sag_aftra_member": False}
        r = c.post(A + "/cn/v1/applications", headers=h("hub"), json={"request_id": rid(), **app_body}).json()
        cid_a = r["clipper_id"]
        adm = c.post(A + f"/cn/v1/clippers/{cid_a}/admission", headers=h("hub"), json={"request_id": rid()}).json()
        say(f"A admission before any rule version: admitted={adm['admitted']} unmet={adm['unmet_lines']}")
        expect("A admission before rules: refused CN-00 only", adm["admitted"] is False
               and [(u["rule_id"], u["code"]) for u in adm["unmet"]] == [("CN-00", "RULES_NOT_IN_FORCE")], adm["unmet"])
        seed = [p for p in c.get(A + "/cn/v1/inbox", headers=h("scheduler")).json() if p["kind"] == "seed"][0]
        d = c.post(A + "/cn/v1/rules/decisions", headers=h(andre=ANDRE), json={"request_id": rid(), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]})
        say(f"A seed approval by Andre -> {d.status_code} rules_version={d.json()['rules_version']}")
        expect("A seed approved -> v1", d.status_code == 200 and d.json()["rules_version"] == 1, d.text[:300])
        adm = c.post(A + f"/cn/v1/clippers/{cid_a}/admission", headers=h("hub"), json={"request_id": rid()}).json()
        say(f"A admission on stand-ins: admitted={adm['admitted']} codes="
            f"{sorted({(u['rule_id'], u['code']) for u in adm['unmet']})}")
        vi_d, cmp_d = "DEPENDENCY_UNAVAILABLE:verification_integrity", "DEPENDENCY_UNAVAILABLE:compliance_38"
        want = {("CN-01", vi_d), ("CN-02", cmp_d), ("CN-03", vi_d), ("CN-04", "DEPENDENCY_UNAVAILABLE:legal_37"),
                ("CN-05", "DEPENDENCY_UNAVAILABLE:finance_31"), ("CN-06", vi_d), ("CN-07", "TRAINING_MISSING"),
                ("CN-19", vi_d)}
        got = {(u["rule_id"], u["code"]) for u in adm["unmet"]}
        expect("A admission on stand-ins: one DEPENDENCY_UNAVAILABLE per stand-in", adm["admitted"] is False
               and got == want, sorted(got ^ want))
        stop(cn_a, "cn_a")
        cn_a = start([sys.executable, "-m", "api"], env_a, str(SVC / "src"), "cn_a_restart", work)
        ha = wait_health(A + '/health', cn_a)
        say(f"CN A restarted on the same data dir + ledger: {ha}")
        expect("A restart: v1, no reconcile needed", ha["rules_version"] == 1 and ha["reconcile_required"] is False, ha)
        ia = c.get(A + '/cn/v1/integrity', headers=h('scheduler')).json()
        say(f"A integrity after restart: {ia}")
        expect("A integrity ok after restart", ia.get("ok") is True and ia.get("anchor_problems") == [], ia)

        # ------------------------------------------------------------------ B: fakes behind a fixture file
        (work / "lb").mkdir()
        led_b = start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(plb),
                                     "LEDGER_LOG_PATH": str(work / "lb" / "ledger.jsonl")}, str(work / "lb"), "ledger_b", work)
        say(f"ledger B health: {wait_health(LB + '/health', led_b)}")
        fx = work / "fixture.json"
        world = {"clock_offset_days": 0, "certs": {}, "strikes": [], "finance_open_items": "none"}
        fx.write_text(json.dumps(world))
        env_b = {**common, "LEDGER_SERVICE_URL": LB, "CN_PORT": str(pb), "CN_DATA_DIR": str(work / "b"),
                 "CN_FIXTURE_FILE": str(fx)}
        cn_b = start([sys.executable, str(SVC / "devtools" / "live_server.py")], env_b, str(SVC), "cn_b", work)
        say(f"CN B (devtools live_server, fakes) health: {wait_health(B + '/health', cn_b)}")
        seed = [p for p in c.get(B + "/cn/v1/inbox", headers=h("scheduler")).json() if p["kind"] == "seed"][0]
        c.post(B + "/cn/v1/rules/decisions", headers=h(andre=ANDRE), json={"request_id": rid(), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]})
        memo = c.post(B + "/cn/v1/rules/proposals", headers=h(andre=ANDRE), json={
            "request_id": rid(), "kind": "counsel_memo", "target_id": "CN-CQ-01",
            "memo": {"memo_sha256": "e" * 64, "memo_ref": "live-fixture-memo", "summary": "Live-run fixture memo"}}).json()
        p = memo["proposal"]
        c.post(B + "/cn/v1/rules/decisions", headers=h(andre=ANDRE), json={"request_id": rid(), "decisions": [
            {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}]})
        vb_ = c.get(B + '/cn/v1/rules', headers=h('scheduler')).json()['rules_version']
        say(f"B rules in force: v{vb_} (seed + counsel memo resolving CN-CQ-01)")
        expect("B rules v2", vb_ == 2, vb_)
        r = c.post(B + "/cn/v1/applications", headers=h("hub"), json={"request_id": rid(), **app_body}).json()
        cid = r["clipper_id"]
        say(f"B application -> clipper {cid}")
        s = c.post(B + f"/cn/v1/clippers/{cid}/connections/start", headers=h("hub"), json={
            "request_id": rid(), "platform": "youtube", "redirect_uri": "https://hub.example/cb", "handle": "@live"}).json()
        state = s["authorization_url"].split("state=")[1]
        cc = c.post(B + f"/cn/v1/clippers/{cid}/connections/complete", headers=h("hub"), json={
            "request_id": rid(), "state": state, "code": "LIVE-OAUTH-CODE-7c1e"}).json()
        ag = c.post(B + f"/cn/v1/clippers/{cid}/age-check", headers=h("hub"), json={
            "request_id": rid(), "dob": "1993-02-14", "dob_field_neutral": True, "method": "photo_id_match",
            "provider_session_ref": "prov-1"}).json()
        import hashlib
        sha = hashlib.sha256(b"Clipper Agreement v3 (test fixture)").hexdigest()
        acc = c.post(B + f"/cn/v1/clippers/{cid}/agreement-acceptances", headers=h("hub"), json={
            "request_id": rid(), "doc_id": "clipper_agreement", "version": "v3", "doc_sha256": sha, "presented_sha256": sha,
            "method": "clickwrap_unticked_box", "box_ticked": True, "session_ref": "sess-live"}).json()
        tr = c.post(B + f"/cn/v1/clippers/{cid}/disclosure-training", headers=h("hub"), json={
            "request_id": rid(), "training_version": "dt-1", "attested": True})
        say(f"B relays: connection={cc['status']}, age={ag['result']}, agreement accepted={acc['accepted']}, "
            f"training={tr.status_code}")
        expect("B relays active/adult/accepted/200", (cc["status"], ag["result"], acc["accepted"], tr.status_code)
               == ("active", "adult", True, 200), (cc, ag, acc, tr.status_code))
        adm = c.post(B + f"/cn/v1/clippers/{cid}/admission", headers=h("hub"), json={"request_id": rid()}).json()
        say(f"B admission with fakes: admitted={adm['admitted']} status={adm['status']} id={adm['admission_id']} "
            f"facts_sha256={adm['facts_sha256'][:16]}...")
        expect("B admitted, active", adm["admitted"] is True and adm["status"] == "active", adm)
        world["certs"][cid] = [[f"vi-cert-{i}", f"sub-{i}", f"camp-{i}", "tiktok", "certified", 20000] for i in range(3)]
        world["clock_offset_days"] = 31
        fx.write_text(json.dumps(world))
        t = c.post(B + "/cn/v1/tiers/run", headers=h("scheduler"), json={"request_id": rid()}).json()
        say(f"B tiers run (+31 days, 3 certified clips): {[(x['from'], x['to']) for x in t['changed']]}")
        expect("B tier T0 -> T1", [(x["from"], x["to"]) for x in t["changed"]] == [("T0", "T1")], t)
        now = datetime.now(timezone.utc) + timedelta(days=31)
        cfg = c.put(B + "/cn/v1/campaigns/camp-live/network-config", headers=h(andre=ANDRE), json={
            "request_id": rid(), "min_tier": "T1", "platforms": ["youtube", "tiktok"], "clipper_jurisdictions": ["US"],
            "max_clippers": 50, "max_submissions_per_clipper": 5,
            "view_terms": {"min_views_to_review": 1000, "max_paid_views_per_clip": 500000},
            "rate_card_ref": {"finance_doc_id": "rc-live", "version": "v1",
                              "sha256": hashlib.sha256(b"rate card rc-live v1").hexdigest()},
            "rate_card_effective_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "opens_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "closes_at": (now + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        say(f"B network config (min_tier T1) by Andre -> {cfg.status_code} v{cfg.json()['config']['config_version']}")
        expect("B network config v1", cfg.status_code == 200 and cfg.json()["config"]["config_version"] == 1, cfg.text[:300])
        e = c.post(B + "/cn/v1/campaigns/camp-live/enrolments", headers=h("hub"),
                   json={"request_id": rid(), "clipper_id": cid}).json()
        say(f"B enrolment: eligible={e['eligible']} enrolment={e['enrolment_id']} kit_delivery={e['kit_delivery_id']}")
        expect("B enrolment eligible with a kit delivery", e["eligible"] is True and bool(e["enrolment_id"])
               and bool(e["kit_delivery_id"]), e)
        kd = [m for m in c.get(B + f"/cn/v1/clippers/{cid}/messages", headers=h("hub")).json()
              if m["template_id"] == "kit_delivered"][0]
        ka = c.post(B + f"/cn/v1/enrolments/{e['enrolment_id']}/kit-acknowledgment", headers=h("hub"), json={
            "request_id": rid(), "kit_delivery_id": e["kit_delivery_id"],
            "kit_sha256": hashlib.sha256(b"kit-1 as read").hexdigest(), "rulebook_version": 1, "rate_card_version": "v1",
            "rulebook_received": True, "disclosure_section_received": True})
        say(f"B kit delivered (message {kd['delivery_status']}); acknowledgment -> {ka.status_code} "
            f"acknowledged_at={ka.json().get('acknowledged_at')}")
        expect("B kit message sent and acknowledged", kd["delivery_status"] == "sent" and ka.status_code == 200
               and bool(ka.json().get("acknowledged_at")), (kd["delivery_status"], ka.text[:300]))
        ann = c.post(B + "/cn/v1/campaigns/camp-live/rulebook-announcements", headers=h("creative_production"),
                     json={"request_id": rid(), "version": 1, "facts": {"note": "re-announce v1"}}).json()
        say(f"B rulebook announcement (Creative protocol): allowed={ann['allowed']} reason={ann['reason']}")
        expect("B announcement allowed", ann["allowed"] is True, ann)
        world["strikes"] = [{"clipper_id": cid, "class": "S3", "n": 1}]
        fx.write_text(json.dumps(world))
        ds = c.post(B + "/cn/v1/discipline/sync", headers=h("scheduler"), json={"request_id": rid()}).json()
        cl = c.get(B + f"/cn/v1/clippers/{cid}", headers=h("scheduler")).json()
        say(f"B discipline sync: mirrored={ds['mirrored']} applied={ds['applied']} ban_proposals={ds['ban_proposals']}; "
            f"clipper status={cl['status']} tier={cl['tier']} suspension={cl['active_suspension']['kind']}")
        expect("B S3 mirrored -> suspension, T0, one ban proposal", len(ds["mirrored"]) == 1
               and [a_["action"] for a_ in ds["applied"]] == ["suspension"] and len(ds["ban_proposals"]) == 1
               and (cl["status"], cl["tier"], cl["active_suspension"]["kind"]) == ("suspended", "T0", "s3_full"), (ds, cl))
        bad = c.post(B + f"/cn/v1/clippers/{cid}/ban-decision", headers=h("scheduler"), json={
            "request_id": rid(), "proposal_id": ds["ban_proposals"][0], "decision": "approve", "note": "x"})
        ban = c.post(B + f"/cn/v1/clippers/{cid}/ban-decision", headers=h(andre=ANDRE), json={
            "request_id": rid(), "proposal_id": ds["ban_proposals"][0], "decision": "approve",
            "note": "bought engagement upheld at V&I"}).json()
        say(f"B ban-decision with a caller token -> {bad.status_code}; with Andre's token -> status={ban['status']} "
            f"clipper={ban['clipper_status']} V&I ban propagation={ban['vi_ban_propagation']} "
            f"offboarding={ban['offboarding_id']}")
        expect("B ban: caller token 403; Andre approved, propagated with his token, offboarding started",
               bad.status_code == 403 and (ban["status"], ban["clipper_status"], ban["vi_ban_propagation"])
               == ("approved", "offboarding", "done") and bool(ban["offboarding_id"]), (bad.status_code, ban))
        off = c.get(B + f"/cn/v1/clippers/{cid}/offboarding", headers=h("hub")).json()
        say(f"B offboarding: status={off['status']} steps={[(s_['step'], s_['status']) for s_ in off['steps']]}")
        expect("B offboarding in progress with every step", off["status"] == "in_progress"
               and [(s_["step"], s_["status"]) for s_ in off["steps"]] == [
                   ("status_offboarding", "done"), ("cn_access_revoked", "done"), ("hub_session_revoked", "done"),
                   ("vi_connections_revoked", "done"), ("finance_open_items", "done"), ("export", "done"),
                   ("deletion", "scheduled")], off)
        stop(cn_b, "cn_b")
        cn_b = start([sys.executable, str(SVC / "devtools" / "live_server.py")], env_b, str(SVC), "cn_b_restart", work)
        hb = wait_health(B + '/health', cn_b)
        say(f"CN B restarted (anchor check at start passed): {hb}")
        expect("B restart: v2, no reconcile needed", hb["rules_version"] == 2 and hb["reconcile_required"] is False, hb)
        world["clock_offset_days"] = 31 + 31
        fx.write_text(json.dumps(world))
        run = c.post(B + "/cn/v1/offboarding/run", headers=h("scheduler"), json={"request_id": rid()}).json()
        cl = c.get(B + f"/cn/v1/clippers/{cid}", headers=h("hub")).json()
        say(f"B offboarding run after the retention period: deleted={run['deleted']} closed={run['closed']}; "
            f"clipper status={cl['status']} contact={cl['contact']}")
        expect("B exit deleted and closed after the retention period", run["deleted"] == [ban["offboarding_id"]]
               and run["closed"] == [ban["offboarding_id"]] and cl["status"] == "offboarded" and cl["contact"] is None,
               (run, cl))
        ib = c.get(B + '/cn/v1/integrity', headers=h('scheduler')).json()
        say(f"B integrity: {ib}")
        expect("B integrity ok", ib.get("ok") is True and ib.get("anchor_problems") == [], ib)
        blob = (work / "b" / "cn_log.jsonl").read_bytes() + (work / "b" / "cn_contacts.json").read_bytes() + \
            (work / "lb" / "ledger.jsonl").read_bytes()
        scan = {"code": b'LIVE-OAUTH-CODE-7c1e' in blob, "dob": b'1993-02-14' in blob, "andre_token": ANDRE.encode() in blob}
        say(f"byte scan of B's log, contact store and ledger file for the OAuth code, DOB and Andre's token: {scan}")
        expect("no OAuth code, DOB or Andre token at rest", not any(scan.values()), scan)
        for name, url in (("A", L), ("B", LB)):
            ents = c.get(url + "/ledger/entries", headers=lh).json()
            cn = [x for x in ents if x.get("department") == "clipper_network"]
            say(f"ledger {name}: {len(ents)} entries, {len(cn)} from clipper_network; types: "
                f"{sorted({x['event_type'] for x in cn})}")
        va = c.get(L + "/ledger/verify", headers=lh)
        vb = c.get(LB + "/ledger/verify", headers=lh)
        say(f"GET /ledger/verify (A) -> {va.status_code} {va.text}")
        say(f"GET /ledger/verify (B) -> {vb.status_code} {vb.text}")
        expect("both ledgers verify", all(x.status_code == 200 and x.json().get("valid") is True for x in (va, vb)),
               (va.text, vb.text))
        say(f"RESULT: {len(EXPECTED) - len(MISMATCHES)}/{len(EXPECTED)} narrated behaviours held"
            + (f"; MISMATCHES: {MISMATCHES}" if MISMATCHES else ""))
        return 1 if MISMATCHES else 0
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
    work, keep = make_work_dir("cn-live-")
    try:
        return _main(work)
    finally:
        finish_work_dir(work, keep)


if __name__ == "__main__":
    sys.exit(main())
