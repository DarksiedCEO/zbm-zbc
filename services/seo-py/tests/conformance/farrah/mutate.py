"""Mutation and tests-bite runner for Farrah's conformance suite (farrah-spec/1 G, TB-FA-1..6; SUITE_PLAN 2.1).

Each mutant is applied to a TEMPORARY COPY of services/seo-py (never to the tree under test), and the named tests run
there in a child pytest process (the fast lane). A mutant is KILLED when that run fails. The tree under test is
never modified. Kept beside the suite (not devtools/conformance/, which lands with the first round of the shared
tooling) so this round does not collide with the rounds running in parallel."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SEO_PY = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
CALLS = json.loads((HERE.parent / "calls_farrah.json").read_text())


def run_mutant(file: str, old: str, new: str, tests: list, timeout: int = 900) -> dict:
    """Apply ``old`` -> ``new`` (exactly one occurrence) to src/``file`` in a copy and run ``tests`` there."""
    tmp = Path(tempfile.mkdtemp(prefix="farrah-mut-"))
    try:
        dst = tmp / "seo-py"
        shutil.copytree(SEO_PY, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
        path = dst / "src" / file
        text = path.read_text()
        if text.count(old) != 1:
            return {"applied": False, "killed": False, "reason": f"old text occurs {text.count(old)} times"}
        path.write_text(text.replace(old, new))
        env = {k: v for k, v in os.environ.items() if k != "CONFORMANCE_LEAD"}
        proc = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
                              cwd=dst, env=env, capture_output=True, text=True, timeout=timeout)
        return {"applied": True, "killed": proc.returncode != 0, "rc": proc.returncode,
                "tail": proc.stdout.strip().splitlines()[-1:] if proc.stdout else []}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


OPERATORS = ((" <= ", " < "), (" >= ", " > "), (" == ", " != "), (" != ", " == "), (" and ", " or "),
             (" or ", " and "), ("if not ", "if "), ("return True", "return False"), ("return False", "return True"),
             (" < ", " <= "), (" > ", " >= "), ("Decimal(0)", "Decimal(1)"), (" + ", " - "))


def generated(file: str, n: int, start_marker: str = "") -> list:
    """``n`` deterministic operator mutants of src/``file``: one per line, spread over the code after
    ``start_marker`` (docstrings and comments skipped); each (old line, new line) is unique in the file."""
    lines = (SEO_PY / "src" / file).read_text().splitlines(keepends=True)
    text = "".join(lines)
    out, in_doc, started = [], False, not start_marker
    for line in lines:
        s = line.strip()
        if not started:
            started = start_marker in line
            continue
        if in_doc:
            in_doc = not s.endswith(('"""', "'''"))
            continue
        if s.startswith(('"""', "'''")):
            in_doc = not (len(s) > 3 and s.endswith(s[:3]))
            continue
        if not s or s.startswith("#") or text.count(line) != 1:
            continue
        code = line.split("  #")[0]
        for a, b in OPERATORS:
            if a in code and re.search(r"[A-Za-z_\)\]]", code):
                out.append((line, line.replace(a, b, 1), f"{a.strip()}->{b.strip()}"))
                break
    if len(out) <= n:
        return out
    step = len(out) / n
    return [out[int(i * step)] for i in range(n)]
