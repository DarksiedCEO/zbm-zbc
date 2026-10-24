#!/usr/bin/env python3
"""Render a client Revenue Recovery report from one orchestrator-go scan result (ADR 0020 section 8).

usage: python3 devtools/rr_report.py SCAN.json --md OUT.md [--json OUT.json]

SCAN.json is the body of ``POST /revenue-recovery/scan?client_id=<client>`` for a connected store. A fixture scan,
or any scan that is not of a connected store, is refused (exit 2): fixture data is never reported to a client.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rr_report import ReportInputError, report_from_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scan")
    ap.add_argument("--md", required=True)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    try:
        rep, md = report_from_file(a.scan)
    except (ReportInputError, KeyError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    Path(a.md).write_text(md, encoding="utf-8")
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=2, sort_keys=True), encoding="utf-8")
    print(f"report written: {a.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
