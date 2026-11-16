"""core-spec/2 ST-2: the SP matrix is REPORTED only (no pass/fail row).

The matrix DEFINITION is core-spec/1 sections 1-2 and P-1..P-6 (``core_matrix``: entries L-1..L-8, SP-01..40,
SEP-1..5, M-1..M-6). This job reads the same cells with the build's guard and with each reference release's guard
(``releases.export`` + ``load_guard``: git history, never vendored) and emits, per (entry, SP, SEP class, lane):

  cells, required verdicts (R / N / X counts), reads at the build and at every reference, and per reference
  LOST (read at the reference, not at the build) and NEW (read at the build, not at the reference).

Ids and counts only: a cell text is never written. Nothing here asserts a read; ``test_core_sp_report.py`` checks
only that the table is complete and self-consistent.

Usage (cwd services/seo-py, src and tests on the path as in the suite):
  python -m conformance.sp_report --out DIR [--per-triple 10] [--only-sp 1,2] [--refs 3120f0b0,ffd28ca8,cbdf5e78]
  [--heavy --shard i/n]
"""
import argparse
import json
import sys
from pathlib import Path

from conformance import core_matrix as M

REFS = ("3120f0b0", "ffd28ca8", "cbdf5e78")
LANES = ("strict", "cv")


def build_guard():
    from agents import secret_guard
    return secret_guard


def ref_guards(refs=REFS) -> dict:
    """{sha: module or None}; None when the history is absent (shallow checkout): the column reads ``absent``."""
    from conformance import releases
    out = {}
    for sha in refs:
        tree = releases.export(sha)
        out[sha] = None if tree is None else releases.load_guard(tree, f"sp_report_sg_{sha}")
    return out


def reads(sg, text: str) -> dict:
    """The two lanes' reads of one text by one guard module: {lane: bool or None (lane absent in that release)}."""
    strict = sg.check(text) == "a_label"
    cvf = getattr(sg, "client_view_findings", None)
    cv = None if cvf is None else "label_value" in cvf(text)
    return {"strict": strict, "cv": cv}


def _row() -> dict:
    return {"cells": 0, "R": 0, "N": 0, "X": 0, "O": 0, "read": {}, "lost": {}, "new": {}}


def table(cells, build, refs: dict) -> dict:
    """{"rows": {"eid|SPnn|SEP-c|lane": row}, "totals": {lane: row}}; refs: {sha: module or None}."""
    rows: dict = {}
    totals = {lane: _row() for lane in LANES}
    for c in cells:
        got = {"build": reads(build, c.text)}
        for sha, sg in refs.items():
            if sg is not None:
                got[sha] = reads(sg, c.text)
        for lane, v in (("strict", c.strict), ("cv", c.cv)):
            k = f"{c.eid}|SP{c.sp:02d}|SEP-{c.sep_class}|{lane}"
            for r in (rows.setdefault(k, _row()), totals[lane]):
                _add(r, v, lane, got, refs)
    return {"rows": rows, "totals": totals}


def _add(r: dict, v: str, lane: str, got: dict, refs: dict) -> None:
    r["cells"] += 1
    vv = v.split(":")[0]
    r[vv if vv in ("R", "N", "X") else "O"] += 1
    b = got["build"][lane]
    for name in ("build", *refs):
        if name not in got or got[name][lane] is None:      # release or lane absent: never counted as a read
            r["read"].setdefault(name, "absent")
            continue
        x = got[name][lane]
        prev = r["read"].get(name, 0)
        r["read"][name] = (prev if isinstance(prev, int) else 0) + int(x)
        if name == "build" or b is None:
            continue
        r["lost"][name] = r["lost"].get(name, 0) + int(x and not b)
        r["new"][name] = r["new"].get(name, 0) + int(b and not x)


def markdown(res: dict, refs) -> str:
    """The report table (one line per (entry, SP, SEP class, lane), then the per-lane totals)."""
    head = ["key", "cells", "R", "N", "X", "O", "read@build"] + [f"read@{s}" for s in refs]
    head += [f"LOST@{s}" for s in refs] + [f"NEW@{s}" for s in refs]
    lines = ["# SP matrix report (core-spec/2 ST-2; REPORTED only, no pass/fail)", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]

    def line(k, r):
        cols = [k.replace("|", "/"), r["cells"], r["R"], r["N"], r["X"], r["O"], r["read"].get("build", 0)]
        cols += [r["read"].get(s, "absent") for s in refs]
        cols += [r["lost"].get(s, "-") for s in refs] + [r["new"].get(s, "-") for s in refs]
        return "| " + " | ".join(str(x) for x in cols) + " |"

    for k in sorted(res["rows"]):
        lines.append(line(k, res["rows"][k]))
    lines.append(line("TOTAL strict", res["totals"]["strict"]))
    lines.append(line("TOTAL cv", res["totals"]["cv"]))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-triple", type=int, default=10)
    ap.add_argument("--only-sp", default="")
    ap.add_argument("--refs", default=",".join(REFS))
    ap.add_argument("--heavy", action="store_true")
    ap.add_argument("--shard", default="")
    a = ap.parse_args(argv)
    only = {int(x) for x in a.only_sp.split(",") if x} or None
    refs = tuple(x for x in a.refs.split(",") if x)
    if a.heavy:
        shard = tuple(int(x) for x in a.shard.split("/")) if a.shard else None
        cells = M.all_cells(only_sp=only, shard=shard)
    else:
        cells = M.ci_cells(per_triple=a.per_triple, only_sp=only)
    res = table(cells, build_guard(), ref_guards(refs))
    res["meta"] = {"spec": "core-spec/2 ST-2", "mode": "heavy" if a.heavy else "ci", "per_triple": a.per_triple,
                   "only_sp": sorted(only) if only else "all", "shard": a.shard or "none", "refs": list(refs)}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "sp_report.json").write_text(json.dumps(res, sort_keys=True, indent=1), encoding="ascii")
    (out / "sp_report.md").write_text(markdown(res, refs), encoding="ascii")
    t = res["totals"]
    print(json.dumps({lane: {k: t[lane][k] for k in ("cells", "read", "lost", "new")} for lane in LANES},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
