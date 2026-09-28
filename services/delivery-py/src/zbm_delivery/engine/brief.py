"""
Brief compiler (spec §C.8.3; SP-01): the runner owns the header; the finding's free text (title, reproduction,
expected, observed) appears ONLY inside the ``--- BEGIN FINDING DATA (untrusted) --- … --- END FINDING DATA ---``
block, verbatim. The system prompt is assembled from ``prompts/`` (the fork) and the policy summary.
"""

from __future__ import annotations

import hashlib
import os
import shlex

DATA_BEGIN = "--- BEGIN FINDING DATA (untrusted) ---"
DATA_END = "--- END FINDING DATA ---"
SKILL_ORDER = ("engine.system.md", "test-driven-development.md", "systematic-debugging.md",
               "verification-before-completion.md", "executing-plans.md", "writing-plans.md")


def load_prompts(prompts_dir: str) -> dict[str, str]:
    out = {}
    for name in sorted(os.listdir(prompts_dir)):
        p = os.path.join(prompts_dir, name)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "r", encoding="utf-8") as fh:
                out[name] = fh.read()
    return out


def system_prompt(prompts: dict[str, str], *, service: str, policy_summary: str) -> str:
    parts = []
    for name in SKILL_ORDER:
        parts.append(prompts[name])
    env = ("\n\n# Environment\n\n"
           f"- The repository copy is at `/mnt/user-data/workspace`; the service is `services/{service}`.\n"
           "- Tools: `ls`, `read_file`, `glob`, `grep`, `write_file`, `str_replace`, `bash`. Paths are absolute under "
           "the workspace. `bash` runs in the sandbox with no network.\n"
           "- Every tool call is decided by the guardrail first; a denied call returns a refusal, not an error to work around.\n"
           f"\n# Tool policy in force\n\n{policy_summary}\n")
    return "\n\n".join(parts) + env


def policy_summary(seed: dict) -> str:
    lines = []
    for name, cls in seed["classes"].items():
        lines.append(f"- {name}: {cls.get('decision')}")
    return "\n".join(lines)


def data_block(finding: dict) -> str:
    """The untrusted block: labelled fields, verbatim. Control characters were refused at the schema edge."""
    rows = []
    for key in ("id", "severity", "title", "file", "line", "class_hint", "reproduction", "expected", "observed"):
        val = finding.get(key)
        if val is None:
            continue
        text = str(val)
        rows.append(f"{key}: {text}" if "\n" not in text else f"{key}: |\n" + "\n".join("  " + ln for ln in text.splitlines()))
    return "\n".join(rows)


def compile_brief(template: str, *, run: dict, finding: dict, round_no: int, max_rounds: int, test_argv: list[str],
                  suite_argv: list[str], engine_notes: str = "", repro_argv: list[str] | None = None) -> str:
    """``repro_argv`` (wave 21, R1): the seeded argv of the finding's own reproduction — built from the node id the
    engine extracted and validated (``<path>::<name>``, a closed character set), never the reviewer's free text."""
    body = template
    fields = {
        "{finding_id}": finding["id"], "{severity}": finding["severity"], "{run_id}": run["run_id"],
        "{service}": run["service"], "{base_sha}": run["base_sha"], "{base_ref}": run["base_ref"],
        "{round}": str(round_no), "{max_rounds}": str(max_rounds), "{file}": finding["file"],
        "{line}": str(finding["line"]), "{class_hint}": finding.get("class_hint") or "(none given)",
        "{test_argv}": " ".join(shlex.quote(a) for a in test_argv),
        "{suite_argv}": " ".join(shlex.quote(a) for a in suite_argv),
        "{repro_argv}": (" ".join(shlex.quote(a) for a in repro_argv) if repro_argv
                         else "(none: this finding names no runnable reproduction and cannot be fixed)"),
        "{data_block}": data_block(finding),
        "{engine_notes}": engine_notes.strip(),
    }
    for k, v in fields.items():
        body = body.replace(k, v)
    return body


def brief_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def outside_data_block(text: str) -> str:
    """Everything of a brief that is NOT inside the data block (test A6: the free text appears nowhere else)."""
    out, inside = [], False
    for ln in text.splitlines():
        if ln.strip() == DATA_BEGIN:
            inside = True
            continue
        if ln.strip() == DATA_END:
            inside = False
            continue
        if not inside:
            out.append(ln)
    return "\n".join(out)
