"""
Report writer (spec §C.8.6): ``report-<run_id>.md`` written by the RUNNER from its records — evidence ids, exit
codes, the runner's parsed counts, commit shas and file lists from the diff. The agent's prose is never copied in
(A5: the model's own "100/100 passed" string cannot appear).
"""

from __future__ import annotations

from typing import Callable

TAIL_LINES = 200


def _tail(text: str, n: int = TAIL_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _counts(c: dict | None) -> str:
    if not c:
        return "(not run)"
    return (f"{c.get('passed', 0)} passed / {c.get('failed', 0)} failed / {c.get('errors', 0)} errors / "
            f"{c.get('skips', 0)} skips")


def _phase(name: str, rec: dict | None, read_evidence: Callable[[str], str]) -> list[str]:
    if not rec:
        return [f"- {name}: (not run)"]
    out = [f"- {name}: exit {rec.get('exit')} · evidence `{rec.get('evidence_id')}` · argv `{' '.join(rec.get('argv') or [])}`"]
    ev = rec.get("evidence_id")
    if ev:
        text = read_evidence(ev)
        if text:
            out.append("")
            out.append("```text")
            out.append(_tail(text))
            out.append("```")
    return out


def render(run: dict, findings: list[dict], read_evidence: Callable[[str], str]) -> str:
    L: list[str] = []
    L.append(f"# Fix run report — {run['run_id']}")
    L.append("")
    L.append(f"- Service: `{run['service']}` · Branch: `{run.get('branch')}` · Base: `{run['base_sha']}` ({run['base_ref']})")
    L.append(f"- Request: `{run['request_id']}` · facts_sha256 `{run['facts_sha256']}`")
    L.append(f"- Model: {run['llm']['provider']} / {run['llm']['model']}" + (" (FAKE, non-production)" if run['llm'].get('fake') else ""))
    L.append(f"- Policy version {run['policy_version']} · prompts manifest `{run['prompts_manifest_sha256']}`")
    L.append("")
    L.append("## Suite")
    L.append("")
    before, after = (run.get("suite") or {}).get("before"), (run.get("suite") or {}).get("after")
    L.append(f"- before (untouched worktree): {_counts(before.get('counts') if before else None)}"
             + (f" · evidence `{before.get('evidence_id')}` · exit {before.get('exit')}" if before else ""))
    L.append(f"- after (last commit): {_counts(after.get('counts') if after else None)}"
             + (f" · evidence `{after.get('evidence_id')}` · exit {after.get('exit')}" if after else ""))
    if before and before.get("counts", {}).get("failed_names"):
        L.append(f"- failing before: {', '.join(before['counts']['failed_names'])}")
    if after and after.get("counts", {}).get("failed_names"):
        L.append(f"- failing after: {', '.join(after['counts']['failed_names'])}")
    L.append("")
    L.append("## Findings")
    for f in findings:
        L.append("")
        L.append(f"### {f['finding_id']} — {f['severity']} — state `{f['state']}` (rounds {f.get('rounds', 0)})")
        L.append("")
        L.append(f"- Location: `{f['file']}:{f['line']}` · class hint `{f.get('class_hint') or '-'}`")
        L += _phase("RED", f.get("red"), read_evidence)
        L += _phase("GREEN", f.get("green"), read_evidence)
        rc = f.get("revert_check")
        if rc:
            L.append(f"- revert check: exit {rc.get('exit')} with the fix reverted (must be != 0) · restored exit "
                     f"{rc.get('restored_exit')} · evidence `{rc.get('evidence_id')}`")
        else:
            L.append("- revert check: (not run)")
        if f.get("commit_sha"):
            L.append(f"- fix commit `{f['commit_sha']}` · files: " + ", ".join(f"`{p}`" for p in f.get("commit_files") or []))
        sweep = f.get("sweep") or {}
        if sweep:
            sites = ", ".join(f"`{s['file']}:{s['line']}`" for s in sweep.get("sites") or []) or "(none listed)"
            L.append(f"- sweep ({sweep.get('class_hint') or '-'}): {sites} · evidence `{sweep.get('evidence_id')}`")
        for ct in f.get("changed_tests") or []:
            L.append(f"- changed test `{ct['path']}` — why sha256 `{ct['why_sha256']}`")
        d = f.get("disproof")
        if d:
            L.append(f"- DISPROOF — VERIFY: reproduction `{' '.join(d.get('reproduction_argv') or [])}` exit {d.get('exit')} · "
                     f"statement sha256 `{d.get('statement_sha256')}` · evidence `{d.get('evidence_id')}`")
        if f.get("suite_failures"):
            L.append("- suite failures blocking `fixed`: " + ", ".join(f"`{n}`" for n in f["suite_failures"]))
        for r in f.get("reasons") or []:
            L.append(f"- reason: {r.get('code')} — {r.get('message')}")
        a = f.get("agent") or {}
        if a:
            L.append(f"- agent: turns {a.get('turns', 0)} · tool calls {a.get('tool_calls', 0)} · tokens in/out "
                     f"{a.get('tokens_in', 0)}/{a.get('tokens_out', 0)}")
    L.append("")
    L.append("## Blocked / disproved")
    L.append("")
    bd = [f for f in findings if f["state"] in ("blocked", "disproved")]
    if not bd:
        L.append("- none")
    for f in bd:
        L.append(f"- {f['finding_id']}: {f['state']} — " + "; ".join(r.get("message", "") for r in f.get("reasons") or []))
    L.append("")
    L.append("## New defects noticed (suite failures not in the findings list)")
    L.append("")
    nd = sorted(set(run.get("new_defects") or []))
    if not nd:
        L.append("- none")
    for n in nd:
        L.append(f"- `{n}`")
    L.append("")
    L.append("## Commits")
    L.append("")
    for c in run.get("commits") or []:
        L.append(f"- `{c['sha']}` message sha256 `{c['message_sha256']}` · files: " + ", ".join(f"`{p}`" for p in c.get("files") or []))
    if not run.get("commits"):
        L.append("- none")
    L.append("")
    L.append("## Evidence")
    L.append("")
    for e in run.get("evidence") or []:
        L.append(f"- `{e['evidence_id']}` {e['kind']} sha256 `{e['sha256']}` ({e['bytes']} bytes)")
    L.append("")
    return "\n".join(L)
