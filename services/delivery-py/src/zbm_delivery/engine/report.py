"""
Report writer (spec §C.8.6): ``report-<run_id>.md`` written by the RUNNER from its records — evidence ids, exit
codes, the runner's verified counts, commit shas and file lists from the diff. The agent's prose is never copied in
(A5: the model's own "100/100 passed" string cannot appear). Round 18 R10: captured output is embedded inside a
fence longer than the longest backtick run in the content (a test's stdout cannot close the fence and inject report
structure); sweep sites are the validated ones (dropped ones are counted with the reason); every number traces to
a ledger event id (the agent line cites its ``agent_usage`` event).
"""

from __future__ import annotations

import re
from typing import Callable

TAIL_LINES = 200
_BACKTICKS = re.compile(r"`+")


def _tail(text: str, n: int = TAIL_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def fence_for(text: str) -> str:
    """A backtick fence strictly longer than any backtick run in ``text`` (at least three)."""
    longest = max((len(m.group(0)) for m in _BACKTICKS.finditer(text)), default=0)
    return "`" * max(3, longest + 1)


def _counts(c: dict | None) -> str:
    if not c:
        return "(not run)"
    return (f"{c.get('passed', 0)} passed / {c.get('failed', 0)} failed / {c.get('errors', 0)} errors / "
            f"{c.get('skips', 0)} skips · status {c.get('status', 'unknown')} ({c.get('why', '-')})")


def _phase(name: str, rec: dict | None, read_evidence: Callable[[str], str]) -> list[str]:
    if not rec:
        return [f"- {name}: (not run)"]
    out = [f"- {name}: exit {rec.get('exit')} · verdict {rec.get('verdict', 'unknown')} · evidence `{rec.get('evidence_id')}` · "
           f"argv `{' '.join(rec.get('argv') or [])}`"]
    ev = rec.get("evidence_id")
    if ev:
        text = read_evidence(ev)
        if text:
            body = _tail(text)
            fence = fence_for(body)
            out.append("")
            out.append(fence + "text")
            out.append(body)
            out.append(fence)
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
            L.append(f"- revert check: exit {rc.get('exit')} with the fix reverted (must be != 0; verdict {rc.get('verdict', '-')}) · "
                     f"restored exit {rc.get('restored_exit')} (verdict {rc.get('restored_verdict', '-')}) · evidence `{rc.get('evidence_id')}`")
        else:
            L.append("- revert check: (not run)")
        v = f.get("verification")
        if v:
            cl = v.get("classification") or {}
            L.append(f"- split-diff verification (base `{v.get('base_sha')}`): src {', '.join(f'`{p}`' for p in cl.get('src') or []) or '-'} · "
                     f"test {', '.join(f'`{p}`' for p in cl.get('test') or []) or '-'} · test-infra {', '.join(cl.get('test_infra') or []) or 'none'} · "
                     f"agent tree {v.get('agent_tree', {}).get('verdict')} · verification checkout {v.get('verification_checkout', {}).get('verdict')} · "
                     f"reverted checkout {v.get('reverted_checkout', {}).get('verdict')}")
        sf = f.get("single_file_revert")
        if sf:
            L.append(f"- single-file revert (everything except `{sf.get('file')}`): exit {sf.get('exit')} · verdict {sf.get('verdict', '-')} "
                     f"(must be fail{'; identical to the reverted checkout' if sf.get('identical_to_reverted') else ''}) · evidence `{sf.get('evidence_id')}`")
        rp = f.get("repro_check")
        if rp:
            L.append(f"- finding's reproduction `{rp.get('target')}`: verification checkout {rp.get('verification', {}).get('verdict')} · "
                     f"reverted {rp.get('reverted', {}).get('verdict')} · evidence `{rp.get('verification', {}).get('evidence_id')}`")
        so = f.get("src_only_check")
        if so:
            L.append(f"- src-only check ({', '.join(f'`{t}`' for t in so.get('targets') or [])}): verdict {so.get('verdict')} · evidence `{so.get('evidence_id')}`")
        if f.get("commit_sha"):
            L.append(f"- fix commit `{f['commit_sha']}` · files: " + ", ".join(f"`{p}`" for p in f.get("commit_files") or [])
                     + f" · tree sha256 `{f.get('commit_tree_sha256')}` (suite ran on `{f.get('suite_tree_sha256')}`)")
        sweep = f.get("sweep") or {}
        if sweep:
            sites = ", ".join(f"`{s['file']}:{s['line']}`" for s in sweep.get("sites") or []) or "(none listed)"
            dropped = sweep.get("dropped") or []
            note = f" · dropped {len(dropped)} sweep site(s) not in the diff" if dropped else ""
            L.append(f"- sweep ({sweep.get('class_hint') or '-'}): {sites} · evidence `{sweep.get('evidence_id')}`{note}")
        for ct in f.get("changed_tests") or []:
            why = ct.get("why")
            L.append(f"- changed test `{ct['path']}` — why sha256 `{ct['why_sha256']}`" + (" · the engineer's reason (untrusted text):" if why else ""))
            if why:
                fence = fence_for(why)
                L.append("")
                L.append(fence + "text")
                L.append(why)
                L.append(fence)
        for r in f.get("outcome_regressions") or []:
            L.append(f"- outcome regression: `{r.get('test')}` was {r.get('baseline')} at baseline, {r.get('after')} after")
        d = f.get("disproof")
        if d:
            L.append(f"- DISPROOF — VERIFY: the finding's reproduction `{' '.join(d.get('reproduction_argv') or [])}` on base "
                     f"`{d.get('base_sha')}`: exit {d.get('exit')}, verdict {d.get('verdict')} · "
                     f"statement sha256 `{d.get('statement_sha256')}` · evidence `{d.get('evidence_id')}`")
        if f.get("suite_failures"):
            L.append("- suite failures blocking `fixed`: " + ", ".join(f"`{n}`" for n in f["suite_failures"]))
        for r in f.get("reasons") or []:
            L.append(f"- reason: {r.get('code')} — {r.get('message')}")
        a = f.get("agent") or {}
        if a and a.get("event_id"):
            L.append(f"- agent: turns {a.get('turns', 0)} · tool calls {a.get('tool_calls', 0)} · opaque exec {a.get('opaque_execs', 0)} · "
                     f"denies {a.get('denies', 0)} · tokens in/out {a.get('tokens_in', 0)}/{a.get('tokens_out', 0)} · "
                     f"ledger event `{a.get('event_id')}`")
    L.append("")
    L.append("## Blocked")
    L.append("")
    bl = [f for f in findings if f["state"] == "blocked"]
    if not bl:
        L.append("- none")
    for f in bl:
        L.append(f"- {f['finding_id']}: blocked — " + "; ".join(r.get("message", "") for r in f.get("reasons") or []))
    L.append("")
    L.append("## Disproved (reviewer: re-run the reproduction argv on the base sha)")
    L.append("")
    ds = [f for f in findings if f["state"] == "disproved"]
    if not ds:
        L.append("- none")
    for f in ds:
        d = f.get("disproof") or {}
        L.append(f"- {f['finding_id']}: reproduction argv `{' '.join(d.get('reproduction_argv') or [])}` (target `{d.get('target')}`) on base "
                 f"`{d.get('base_sha')}` → exit {d.get('exit')}, verdict {d.get('verdict')} · evidence `{d.get('evidence_id')}` · "
                 f"statement sha256 `{d.get('statement_sha256')}` · the engineer's own argv "
                 f"`{' '.join(d.get('agent_argv_ignored') or [])}` was ignored (R3)")
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
