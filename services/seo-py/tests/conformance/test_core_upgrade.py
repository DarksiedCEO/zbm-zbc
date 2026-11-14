"""core-spec/1 O-4 UP (heavy lane): data directories written by ffd28ca8 and cbdf5e78 (4 tenants, 32 IDN punycode
domains, 5 IDN competitors, citation runs, a schedule) are started on the build: 0 new 5xx, 0 skipped slots. The
releases come from git history (``releases.export``); each writes its directory in its own interpreter, the build
then reads it here. Counts only."""
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
for t, domains in spec["tenants"].items():
    domains = [d for d in domains if svc_tenants.valid_domain(d) and not svc_tenants.domain_refused(d)]
    comps = [d for d in spec["competitors"] if svc_tenants.valid_domain(d) and not svc_tenants.domain_refused(d)]
    h.tenant(t, "client", tuple(domains))
    port = vw.Port([ProviderAnswer("ANSWER", "Z Best Media recovers revenue.", ["https://" + domains[0] + "/x"], "m", "1",
                                   cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")])
    h.ports.engines["openai"] = port
    ps = h.ok(h.post(f"/tenants/{t}/prompt-sets", {"request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"],
        "competitor_domains": comps, "prompts": ["who recovers revenue"], "engines": ["openai"]},
        caller="dashboard"), 201)
    run = h.post(f"/tenants/{t}/citation-runs", {"request_id": rid(), "domain": domains[0],
                                                "prompt_set_id": ps["prompt_set_id"]}, caller="dashboard")
    out["ids"][t] = {"prompt_set": ps["prompt_set_id"], "run_status": run.status_code, "domains": len(domains)}
print(json.dumps(out))
'''


def spec() -> dict:
    hosts = [h for f, h in C.hosts(8000) if f in ("idn", "idn_hyphen")]
    return {"tenants": {f"t{i}": hosts[i * 8:(i + 1) * 8] for i in range(4)}, "competitors": hosts[40:45]}


@pytest.mark.parametrize("release", ["ffd28ca8", "cbdf5e78"])
def test_core_O_4_UP_directories_written_by_a_release_start_on_the_build(release, tmp_path):
    tree = RL.export(release)
    if tree is None:
        pytest.skip("git history of the release is not present (a shallow checkout)")
    s = spec()
    p = subprocess.run([sys.executable, "-c", WRITE, str(tree), str(tmp_path), json.dumps(s)], capture_output=True,
                       text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-1500:]
    written = json.loads(p.stdout.strip().splitlines()[-1])
    data = tmp_path / "data"
    from helpers import Harness
    (tmp_path / "build").mkdir()
    h = Harness(tmp_path / "build", data_dir=str(data))
    fives, skipped = 0, 0
    for t, ids in written["ids"].items():
        for path in (f"/tenants/{t}/prompt-sets/{ids['prompt_set']}", f"/tenants/{t}/citation-runs"):
            r = h.get(path, caller="dashboard")
            fives += r.status_code >= 500
            skipped += r.status_code == 404
    h.svc.close()
    doms = sum(v["domains"] for v in written["ids"].values())
    print(f"O-4 UP {release}: tenants={len(written['ids'])} IDN domains={doms} 5xx={fives} missing={skipped}")
    shutil.rmtree(tmp_path, ignore_errors=True)
    assert fives == 0 and skipped == 0
