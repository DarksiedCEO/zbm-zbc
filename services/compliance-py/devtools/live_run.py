"""
Live HTTP run: real ledger-rust binary + compliance-py (production entrypoint
``cd src && python3 -m api``) + a second compliance-py via devtools/live_server.py
(fixture fetcher, for the Change Watcher leg). Real processes, real sockets,
real bearer/caller/Andre tokens. Also drives the onboarding-py and creative-py
thin clients (HttpComplianceDepartment, HttpCompliance38) against the live server.

usage: LEDGER_BIN=/path/to/server python3 devtools/live_run.py [--ports 18950,18951,18952]
Kills only the PIDs it started.
"""

from __future__ import annotations

import hashlib
import json
import os
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
                          ["18950", "18951", "18952"])]
LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
SVC_TOKEN = "live-compliance-service-token-" + "s" * 12
ANDRE = "live-andre-approval-token-" + "a" * 16
CALLERS = {n: f"live-caller-{n}-" + "c" * 24 for n in ("onboarding", "creative_production", "finance_31", "legal_37",
                                                        "people_43", "scheduler")}
LOG: list[str] = []
PROCS: list[subprocess.Popen] = []


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def wait_health(url: str, timeout: float = 20.0) -> dict:
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


def main() -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    work = Path(tempfile.mkdtemp(prefix="cmp-live-", dir=os.environ.get("LIVE_WORK_DIR")))
    (work / "fixtures").mkdir()
    pl, pa, pb = PORTS
    L, A, B = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{pa}", f"http://127.0.0.1:{pb}"
    try:
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger.jsonl")}, str(work), "ledger", work)
        say(f"ledger health: {wait_health(L + '/health')}")
        common = {"COMPLIANCE_SERVICE_TOKEN": SVC_TOKEN, "COMPLIANCE_ANDRE_APPROVAL_TOKEN": ANDRE,
                  "COMPLIANCE_CALLER_TOKENS": json.dumps(CALLERS), "LEDGER_SERVICE_URL": L,
                  "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
        start([sys.executable, "-m", "api"], {**common, "COMPLIANCE_PORT": str(pa), "COMPLIANCE_DATA_DIR": str(work / "a")},
              str(SVC / "src"), "compliance_a", work)
        say(f"compliance A health: {wait_health(A + '/health')}")

        def h(caller=None, andre=None):
            d = {"Authorization": f"Bearer {SVC_TOKEN}"}
            if caller:
                d["X-Compliance-Caller-Token"] = CALLERS[caller]
            if andre:
                d["X-Andre-Approval-Token"] = andre
            return d

        c = httpx.Client(timeout=30)
        n = iter(range(1, 10_000))

        def rid():
            return f"live-{next(n)}"

        # auth sanity on the wire
        non_ascii = {"Authorization": "Bearer t\u00f6k".encode("utf-8")}
        say(f"no bearer -> {c.get(A + '/compliance/v1/inbox').status_code}; "
            f"non-ASCII bearer -> {c.get(A + '/compliance/v1/inbox', headers=non_ascii).status_code}; "
            f"/docs -> {c.get(A + '/docs').status_code}")
        # gates before the seed is approved
        r = c.post(A + "/compliance/v1/rule", headers=h("onboarding"),
                   json={"request_id": rid(), "subject_id": "client-live", "lane": "client", "facts": {}}).json()
        say(f"activation before seed approval: allowed={r['allowed']} unmet={[(u['obligation_id'], u['code']) for u in r['unmet']]}")
        # register approval: refused, then the seed with Andre's token
        seed = [p for p in c.get(A + "/compliance/v1/inbox", headers=h("scheduler")).json() if p["kind"] == "seed"][0]
        dec = {"request_id": rid(), "decisions": [{"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"],
                                                   "decision": "approve"}]}
        say(f"approval with the service token as Andre token -> {c.post(A + '/compliance/v1/register/decisions', headers=h(andre=SVC_TOKEN), json=dec).status_code}")
        r = c.post(A + "/compliance/v1/register/decisions", headers=h(andre=ANDRE), json=dec)
        say(f"seed approval -> {r.status_code} {r.json()}")
        say(f"health: {c.get(A + '/health').json()}")
        # controls (the fourth gate: control monitoring)
        r = c.post(A + "/compliance/v1/controls/internal/run", headers=h("scheduler"), json={"request_id": rid()}).json()
        say("internal controls: " + json.dumps({k: v["result"] for k, v in r["results"].items()}))
        r = c.post(A + "/compliance/v1/controls/C-08/results", headers=h("people_43"),
                   json={"request_id": rid(), "result": "pass", "tested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "evidence": [{"kind": "ack_export", "ref": "acks-2026-09", "sha256": "a" * 64}]}).json()
        say(f"C-08 pushed by people_43 -> {r['control']['status']}")
        tc = c.get(A + "/compliance/v1/trust-center", headers=h("scheduler")).json()
        say("trust center: " + ", ".join(f"{x['control_id']}={x['status']}" for x in tc))
        # gate 1: activation (client allowed; creator blocked by stand-ins)
        client_facts = {"jurisdiction": {"declared_country": "US", "declared_region": "US-NY", "attested": True,
                                         "attestation_ref": "att-1"},
                        "flags": {k: False for k in ("political_content", "child_directed", "health_data_shared",
                                                     "biz_opp_client", "audience_data_sale", "marketplace", "pooled_client_funds")},
                        "target_jurisdictions": ["US", "GB"], "platforms": ["youtube"], "client_category": "general",
                        "claims": {"claims_present": False, "health_or_earnings_claim": False, "claim_file_id": None,
                                   "claim_file_approved": False},
                        "services_include_review_suppression": False, "outbound_cold_contact_countries": [],
                        "hbnr_clause_signed": False}
        r = c.post(A + "/compliance/v1/rule", headers=h("onboarding"),
                   json={"request_id": rid(), "subject_id": "client-live", "lane": "client", "facts": client_facts}).json()
        say(f"GATE activation (client, US->US/GB): allowed={r['allowed']} ruling={r['ruling_id']} ledger_event={r['ledger_event_id']}")
        # thin clients against the live server
        sys.path[:0] = [str(SERVICES / "onboarding-py" / "src"), str(SERVICES / "creative-py" / "src")]
        from integrations.compliance38 import HttpComplianceDepartment
        from shared.compliance38 import HttpCompliance38
        onb = HttpComplianceDepartment(A, SVC_TOKEN, CALLERS["onboarding"])
        ruling = onb.rule("client-live-2", "client", client_facts)
        say(f"onboarding thin client rule(client) -> allowed={ruling.allowed} detail={ruling.detail}")
        ruling = onb.rule("clipper-live", "zbc_creator", {})
        say(f"onboarding thin client rule(zbc_creator, facts={{}}) -> allowed={ruling.allowed}, {len(ruling.unmet)} unmet lines, first: {ruling.unmet[0][:110]}")
        bad = HttpComplianceDepartment(A, SVC_TOKEN, CALLERS["creative_production"])
        say(f"onboarding thin client with the wrong caller token -> {bad.rule('x', 'client', {})}")
        scr = c.post(A + "/compliance/v1/sanctions/screen", headers=h("onboarding"),
                     json={"request_id": rid(), "subject_id": "clipper-live", "role": "payee", "legal_name": "Live Clipper",
                           "dob": "1990-01-01", "country": "US", "region": "US-CA"}).json()
        say(f"sanctions screen (provider stand-in) -> {scr['result']}")
        creator = {"jurisdiction": {"declared_country": "US", "declared_region": "US-CA", "attested": True, "attestation_ref": "a"},
                   "network_country_signal": "US", "flags": client_facts["flags"],
                   "age": {"verification_attestation_id": "vi-1", "method": "photo_id_match", "dob_field_neutral": True},
                   "recruitment_channel": "inbound", "payee_type": "individual", "sanctions_screen_id": scr["screen_id"],
                   "owner_screen_ids": [], "tax_form_kind": "w9", "rail_kyc": {"status": "verified", "rail": "stripe"},
                   "creator_agreement_version": "cav-3", "disclosure_training_attested": True,
                   "accounts": [{"platform": "youtube", "handle_sha256": "b" * 64}], "accounts_complete_attested": True}
        r = c.post(A + "/compliance/v1/rule", headers=h("onboarding"),
                   json={"request_id": rid(), "subject_id": "clipper-live", "lane": "zbc_creator", "facts": creator}).json()
        say(f"GATE activation (creator): allowed={r['allowed']} unmet={sorted({(u['obligation_id'], u['code']) for u in r['unmet']})}")
        # gate 2: payout via creative thin client
        cre = HttpCompliance38(A, SVC_TOKEN, CALLERS["creative_production"])
        g = cre.review("zbc_clip", "sub-live-1", {"campaign_id": "camp-live", "rulebook_version": 1, "post_ref": "p",
                                                  "clipper_id": "clipper-live", "posted_at": "2026-09-01T00:00:00+00:00",
                                                  "clip_review": "pass"})
        say(f"GATE payout via creative thin client (today's six facts): allowed={g.allowed} reference={g.reference} reason={g.reason[:160]}")
        # gate 3: publish
        g = cre.review("zbm_work", "work-live-1", {"brief_id": "brief-1", "export": {"fmt": "mp4"}, "rights": {"ok": True}})
        say(f"GATE publish via creative thin client (brief_id/export/rights): allowed={g.allowed} reason={g.reason[:160]}")
        pub = {"brief_id": "brief-1", "asset_type": "site", "asset_content_sha256": "d" * 64, "client_id": "client-live",
               "target_jurisdictions": ["US"], "platforms": ["web"],
               "flags": {k: False for k in ("paid_or_endorsement", "synthetic_performer", "ai_manipulated_media",
                                            "real_person_likeness", "implied_affiliation", "personal_use_claim", "claims_present",
                                            "health_or_earnings_claim", "child_directed", "child_access_likely",
                                            "political_content", "audience_data_sale", "uses_tracking", "collects_pii",
                                            "consumer_ecommerce")},
               "claim_file_id": None, "claim_file_approved": False,
               "docs": {"privacy_policy": {"doc_id": "pp", "version": "v1"}, "terms": {"doc_id": "tos", "version": "v1"}, "forms": []},
               "tracking": {"tracking_disclosed": True, "consent_banner_present": True, "consent_before_nonessential": True,
                            "opt_out_present": True}}
        r = c.post(A + "/compliance/v1/gates/publish", headers=h("creative_production"),
                   json={"request_id": rid(), "subject_kind": "zbm_work", "subject_id": "site-live", "facts": pub}).json()
        say(f"GATE publish (site, full facts): allowed={r['allowed']} unmet={sorted({(u['obligation_id'], u['code']) for u in r['unmet']})}")
        # a second register approval: counsel memo supersedes CQ-01 (legal_37 proposes, Andre approves)
        cq = c.get(A + "/compliance/v1/register/CQ-01", headers=h("scheduler")).json()["row"]
        cq.pop("effective_status")
        memo_sha = hashlib.sha256(b"live counsel memo CQ-01").hexdigest()
        url = "urn:zbm:counsel-memo:cq-01-live"
        row = {**cq, "id": "CQ-01-M1", "domain": "counsel", "title": "Counsel memo answering CQ-01 (live run fixture)",
               "obligation": "Counsel memo answering CQ-01 (live run fixture).", "source_url": url, "source_kind": "guidance",
               "source_quality": "primary", "verified_at": time.strftime("%Y-%m-%d", time.gmtime()), "status": "verified",
               "check": "engine_invariant", "counsel_flag": False}
        ev = {"source_url": url, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "snapshot_sha256": memo_sha,
              "normalized_text_sha256": memo_sha, "quoted_excerpt": "memo conclusion (fixture)", "doc_number": None}
        p = c.post(A + "/compliance/v1/register/proposals", headers=h("legal_37"),
                   json={"request_id": rid(), "kind": "supersede", "target_id": "CQ-01", "proposed_row": row, "evidence": ev})
        say(f"legal_37 proposal -> {p.status_code}")
        p = p.json()["proposal"]
        r = c.post(A + "/compliance/v1/register/decisions", headers=h(andre=ANDRE), json={"request_id": rid(), "decisions": [
            {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve", "note": "memo signed"}]})
        say(f"Andre approves memo -> {r.status_code} register_version={r.json()['register_version']}")
        # Change Watcher leg (process B, fixture fetcher)
        fx = work / "fixtures"
        page = "https://cppa.ca.gov/regulations/ccpa_updates.html"
        fpath = fx / (hashlib.sha256(page.encode()).hexdigest() + ".bin")
        fpath.write_bytes(b"<html><body><h1>CCPA Updates</h1><p>Proposed regulations under review.</p></body></html>")
        start([sys.executable, str(SVC / "devtools" / "live_server.py")],
              {**common, "COMPLIANCE_PORT": str(pb), "COMPLIANCE_DATA_DIR": str(work / "b"), "COMPLIANCE_FIXTURE_DIR": str(fx),
               "COMPLIANCE_WATCHER_ENABLED": "1"}, str(SVC), "compliance_b", work)
        say(f"compliance B (fixture fetcher) health: {wait_health(B + '/health')}")
        seedb = [x for x in c.get(B + "/compliance/v1/inbox", headers=h("scheduler")).json() if x["kind"] == "seed"][0]
        c.post(B + "/compliance/v1/register/decisions", headers=h(andre=ANDRE), json={"request_id": rid(), "decisions": [
            {"proposal_id": seedb["proposal_id"], "content_sha256": seedb["content_sha256"], "decision": "approve"}]})
        w1 = c.post(B + "/compliance/v1/watcher/run", headers=h("scheduler"), json={"request_id": rid()}).json()
        say(f"watcher run 1 (baseline): ok={w1['ok']} failed={len(w1['failed'])} proposals={w1['proposals']}")
        fpath.write_bytes(b"<html><body><h1>CCPA Updates</h1><p>The regulations on automated decisionmaking technology, "
                          b"risk assessments and cybersecurity audits take effect January 1, 2026.</p></body></html>")
        w2 = c.post(B + "/compliance/v1/watcher/run", headers=h("scheduler"), json={"request_id": rid()}).json()
        say(f"watcher run 2 (changed page): proposals={w2['proposals']}")
        inbox = [x for x in c.get(B + "/compliance/v1/inbox", headers=h("scheduler")).json() if x["proposal_id"] in w2["proposals"]]
        for x in inbox:
            say(f"  proposal {x['proposal_id']} kind={x['kind']} target={x['target_id']} proposed_by={x['proposed_by']}")
        r = c.post(B + "/compliance/v1/register/decisions", headers=h(andre=ANDRE), json={"request_id": rid(), "decisions": [
            {"proposal_id": x["proposal_id"], "content_sha256": x["content_sha256"], "decision": "approve"} for x in inbox]})
        say(f"Andre approves watcher proposals -> {r.status_code} register_version={r.json()['register_version']}")
        st = c.get(B + "/compliance/v1/register/US-CPPA-2025", headers=h("scheduler")).json()["row"]["status"]
        say(f"US-CPPA-2025 status in B after approval: {st}")
        # audit export and ledger verification
        ex = c.get(A + "/compliance/v1/audit/export", headers=h("scheduler")).json()
        say(f"audit export page: {len(ex['records'])} records, ledger event {ex['ledger_event_id']}")
        ents = c.get(L + "/ledger/entries", headers={"Authorization": f"Bearer {LEDGER_TOKEN}"}).json()
        cmp = [e for e in ents if e.get("department") == "compliance"]
        kinds = sorted({e["event_type"] for e in cmp})
        say(f"ledger entries: {len(ents)} total, {len(cmp)} from compliance; event types: {kinds}")
        v = c.get(L + "/ledger/verify", headers={"Authorization": f"Bearer {LEDGER_TOKEN}"})
        say(f"GET /ledger/verify -> {v.status_code} {v.text}")
        return 0 if v.status_code == 200 and v.json().get("valid") is True else 1
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
        print(f"logs in {work}")


if __name__ == "__main__":
    sys.exit(main())
