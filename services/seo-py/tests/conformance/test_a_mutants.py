"""A-31: the catalogued mutants (devtools/conformance/mutants.py). Fast tier: the catalogue is complete (>= 20 a
module, every equivalent mutant has its written proof). Lead gate: every behavioural mutant is killed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SVC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SVC / "devtools" / "conformance"))


def test_A_31_mutant_catalogue_complete():
    import mutants
    per = {}
    for m in mutants.MUTANTS:
        per[m["file"]] = per.get(m["file"], 0) + 1
        assert m["status"] in ("behavioural", "equivalent")
        if m["status"] == "equivalent":
            assert len(m.get("proof", "")) > 40, m["id"]
        src = (SVC / "src" / m["file"]).read_text(encoding="utf-8")
        assert src.count(m["anchor"]) == 1, (m["id"], "anchor must occur exactly once")
    for g, files in mutants.GROUPS.items():
        n = sum(per.get(f, 0) for f in files)
        assert n >= mutants.MIN_PER_MODULE.get(g, 20), (g, n)


@pytest.mark.lead_gate
def test_A_31_every_behavioural_mutant_is_killed(tmp_path):
    out = subprocess.run([sys.executable, str(SVC / "devtools" / "conformance" / "mutants.py"), "--work",
                          str(tmp_path)], capture_output=True, text=True, timeout=5400)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-2000:]
