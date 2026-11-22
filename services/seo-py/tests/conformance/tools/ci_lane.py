"""core-spec/2 G-2: run the CI lane or the heavy lane of lanes_core2 and print its wall time (counts only).

    python tests/conformance/tools/ci_lane.py ci [--dry] [--workers N]   the CI lane (-m core2_ci, heavy tests skipped)
    CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=i/n python tests/conformance/tools/ci_lane.py heavy [--dry]
Run from services/seo-py. Exit code = pytest's (CI lane split: the worst file's code). --dry prints the paths and the
missing ones and runs nothing.

CI lane split (C2-G2b, rebalanced C2-G2c): --workers N (default 2; 1 = the old single pytest process). The 31 files are split into N
sets (longest first by MEASURED_S; each set holds >= 1 tests/conformance file so its conftest.py marks core2_ci);
each set is ONE pytest process with the SAME arguments (-q -rfE -p no:cacheprovider -m core2_ci <set>), all N at
once. The counts of the sets are summed into one summary line (per_file_s = each set's files and wall). Same tests,
same assertions, same counts as the single process.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

TESTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, TESTS)
from conformance import lanes_core2 as L  # noqa: E402

# CI files slowest first (queue order only; every CI file runs whatever this list says)
SLOW_FIRST = [
    "tests/conformance/test_core_lp_ti.py",
    "tests/conformance/test_core_cv10cde.py",
    "tests/test_cv_monotonic.py",
    "tests/conformance/test_core_vm.py",
    "tests/conformance/test_core_al_e2e.py",
    "tests/test_canary_all_routes.py",
    "tests/test_cv_bounded_label.py",
    "tests/test_reveal.py",
    "tests/test_vault_wiring.py",
    "tests/test_client_view.py",
]
# per-file seconds measured 2026-11-16 INSIDE one single-process conformance session (C2-G2c: -m core2_ci
# --durations=0, setup+call+teardown summed per file, load 1.18 -> 1.00; core-v2/c2g2c/per_file_s.json); unlisted ~3 s
MEASURED_S = {
    "tests/conformance/test_core_lp_ti.py": 203.3,
    "tests/conformance/test_core_cv10cde.py": 119.6,
    "tests/test_cv_monotonic.py": 97.9,
    "tests/conformance/test_core_vm.py": 96.9,
    "tests/conformance/test_core_al_e2e.py": 88.8,
    "tests/test_canary_all_routes.py": 81.3,
    "tests/test_cv_bounded_label.py": 39.9,
    "tests/test_reveal.py": 11.5,
    "tests/test_vault_wiring.py": 8.2,
    "tests/test_client_view.py": 8.0,
    "tests/conformance/test_core_smuggling.py": 6.2,
    "tests/test_cv_allowlist_service.py": 6.0,
    "tests/test_vault_scope.py": 3.8,
    "tests/test_cv_al_merged.py": 3.7,
    "tests/test_t4_type_decisions.py": 2.8,
    "tests/test_vault_v1_proof.py": 1.7,
    "tests/conformance/test_core_cv8.py": 1.3,
    "tests/test_withhold_t2_ref.py": 1.1,
    "tests/test_cv_allowlist.py": 1.0,
    "tests/test_cv_shape.py": 1.0,
    "tests/test_error_body_typed.py": 0.8,
    "tests/test_log_sink_typed.py": 0.6,
    "tests/test_audit_evidence_legacy.py": 0.5,
}
_COUNT = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)")


def command(lane: str, files: list = None) -> list:
    mark = "core2_ci" if lane == "ci" else "conformance_heavy"
    return [sys.executable, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider", "-m", mark,
            *(files if files is not None else L.paths(lane))]


def queue_order(paths: list) -> list:
    return [p for p in SLOW_FIRST if p in paths] + [p for p in paths if p not in SLOW_FIRST]


def counts(summary: str) -> dict:
    out: dict = {}
    for n, k in _COUNT.findall(summary):
        k = {"error": "errors", "warning": "warnings"}.get(k, k)
        out[k] = out.get(k, 0) + int(n)
    return out


def partition(paths: list, workers: int) -> list:
    """Longest-first greedy split by MEASURED_S; every set gets >= 1 tests/conformance file, because the conformance
    conftest.py (loaded only when a file under tests/conformance/ is collected) puts the core2_ci marker on every
    CI test - a set without one would deselect its tests."""
    conf = [p for p in queue_order(paths) if p.startswith("tests/conformance/")]
    if len(conf) < workers:
        raise SystemExit(f"--workers {workers}: only {len(conf)} conformance files")
    sets: list = [[] for _ in range(workers)]
    load = [0.0] * workers
    seeded = sorted(conf, key=lambda p: -MEASURED_S.get(p, 3.0))[:workers]   # the N largest conformance files
    for i, p in enumerate(seeded):
        sets[i].append(p)
        load[i] += MEASURED_S.get(p, 3.0)
    for p in sorted((p for p in paths if p not in seeded), key=lambda p: -MEASURED_S.get(p, 3.0)):
        i = load.index(min(load))
        sets[i].append(p)
        load[i] += MEASURED_S.get(p, 3.0)
    return [[p for p in paths if p in s] for s in sets]   # lane order inside a set


def run_split(lane: str, workers: int) -> tuple:
    sets = partition(L.paths(lane), workers)
    res: dict = {}

    def worker(i: int) -> None:
        t = time.monotonic()
        r = subprocess.run(command(lane, sets[i]), cwd=L.ROOT, capture_output=True, text=True)
        res[i] = (r.returncode, r.stdout, round(time.monotonic() - t, 1))

    th = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for x in th:
        x.start()
    for x in th:
        x.join()
    total: dict = {}
    tail: list = []
    rc = 0
    for f in range(workers):
        code, out, _ = res[f]
        lines = out.strip().splitlines()
        for k, v in counts(lines[-1] if lines else "").items():
            total[k] = total.get(k, 0) + v
        tail += [x for x in lines if x.startswith(("FAILED", "ERROR"))]
        if code not in (0, 5):          # 5 = nothing collected in that file (counts still summed)
            rc = max(rc, code)
    if not total.get("passed") and not total.get("failed"):
        rc = rc or 5
    summary = ", ".join(f"{total[k]} {k}" for k in ("failed", "passed", "skipped", "errors", "xfailed", "xpassed",
                                                     "deselected", "warnings") if total.get(k))
    per_file = {f"set{i}": {"files": len(sets[i]), "wall_s": res[i][2]} for i in range(workers)}
    return rc, tail[:20], summary, per_file


def main(argv: list) -> int:
    lane = argv[1] if len(argv) > 1 else "ci"
    if lane not in ("ci", "heavy"):
        raise SystemExit("lane: ci | heavy")
    workers = int(argv[argv.index("--workers") + 1]) if "--workers" in argv else 2
    miss = L.missing(lane)
    if "--dry" in argv or miss:
        print(json.dumps({"lane": lane, "files": len(L.paths(lane)), "missing": miss}))
        return 1 if miss else 0
    if lane == "heavy" and os.environ.get("CONFORMANCE_HEAVY") != "1":
        raise SystemExit("heavy lane: set CONFORMANCE_HEAVY=1 (and CONFORMANCE_SHARD=i/n)")
    t = time.monotonic()
    if lane == "ci" and workers > 1:
        rc, tail, summary, per_file = run_split(lane, workers)
        wall = round(time.monotonic() - t, 1)
        print("\n".join(tail))
        print(json.dumps({"lane": lane, "files": len(L.paths(lane)), "workers": workers, "rc": rc, "wall_s": wall,
                          "summary": summary, "per_file_s": per_file}))
        return rc
    r = subprocess.run(command(lane), cwd=L.ROOT, capture_output=True, text=True)
    wall = round(time.monotonic() - t, 1)
    tail = [x for x in r.stdout.strip().splitlines() if x.startswith(("FAILED", "ERROR"))][:20]
    print("\n".join(tail))
    print(json.dumps({"lane": lane, "files": len(L.paths(lane)), "rc": r.returncode, "wall_s": wall,
                      "summary": (r.stdout.strip().splitlines() or [""])[-1]}))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
