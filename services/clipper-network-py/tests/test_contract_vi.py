"""AEGIS N16-6: Clipper Network's V&I thin client against V&I's real app (in-process TestClient transport, see
tests/contract_vi_runner.py). A wire-shape drift on either side fails here."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RUNNER = Path(__file__).resolve().parent / "contract_vi_runner.py"


def test_cn_vi_client_matches_the_real_vi_app():
    r = subprocess.run([sys.executable, str(RUNNER)], capture_output=True, text=True, timeout=600,
                       cwd=str(RUNNER.parent))
    assert r.returncode == 0, r.stderr[-6000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    failed = [c for c in out["checks"] if not c["ok"]]
    assert not failed, json.dumps(failed, indent=1)
    assert len(out["checks"]) >= 25
    assert set(out["protocol"]) <= set(out["methods"]), "every V&I port method must be exercised against V&I"
