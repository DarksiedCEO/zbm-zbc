#!/usr/bin/env python3
"""War room: red-team launch gate for public-facing departments (ADR 0018). Standard library only.

  python3 devtools/warroom/run.py --all --seed 1                  every seeded department, the gate
  python3 devtools/warroom/run.py --department service-py --seed 7
  python3 devtools/warroom/run.py --replay 'service-py/email-clear-opt-out#012.2@1'
  python3 devtools/warroom/run.py --replay service-py/R0001       a replay-library case
  python3 devtools/warroom/run.py --all --seed 1 --promote        append new MUST failures to the replay library
  python3 devtools/warroom/run.py --department seo-py-callum --seed 9 --generated 400   explore: more invented cases

A failing GENERATED case (a scenario with a ``generator``: the war room invented it) is appended to the replay library
on every run, --promote or not (memory only grows). On CI (``CI`` or ``GITHUB_ACTIONS`` set) the checkout is never
written: the entries go to ``replay-additions.json`` in ``--out`` and the gate fails on them as on any new failure.

Exit status: 0 every department's gate passed; 1 a gate failed (a new MUST failure, or a case that ERRORed);
2 usage or library error. ``--out DIR`` writes ``warroom-report.json`` and ``warroom-report.md`` there (the markdown
is printed either way)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine  # noqa: E402
import personas  # noqa: E402


def _replay(case_id: str, python: str) -> int:
    dept = case_id.partition("/")[0]
    lib = engine.load_library(dept)
    if "#" in case_id:
        _, scenario, idx, variant, seed = engine.parse_case_id(case_id)
        cases = [c for c in engine.generate(lib, seed, max(variant, 0), max(idx + 1, 1)) if c["case_id"] == case_id]
        if not cases:                              # a generated case beyond --generated's count is still rebuilt
            cases = [c for c in engine.generate(lib, seed, max(variant, 0)) if c["case_id"] == case_id]
    else:
        cases = [c for c in engine.replay_cases(lib) if c["case_id"] == case_id]
    if not cases:
        print(f"war room: no case {case_id!r} (a seeded id is <dept>/<scenario>#<seed index>.<variant>@<seed>; "
              f"a replay-library id is <dept>/R<nnnn>)", file=sys.stderr)
        return 2
    case = cases[0]
    obs = engine.run_worker(lib, [case], python)[case["case_id"]]
    v = engine.judge(case, obs)
    print(json.dumps({"case": {k: case[k] for k in ("case_id", "scenario", "persona", "chain", "seed_value", "input")},
                      "outcome": v["outcome"], "known_failure": case.get("known_failure"), "checks": v["checks"],
                      "error": v["error"], "steps": obs.get("steps"), "state": obs.get("state")},
                     indent=1, ensure_ascii=False, default=str))
    return 0 if v["outcome"] == engine.PASS or case.get("known_failure") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="every department with a scenario library")
    g.add_argument("--department", action="append", help="one department (repeatable), e.g. service-py")
    g.add_argument("--replay", metavar="CASE_ID", help="re-run one case exactly and print everything it saw")
    ap.add_argument("--list", action="store_true", help="list the departments (or --department) and their case counts")
    ap.add_argument("--seed", type=int, default=1, help="global chaos seed (default 1; CI uses 1)")
    ap.add_argument("--variants", type=int, default=None,
                    help="chaos variants per seed phrase (default: the library's variants_per_seed)")
    ap.add_argument("--jobs", type=int, default=3, help="departments run in parallel (one worker process each)")
    ap.add_argument("--python", default=engine.default_python(),
                    help="interpreter with the services' pinned requirements (default: this one)")
    ap.add_argument("--out", type=Path, default=None, help="directory for warroom-report.json / .md")
    ap.add_argument("--promote", action="store_true",
                    help="append new MUST failures to the replay library (known_failure: null until triaged)")
    ap.add_argument("--generated", type=int, default=None,
                    help="cases per generated scenario (default: the library's count; exploration, not CI)")
    ap.add_argument("--no-auto-promote", action="store_true",
                    help="do not append failing generated cases to the replay library (self-tests only)")
    ap.add_argument("--llm-personas", action="store_true",
                    help="ask the LLM persona port for extra personas (not connected: reported, never faked)")
    a = ap.parse_args(argv)

    if a.replay:
        return _replay(a.replay, a.python)
    if not (a.all or a.department or a.list):
        ap.error("one of --all, --department, --replay or --list is required")
    try:
        depts = sorted(set(a.department)) if a.department else engine.departments()
        for d in depts:
            engine.load_library(d)
    except engine.LibraryError as e:
        print(f"war room: {e}", file=sys.stderr)
        return 2
    if a.list:
        for d in depts:
            lib = engine.load_library(d)
            cs = engine.generate(lib, a.seed, a.variants, a.generated)
            gen = sum(1 for c in cs if c.get("generated"))
            print(f"{d}: {len(engine.replay_cases(lib))} replay + {len(cs) - gen} seeded + {gen} generated cases "
                  f"(seed {a.seed})")
        return 0
    if a.llm_personas:
        print(f"war room: LLM personas: {personas.LLMPersonaPort().status()} — deterministic personas only",
              file=sys.stderr)

    results = engine.run(depts, a.seed, a.variants, a.python, a.jobs, a.generated)
    rep = engine.report(results, a.seed, a.variants)
    md = engine.markdown(rep)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "warroom-report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
        (a.out / "warroom-report.md").write_text(md, encoding="utf-8")
    print(md)
    today = date.today().isoformat()
    on_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    if a.promote:
        added = engine.promote(results, today)
        for d, ids in added.items():
            print(f"war room: promoted to devtools/warroom/replay/{d}.json: {', '.join(ids)}", file=sys.stderr)
    elif not a.no_auto_promote:
        added = engine.promote(results, today, generated_only=True, write=not on_ci)
        for d, got in added.items():
            if on_ci:
                print(f"war room: {len(got)} failing generated case(s) of {d} NOT written to the checkout on CI; "
                      f"replay them with --replay <origin> and --promote locally", file=sys.stderr)
            else:
                print(f"war room: failing generated cases saved to devtools/warroom/replay/{d}.json: "
                      f"{', '.join(got)} (triage them in findings.md)", file=sys.stderr)
        if on_ci and added and a.out:
            (a.out / "replay-additions.json").write_text(json.dumps(added, indent=1, ensure_ascii=False) + "\n",
                                                         encoding="utf-8")
    return 0 if rep["gate"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
