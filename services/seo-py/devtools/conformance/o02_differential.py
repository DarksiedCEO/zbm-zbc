"""
O-02: the differential of the O-01 corpus against reference trees (a4af277 Gate 5, 91db0abb Wave-1 round 2).

    python devtools/conformance/o02_differential.py --ref <dir holding the ref tree's services/seo-py> --n 40

Runs the corpus (tests/conformance/gen/sites.py, built with the same seeds) through Selene, Delia and Roman of THIS
tree and of the reference tree, each in its own interpreter (the reference tree's src on sys.path, its own Fetcher
behind the same in-memory web), and prints every (agent, code, severity, decision) whose count changed, with the
spec item that explains it from ``EXPLAINED``. Exit 1 on an unexplained change. Locations are not compared: this
tree stores codes where the references stored templates (spec 0.1), so the same finding has another ``url``.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
SVC = HERE.parents[2]
# a changed finding and the spec item that requires the change (O-02: every change listed with its B item)
EXPLAINED = {
    "LANG_INVALID": "B-RO-05 / Q-12 (new finding)",
    "SITEMAP_TOO_LARGE": "B-DE-03 (new finding)",
    "ROBOTS_LINES_IGNORED": "B-RB-06 (a sitemap or ignored line no longer ends a user-agent run)",
    "STRUCTURED_DATA_RECOMMENDED_MISSING": "B-RO-07 (types from the rule table only)",
}

RUNNER = r"""
import collections, inspect, json, sys
tree, conf, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path[:0] = [tree + "/src", conf]
import httpx
from agents import RunContext, delia, osei, roman, selene
from clock import FixedClock
from datetime import datetime, timezone
from primitives import fetch as fetch_mod
from gen import sites
from common import rng
r = rng(7, "o01")
out = []
for i in range(n):
    s = sites.build(r, i)
    web = s["web"]
    f = fetch_mod.Fetcher(timeout_s=2, max_bytes=256 * 1024, max_redirects=3, resolver=web.resolve,
                          policy=lambda h, ip, p: True, transport=httpx.MockTransport(web.handle))
    clock = FixedClock(datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc))
    kw = dict(fetcher=f, renderer=None, guard=lambda **k: None, clock=clock, osei=osei.Osei(clock), domain="site.test",
              scheme="http", paths=list(s["paths"]))
    if "path_key" in inspect.signature(RunContext).parameters:
        kw["path_key"] = None
    c = RunContext(**kw)
    sel = selene.run(c)
    env, pages = sel if isinstance(sel, tuple) else (sel, {})
    envs = [env, delia.run(c) if c.robots is not None else None, roman.run(c, pages)]
    out.append(sorted("|".join((e["agent"], x["code"], x["severity"], x["decision"])) for e in envs if e
                      for x in e["findings"]))
print(json.dumps(out))
"""


def run_tree(tree: Path, n: int) -> list:
    conf = SVC / "tests" / "conformance"
    sys_tests = SVC / "tests"
    code = "import sys; sys.path.insert(0, %r)\n" % str(sys_tests) + RUNNER
    r = subprocess.run([sys.executable, "-c", code, str(tree), str(conf), str(n)], capture_output=True, text=True,
                       timeout=1800)
    if r.returncode != 0:
        raise SystemExit(f"runner failed for {tree}: {r.stderr[-2000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, type=Path, help="directory of the reference services/seo-py")
    ap.add_argument("--n", type=int, default=40)
    a = ap.parse_args()
    head, ref = run_tree(SVC, a.n), run_tree(a.ref, a.n)
    delta = collections.Counter()
    for h, f in zip(head, ref):
        ch, cf = collections.Counter(h), collections.Counter(f)
        for k in set(ch) | set(cf):
            if ch[k] != cf[k]:
                delta[k] += ch[k] - cf[k]
    unexplained = 0
    for k, d in sorted(delta.items()):
        code = k.split("|")[1]
        why = EXPLAINED.get(code)
        unexplained += why is None
        print(f"{d:+d} {k}  {why or 'UNEXPLAINED'}")
    print(f"sites {a.n}; changed keys {len(delta)}; unexplained {unexplained}")
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
