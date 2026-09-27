"""
DEVTOOLS ONLY. Advisory gate (spec §C.7.3): runs ``pip-audit`` against ``uv export --frozen`` when the advisory
database is reachable. Unreachable (or pip-audit absent) → ``audit: not_run`` and exit 2 — the build is NOT green
and the report says so. Any advisory with a fix version available → exit 1. Nothing is installed by this script:
pip-audit must already be on PATH (it is not a runtime dependency of the service).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def main(argv: list[str]) -> int:
    out_path = os.path.join(ROOT, "docs", "evidence", "pip-audit.json")
    result: dict = {"audit": "not_run", "why": "", "advisories": []}
    uv = shutil.which("uv")
    if uv is None:
        result["why"] = "uv not on PATH"
    else:
        exp = subprocess.run([uv, "export", "--frozen", "--no-dev", "--no-hashes", "--format", "requirements-txt"],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
        if exp.returncode != 0:
            result["why"] = "uv export --frozen failed"
        else:
            req = os.path.join(ROOT, "docs", "evidence", "requirements.frozen.txt")
            os.makedirs(os.path.dirname(req), exist_ok=True)
            with open(req, "w", encoding="utf-8") as fh:
                fh.write(exp.stdout)
            pa = shutil.which("pip-audit")
            if pa is None:
                result["why"] = "pip-audit not on PATH (not a runtime dependency; install it in a tooling venv)"
            else:
                run = subprocess.run([pa, "-r", req, "--format", "json", "--progress-spinner", "off"], cwd=ROOT,
                                     capture_output=True, text=True, timeout=600)
                try:
                    data = json.loads(run.stdout or "{}")
                except ValueError:
                    data = {}
                deps = data.get("dependencies") if isinstance(data, dict) else None
                if deps is None:
                    result["why"] = f"pip-audit did not answer (rc={run.returncode}): advisory DB unreachable?"
                else:
                    result["audit"] = "run"
                    for d in deps:
                        for v in d.get("vulns") or []:
                            result["advisories"].append({"name": d.get("name"), "version": d.get("version"), "id": v.get("id"),
                                                         "fix_versions": v.get("fix_versions") or []})
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["audit"] != "run":
        return 2
    return 1 if any(a["fix_versions"] for a in result["advisories"]) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
