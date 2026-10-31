#!/usr/bin/env python3
"""Vulnerability review of git-pinned dependencies (red team Oct 30-31 2026, RTA-8; fix wave A).

delivery-py installs deer-flow from a git commit (``[tool.uv.sources]`` in services/delivery-py/pyproject.toml).
pip-audit refuses URL requirements, so CI's pip-audit step excludes it and nothing reviewed it. This asks OSV
(https://api.osv.dev/v1/query, the same database osv-scanner uses) for every advisory affecting that exact commit
of that repository, and fails when one is returned. It needs no extra tool on the runner (urllib only).

    python3 devtools/osv_git_audit.py services/delivery-py/pyproject.toml
    python3 devtools/osv_git_audit.py --print-queries services/delivery-py/pyproject.toml   # no network

OSV matches a commit against the GIT ranges of advisories; a repository with no OSV advisories answers ``{}``. The
review is also a human one: docs/ci.md "delivery-py: deer-flow" says what to check when the pinned commit moves.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

OSV_URL = "https://api.osv.dev/v1/query"
_SHA = re.compile(r"[0-9a-f]{40}")


def git_pins(pyproject: Path) -> list[tuple[str, str, str]]:
    """(package, repository URL, commit) for every git source pinned by a full commit SHA."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    sources = data.get("tool", {}).get("uv", {}).get("sources", {}) or {}
    out = []
    for name, src in sorted(sources.items()):
        if isinstance(src, dict) and "git" in src:
            rev = str(src.get("rev", ""))
            if not _SHA.fullmatch(rev):
                raise SystemExit(f"{name}: git source is not pinned to a full commit SHA ({rev!r})")
            out.append((name, src["git"], rev))
    return out


def query(commit: str) -> dict:
    return {"commit": commit}


def ask(commit: str, timeout: float = 30.0) -> list[dict]:
    req = urllib.request.Request(OSV_URL, data=json.dumps(query(commit)).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - fixed https URL
        body = json.loads(resp.read() or b"{}")
    return list(body.get("vulns") or [])


def main(argv: list[str]) -> int:
    dry = "--print-queries" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")] or [Path("services/delivery-py/pyproject.toml")]
    found = 0
    for p in paths:
        commits = {}
        for name, url, rev in git_pins(p):
            commits.setdefault(rev, []).append((name, url))
        for rev, pkgs in commits.items():
            names = ", ".join(n for n, _ in pkgs)
            if dry:
                print(f"{p}: {names} @ {pkgs[0][1]} {rev}: POST {OSV_URL} {json.dumps(query(rev))}")
                continue
            vulns = ask(rev)
            if vulns:
                found += len(vulns)
                for v in vulns:
                    print(f"VULNERABLE {names} @ {rev}: {v.get('id')} {v.get('summary', '')[:160]}")
            else:
                print(f"ok {names} @ {rev}: no OSV advisory affects this commit")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
