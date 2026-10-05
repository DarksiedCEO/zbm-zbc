"""
Live HTTP run for finance-py: real ledger-rust binaries, real processes, real sockets, real tokens.

Leg A (production entrypoint, day one): ledger A + finance-py via ``cd src && python3 -m api`` with a data directory.
Every dependency is its fail-closed stand-in: Andre approves the Finance rules, and still no payable accrues, no payee
activates, reconciliation cannot match and no payout run proposes anything (the day-one effect, spec §G).

Leg B (devtools/live_server.py: the test fakes + a settable clock): ledger B + finance-py walking the money:
prepayment (deposit invoice issued by Andre, bank statement line -> F1) -> certification (fake V&I) -> Creative's
handoff -> payable (F2/F3) -> reconciliation green -> weekly run, batch proposed -> Andre approves -> rail funding
approved by Andre (F4a) -> release after the 12 h delay (fake rail, F4d) -> rail webhook paid (F4e, batch settled) ->
V&I clawback after payment (F5a receivable) -> next week's earnings net the receivable (F4b) and pay the rest ->
reconciliation green -> RESTART on the same data directory and ledger (anchors verified at start) ->
GET /ledger/verify valid on both ledgers.

usage: LEDGER_BIN=/path/to/ledger-rust/server python3 devtools/live_run.py [--ports 19450,19451,19452,19453]
Kills only the PIDs it started. Exit 0 only when both ledgers verify valid and every checked expectation held.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]
PORTS = [int(p) for p in (sys.argv[sys.argv.index("--ports") + 1].split(",") if "--ports" in sys.argv else
                          ["19450", "19451", "19452", "19453"])]
LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
FIN_TOKEN = "live-fin-service-token-" + "s" * 16
ANDRE = "live-fin-andre-approval-token-" + "a" * 12
CALLERS = {n: f"live-fin-caller-{n}-" + "c" * 24 for n in ("compliance_38", "clipper_network", "verification_integrity",
                                                             "creative_production", "onboarding", "legal_37",
                                                             "scheduler", "rail_gateway", "bank_feed")}
LOG: list[str] = []
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool]] = []
N = iter(range(1, 10**6))


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


# Fix wave 26b (scout C6-2, C5-7): the run's work dir holds the ledger and service logs (the run's evidence). It was
# made with mkdtemp and never removed, so every local run left one behind. Now LIVE_WORK_DIR=<dir> (the name every
# live_run.py reads) KEEPS the run's dir inside <dir> and the end of the run prints where it is — CI's live-runs job
# asks for $RUNNER_TEMP/live-work, then lists and removes it; unset, the dir is made in the temp dir and removed when
# the run ends, passed or failed (the run's narration is on stdout either way).
def make_work_dir(prefix: str) -> tuple[Path, bool]:
    keep_in = os.environ.get("LIVE_WORK_DIR") or None
    old = os.environ.get("FIN_LIVE_WORKDIR") or None       # this script's name for it before wave 26b (C5-7)
    if old and keep_in is None:
        print("live_run: FIN_LIVE_WORKDIR is deprecated (fix wave 26b, C5-7): use LIVE_WORK_DIR, the name every "
              "live_run.py reads; it is honoured for now", file=sys.stderr, flush=True)
        keep_in = old
    elif old and old != keep_in:
        print(f"live_run: FIN_LIVE_WORKDIR is ignored: LIVE_WORK_DIR={keep_in} is set (fix wave 26b, C5-7)",
              file=sys.stderr, flush=True)
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


def rid(p="live") -> str:
    return f"{p}-{next(N)}"


def start(cmd, env, cwd, name, logdir) -> subprocess.Popen:
    out = open(Path(logdir) / f"{name}.log", "ab")
    p = subprocess.Popen(cmd, env={**os.environ, **env}, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
    PROCS.append(p)
    say(f"started {name} pid={p.pid}")
    return p


def stop(p: subprocess.Popen, name: str) -> None:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
    say(f"stopped {name} pid={p.pid} rc={p.returncode}")


def wait_up(url: str, secs: float = 30) -> None:
    t = time.time() + secs
    while time.time() < t:
        try:
            if httpx.get(url, timeout=1).status_code in (200, 401):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


class Fin:
    def __init__(self, base: str):
        self.base = base
        self.c = httpx.Client(base_url=base, timeout=30)

    def h(self, caller=None, andre=False):
        hd = {"Authorization": f"Bearer {FIN_TOKEN}"}
        if caller:
            hd["X-FIN-Caller-Token"] = CALLERS[caller]
        if andre:
            hd["X-Andre-Approval-Token"] = ANDRE
        return hd

    def post(self, path, body, caller=None, andre=False, ok=(200, 201)):
        r = self.c.post(path, json=body, headers=self.h(caller, andre))
        if ok and r.status_code not in ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:600]}")
        return r

    def put(self, path, body):
        r = self.c.put(path, json=body, headers=self.h(andre=True))
        if r.status_code != 200:
            raise RuntimeError(f"PUT {path} -> {r.status_code}: {r.text[:600]}")
        return r

    def get(self, path, caller="scheduler", andre=False, **params):
        r = self.c.get(path, headers=self.h(caller, andre), params=params)
        if r.status_code != 200:
            raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:600]}")
        return r.json()

    def dev(self, path, body):
        r = self.c.post(path, json=body, headers=self.h())
        if r.status_code != 200:
            raise RuntimeError(f"devtools {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # --- steps
    def approve_rules(self):
        seed = [p for p in self.get("/fin/v1/rules")["open_proposals"] if p["kind"] == "seed"][0]
        return self.post("/fin/v1/rules/decisions", {"request_id": rid(), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]},
            andre=True).json()

    def verify_counsel(self, cq):
        row = {k: v for k, v in {r["rule_id"]: r for r in self.get("/fin/v1/rules")["rules"]}[cq].items()
               if k != "in_force"}
        row.update(status="verified", parameters={**row["parameters"], "memo_ref": f"memo-{cq}-live"})
        p = self.post("/fin/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": cq,
                                                  "proposed_row": row}, andre=True).json()["proposal"]
        self.post("/fin/v1/rules/decisions", {"request_id": rid(), "decisions": [
            {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
             "acknowledge_weakening": True}]}, andre=True)

    def recon(self):
        return self.post("/fin/v1/reconciliations/run", {"request_id": rid()}, caller="scheduler").json()


def ledger_verify(base: str) -> dict:
    r = httpx.get(f"{base}/ledger/verify", headers={"Authorization": f"Bearer {LEDGER_TOKEN}"}, timeout=60)
    return r.json() if r.status_code == 200 else {"status": r.status_code}


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pla, pfa, plb, pfb = PORTS
    say(f"work dir {work}; ports {PORTS}")
    LA, LB = f"http://127.0.0.1:{pla}", f"http://127.0.0.1:{plb}"
    common = {"FIN_SERVICE_TOKEN": FIN_TOKEN, "FIN_ANDRE_APPROVAL_TOKEN": ANDRE, "FIN_CALLER_TOKENS": json.dumps(CALLERS),
              "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "PYTHONDONTWRITEBYTECODE": "1"}
    for k in list(os.environ):
        if k.startswith("FIN_"):
            os.environ.pop(k)
    try:
        # ------------------------------------------------------------------ leg A: production entrypoint, day one
        for d in ("la", "lb", "fa", "fb"):
            (work / d).mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pla),
                             "LEDGER_LOG_PATH": str(work / "la" / "ledger.jsonl")}, str(work / "la"), "ledger_a", work)
        wait_up(f"{LA}/health")
        fa = start([sys.executable, "-m", "api"], {**common, "LEDGER_SERVICE_URL": LA, "FIN_PORT": str(pfa),
                                                   "FIN_DATA_DIR": str(work / "fa")}, str(SVC / "src"), "finance_a", work)
        wait_up(f"http://127.0.0.1:{pfa}/health")
        A = Fin(f"http://127.0.0.1:{pfa}")
        say("LEG A — production entrypoint, every dependency a fail-closed stand-in")
        A.approve_rules()
        check("A: rules version 1 in force", A.get("/fin/v1/rules")["rules_version"] == 1)
        h = A.post("/fin/v1/payout-handoffs", {"request_id": rid(), "submission_id": "sub-a", "facts": {
            "submission_id": "sub-a", "eligible": True, "blockers": [], "clip_review_outcome": "pass",
            "verification": {"verified": True}, "compliance": {"allowed": True, "reference": "r-a"}}},
            caller="creative_production").json()
        check("A: a Creative handoff claiming verified=true accrues nothing (V&I stand-in)",
              not h["allowed"] and "DEPENDENCY_UNAVAILABLE:verification_integrity" in h["reason"])
        p = A.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-a", "kind": "clipper",
                                      "declared_country": "US"}, caller="onboarding").json()
        check("A: payee activation refused (rail stand-in)", not p["allowed"])
        r = A.recon()
        check("A: reconciliation cannot match (bank feed stand-in)", not r["fc01"])
        run = A.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler", ok=None)
        check("A: payout run refused (409)", run.status_code == 409)
        br = A.get("/fin/v1/clients/client-1/billing-readiness", caller="onboarding")
        check("A: billing readiness not allowed (FIN-CQ-11 unverified, Legal stand-in)", not br["allowed"])
        check("A: ledger A verifies valid", ledger_verify(LA).get("valid") is True)
        stop(fa, "finance_a")

        # ------------------------------------------------------------------ leg B: the money path with fakes
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(plb),
                             "LEDGER_LOG_PATH": str(work / "lb" / "ledger.jsonl")}, str(work / "lb"), "ledger_b", work)
        wait_up(f"{LB}/health")
        keydir = work / "stripe-keys"
        keydir.mkdir(mode=0o700)
        for name, val in (("sk", "sk_test_51LiveRunSimulatedKey0000000000"), ("wh", "whsec_LiveRunSimulatedSecret00000")):
            (keydir / name).write_text(val)
            os.chmod(keydir / name, 0o600)
        envb = {**common, "LEDGER_SERVICE_URL": LB, "FIN_PORT": str(pfb), "FIN_DATA_DIR": str(work / "fb"),
                "FIN_DEVTOOLS_STATE_FILE": str(work / "fb_world.pkl"),
                # Stripe incoming against the simulated Stripe (ADR 0009 amendment, Oct 5 2026)
                "FIN_STRIPE_INCOMING": "1", "FIN_STRIPE_SECRET_KEY_FILE": str(keydir / "sk"),
                "FIN_STRIPE_WEBHOOK_SECRET_FILE": str(keydir / "wh"),
                "FIN_STRIPE_SUCCESS_URL": "https://zbestmedia.com/pay/thanks",
                "FIN_STRIPE_CANCEL_URL": "https://zbestmedia.com/pay/cancelled", "FIN_CARD_PREPAYMENTS": "1"}
        fb = start([sys.executable, str(SVC / "devtools" / "live_server.py")], envb, str(SVC), "finance_b", work)
        wait_up(f"http://127.0.0.1:{pfb}/health")
        B = Fin(f"http://127.0.0.1:{pfb}")
        say("LEG B — fakes + settable clock; real ledger, real data dir, real launcher")
        B.approve_rules()
        for cq in ("FIN-CQ-01", "FIN-CQ-11"):
            B.verify_counsel(cq)
        B.post("/fin/v1/controls/FC-05/results", {"request_id": rid(), "result": "pass", "evidence_ref": "roster-live"},
               andre=True)
        rc = B.post("/fin/v1/rate-cards/proposals", {"request_id": rid(), "campaign_id": "camp-1",
                                                     "creator_rate_per_1000": {t: "2.35" for t in ("T0", "T1", "T2", "T3")},
                                                     "max_paid_views_per_clip": 1_000_000,
                                                     "effective_at": "2026-09-01T00:00:00Z"}, andre=True).json()["proposal"]
        B.post("/fin/v1/rate-cards/decisions", {"request_id": rid(), "decisions": [
            {"proposal_id": rc["proposal_id"], "content_sha256": rc["content_sha256"], "decision": "approve",
             "acknowledge_weakening": True}]}, andre=True)
        of = {"doc_id": "of-1", "version": 1, "doc_sha256": "a" * 64, "acceptance_id": "acc-1"}
        B.put("/fin/v1/campaigns/camp-1/commercial-profile", {"request_id": rid(), "client_id": "client-1",
                                                               "order_form": of, "budget": "1000.00",
                                                               "client_rate_per_1000": "4.00",
                                                               "rate_card_doc_id": rc["doc_id"]})
        inv = B.post("/fin/v1/invoices", {"request_id": rid(), "entity": "zbc", "client_id": "client-1",
                                          "campaign_id": "camp-1", "kind": "campaign_deposit",
                                          "lines": [{"line_code": "campaign_deposit", "quantity": 1,
                                                     "unit_price": "1000.00"}], "payment_methods": ["ach", "wire"],
                                          "legal_ref": of}, caller="scheduler").json()["invoice"]
        inv = B.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision", {"request_id": rid(), "content_sha256":
                                                                        inv["content_sha256"], "decision": "approve"},
                     andre=True).json()["invoice"]
        check("B: deposit invoice issued by Andre", inv["status"] == "issued")
        B.dev("/devtools/bank/deposit", {"entity": "zbc", "account": "1020", "amount": "1000.00"})
        rc_ = B.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [
            {"txn_ref_sha256": hashlib.sha256(b"live-txn-1").hexdigest(), "entity": "zbc", "account": "1020",
             "direction": "credit", "amount": "1000.00", "value_date": time.strftime("%Y-%m-%d"),
             "reference_token": inv["invoice_id"]}]}, caller="bank_feed").json()
        check("B: prepayment matched into ZBC Client Campaign Deposits (F1)", rc_["results"][0]["status"] == "matched")
        pay = B.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-a", "kind": "clipper",
                                        "declared_country": "US", "callback_contact_ref": "vault:contact-clip-a"},
                     caller="clipper_network").json()
        check("B: payee activated at the (fake) rail", pay["allowed"])
        B.dev("/devtools/vi/certify", {"submission_id": "sub-1", "clipper_id": "clip-a", "campaign_id": "camp-1",
                                       "views": 12345, "create_time": "2026-09-20T12:00:00Z"})
        B.dev("/devtools/compliance/rule", {"ruling_id": "cmp-rul-sub-1", "subject_id": "sub-1"})
        facts = {"submission_id": "sub-1", "eligible": True, "blockers": [], "clip_review_outcome": "pass",
                 "verification": {"verified": True}, "compliance": {"allowed": True, "reference": "cmp-rul-sub-1"}}
        hand = B.post("/fin/v1/payout-handoffs", {"request_id": rid(), "submission_id": "sub-1", "facts": facts},
                      caller="creative_production").json()
        payable = B.get(f"/fin/v1/payables/{hand['reference']}")
        check("B: payable 29.01 (12,345 views x 2.35 / 1000, one HALF_UP), revenue 49.38",
              hand["allowed"] and payable["amount"] == "29.01" and payable["revenue_amount"] == "49.38")
        r = B.recon()
        check("B: reconciliation green before the run", r["fc01"])
        batch = B.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler").json()["batch"]
        check("B: batch proposed (maker), net 29.01", batch["status"] == "proposed" and batch["totals"]["net"] == "29.01")
        rel_early = B.post(f"/fin/v1/payout-batches/{batch['batch_id']}/release", {"request_id": rid()},
                           caller="scheduler", ok=None)
        check("B: release before Andre approves -> 409 NOT_APPROVED", rel_early.status_code == 409)
        dec = B.post(f"/fin/v1/payout-batches/{batch['batch_id']}/decision",
                     {"request_id": rid(), "content_sha256": batch["content_sha256"], "decision": "approve"},
                     andre=True).json()
        check("B: Andre approved the batch content hash", dec["status"] == "approved")
        fop = B.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": batch["batch_id"]},
                     caller="scheduler").json()["operation"]
        fund = B.post(f"/fin/v1/treasury/funding/{fop['op_id']}/decision",
                      {"request_id": rid(), "content_sha256": fop["content_sha256"], "decision": "approve"},
                      andre=True).json()
        check("B: rail funding approved by Andre and posted (F4a)", fund["operation"]["status"] == "done")
        andre_rel = B.post(f"/fin/v1/payout-batches/{batch['batch_id']}/release", {"request_id": rid()},
                           caller="scheduler", andre=True, ok=None)
        check("B: Andre's token on the release route -> 403 (approver cannot release)", andre_rel.status_code == 403)
        B.dev("/devtools/advance", {"hours": 13})
        B.recon()
        rel = B.post(f"/fin/v1/payout-batches/{batch['batch_id']}/release", {"request_id": rid()},
                     caller="scheduler").json()
        item = rel["batch"]["items"][0]
        check("B: item submitted to the (fake) rail with its idempotency key (F4d)", item["status"] == "submitted")
        again = B.post(f"/fin/v1/payout-batches/{batch['batch_id']}/release", {"request_id": rid()},
                       caller="scheduler", ok=None)
        check("B: a second release call submits nothing (409)", again.status_code == 409)
        B.dev("/devtools/rail/paid", {"rail": "stripe", "idempotency_key": item["idempotency_key"]})
        ev = B.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [
            {"event_id": "evt-live-1", "type": "paid", "item_id": item["item_id"], "signature": "sig-ok-test-only"}]},
            caller="rail_gateway").json()
        st = B.get(f"/fin/v1/payout-batches/{batch['batch_id']}")
        check("B: rail webhook paid -> F4e, batch settled", ev["results"][0]["status"] == "paid" and st["status"] == "settled")
        B.dev("/devtools/vi/clawback", {"submission_id": "sub-1", "views_delta": -2345})
        B.dev("/devtools/advance", {"days": 1})
        cs = B.post("/fin/v1/jobs/clawback-sync/run", {"request_id": rid()}, caller="scheduler").json()
        payee = B.get("/fin/v1/payees/clip-a")
        check("B: V&I clawback after payment -> F5a receivable 5.51 (netting, never a pull)",
              cs["summary"]["applied"] == 1 and payee["clawback_open"] == "5.51")
        B.dev("/devtools/advance", {"days": 6})
        B.dev("/devtools/vi/certify", {"submission_id": "sub-2", "clipper_id": "clip-a", "campaign_id": "camp-1",
                                       "views": 10000, "create_time": "2026-09-27T12:00:00Z"})
        B.dev("/devtools/compliance/rule", {"ruling_id": "cmp-rul-sub-2", "subject_id": "sub-2"})
        B.post("/fin/v1/payout-handoffs", {"request_id": rid(), "submission_id": "sub-2", "facts": {
            **facts, "submission_id": "sub-2", "compliance": {"allowed": True, "reference": "cmp-rul-sub-2"}}},
            caller="creative_production")
        r = B.recon()
        check("B: reconciliation green in week 2", r["fc01"])
        b2 = B.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler").json()["batch"]
        it2 = b2["items"][0]
        check("B: week-2 item nets the clawback: gross 23.50, netted 5.51, net 17.99",
              (it2["gross"], it2["netted"], it2["net"]) == ("23.50", "5.51", "17.99"))
        B.post(f"/fin/v1/payout-batches/{b2['batch_id']}/decision",
               {"request_id": rid(), "content_sha256": b2["content_sha256"], "decision": "approve"}, andre=True)
        fop = B.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": b2["batch_id"]},
                     caller="scheduler").json()["operation"]
        B.post(f"/fin/v1/treasury/funding/{fop['op_id']}/decision",
               {"request_id": rid(), "content_sha256": fop["content_sha256"], "decision": "approve"}, andre=True)
        B.dev("/devtools/advance", {"hours": 13})
        B.recon()
        rel2 = B.post(f"/fin/v1/payout-batches/{b2['batch_id']}/release", {"request_id": rid()},
                      caller="scheduler").json()
        it2 = rel2["batch"]["items"][0]
        B.dev("/devtools/rail/paid", {"rail": "stripe", "idempotency_key": it2["idempotency_key"]})
        B.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [
            {"event_id": "evt-live-2", "type": "paid", "item_id": it2["item_id"], "signature": "sig-ok-test-only"}]},
            caller="rail_gateway")
        payee = B.get("/fin/v1/payees/clip-a")
        check("B: receivable netted to 0.00 at release (F4b), week-2 batch settled",
              payee["clawback_open"] == "0.00" and B.get(f"/fin/v1/payout-batches/{b2['batch_id']}")["status"] == "settled")
        r = B.recon()
        check("B: reconciliation after both payouts: every leg matched, no break",
              r["fc01"] and not [b for b in B.get("/fin/v1/breaks")["breaks"] if b["status"] == "open"])
        # ---- media billing (ADR 0009 amendment, Oct 5 2026): ZBM principal, prepaid, collect before pay
        def la_today() -> str:
            now = datetime.fromisoformat(B.get("/devtools/now")["now"].replace("Z", "+00:00"))
            return now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()

        day0 = la_today()
        flight_end = (date.fromisoformat(day0) + timedelta(days=20)).isoformat()
        io = {"doc_id": "io-1", "version": 1, "doc_sha256": "c" * 64, "acceptance_id": "acc-io-1"}
        mb = B.post("/fin/v1/media-buys", {"request_id": rid(), "client_id": "zbm-client-1", "media_type": "radio",
                                           "vendor_ref": "vendor-station-1", "description": "Drive-time radio, 3 weeks",
                                           "flight_start": day0, "flight_end": flight_end, "media_cost": "4000.00",
                                           "display": "blended", "legal_ref": io}, andre=True).json()
        check("M: media buy 4000.00 + 15% = 4600.00, cost and fee stored, client shown one line",
              mb["media_buy"]["total"] == "4600.00" and mb["media_buy"]["fee"] == "600.00"
              and len(mb["invoice"]["client_lines"]) == 1 and len(mb["invoice"]["lines"]) == 2)
        minv = B.post(f"/fin/v1/invoices/{mb['invoice']['invoice_id']}/decision",
                      {"request_id": rid(), "content_sha256": mb["invoice"]["content_sha256"], "decision": "approve"},
                      andre=True).json()["invoice"]
        check("M: media prepayment invoice issued (F12)", minv["status"] == "issued")
        B.dev("/devtools/bank/deposit", {"entity": "zbm", "account": "1010", "amount": "4600.00"})
        mr = B.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [
            {"txn_ref_sha256": hashlib.sha256(b"live-media-1").hexdigest(), "entity": "zbm", "account": "1010",
             "direction": "credit", "amount": "4600.00", "value_date": day0,
             "reference_token": minv["invoice_id"]}]}, caller="bank_feed").json()
        check("M: prepayment matched (F11a)", mr["results"][0]["status"] == "matched")
        bid = mb["media_buy"]["buy_id"]
        early = B.post(f"/fin/v1/media-buys/{bid}/vendor-payments",
                       {"request_id": rid(), "amount": "4000.00", "paid_on": day0, "method": "ach",
                        "payment_ref_sha256": hashlib.sha256(b"live-vendor-1").hexdigest()}, andre=True, ok=None)
        check("M: vendor payment the day the money landed -> 409 COLLECT_BEFORE_PAY",
              early.status_code == 409 and "COLLECT_BEFORE_PAY" in early.text)
        B.dev("/devtools/advance", {"days": 7})
        vp = B.post(f"/fin/v1/media-buys/{bid}/vendor-payments",
                    {"request_id": rid(), "amount": "4000.00", "paid_on": la_today(), "method": "ach",
                     "payment_ref_sha256": hashlib.sha256(b"live-vendor-1").hexdigest()}, andre=True).json()
        B.dev("/devtools/bank/deposit", {"entity": "zbm", "account": "1010", "amount": "-4000.00"})
        check("M: after the hold, vendor payment recorded (F12v)", vp["media_buy"]["status"] == "vendor_paid")
        B.dev("/devtools/advance", {"days": 15})
        dl = B.post(f"/fin/v1/media-buys/{bid}/delivery", {"request_id": rid(), "delivered_on": la_today(),
                                                            "evidence_refs": ["affidavit-live-1"]}, andre=True).json()
        check("M: delivery posts revenue 4600.00 and cost 4000.00 together (F12r)",
              dl["media_buy"]["status"] == "delivered")
        tbz = B.get("/fin/v1/journal/zbm/trial-balance")
        check("M: ZBM trial balance difference 0.00", tbz["difference"] == "0.00")
        r = B.recon()
        check("M: reconciliation after the media buy: every leg matched (ZBM 1010, 2120, 1150 per buy)", r["fc01"])
        # ---- Stripe incoming (ADR 0009 amendment, Oct 5 2026): checkout, signed webhooks, fees, dispute, payout
        def stripe_event(typ, obj):
            ev = B.dev("/devtools/stripe/event", {"type": typ, "object": obj})
            return B.post("/fin/v1/stripe/events", {"request_id": rid(), **ev}, caller="rail_gateway").json()

        rr = B.post("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-rr",
                                         "kind": "service", "payment_methods": ["ach", "card"], "legal_ref": io,
                                         "lines": [{"line_code": "revenue_recovery_services", "quantity": 1,
                                                    "unit_price": "2500.00"}]}, caller="onboarding").json()["invoice"]
        rr = B.post(f"/fin/v1/invoices/{rr['invoice_id']}/decision",
                    {"request_id": rid(), "content_sha256": rr["content_sha256"], "decision": "approve"},
                    andre=True).json()["invoice"]
        co = B.post(f"/fin/v1/invoices/{rr['invoice_id']}/stripe-checkout", {"request_id": rid()},
                    caller="onboarding").json()["checkout"]
        check("S: Stripe checkout for a 2500.00 Revenue Recovery invoice offers ACH and card",
              co["methods"] == ["us_bank_account", "card"] and co["url"].startswith("https://checkout.stripe.com/"))
        pi = B.dev("/devtools/stripe/pay", {"session_id": co["session_id"], "method": "card"})["payment_intent"]
        ev = stripe_event("checkout.session.completed", {"id": co["session_id"], "object": "checkout.session"})
        check("S: signed webhook verified, payment read back from Stripe and matched (F13 + F13f card fee 72.80)",
              ev["status"] == "matched"
              and B.get(f"/fin/v1/invoices/{rr['invoice_id']}")["status"] == "paid")
        dup = stripe_event("payment_intent.succeeded", {"id": pi, "object": "payment_intent"})
        check("S: the same payment announced again books nothing", dup["status"] == "payment_already_booked")
        du = B.dev("/devtools/stripe/dispute", {"payment_intent": pi})["dispute_id"]
        d1 = stripe_event("charge.dispute.created", {"id": du, "object": "dispute"})
        B.dev("/devtools/stripe/dispute", {"close": True, "dispute_id": du, "won": True, "fee_back": 1500})
        d2 = stripe_event("charge.dispute.closed", {"id": du, "object": "dispute"})
        check("S: card dispute withdrawn (F7) then won and reinstated (F7a)",
              d1["status"] == "dispute_needs_response" and d2["status"] == "dispute_won")
        po = B.dev("/devtools/stripe/payout", {"amount_cents": 242720, "status": "in_transit"})["payout_id"]
        stripe_event("payout.created", {"id": po, "object": "payout"})
        rs = B.recon()
        l3 = [x for x in rs["recon"]["legs"] if x["subject"] == "zbm:1060"]
        check("S: L3 Stripe balance vs 1060 (payout in transit counted) matched", l3 and l3[0]["status"] == "matched")
        B.dev("/devtools/stripe/payout", {"payout_id": po, "status": "paid"})
        p1 = stripe_event("payout.paid", {"id": po, "object": "payout"})
        tbz = B.get("/fin/v1/journal/zbm/trial-balance")
        check("S: payout paid to the operating account (F13p); ZBM trial balance difference 0.00",
              p1["status"] == "payout_paid" and tbz["difference"] == "0.00")
        B.dev("/devtools/bank/deposit", {"entity": "zbm", "account": "1010", "amount": "2427.20"})   # the bank shows it
        rs = B.recon()
        check("S: reconciliation after Stripe: every leg matched", rs["fc01"])
        tb_before = B.get("/fin/v1/journal/zbc/trial-balance")
        check("B: ZBC trial balance difference 0.00", tb_before["difference"] == "0.00")
        integ = B.get("/fin/v1/integrity")
        check("B: integrity green before restart (chain + anchors + /ledger/verify)", integ["status"] == "green")
        say(f"B: journal entries {integ['journal_entries']}, log lines {integ['log_lines']}")
        stop(fb, "finance_b")
        fb2 = start([sys.executable, str(SVC / "devtools" / "live_server.py")], envb, str(SVC), "finance_b_restart", work)
        wait_up(f"http://127.0.0.1:{pfb}/health")
        B2 = Fin(f"http://127.0.0.1:{pfb}")
        check("B: restart on the same data dir and ledger: started (anchors verified at start)",
              B2.get("/fin/v1/rules")["rules_version"] == 3)
        check("B: restart: trial balance identical", B2.get("/fin/v1/journal/zbc/trial-balance") == tb_before)
        check("M: restart: ZBM trial balance and the media buy survive",
              B2.get("/fin/v1/journal/zbm/trial-balance") == tbz
              and B2.get(f"/fin/v1/media-buys/{bid}")["status"] == "delivered")
        check("S: restart: the Stripe checkout and the paid invoice survive",
              B2.get(f"/fin/v1/invoices/{rr['invoice_id']}/stripe-checkout", caller="onboarding")["sessions"][0]
              ["session_id"] == co["session_id"] and B2.get(f"/fin/v1/invoices/{rr['invoice_id']}")["status"] == "paid")
        check("B: restart: integrity green", B2.get("/fin/v1/integrity")["status"] == "green")
        leases = [e for e in httpx.get(f"{LB}/ledger/entries", headers={"Authorization": f"Bearer {LEDGER_TOKEN}"},
                                       timeout=60).json() if e.get("event_type") == "instance_lease"]
        check("B: a new instance lease was recorded at restart", len(leases) >= 2)
        vb = ledger_verify(LB)
        va = ledger_verify(LA)
        say(f"ledger A verify: {va}")
        say(f"ledger B verify: {vb}")
        check("GET /ledger/verify valid on ledger A", va.get("valid") is True)
        check("GET /ledger/verify valid on ledger B", vb.get("valid") is True)
        stop(fb2, "finance_b_restart")
    except Exception as exc:  # noqa: BLE001
        say(f"ERROR: {type(exc).__name__}: {exc}")
        CHECKS.append(("run completed without error", False))
    finally:
        for p in PROCS:
            stop(p, f"pid {p.pid}")
    passed = sum(ok for _, ok in CHECKS)
    say(f"RESULT: {passed}/{len(CHECKS)} checks passed; work dir {work}")
    (work / "live_run.log").write_text("\n".join(LOG) + "\n")
    return 0 if CHECKS and passed == len(CHECKS) else 1


def main() -> int:
    work, keep = make_work_dir("finlive-")
    try:
        return _main(work)
    finally:
        finish_work_dir(work, keep)


if __name__ == "__main__":
    sys.exit(main())
