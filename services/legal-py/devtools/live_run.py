"""
Live HTTP run for legal-py: the real ledger-rust binary, the production entrypoint (``cd src && python3 -m api``),
real processes, real sockets, real tokens, the real system clock.

Compliance (38) is a small stub HTTP server inside this script (it records proposals and answers every register
row ``unverified``), reached through Legal's REAL thin client (LEGAL_COMPLIANCE_*); every other port is its
fail-closed production stand-in (no counsel channel, no e-sign provider, Cybersecurity 22 / People 43 not built).

Flow: day-one refusals -> Andre approves the rules -> engagement letter countersigned + approved -> counsel memo
-> playbook -> document draft (MSA template) -> counsel sign-off record -> Andre approval -> template fill ->
sign-off + approval -> clickwrap acceptance (evidence-sufficient after the CQ-19 memo) -> obligations extracted ->
onboarding contract terms -> memo intake -> Compliance proposal delivered to the stub -> takedown notice ->
counter-notice window -> restore refused before the window -> demand letter -> hold -> RESTART on the same data
directory (anchors checked at start, integrity green, state replayed) -> a tampered copy refuses to start ->
GET /ledger/verify valid.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py [--ports 19500,19501,19502,19503]
Kills only the PIDs it started. Exit 0 only when the ledger verifies valid and every checked expectation held.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]
PORTS = [int(p) for p in (sys.argv[sys.argv.index("--ports") + 1].split(",") if "--ports" in sys.argv else
                          ["19500", "19501", "19502", "19503"])]
LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
TOKEN = "live-legal-service-token-" + "s" * 16
ANDRE = "live-legal-andre-approval-token-" + "a" * 12
CMP_TOKEN = "live-compliance-stub-token-" + "c" * 12
CMP_CALLER = "live-compliance-stub-caller-legal37-" + "k" * 8
CALLERS = {n: f"live-legal-caller-{n}-" + "c" * 24 for n in ("compliance_38", "clipper_network", "verification_integrity",
                                                              "creative_production", "onboarding", "finance_31", "hub",
                                                              "esign_gateway", "scheduler")}
COUNSEL = "eng-live-counsel-1"
LOG: list[str] = []
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool]] = []


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


# --- the Compliance stub (records proposals; every row unverified) ------------------------------------------------

STUB: dict = {"proposals": [], "bad_auth": 0}


class ComplianceStub(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _auth(self) -> bool:
        ok = (self.headers.get("Authorization") == f"Bearer {CMP_TOKEN}"
              and self.headers.get("X-Compliance-Caller-Token") == CMP_CALLER)
        if not ok:
            STUB["bad_auth"] += 1
        return ok

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self._auth():
            return self._send(403, {"detail": "caller"})
        if self.path != "/compliance/v1/register/proposals":
            return self._send(404, {"detail": "no route"})
        urls = [str((body.get("evidence") or {}).get("source_url")), str((body.get("proposed_row") or {}).get("source_url"))]
        if not all(u.startswith(("https://", "http://", "urn:")) for u in urls):   # what compliance-py enforces
            return self._send(422, {"detail": "source_url must be an http(s) or urn: URL"})
        for rid_, b in STUB["proposals"]:
            if rid_ == body.get("request_id"):
                return self._send(201, {"proposal": {"proposal_id": f"prop-{rid_[:40]}"}})
        STUB["proposals"].append((body.get("request_id"), body))
        return self._send(201, {"proposal": {"proposal_id": f"prop-{str(body.get('request_id'))[:40]}"}})

    def do_GET(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"detail": "caller"})
        oid = self.path.rsplit("/", 1)[-1]
        return self._send(200, {"register_version": 1, "row": {"id": oid, "effective_status": "unverified"}})


class Api:
    def __init__(self, base: str):
        self.base = base
        self.c = httpx.Client(timeout=60)
        self.n = 0

    def rid(self, p="live") -> str:
        self.n += 1
        return f"{p}-{self.n}"

    def h(self, caller=None, andre=False):
        d = {"Authorization": f"Bearer {TOKEN}"}
        if caller:
            d["X-LEGAL-Caller-Token"] = CALLERS[caller]
        if andre:
            d["X-Andre-Approval-Token"] = ANDRE
        return d

    def post(self, path, body, caller=None, andre=False):
        return self.c.post(self.base + path, json=body, headers=self.h(caller, andre))

    def put(self, path, body, caller=None):
        return self.c.put(self.base + path, json=body, headers=self.h(caller))

    def get(self, path, caller="scheduler", andre=False, **params):
        return self.c.get(self.base + path, headers=self.h(caller, andre), params=params)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, plg, pcmp, pcopy = PORTS
    L, LG, CMP = (f"http://127.0.0.1:{p}" for p in (pl, plg, pcmp))
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    stub = ThreadingHTTPServer(("127.0.0.1", pcmp), ComplianceStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    say(f"Compliance stub listening on {CMP} (in this process)")
    env = {"LEGAL_SERVICE_TOKEN": TOKEN, "LEGAL_ANDRE_APPROVAL_TOKEN": ANDRE, "LEGAL_CALLER_TOKENS": json.dumps(CALLERS),
           "LEDGER_SERVICE_URL": L, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEGAL_PORT": str(plg),
           "LEGAL_DATA_DIR": str(work / "legal"), "LEGAL_COMPLIANCE_URL": CMP, "LEGAL_COMPLIANCE_TOKEN": CMP_TOKEN,
           "LEGAL_COMPLIANCE_CALLER_TOKEN": CMP_CALLER}
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger", work)
        say(f"ledger health: {wait_health(L + '/health')}")
        lp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "legal", work)
        hl = wait_health(LG + "/health")
        say(f"legal-py health: {hl}")
        check("day one: no rules, durable, counsel channel not wired",
              hl["rules_version"] is None and hl["in_memory"] is False and hl["counsel_channel_wired"] is False)
        a = Api(LG)
        r = a.post("/legal/v1/requests", {"request_id": a.rid(), "channel": "email", "requester_ref": "r",
                                          "kind": "question"}, caller="hub")
        check("before Andre's approval every action is refused RULES_NOT_IN_FORCE",
              r.status_code == 409 and r.json()["detail"] == "RULES_NOT_IN_FORCE")
        r = a.get("/legal/v1/documents/clipper_agreement/current", caller="compliance_38").json()
        say(f"current_version(clipper_agreement) before rules -> available={r['available']} reason={r['reason']}")
        seed = [p for p in a.get("/legal/v1/rules").json()["open_proposals"] if p["kind"] == "seed"][0]
        r = a.post("/legal/v1/rules/decisions", {"request_id": a.rid(), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]},
            andre=True)
        check("Andre approves the Legal rules seed (version 1)", r.status_code == 200 and r.json()["rules_version"] == 1)
        r = a.post("/legal/v1/playbooks/proposals", {"request_id": a.rid(), "playbook": {
            "playbook_id": "x", "doc_type": "nda", "version": "1.0", "clauses": [{}]}}, caller="scheduler")
        check("a playbook change without Andre's token -> 403", r.status_code == 403)

        # --- engagement letter (LG-17) --------------------------------------------------------------------
        eng_text = f"Engagement letter with {COUNSEL}: scope, billing guidelines, AI-use terms (ENG-AI-01)."
        e = a.post("/legal/v1/documents/engagement_letter/versions", {"request_id": a.rid(),
                   "entity": "zbm", "text": eng_text, "clause_ids": [{"clause_id": "ENG-AI-01", "position": "standard"}]},
                   andre=True).json()
        a.post("/legal/v1/documents/engagement_letter/versions/1.0/counsel-signoff",
               {"request_id": a.rid(), "counsel_ref": COUNSEL, "signed_on": time.strftime("%Y-%m-%d"),
                "doc_sha256": e["sha256"], "countersignature_b64": b64(b"countersigned engagement letter")}, andre=True)
        r = a.post("/legal/v1/documents/engagement_letter/versions/1.0/decision",
                   {"request_id": a.rid(), "decision": "approve", "version_sha256": e["sha256"]}, andre=True)
        check("engagement letter countersigned by counsel and approved by Andre", r.json().get("status") == "approved")

        # --- counsel memo #1: playbook clauses, the MSA versions, CQ-19 ----------------------------------------
        today = time.strftime("%Y-%m-%d")
        clauses = ["MSA-RENEW-01", "MSA-PAY-01", "MSA-CCPA-01"]
        m1 = a.post("/legal/v1/memos", {"request_id": a.rid(), "counsel_ref": COUNSEL, "memo_date": today,
                    "content_b64": b64(b"Counsel memo 1: MSA positions and versioned clickwrap evidence."),
                    "cites": {"clause_ids": clauses, "doc_versions": ["client_msa@1.0", "client_msa@1.1"],
                              "cq_ids": ["CQ-19"]},
                    "answers": [{"cq_id": "CQ-19", "resolution": "verified_rule"}]}, andre=True)
        m1b = m1.json()
        say(f"memo #1 -> {m1.status_code} {m1b.get('memo_id')} effects={m1b.get('effects')}")
        check("memo intake files the memo; CQ-19 verified via counsel memo",
              m1.status_code == 201 and m1b["effects"][0]["effect"] == "verified")

        def cl(cid, std, fb1=None, obligations=()):
            return {"clause_id": cid, "title": cid.lower(), "standard_text": std, "fallback_1_text": fb1,
                    "fallback_2_text": None, "walk_away": [], "rationale_code": "house_position",
                    "escalation": {"fallback_1": "agent", "fallback_2": "counsel", "unmatched": "counsel"},
                    "obligations": list(obligations)}
        pb = a.post("/legal/v1/playbooks/proposals", {"request_id": a.rid(), "counsel_memo_id": m1b["memo_id"],
                    "playbook": {"playbook_id": "pb_client_msa", "doc_type": "client_msa", "version": "1.0", "clauses": [
                        cl("MSA-RENEW-01", "Renewal needs written notice 30 days before the end date.",
                           obligations=[{"code": "renewal_notice", "party": "zbm", "owner": "andre",
                                         "due_rule": "offset(end_date,-30d)", "lead_days": 14}]),
                        cl("MSA-PAY-01", "Invoices are payable within 10 business days.",
                           "Invoices are payable within 15 business days.",
                           obligations=[{"code": "payment_terms", "party": "counterparty", "owner": "finance_31",
                                         "due_rule": "offset(accepted_at,+10bd)", "lead_days": 3}]),
                        cl("MSA-CCPA-01", "ZBM acts as a service provider under the attached CCPA terms.")]}},
                    andre=True).json()["proposal"]
        r = a.post("/legal/v1/playbooks/decisions", {"request_id": a.rid(), "proposal_id": pb["proposal_id"],
                                                     "content_sha256": pb["content_sha256"], "decision": "approve"},
                   andre=True)
        check("client_msa playbook published by Andre with the memo", r.json().get("status") == "approved")

        # --- document draft -> counsel sign-off -> Andre approval -------------------------------------------------
        tpl = "MASTER SERVICES AGREEMENT between Z Best Media and {{client_name}}; term ends {{end_date}}."
        r = a.post("/legal/v1/documents/client_msa/versions", {"request_id": a.rid(), "version": "1.0", "entity": "zbm",
                   "text": tpl}, andre=True)
        check("a caller-supplied version number -> 422 (Legal assigns it, AEGIS N17-5)", r.status_code == 422)
        r = a.post("/legal/v1/documents/client_msa/versions", {"request_id": a.rid(), "entity": "zbm", "text": "x"},
                   caller="scheduler")
        check("an upload by anyone but Andre -> 403 (AEGIS N17-5)", r.status_code == 403)
        d = a.post("/legal/v1/documents/client_msa/versions", {"request_id": a.rid(), "entity": "zbm",
                   "text": tpl, "template_variables": {"client_name": {"type": "string", "max_length": 120},
                                                      "end_date": {"type": "date"}},
                   "clause_ids": [{"clause_id": c, "position": "standard"} for c in clauses]}, andre=True)
        db = d.json()
        say(f"document draft client_msa 1.0 -> {d.status_code} sha256={db['sha256'][:16]}... status={db['status']}")
        r = a.post("/legal/v1/documents/client_msa/versions/1.0/decision",
                   {"request_id": a.rid(), "decision": "approve", "version_sha256": db["sha256"]}, andre=True)
        check("approval before a counsel sign-off record -> 409 COUNSEL_RECORD_MISSING",
              r.status_code == 409 and r.json()["detail"] == "COUNSEL_RECORD_MISSING")
        r = a.post("/legal/v1/documents/client_msa/versions/1.0/counsel-signoff",
                   {"request_id": a.rid(), "counsel_ref": COUNSEL, "signed_on": today, "doc_sha256": db["sha256"],
                    "memo_id": m1b["memo_id"], "memo_sha256": m1b["memo_sha256"]}, andre=True)
        check("counsel sign-off record (memo id + doc hash) recorded", r.status_code == 200)
        r = a.post("/legal/v1/documents/client_msa/versions/1.0/decision",
                   {"request_id": a.rid(), "decision": "approve", "version_sha256": db["sha256"]}, andre=True)
        check("Andre approves client_msa 1.0", r.json().get("status") == "approved")
        f = a.post("/legal/v1/documents/client_msa/versions", {"request_id": a.rid(), "party_ref": "client:acme", "entity": "zbm",
                   "variables": {"client_name": "Acme Outdoor Co", "end_date": "2027-09-30"}}, caller="scheduler").json()
        check("the scheduler's fill is numbered 1.1 by Legal and bound to client:acme", f.get("version") == "1.1")
        say(f"scheduler template fill client_msa 1.1 -> sha256={f['sha256'][:16]}... review_label={f['review_label']}")
        a.post("/legal/v1/documents/client_msa/versions/1.1/counsel-signoff",
               {"request_id": a.rid(), "counsel_ref": COUNSEL, "signed_on": today, "doc_sha256": f["sha256"],
                "memo_id": m1b["memo_id"], "memo_sha256": m1b["memo_sha256"]}, andre=True)
        r = a.post("/legal/v1/documents/client_msa/versions/1.1/decision",
                   {"request_id": a.rid(), "decision": "approve", "version_sha256": f["sha256"]}, andre=True)
        cur = a.get("/legal/v1/documents/client_msa/current", caller="compliance_38").json()
        say(f"current_version(client_msa) -> {cur['current_version']} sha={str(cur['doc_sha256'])[:16]}... "
            f"review_by={cur['review_by']}")
        check("the filled 1.1 is current with its own hash", cur["current_version"] == "1.1" and cur["doc_sha256"] == f["sha256"])
        bad = a.post("/legal/v1/documents/client_msa/versions", {"request_id": a.rid(), "party_ref": "client:acme", "entity": "zbm",
                     "variables": {"client_name": "You must sign this today", "end_date": "2027-09-30"}}, caller="scheduler")
        check("a template variable that reads as advice -> 422 ADVICE_TEXT_BLOCKED",
              bad.status_code == 422 and bad.json()["detail"] == "ADVICE_TEXT_BLOCKED")

        # --- acceptance record -> obligations ---------------------------------------------------------------------
        acc = a.post("/legal/v1/acceptances", {"request_id": a.rid(), "party_ref": "client:acme",
                     "signer_identity_ref": "onb-login-acme-1", "doc_id": "client_msa", "version": "1.1",
                     "doc_sha256": f["sha256"], "presented_sha256": f["sha256"], "method": "clickwrap_unticked_box",
                     "presentation": "scroll_to_accept", "affirmative_act": True}, caller="onboarding")
        ab = acc.json()
        say(f"acceptance -> {acc.status_code} {ab.get('acceptance_id')} evidence_sufficient={ab.get('evidence_sufficient')} "
            f"obligations={len(ab.get('obligation_ids', []))}")
        check("acceptance record: doc id + version + sha256 + timestamp + method, evidence sufficient (CQ-19 memo)",
              acc.status_code == 201 and ab["evidence_sufficient"] is True and ab["doc_sha256"] == f["sha256"])
        r = a.post("/legal/v1/acceptances", {"request_id": a.rid(), "party_ref": "client:acme",
                   "signer_identity_ref": "x", "doc_id": "client_msa", "version": "1.1", "doc_sha256": f["sha256"],
                   "presented_sha256": f["sha256"], "method": "clickwrap_unticked_box", "presentation": "link",
                   "affirmative_act": True, "ip": "203.0.113.5"}, caller="onboarding")
        check("an acceptance carrying an IP address -> 422", r.status_code == 422)
        r = a.post("/legal/v1/acceptances", {"request_id": a.rid(), "party_ref": "client:acme",
                   "signer_identity_ref": "client:192.168.1.1", "doc_id": "client_msa", "version": "1.1",
                   "doc_sha256": f["sha256"], "presented_sha256": f["sha256"], "method": "clickwrap_unticked_box",
                   "presentation": "link", "affirmative_act": True}, caller="onboarding")
        check("an IP address inside a ref value -> 422 (AEGIS N17-6)", r.status_code == 422)
        r = a.post("/legal/v1/acceptances", {"request_id": a.rid(), "party_ref": "client:globex",
                   "signer_identity_ref": "globex-1", "doc_id": "client_msa", "version": "1.1",
                   "doc_sha256": f["sha256"], "presented_sha256": f["sha256"], "method": "clickwrap_unticked_box",
                   "presentation": "link", "affirmative_act": True}, caller="onboarding")
        check("another client accepting acme's instance -> 409 INSTANCE_NOT_BOUND (AEGIS N17-4)",
              r.status_code == 409 and r.json()["detail"] == "INSTANCE_NOT_BOUND")
        r = a.post("/legal/v1/acceptances", {"request_id": a.rid(), "party_ref": "client:initech",
                   "signer_identity_ref": "initech-1", "doc_id": "client_msa", "version": "1.0",
                   "doc_sha256": db["sha256"], "presented_sha256": db["sha256"], "method": "clickwrap_unticked_box",
                   "presentation": "link", "affirmative_act": True}, caller="onboarding")
        check("accepting the unfilled template -> 409 TEMPLATE_NOT_ACCEPTABLE (AEGIS N17-4)",
              r.status_code == 409 and r.json()["detail"] == "TEMPLATE_NOT_ACCEPTABLE")
        obs = a.get("/legal/v1/obligations", party_ref="client:acme").json()["items"]
        say("obligations: " + "; ".join(f"{o['obligation_code']} due {o['due']} owner {o['owner_department']}" for o in obs))
        check("obligations extracted and bound (renewal 2027-08-31; payment +10 business days)",
              {o["obligation_code"] for o in obs} == {"renewal_notice", "payment_terms"}
              and any(o["due"] == "2027-08-31" for o in obs))
        r = a.put("/legal/v1/contracts/acme/terms", {"request_id": a.rid(), "terms": {
            "client_id": "acme", "signed": True, "signed_at": None, "start_date": today, "end_date": "2027-09-30",
            "services": ["revenue_recovery"], "allowed_commitment_categories": ["callback", "report", "audit"],
            "monthly_spend_cap_usd": "2500.00", "ccpa_cpra_clause_present": False},
            "executed": {"doc_id": "client_msa", "version": "1.1", "doc_sha256": f["sha256"],
                         "acceptance_id": ab["acceptance_id"]}}, caller="onboarding")
        got = a.get("/legal/v1/contracts/acme/terms", caller="onboarding").json()
        check("onboarding contract terms stored; signed derived from the evidence",
              r.status_code == 200 and got["signed"] is True and got["ccpa_cpra_clause_present"] is False)

        # --- memo intake -> Compliance proposal (stub) ---------------------------------------------------------
        m2 = a.post("/legal/v1/memos", {"request_id": a.rid(), "counsel_ref": COUNSEL, "memo_date": today,
                    "content_b64": b64(b"Counsel memo 2: creator agreement venue and minimum-live forfeiture."),
                    "cites": {"cq_ids": ["CQ-11"]},
                    "answers": [{"cq_id": "CQ-11", "resolution": "verified_rule",
                                 "quoted_excerpt": "Counsel answers CQ-11 for the current clipper agreement."}]},
                    andre=True).json()
        check("memo #2 filed; no Compliance call until the row is proposed (step 1 of 2, AEGIS N17-8)",
              m2["proposals"] == [] and len(STUB["proposals"]) == 0)
        mp = a.post(f"/legal/v1/memos/{m2['memo_id']}/compliance-proposals", {"request_id": a.rid(), "proposals": [
                    {"kind": "supersede", "target_id": "CQ-11",
                     "proposed_row": {"id": "CQ-11-MEMO-1", "status": "verified",
                                      "source_url": f"urn:legal37:memos:{m2['memo_id']}"},
                     "quoted_excerpt": "Counsel answers CQ-11 for the current clipper agreement."}]}, andre=True).json()
        say(f"memo #2 -> {m2['memo_id']} proposals={mp['proposals']}")
        check("step 2 produced exactly one Compliance proposal, delivered through the thin client",
              len(mp["proposals"]) == 1 and mp["proposals"][0]["status"] == "delivered" and len(STUB["proposals"]) == 1)
        _, sent = STUB["proposals"][0]
        ev = sent["evidence"]
        say(f"stub received: kind={sent['kind']} target={sent['target_id']} source_url={ev['source_url']} "
            f"doc_number={ev['doc_number']}")
        check("proposal carries urn:legal37:memos:<id> evidence (row and evidence) and the memo hash",
              ev["source_url"] == f"urn:legal37:memos:{m2['memo_id']}" and ev["snapshot_sha256"] == m2["memo_sha256"]
              and sent["proposed_row"]["source_url"] == ev["source_url"])
        row = a.get("/legal/v1/register/CQ-11").json()
        check("CQ-11 stays unverified until Andre approves at Compliance", row["status"] == "unverified")
        r = a.post("/legal/v1/memos", {"request_id": a.rid(), "counsel_ref": COUNSEL, "memo_date": today,
                   "content_b64": b64(b"memo 3"), "cites": {"cq_ids": ["CQ-21"]},
                   "answers": [{"cq_id": "CQ-19", "resolution": "verified_rule"}]}, andre=True)
        check("a memo answering a row it does not cite -> 409 MEMO_DOES_NOT_CITE, no Compliance call",
              r.status_code == 409 and r.json()["detail"] == "MEMO_DOES_NOT_CITE" and len(STUB["proposals"]) == 1)

        # --- takedown notice -> counter-notice window -> hold ------------------------------------------------
        post_sha = hashlib.sha256(b"https://www.tiktok.com/@c/video/123").hexdigest()
        els = {k: True for k in ("signature", "work_identified", "material_located", "contact", "good_faith_statement",
                                 "perjury_statement")}
        n = a.post("/legal/v1/takedowns", {"request_id": a.rid(), "target": {"kind": "platform_post",
                   "post_ref_sha256": post_sha, "platform": "tiktok"}, "elements": els}, caller="hub").json()
        say(f"takedown notice -> {n['notice_id']} valid={n['valid']} matter={n['matter_id']} holds={n['hold_ids']}")
        cnt = a.get("/legal/v1/takedowns/count", caller="verification_integrity", post_ref_sha256=post_sha).json()
        check("V&I count answers 1 open valid notice", cnt["available"] is True and cnt["notices"] == 1)
        cn = a.post(f"/legal/v1/takedowns/{n['notice_id']}/counter-notice", {"request_id": a.rid()}, caller="hub").json()
        say(f"counter-notice received {cn['received_date']} -> restore window {cn['restore_not_before']} .. "
            f"{cn['restore_not_after']}")
        check("restore window set (10th and 14th business days)", cn["status"] == "counter_noticed")
        r = a.post(f"/legal/v1/takedowns/{n['notice_id']}/restore", {"request_id": a.rid()}, caller="hub")
        check("restore before the 10th business day -> 409 RESTORE_WINDOW_NOT_OPEN",
              r.status_code == 409 and r.json()["detail"] == "RESTORE_WINDOW_NOT_OPEN")
        m = a.post("/legal/v1/requests", {"request_id": a.rid(), "channel": "email", "requester_ref": "label-counsel-1",
                                          "kind": "demand_letter", "subject_refs": ["clipper:cn-clp-X"],
                                          "custodians": ["andre"]}, caller="hub").json()
        say(f"demand letter -> {m['severity']} {m['route']} hold={m['hold_ids']}")
        hc = a.get("/legal/v1/holds/check", subject_ref="clipper:cn-clp-X").json()
        check("hold issued at once; holds/check -> held", m["route"] == "counsel_same_day" and hc["held"] is True)
        r = a.post(f"/legal/v1/holds/{m['hold_ids'][0]}/release", {"request_id": a.rid()}, andre=True)
        check("hold release without a memo -> 409", r.status_code == 409)
        mus = a.post("/legal/v1/music/rulings", {"request_id": a.rid(), "subject_kind": "zbc_clip", "subject_id": "clip-1",
                     "platform": "tiktok", "paid": True, "music": {"present": True, "source": "commercial_library",
                                                                   "track_or_license_id": "tt-123"},
                     "reposted_or_reedited_by_zbc": False, "music_changed_since_approval": False},
                     caller="creative_production").json()
        check("music-bearing paid clip blocked pending CQ-21", mus["allowed"] is False
              and mus["reasons"][0]["cq_id"] == "CQ-21")
        integ = a.get("/legal/v1/integrity").json()
        check("integrity green before restart", integ["status"] == "green")
        say(f"local log lines: {integ['log_lines']}")

        # --- restart with the anchor check ------------------------------------------------------------------
        stop(lp, "legal")
        shutil.copytree(work / "legal", work / "tampered")
        tl = work / "tampered" / "legal_log.jsonl"
        lines = tl.read_bytes().splitlines()
        lines[5] = lines[5].replace(b'"anchored":true', b'"anchored":true ', 1)
        tl.write_bytes(b"\n".join(lines) + b"\n")
        lp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "legal_restart", work)
        hl2 = wait_health(LG + "/health")
        say(f"legal-py after restart: {hl2}")
        integ2 = a.get("/legal/v1/integrity").json()
        check("restart: anchors verified at start, integrity green", integ2["status"] == "green"
              and integ2["log_lines"] >= integ["log_lines"])
        cur2 = a.get("/legal/v1/documents/client_msa/current", caller="clipper_network").json()
        hc2 = a.get("/legal/v1/holds/check", subject_ref="clipper:cn-clp-X").json()
        cnt2 = a.get("/legal/v1/takedowns/count", caller="verification_integrity", post_ref_sha256=post_sha).json()
        check("restart: current version, hold and takedown count replayed",
              cur2["current_version"] == "1.1" and hc2["held"] is True and cnt2["notices"] == 1 and hl2["rules_version"] == 1)
        r = a.post("/legal/v1/requests", {"request_id": a.rid(), "channel": "portal", "requester_ref": "clipper-7",
                                          "kind": "question"}, caller="hub")
        check("a write after restart is recorded", r.status_code == 201 and r.json()["route"] == "template_lane")
        tp = subprocess.run([sys.executable, "-m", "api"], env={**os.environ, **env, "LEGAL_DATA_DIR": str(work / "tampered"),
                                                                 "LEGAL_PORT": str(pcopy)},
                            cwd=str(SVC / "src"), capture_output=True, timeout=60)
        say(f"tampered copy start -> exit {tp.returncode}: {tp.stderr.decode()[-200:].strip()}")
        check("a tampered log copy refuses to start",
              tp.returncode != 0 and (b"RuntimeError" in tp.stderr or b"StoreCorrupt" in tp.stderr))

        # --- the ledger ---------------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        lg = [x for x in ents if x.get("department") == "legal"]
        types = sorted({x["event_type"] for x in lg})
        say(f"ledger: {len(ents)} entries, {len(lg)} from legal; {len(types)} event types")
        say("  event types: " + ", ".join(types))
        blob = json.dumps(ents)
        check("no document or memo text on the ledger",
              "MASTER SERVICES" not in blob and "Counsel memo 1:" not in blob and "Acme Outdoor" not in blob
              and "Counsel answers CQ-11" not in blob)
        v = httpx.get(L + "/ledger/verify", headers=lh, timeout=120)
        say(f"GET /ledger/verify -> {v.status_code} {v.text}")
        check("ledger verifies valid", v.status_code == 200 and v.json().get("valid") is True)
        check("Compliance stub saw no bad credentials", STUB["bad_auth"] == 0)
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
        stub.shutdown()
        (work / "live_run.txt").write_text("\n".join(LOG) + "\n")


def main() -> int:
    work, keep = make_work_dir("legal-live-")
    try:
        return _main(work)
    finally:
        finish_work_dir(work, keep)


if __name__ == "__main__":
    sys.exit(main())
