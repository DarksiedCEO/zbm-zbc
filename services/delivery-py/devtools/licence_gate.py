"""
DEVTOOLS ONLY (also run at start by ``gate.py`` and as test G12). Licence gate over an installed site-packages
(spec §C.7.2): prints the report, writes it as JSON under ``docs/evidence/licences-<date>.json`` when ``--write``
is given, exits 1 on any problem. ``--site-packages <dir>`` overrides the running interpreter's.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from zbm_delivery import licences  # noqa: E402
from zbm_delivery.gate import site_packages_dir  # noqa: E402


def main(argv: list[str]) -> int:
    sp = site_packages_dir()
    if "--site-packages" in argv:
        sp = argv[argv.index("--site-packages") + 1]
    with open(os.path.join(ROOT, "seed", "licence_allowlist.json"), "rb") as fh:
        allow = json.loads(fh.read())
    with open(os.path.join(ROOT, "seed", "licence_exceptions.json"), "rb") as fh:
        exc = json.loads(fh.read())
    rep = licences.check(sp, allow, exc)
    out = rep.as_dict()
    out["generated_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    out["python"] = sys.version.split()[0]
    text = json.dumps(out, indent=2, sort_keys=True) + "\n"
    if "--write" in argv:
        path = os.path.join(ROOT, "docs", "evidence", f"licences-{out['generated_at'][:10]}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"written {path}")
    print(f"{len(rep.dists)} distributions in {sp}; ok={rep.ok}")
    for p in rep.problems:
        print(f"  PROBLEM {p}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
