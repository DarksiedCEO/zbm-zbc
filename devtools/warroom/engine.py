"""War room engine (ADR 0018): scenario libraries -> deterministic cases -> one sandboxed worker per service ->
invariants -> per-department gate. Standard library only (the services' own dependencies are used only inside the
workers, which run each service's harness).

Determinism: a case's input is a pure function of (library, seed corpus, global seed, scenario id, seed index,
variant index); the worker's outcome for that input is a pure function of the service's code (fixed clocks, fakes,
fresh state per case). Two runs with the same seed give the same cases, outcomes and report, apart from the
``timing`` section (measured, reported, never asserted).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

import chaos
import corpus
import invariants

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SCENARIOS = HERE / "scenarios"
REPLAY = HERE / "replay"
DRIVERS = HERE / "drivers"
SEVERITIES = ("MUST", "SHOULD")
WORKER_TIMEOUT_S = 900
PASS, FAIL, ERROR = "PASS", "FAIL", "ERROR"


class LibraryError(Exception):
    pass


# ============================================================================================= libraries

def departments() -> list[str]:
    return sorted(p.stem for p in SCENARIOS.glob("*.json"))


def load_library(dept: str) -> dict:
    path = SCENARIOS / f"{dept}.json"
    if not path.is_file():
        raise LibraryError(f"no scenario library for {dept!r} (have: {', '.join(departments())})")
    lib = json.loads(path.read_text(encoding="utf-8"))
    for key in ("library", "version", "service", "driver", "scenarios"):
        if key not in lib:
            raise LibraryError(f"{path.name}: missing {key!r}")
    seen = set()
    for sc in lib["scenarios"]:
        if sc["id"] in seen:
            raise LibraryError(f"{path.name}: duplicate scenario id {sc['id']}")
        seen.add(sc["id"])
        for inv in sc["invariants"]:
            if inv.get("severity") not in SEVERITIES:
                raise LibraryError(f"{path.name}/{sc['id']}/{inv.get('id')}: severity must be MUST or SHOULD")
            if inv["check"] not in invariants.INVARIANTS:
                raise LibraryError(f"{path.name}/{sc['id']}/{inv['id']}: unknown check {inv['check']}")
        for t in sc.get("transforms", []):
            if t not in chaos.TRANSFORMS:
                raise LibraryError(f"{path.name}/{sc['id']}: unknown transform {t}")
    return lib


def load_replay(dept: str) -> dict:
    path = REPLAY / f"{dept}.json"
    if not path.is_file():
        return {"library": dept, "cases": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_replay(dept: str, doc: dict) -> None:
    REPLAY.mkdir(exist_ok=True)
    (REPLAY / f"{dept}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def scenario_seeds(lib: dict, sc: dict) -> list[str]:
    service_dir = REPO / "services" / lib["service"]
    out: list[str] = []
    for src in sc["seeds"]:
        if "extract" in src:
            out += corpus.extract(src["extract"], service_dir)
        else:
            out += list(src["literal"])
    seen, uniq = set(), []
    for s in out:                       # a phrase pinned by two tests is one seed
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


# ============================================================================================= cases

def _case_invariants(sc: dict, chain: list[str]) -> list[dict]:
    """The scenario's invariants, with a MUST lowered to SHOULD where the library says this transform changes what
    the documented guarantee covers (``downgrade``: {transform: [invariant ids]})."""
    lowered = {iid for t in chain for iid in sc.get("downgrade", {}).get(t, [])}
    return [{**inv, "severity": "SHOULD" if inv["id"] in lowered else inv["severity"]} for inv in sc["invariants"]]


def _case(lib: dict, sc: dict, case_id: str, value, chain: list[str], params: dict, seed_value) -> dict:
    inp = dict(sc.get("fixed_input", {}))
    inp.update(params)
    if sc.get("input_field"):
        inp[sc["input_field"]] = value
    if sc.get("seed_field"):                    # the unmutated seed too (e.g. the identity a variant must match)
        inp[sc["seed_field"]] = seed_value
    return {"case_id": case_id, "department": lib["service"], "scenario": sc["id"], "persona": sc["persona"],
            "seed_value": seed_value, "chain": chain, "input": inp, "steps": sc["steps"],
            "invariants": _case_invariants(sc, [c.split("=")[0] for c in chain])}


def generate(lib: dict, seed: int, variants: int | None = None) -> list[dict]:
    """Every case of a library for a global seed: per seed phrase the unmutated base case, then ``variants`` chaos
    variants (library default ``variants_per_seed``; a scenario may set its own). Identical inputs are one case."""
    cases: list[dict] = []
    for sc in lib["scenarios"]:
        seeds = scenario_seeds(lib, sc) if sc.get("seeds") else [None]
        n_var = variants if variants is not None else sc.get("variants_per_seed", lib.get("variants_per_seed", 2))
        seen: set[str] = set()
        for i, s in enumerate(seeds):
            for k in range(0, n_var + 1):
                rng = chaos.case_rng(seed, sc["id"], i, k)
                chain: list[str] = []
                params = {}
                value = s
                if k > 0:
                    for name, space in sorted(sc.get("params", {}).items()):
                        params[name] = rng.choice(space)
                        chain.append(f"{name}={params[name]}")
                    if sc.get("transforms") and s is not None:
                        picked = chaos.pick_chain(rng, sc["transforms"], sc.get("chain_max", 2),
                                                  sc.get("require", []))
                        value = chaos.apply_chain(s, picked, rng)
                        chain += picked
                elif sc.get("params"):
                    params = {name: space[0] for name, space in sorted(sc["params"].items())}
                key = json.dumps([value, params], sort_keys=True, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                cases.append(_case(lib, sc, f"{lib['service']}/{sc['id']}#{i:03d}.{k}@{seed}", value, chain, params,
                                   s))
    return cases


def replay_cases(lib: dict) -> list[dict]:
    """The replay library: every case ever promoted, materialised (input as it was sent), run on every run."""
    scs = {sc["id"]: sc for sc in lib["scenarios"]}
    out = []
    for rc in load_replay(lib["service"])["cases"]:
        sc = scs.get(rc["scenario"])
        if sc is None:
            raise LibraryError(f"replay case {rc['replay_id']}: scenario {rc['scenario']} no longer exists")
        c = _case(lib, sc, rc["replay_id"], None, rc["chain"], {}, rc.get("seed_value"))
        c["input"] = dict(rc["input"])
        c["known_failure"] = rc.get("known_failure")
        c["origin"] = rc.get("origin")
        out.append(c)
    return out


def input_key(case: dict) -> str:
    return json.dumps([case["scenario"], case["input"]], sort_keys=True, ensure_ascii=False)


def parse_case_id(case_id: str) -> tuple[str, str, int, int, int]:
    """``<dept>/<scenario>#<seed index>.<variant>@<global seed>`` -> its parts."""
    dept, _, rest = case_id.partition("/")
    scenario, _, rest = rest.partition("#")
    idx, _, rest = rest.partition(".")
    variant, _, seed = rest.partition("@")
    return dept, scenario, int(idx), int(variant), int(seed)


# ============================================================================================= running

def run_worker(lib: dict, cases: list[dict], python: str) -> dict[str, dict]:
    service_dir = REPO / "services" / lib["service"]
    payload = [{"case_id": c["case_id"], "input": c["input"], "steps": c["steps"]} for c in cases]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([python, "-B", str(HERE / "worker.py"), str(service_dir), str(DRIVERS / lib["driver"])],
                           input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=service_dir,
                           timeout=WORKER_TIMEOUT_S)
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out, err, code = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), \
            "worker timed out", -1
    results = {}
    for line in out.splitlines():
        if line.startswith("WARROOM-RESULT "):
            r = json.loads(line[len("WARROOM-RESULT "):])
            results[r["case_id"]] = r
    for c in cases:                     # a case the worker never answered is an ERROR, never a pass
        if c["case_id"] not in results:
            results[c["case_id"]] = {"case_id": c["case_id"], "steps": [], "state": {}, "elapsed_ms": 0.0,
                                     "error": f"worker exited {code} before this case; stderr tail: {err[-1500:]}"}
    return results


def judge(case: dict, obs: dict) -> dict:
    checks = []
    if obs.get("error"):
        outcome = ERROR
    else:
        for inv in case["invariants"]:
            held, detail = invariants.evaluate(inv, obs)
            checks.append({"id": inv["id"], "severity": inv["severity"], "held": held, "detail": detail})
        outcome = PASS if all(c["held"] for c in checks if c["severity"] == "MUST") else FAIL
    return {"case_id": case["case_id"], "scenario": case["scenario"], "persona": case["persona"],
            "chain": case["chain"], "seed_value": case.get("seed_value"), "input": case["input"],
            "known_failure": case.get("known_failure"), "outcome": outcome, "checks": checks,
            "error": obs.get("error"), "elapsed_ms": obs.get("elapsed_ms", 0.0), "observation": {
                "steps": [{"action": s["action"], "status": s["status"]} for s in obs.get("steps", [])],
                "state": obs.get("state", {})}}


def run_department(dept: str, seed: int, variants: int | None, python: str,
                   only: list[str] | None = None) -> list[dict]:
    lib = load_library(dept)
    try:
        replays = replay_cases(lib)
        seeded = generate(lib, seed, variants)
    except (corpus.CorpusError, LibraryError, KeyError, ValueError) as e:
        return [{"case_id": f"{dept}/<library>", "scenario": "<library>", "persona": "", "chain": [],
                 "seed_value": None, "input": {}, "known_failure": None, "outcome": ERROR, "checks": [],
                 "error": f"library could not be built: {type(e).__name__}: {e}", "elapsed_ms": 0.0,
                 "observation": {}}]
    covered = {input_key(c) for c in replays}
    cases = replays + [c for c in seeded if input_key(c) not in covered]
    if only is not None:
        cases = [c for c in cases if c["case_id"] in only]
    if not cases:
        return []
    results = run_worker(lib, cases, python)
    return [judge(c, results[c["case_id"]]) for c in cases]


def run(depts: list[str], seed: int, variants: int | None, python: str, jobs: int) -> dict[str, list[dict]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {d: ex.submit(run_department, d, seed, variants, python) for d in depts}
        return {d: futs[d].result() for d in depts}


# ============================================================================================= gate and report

def summarise(dept: str, verdicts: list[dict]) -> dict:
    must_cases = [v for v in verdicts if v["outcome"] != ERROR and not v["known_failure"]]
    must_pass = [v for v in must_cases if v["outcome"] == PASS]
    should = [c for v in verdicts for c in v["checks"] if c["severity"] == "SHOULD"]
    new_fail = [v for v in verdicts if v["outcome"] == FAIL and not v["known_failure"]]
    known = [v for v in verdicts if v["known_failure"]]
    errors = [v for v in verdicts if v["outcome"] == ERROR]
    by_t: dict[str, dict] = {}
    for v in verdicts:
        for t in (v["chain"] or ["(base)"]):
            t = t.split("=")[0]
            b = by_t.setdefault(t, {"cases": 0, "must_fail": 0, "should_miss": 0})
            b["cases"] += 1
            b["must_fail"] += v["outcome"] == FAIL
            b["should_miss"] += sum(1 for c in v["checks"] if c["severity"] == "SHOULD" and not c["held"])
    by_s: dict[str, dict] = {}
    for v in verdicts:
        b = by_s.setdefault(v["scenario"], {"cases": 0, "pass": 0, "fail": 0, "error": 0, "known_failure": 0})
        b["cases"] += 1
        b["known_failure" if v["known_failure"] else v["outcome"].lower()] += 1
    gate = "PASS" if not new_fail and not errors and verdicts else "FAIL"
    return {
        "department": dept, "gate": gate, "cases": len(verdicts),
        "outcomes": {o: sum(v["outcome"] == o for v in verdicts) for o in (PASS, FAIL, ERROR)},
        "must_pass_rate": round(len(must_pass) / len(must_cases), 4) if must_cases else None,
        "should_score": round(sum(c["held"] for c in should) / len(should), 4) if should else None,
        "should_checks": len(should),
        "by_scenario": dict(sorted(by_s.items())), "by_transform": dict(sorted(by_t.items())),
        "new_must_failures": [_brief(v) for v in new_fail], "errors": [_brief(v) for v in errors],
        "known_failures": [{**_brief(v), "finding": v["known_failure"], "still_failing": v["outcome"] == FAIL}
                           for v in known],
        "should_misses": [_brief(v, severity="SHOULD") for v in verdicts
                          if any(c["severity"] == "SHOULD" and not c["held"] for c in v["checks"])],
    }


def _brief(v: dict, severity: str = "MUST") -> dict:
    failed = [f"{c['id']}: {c['detail']}" for c in v["checks"] if c["severity"] == severity and not c["held"]]
    return {"case_id": v["case_id"], "scenario": v["scenario"], "chain": v["chain"],
            "input": v["input"], "failed": failed, **({"error": v["error"]} if v.get("error") else {})}


def report(results: dict[str, list[dict]], seed: int, variants: int | None) -> dict:
    deps = {d: summarise(d, vs) for d, vs in sorted(results.items())}
    slow = sorted(((v["elapsed_ms"], v["case_id"]) for vs in results.values() for v in vs), reverse=True)[:10]
    return {
        "warroom": 1, "seed": seed, "variants_override": variants,
        "llm_personas": "NOT_CONNECTED",
        "gate": "PASS" if deps and all(d["gate"] == "PASS" for d in deps.values()) else "FAIL",
        "departments": deps,
        "timing": {"note": "measured, reported, never asserted; not part of the deterministic result",
                   "slowest_cases_ms": [{"case_id": c, "ms": ms} for ms, c in slow],
                   "per_department_ms": {d: round(sum(v["elapsed_ms"] for v in vs), 1)
                                         for d, vs in sorted(results.items())}},
    }


def markdown(rep: dict) -> str:
    L = [f"# War room report (seed {rep['seed']})", "",
         f"Gate: **{rep['gate']}**. LLM personas: {rep['llm_personas']} (deterministic personas only).", "",
         "| Department | Gate | Cases | PASS | FAIL | ERROR | MUST pass rate (excl. known) | SHOULD score | Known failures |",
         "|---|---|---|---|---|---|---|---|---|"]
    for d, s in rep["departments"].items():
        o = s["outcomes"]
        L.append(f"| {d} | {s['gate']} | {s['cases']} | {o['PASS']} | {o['FAIL']} | {o['ERROR']} | "
                 f"{_pct(s['must_pass_rate'])} | {_pct(s['should_score'])} | {len(s['known_failures'])} |")
    for d, s in rep["departments"].items():
        L += ["", f"## {d}", "", "| Transform | Cases | MUST failures | SHOULD misses |", "|---|---|---|---|"]
        L += [f"| {t} | {b['cases']} | {b['must_fail']} | {b['should_miss']} |" for t, b in s["by_transform"].items()]
        L += ["", "| Scenario | Cases | PASS | FAIL | ERROR | known failure |", "|---|---|---|---|---|---|"]
        L += [f"| {sc} | {b['cases']} | {b['pass']} | {b['fail']} | {b['error']} | {b['known_failure']} |"
              for sc, b in s["by_scenario"].items()]
        for title, key in (("New MUST failures (gate-blocking)", "new_must_failures"), ("Errors", "errors"),
                           ("Known failures", "known_failures")):
            if s[key]:
                L += ["", f"### {title}", ""]
                for f in s[key]:
                    extra = f" finding {f['finding']}{'' if f.get('still_failing') else ' (now passes: clear it)'}" \
                        if "finding" in f else ""
                    L.append(f"- `{f['case_id']}` chain {f['chain']}{extra}: {'; '.join(f['failed']) or f.get('error', '')[:300]}")
        if s["should_misses"]:
            L += ["", f"### SHOULD misses ({len(s['should_misses'])}, scored, not blocking)", ""]
            L += [f"- `{f['case_id']}` {f['chain']}: {'; '.join(f['failed'])}" for f in s["should_misses"][:15]]
            if len(s["should_misses"]) > 15:
                L.append(f"- ... {len(s['should_misses']) - 15} more in the JSON report")
    L += ["", "## Slowest cases (measured, not asserted)", ""]
    L += [f"- `{x['case_id']}` {x['ms']} ms" for x in rep["timing"]["slowest_cases_ms"]]
    return "\n".join(L) + "\n"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def deterministic_view(rep: dict) -> dict:
    """The report without its measured timing (what two runs with one seed must agree on)."""
    return {k: v for k, v in rep.items() if k != "timing"}


def promote(results: dict[str, list[dict]], today: str) -> dict[str, list[str]]:
    """Append every new MUST failure to its department's replay library (append-only: nothing is ever removed or
    rewritten here). The new entries carry ``known_failure: null`` and so still block the gate until a person files
    a finding in findings.md and writes its id into the entry."""
    added: dict[str, list[str]] = {}
    for dept, verdicts in results.items():
        doc = load_replay(dept)
        have = {json.dumps([c["scenario"], c["input"]], sort_keys=True, ensure_ascii=False) for c in doc["cases"]}
        n = len(doc["cases"])
        for v in verdicts:
            key = json.dumps([v["scenario"], v["input"]], sort_keys=True, ensure_ascii=False)
            if v["outcome"] != FAIL or v["known_failure"] or key in have or "/<library>" in v["case_id"]:
                continue
            n += 1
            rid = f"{dept}/R{n:04d}"
            doc["cases"].append({"replay_id": rid, "scenario": v["scenario"], "origin": v["case_id"],
                                 "chain": v["chain"], "seed_value": v["seed_value"], "input": v["input"],
                                 "added": today, "known_failure": None,
                                 "failed": [f for f in _brief(v)["failed"]]})
            have.add(key)
            added.setdefault(dept, []).append(rid)
        if dept in added:
            save_replay(dept, doc)
    return added


def default_python() -> str:
    return sys.executable
