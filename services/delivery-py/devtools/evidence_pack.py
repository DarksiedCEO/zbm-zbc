"""
DEVTOOLS ONLY. Produces the evidence the build report cites (DEPT28_SPEC §G) under docs/evidence/dept28/:
S1's ledger event list with the request_id/facts_sha256 chain, S1's evidence directory listing with sha256s, the
S1 and A5 reports (the runner's counts, not the model's string), the G10 refusal messages, the prompts sha256 table
beside the Superpowers originals' hashes, and the resolved dependency versions. It drives the same harness the
certification tests use (tests/helpers.py) with the fakes; nothing here is production code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
OUT = ROOT / "docs" / "evidence" / "dept28"
ORIGINALS = {"test-driven-development.md": "64b03fce", "systematic-debugging.md": "808fc571", "verification-before-completion.md": "2befe7fc",
             "executing-plans.md": "f38e8f2d", "writing-plans.md": "a6c67c19", "reviewer.md": "bfed55c8 / 0828a5e9 / 82e370be"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def s1_and_a5() -> None:
    from helpers import Harness, TEST_ADD, finding, findings_doc, write_test
    h = Harness()
    try:
        r = h.submit()
        body = r.json()
        run_id = body["run_id"]
        run = h.run(run_id)
        events = [e for e in h.ledger.events]
        lines = [f"# S1 ledger events — run {run_id} (request_id {body['request_id']}, facts_sha256 {body['facts_sha256']})", ""]
        for e in events:
            p = e["payload"]
            chain = {k: p[k] for k in ("request_id", "facts_sha256", "run_id", "finding_id", "from", "to", "phase", "exit", "decision", "class", "tool") if k in p}
            lines.append(f"{e['event_type']:28} {e['actor']:20} {e['event_id']}  {json.dumps(chain, sort_keys=True)}")
        lines.append("")
        lines.append(f"total events: {len(events)}; run status: {run['status']}; branch {run['branch']}; commits {[c['sha'][:12] for c in run['commits']]}")
        (OUT / "s1-ledger-events.txt").write_text("\n".join(lines) + "\n")
        ev_lines = [f"# S1 evidence directory — {h.svc.evidence_root}/{run_id}", ""]
        for ev in run["evidence"]:
            path = Path(h.svc.evidence_root) / run_id / ev["evidence_id"]
            ev_lines.append(f"{ev['evidence_id']}  {ev['kind']:16} {ev['bytes']:8} bytes  sha256 {ev['sha256']}  mode {oct(path.stat().st_mode & 0o777)}  on-disk sha256 {sha(path)}")
        (OUT / "s1-evidence-listing.txt").write_text("\n".join(ev_lines) + "\n")
        (OUT / "s1-report.md").write_text(h.report(run_id))
        (OUT / "s1-run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
        (OUT / "s1-findings.json").write_text(json.dumps(h.findings(run_id), indent=2, sort_keys=True) + "\n")
        (OUT / "s1-docker-argv.txt").write_text("\n".join(" ".join(c) if len(" ".join(c)) < 400 else " ".join(c)[:400] + " …" for c in h.docker.calls) + "\n")
    finally:
        h.close()
    scenario = [{"text": "FIXED\nSUITE: 100/100 passed"},
                write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
                {"text": "FIXED\nSUITE: 100/100 passed\nall green, done"}, {"text": "FIXED\nSUITE: 100/100 passed"}, {"text": "FIXED"}]
    h = Harness(scenario=scenario, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "4"})
    try:
        run_id = h.submit(findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        run = h.run(run_id)
        f = h.findings(run_id)[0]
        text = [f"# A5 self-report — run {run_id}: status {run['status']}, finding state {f['state']}, rounds {f['rounds']}", "",
                "The model replied 'FIXED / SUITE: 100/100 passed' three times. The runner's captured runs:", ""]
        for e in h.events("test_run"):
            p = e["payload"]
            text.append(f"  test_run phase={p['phase']} exit={p['exit']} evidence={p['evidence_id']}")
        for e in h.events("suite_run"):
            p = e["payload"]
            text.append(f"  suite_run phase={p['phase']} passed={p['passed']} failed={p['failed']}")
        blob = json.dumps(h.ledger.events)
        text.append("")
        text.append(f"'100/100' in any ledger payload: {'100/100' in blob}")
        text.append(f"run reasons: {json.dumps(run['reasons'])}")
        (OUT / "a5-self-report.md").write_text("\n".join(text) + "\n")
    finally:
        h.close()


def g10() -> None:
    from helpers import base_env
    from test_guardrails import MUTATIONS, _mutate
    from zbm_delivery import config as C, gate as G
    env = base_env("/tmp", "/tmp")
    settings = C.load(env)
    doc, _ = G.load_config_doc(str(ROOT / "config" / "deerflow.engine.yaml"), env)
    lines = ["# G10 — refusal messages for every single-key mutation of config/deerflow.engine.yaml", ""]
    for key, value in MUTATIONS:
        problems = G.config_problems(_mutate(doc, key, value), settings)
        lines.append(f"{key} = {json.dumps(value)[:60]}")
        for p in problems:
            lines.append(f"    refused: {p}")
    (OUT / "g10-config-refusals.txt").write_text("\n".join(lines) + "\n")


def prompts() -> None:
    lines = ["# prompts/ sha256 (ours) beside the Superpowers originals' sha256 prefix (obra/superpowers@8ca22dba)", ""]
    for p in sorted((ROOT / "prompts").iterdir()):
        lines.append(f"{sha(p)}  {p.name:40} original: {ORIGINALS.get(p.name, '(ours)')}")
    (OUT / "prompts-sha256.txt").write_text("\n".join(lines) + "\n")


def deps() -> None:
    lock = (ROOT / "uv.lock").read_text()
    want = ("anyio", "langchain-anthropic", "soupsieve", "websockets", "langgraph", "langchain", "langchain-core", "langchain-openai",
            "langgraph-checkpoint", "langgraph-prebuilt", "deerflow-harness", "deerflow-extension-api", "fastapi", "pydantic", "uvicorn", "httpx",
            "tiktoken", "pytest")
    lines = ["# resolved versions (uv.lock) — the three floors of spec 0.3 and the audited langchain/langgraph family", ""]
    for name in want:
        m = re.search(rf'^name = "{re.escape(name)}"\nversion = "([^"]+)"', lock, re.M)
        lines.append(f"{name:24} {m.group(1) if m else '(absent)'}")
    floors = {"anyio": "4.14.2", "langchain-anthropic": "1.4.6", "soupsieve": "2.9.0"}
    lines.append("")
    for name, floor in floors.items():
        m = re.search(rf'^name = "{re.escape(name)}"\nversion = "([^"]+)"', lock, re.M)
        lines.append(f"floor {name} >= {floor}: resolved {m.group(1)} -> {'held' if m and tuple(map(int, m.group(1).split('.'))) >= tuple(map(int, floor.split('.'))) else 'NOT HELD'}")
    lines.append("")
    lines.append("# dropped packages in uv export --frozen (still listed, marker sys_platform == 'never', never installed):")
    exp = subprocess.run(["uv", "export", "--frozen", "--no-hashes", "--no-dev"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    for ln in exp.stdout.splitlines():
        if any(ln.startswith(d) for d in ("langchain-openviking", "openviking-sdk", "langgraph-api", "langgraph-runtime-inmem", "langgraph-cli")):
            lines.append("  " + ln)
    lines.append("")
    lines.append("# ls site-packages | grep (must be empty for the dropped set and forbiddenfruit / telegram):")
    sp = ROOT / ".venv" / "lib" / "python3.12" / "site-packages"
    hits = [e for e in sorted(os.listdir(sp)) if any(k in e.lower() for k in ("openviking", "langgraph_api", "langgraph_runtime", "langgraph_cli", "forbiddenfruit", "telegram"))]
    lines.append("  " + (", ".join(hits) if hits else "(none present)"))
    lines.append("")
    chk = subprocess.run(["uv", "lock", "--check"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    lines.append(f"# uv lock --check: rc={chk.returncode} {chk.stderr.strip()[:200]}")
    (OUT / "dependencies.txt").write_text("\n".join(lines) + "\n")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("UV_CACHE_DIR", os.environ.get("UV_CACHE_DIR", "/tmp/uvcache"))
    deps()
    prompts()
    g10()
    s1_and_a5()
    print("evidence written to", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
