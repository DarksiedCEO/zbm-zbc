"""core-spec/1 O-4 UP / core-spec/2 V-6 (heavy lane): data directories written by ffd28ca8, cbdf5e78 and 3120f0b0
(4 own tenants, 32 IDN punycode domains, 5 IDN competitors, one client tenant bound to a fake Finance (31)
party, citation runs, one ZBM schedule) are started on the build: 0 new
5xx, 0 skipped schedule SLOTS (C2-10c, ruling 09:10 PT F10-3: every slot due on the build runs; a 404 on a read is
counted separately as ``missing``), and legacy plaintext at rest = 0 after first touch (V-6 / ADR 0051 legacy rule,
decision 58) except the releases' raw Finance party id in the append-only log (OWNER DECISION 2026-11-15 ~15:40 PT
"count and contain": counted, never shown or exported, nothing new written raw; C2-10c3): every file of the directory is scanned for the planted texts and every log line through the build's
guard (``legacy_prose_lines``), before and after the build touches the directory. The releases come from git history
(``releases.export``); each writes its directory in its own interpreter, the build then reads it here. Counts only."""
import json
import shutil
import subprocess
import sys

import pytest

from conformance import corpora as C
from conformance import releases as RL

pytestmark = [pytest.mark.conformance, pytest.mark.conformance_heavy]

WRITE = r'''
import json, random, sys, uuid
sys.path[:0] = [sys.argv[1] + "/tests", sys.argv[1] + "/src"]
import test_vault_wiring as vw
from helpers import Harness, rid
from ports import ProviderAnswer
spec = json.loads(sys.argv[3])
import pathlib
(pathlib.Path(sys.argv[2]) / "tmp").mkdir(parents=True, exist_ok=True)
h = Harness(pathlib.Path(sys.argv[2]) / "tmp", data_dir=sys.argv[2] + "/data")
out = {"ids": {}}
import svc_tenants
for t, domains in list(spec["tenants"].items()) + list(spec["client"].items()):
    domains = [d for d in domains if svc_tenants.valid_domain(d) and not svc_tenants.domain_refused(d)]
    comps = [d for d in spec["competitors"] if svc_tenants.valid_domain(d) and not svc_tenants.domain_refused(d)]
    client = t in spec["client"]
    h.tenant(t, "client" if client else "own", tuple(domains))   # C2-10c: "own" tenants write their runs directly
    if client:   # C2-10c2 F10c-2: a client tenant's run needs its Finance (31) binding (fake party id, synthetic)
        h.ok(h.post(f"/tenants/{t}/finance-client", {"request_id": rid(), "finance_client_id": t + "-party-1"},
                    andre=True))
    port = vw.Port([ProviderAnswer("ANSWER", "Z Best Media recovers revenue.", ["https://" + domains[0] + "/x"], "m", "1",
                                   cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")])
    h.ports.engines["openai"] = port
    ps = h.ok(h.post(f"/tenants/{t}/prompt-sets", {"request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"],
        "competitor_domains": comps, "prompts": ["who recovers revenue"], "engines": ["openai"]},
        caller="dashboard"), 201)
    run = h.post(f"/tenants/{t}/citation-runs", {"request_id": rid(), "domain": domains[0],
                                                "prompt_set_id": ps["prompt_set_id"]}, andre=True)
    out["ids"][t] = {"prompt_set": ps["prompt_set_id"], "run_status": run.status_code,
                     "run_reason": str(run.json().get("detail", ""))[:60] if run.status_code >= 400 else "", "domains": len(domains)}
h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
sch = h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": "site.test", "scheme": "http", "paths": ["/"],
                                        "every_days": 7})
out["schedule"] = {"status": sch.status_code, "id": sch.json().get("schedule_id")}
# C2-10c: the ledger the lines are anchored on goes with the directory (a real upgrade keeps its ledger); without it
# the build serves reads but refuses every write INTEGRITY_UNVERIFIED (a harness artefact, not an upgrade result)
(pathlib.Path(sys.argv[2]) / "ledger.json").write_text(json.dumps(h.ledger.events))
print(json.dumps(out))
'''


def spec() -> dict:
    hosts = [h for f, h in C.hosts(8000) if f in ("idn", "idn_hyphen")]
    return {"tenants": {f"t{i}": hosts[i * 8:(i + 1) * 8] for i in range(4)}, "competitors": hosts[40:45],
            "client": {"c0": hosts[45:48]}}        # C2-10c2 F10c-2: one client tenant bound to Finance (31)


PLANTED = ("Z Best Media recovers revenue", "who recovers revenue", "Z Best Media",    # the texts WRITE stores
           "c0-party-1")      # C2-10c2: the client's Finance (31) party id (the build keeps a T-2 code; releases did not)


PARTY = PLANTED[-1]
LOG = "seo_log.jsonl"


def plaintext_at_rest(data, *, log=None) -> int:
    """Planted texts found in the files of the data directory, as bytes (UTF-8): counts only. ``log`` True: only the
    append-only seo_log.jsonl; False: every other file; None: all files."""
    return sum(f.read_bytes().count(x.encode()) for f in data.rglob("*") if f.is_file()
               and (log is None or (f.name == LOG) is log) for x in PLANTED)


def contained(h, client="c0") -> dict:
    """C2-10c3 (OWNER DECISION 2026-11-15 ~15:40 PT "count and contain"): the upgraded client's binding is its T-2
    code; the legacy raw party id is shown by no view and exported by no route; the E-COMP request (Compliance fake)
    receives the raw id from the tenant vault. Counts / booleans only."""
    code = h.svc.tenants[client]["finance_client_id"]
    views = json.dumps([h.svc.tenant_view(client), h.svc.tenants_view(), h.svc.audit_export(1, 10 ** 6),
                        h.svc.audit_evidence(10 ** 6, 0)], default=repr)
    r = h.get(f"/tenants/{client}", caller="dashboard")
    views += r.text if r.status_code < 500 else ""
    h.ports.compliance.asked.clear()
    h.svc._verify_compliance(code, client)
    return {"code": bool(isinstance(code, str) and h.svc._FC_T2.match(code)), "shown": views.count(PARTY),
            "exported_withheld": h.svc.audit_export(1, 10 ** 6)["withheld"] > 0,
            "ecomp_raw": h.ports.compliance.asked == [PARTY], "ecomp_n": len(h.ports.compliance.asked),
            "fives": r.status_code >= 500}


def build(tmp_path, name, data, srv, ledger):
    from fixture_server import install_site
    from helpers import Harness
    from ports import Ports
    install_site(srv)
    ports = Ports.default()
    ports.fetcher = srv.site_fetcher()
    (tmp_path / name).mkdir()
    # C2-10c2: a real upgrade keeps its vault key. The release's Harness generated a random one at tmp/vault.key
    # (helpers.write_vault_key, same in all three releases); without this handover the build's own random key cannot
    # open the schedule's vaulted paths -> vault_unavailable -> every slot refused (a harness artefact, not V-6)
    key = tmp_path / "tmp" / "vault.key"
    assert key.is_file()
    return Harness(tmp_path / name, data_dir=str(data), ports=ports, ledger=ledger, SEO_VAULT_KEY_FILE=str(key),
                   SEO_VAULT_KEY_ID="k-test")


@pytest.mark.local_http
@pytest.mark.parametrize("release", ["ffd28ca8", "cbdf5e78", "3120f0b0"])
def test_core_O_4_UP_directories_written_by_a_release_start_on_the_build(release, tmp_path, srv):
    tree = RL.export(release)
    if tree is None:
        pytest.skip("git history of the release is not present (a shallow checkout)")
    s = spec()
    p = subprocess.run([sys.executable, "-c", WRITE, str(tree), str(tmp_path), json.dumps(s)], capture_output=True,
                       text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-1500:]
    written = json.loads(p.stdout.strip().splitlines()[-1])
    assert {v["run_status"] for v in written["ids"].values()} <= {200, 201, 202}, \
        sorted({v["run_reason"] for v in written["ids"].values()})                   # the release wrote its runs
    assert written["schedule"]["status"] == 201 and written["schedule"]["id"]
    data = tmp_path / "data"
    plain_before = plaintext_at_rest(data)
    log_before, other_before = plaintext_at_rest(data, log=True), plaintext_at_rest(data, log=False)
    from helpers import FakeLedger
    ledger = FakeLedger()
    ledger.events = json.loads((tmp_path / "ledger.json").read_text())
    ledger.by_id = {e["event_id"]: e for e in ledger.events}
    h = build(tmp_path, "build", data, srv, ledger)
    legacy_before = h.svc.legacy_prose_lines
    from helpers import rid
    fives, missing = 0, 0
    sid = written["schedule"]["id"]
    reads = [f"/tenants/zbm/schedules/{sid}"]
    for t, ids in written["ids"].items():
        reads += [f"/tenants/{t}/prompt-sets/{ids['prompt_set']}", f"/tenants/{t}/citation-runs"]
    for path in reads:
        r = h.get(path, caller="dashboard")
        fives += r.status_code >= 500
        missing += r.status_code == 404
    # V-6 "0 skipped slots": every schedule slot due on the build runs (slot 0 at start, slot 1 a week later)
    due, ran, tick5, ticks = 0, 0, 0, []
    for step in range(2):
        if step:
            h.clock.advance(days=7)
        r = h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler")
        tick5 += r.status_code >= 500
        ticks.append((r.status_code, {k: v for k, v in r.json().items() if v} if r.status_code == 200 else
                      str(r.json().get("detail", ""))[:60]))
        due += 1
        ran += r.json().get("ran", 0) if r.status_code == 200 else 0
    slots = h.ok(h.get(f"/tenants/zbm/schedules/{sid}", caller="dashboard")).get("slots", {})
    # C2-10c2: the scheduler's refusal inputs (svc_schedules.schedule_tick), as booleans only
    diag = {"schedule_vault_unavailable": bool(h.svc.schedules[sid].get("vault_unavailable")),
            "key_configured": bool(h.svc.vault.key_configured)}
    skipped = (due - ran) + sum(1 for v in slots.values() if isinstance(v, dict) and v.get("status") == "skipped")
    cont = contained(h)
    h.svc.close()
    # first touch done (start, reads, two ticks): restart on the same directory and count again
    h2 = build(tmp_path, "build2", data, srv, ledger)
    legacy_after = h2.svc.legacy_prose_lines
    cont2 = contained(h2)
    h2.svc.close()
    plain_after = plaintext_at_rest(data)
    log_after, other_after = plaintext_at_rest(data, log=True), plaintext_at_rest(data, log=False)
    doms = sum(v["domains"] for v in written["ids"].values())
    print(f"O-4 UP / V-6 {release}: tenants={len(written['ids'])} IDN domains={doms} reads={len(reads)} 5xx={fives + tick5} "
          f"missing={missing} slots_due={due} ran={ran} skipped_slots={skipped} plaintext_at_rest before={plain_before} "
          f"after={plain_after} (log {log_before}->{log_after}, other files {other_before}->{other_after}) "
          f"legacy_prose_lines before={legacy_before} after={legacy_after} contained={cont} restart={cont2} "
          f"diag={diag} ticks={ticks}")
    shutil.rmtree(tmp_path, ignore_errors=True)
    assert fives + tick5 == 0 and missing == 0 and skipped == 0
    # OWNER DECISION 2026-11-15 ~15:40 PT "count and contain" (C2-10c3; V-6 exception for legacy finance party ids,
    # ADR): the releases' tenant_finance_client_set line stays untouched in the append-only log (no history
    # mutation) and is COUNTED (legacy_prose_lines), never shown or exported; nothing new is written raw after the
    # first touch; the replay binds the T-2 code and the E-COMP request gets the raw id from the tenant vault
    assert other_before == 0 and other_after == 0 and log_after == log_before == plain_before
    assert legacy_after == legacy_before and (legacy_before > 0) is (log_before > 0)
    for c in (cont, cont2):
        assert c["code"] and c["shown"] == 0 and c["ecomp_raw"] and not c["fives"]
        assert c["exported_withheld"] is (log_before > 0)
