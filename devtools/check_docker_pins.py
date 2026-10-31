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


def unpinned(text: str) -> list[str]:
    stages: set[str] = set()
    bad = []
    for m in FROM.finditer(text):
        image, alias = m.group(1), m.group(2)
        if image.lower() not in stages and image != "scratch" and not PINNED.fullmatch(image):
            bad.append(image)
        if alias:
            stages.add(alias.lower())
    return bad


def main(argv: list[str]) -> int:
    problems = []
    for p in dockerfiles():
        for image in unpinned(p.read_text(encoding="utf-8")):
            problems.append((p.relative_to(REPO), image))
    for path, image in problems:
        print(f"UNPINNED {path}: FROM {image} (pin it as {image.split('@')[0]}@sha256:<digest>)")
        if "--resolve" in argv:
            print(f"    docker buildx imagetools inspect {image} --format '{{{{json .Manifest.Digest}}}}'")
    if problems:
        print(f"check_docker_pins: {len(problems)} base image(s) not pinned by digest (red team RTA-6)")
        return 1
    print("check_docker_pins: every FROM is pinned by digest")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
