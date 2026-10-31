#!/usr/bin/env python3
"""Every Dockerfile base image is pinned by digest (red team Oct 30-31 2026, RTA-6; fix wave A).

The public stripe-gateway and orchestrator and the evidence ledger compiled their binaries on ``golang:<tag>`` and
``rust:<tag>`` builder images pinned by TAG only: a re-pushed tag would change the compiled binary of both
internet-facing services and of the ledger. This check fails on any ``FROM`` that is not ``<image>@sha256:<64 hex>``
(or ``@sha256:${ARG}`` whose digest a build argument supplies, as delivery-py's sandbox does) and is not a reference
to an earlier build stage.

    python3 devtools/check_docker_pins.py            # exit 1 and list every unpinned FROM
    python3 devtools/check_docker_pins.py --resolve  # also print `docker buildx imagetools inspect` commands

The digests could not be looked up from the sandbox that wrote this check (the registry was not reachable); the
first CI run lists the current digests (railway-images job, "Base image digests to pin") so they can be pasted in.
Dependabot (``.github/dependabot.yml``, ecosystem ``docker``) proposes digest bumps afterwards.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.I | re.M)
# AEGIS wave A F-4: images pulled by COPY --from=<image> and RUN --mount=...,from=<image> are base images too
COPY_FROM = re.compile(r"^\s*COPY\s+(?:--\S+\s+)*?--from=(\S+)", re.I | re.M)
MOUNT_FROM = re.compile(r"--mount=\S*?\bfrom=([^,\s]+)", re.I)
PINNED = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:(?:[0-9a-f]{64}|\$\{[A-Z_][A-Z0-9_]*\})$")


def dockerfiles(root: Path = REPO) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "node_modules" in p.parts or ".git" in p.parts or "target" in p.parts:
            continue
        n = p.name.lower()
        if n == "dockerfile" or n.startswith("dockerfile.") or n.endswith(".dockerfile"):
            out.append(p)
    return out


DIRECTIVE = re.compile(r"^#\s*([a-z]+)\s*=\s*(\S+)\s*$", re.I)


def _directives(text: str) -> dict:
    """Parser directives (``# syntax=``, ``# escape=``): only comment lines at the very top of the file count."""
    out: dict = {}
    for line in text.splitlines():
        m = DIRECTIVE.match(line.strip())
        if not m:
            break
        out.setdefault(m.group(1).lower(), m.group(2))
    return out


def _logical_lines(text: str) -> list[str]:
    """Dockerfile lines with continuations joined (AEGIS round 2 N-9: the escape character may be a backtick), and an
    ``ONBUILD`` prefix removed (its instruction pulls images when the image is used as a base)."""
    esc = _directives(text).get("escape", "\\")
    esc = esc if esc in ("\\", "`") else "\\"
    lines = text.splitlines(keepends=True)
    while lines and DIRECTIVE.match(lines[0].strip()):
        lines.pop(0)                 # a directive line is never continued (its own value may be the escape char)
    joined = re.sub(re.escape(esc) + r"[ \t]*\r?\n", " ", "".join(lines))
    return [re.sub(r"^\s*ONBUILD\s+", "", ln, flags=re.I) for ln in joined.splitlines()]


def unpinned(text: str) -> list[str]:
    """Every image reference (FROM, COPY --from, RUN --mount=from) that is neither pinned by digest nor an earlier
    build stage (by name or index) nor ``scratch``."""
    stages: set[str] = set()
    n_stages = 0
    bad = []

    def check(image: str) -> None:
        low = image.lower()
        if low in stages or image == "scratch" or (image.isdigit() and int(image) < n_stages):
            return
        if not PINNED.fullmatch(image):
            bad.append(image)

    syntax = _directives(text).get("syntax")
    if syntax:
        check(syntax)            # AEGIS round 2 N-9: the BuildKit frontend image is pulled like a base image
    for line in _logical_lines(text):
        m = FROM.match(line)
        if m:
            check(m.group(1))
            if m.group(2):
                stages.add(m.group(2).lower())
            n_stages += 1
            continue
        m = COPY_FROM.match(line)
        if m:
            check(m.group(1))
        if re.match(r"^\s*RUN\b", line, re.I):
            for mm in MOUNT_FROM.finditer(line):
                check(mm.group(1))
    return bad


def main(argv: list[str]) -> int:
    problems = []
    for p in dockerfiles():
        for image in unpinned(p.read_text(encoding="utf-8")):
            problems.append((p.relative_to(REPO), image))
    for path, image in problems:
        print(f"UNPINNED {path}: {image} (pin it as {image.split('@')[0]}@sha256:<digest>)")
        if "--resolve" in argv:
            print(f"    docker buildx imagetools inspect {image} --format '{{{{json .Manifest.Digest}}}}'")
        if "--print-digests" in argv:
            # CI (railway-images job, Docker present): resolve and print the exact line to paste; never fails here
            import subprocess
            r = subprocess.run(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest.Digest}}"],
                               capture_output=True, text=True, timeout=120)
            digest = r.stdout.strip().strip('"') if r.returncode == 0 else None
            print(f"    PASTE in {path}: {image}@{digest}" if digest else f"    could not resolve {image}: {r.stderr[:200]}")
    if "--print-digests" in argv:
        return 0
    if problems:
        print(f"check_docker_pins: {len(problems)} base image(s) not pinned by digest (red team RTA-6)")
        return 1
    print("check_docker_pins: every FROM is pinned by digest")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
