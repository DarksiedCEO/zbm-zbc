"""PR #10 CI: finance's live run read ``journal_entries`` from ``GET /fin/v1/integrity`` with a caller token after AEGIS
r12 R12-1 made the counters Andre's (a non-Andre caller gets ``status`` and ``checked_at`` only). Every read of an
Andre-only answer in devtools/live_run.py must ask with Andre's token; this checks it without the ledger binary."""

from __future__ import annotations

import ast
from pathlib import Path

LIVE = Path(__file__).resolve().parents[1] / "devtools" / "live_run.py"
ANDRE_ONLY_READS = ("/fin/v1/audit/evidence", "/fin/v1/audit/export")
COUNTERS = ("journal_entries", "log_lines", "anchors", "problems")


def _gets(tree=None):
    for n in ast.walk(tree or ast.parse(LIVE.read_text())):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and n.args \
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            kw = {k.arg: k.value for k in n.keywords}
            andre = isinstance(kw.get("andre"), ast.Constant) and kw["andre"].value is True
            yield n, n.args[0].value, andre


def test_every_andre_only_read_in_the_live_run_carries_andres_token():
    bad = [(path, n.lineno) for n, path, andre in _gets() if path in ANDRE_ONLY_READS and not andre]
    assert not bad, bad


def test_integrity_counters_are_read_only_from_an_andre_answer():
    tree = ast.parse(LIVE.read_text())
    gets = list(_gets(tree))
    andre_vars = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            for call, path, andre in gets:
                if call is n.value and path == "/fin/v1/integrity" and andre:
                    andre_vars |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    reads = [(n.value.id, n.slice.value, n.lineno) for n in ast.walk(tree)
             if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
             and isinstance(n.slice, ast.Constant) and n.slice.value in COUNTERS]
    integ_reads = [r for r in reads if r[0].startswith("integ")]
    assert integ_reads, "the live run no longer reads integrity counters: update this test"
    assert all(v in andre_vars for v, _, _ in integ_reads), (integ_reads, andre_vars)
