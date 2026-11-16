"""priya-spec/2 section 5 CODES (e) - NOT APPLICABLE, with evidence (card P2-8e; lead ruling 19:55 PT 2026-11-14).

No Priya feature compares two runs' withheld code sets, so "rows compared as R2 across a key rotation, never as
appeared / disappeared" has no subject today. This static test is the evidence: (1) the only modules that touch
withheld codes, withheld rows or codes_key_id are Priya's own two (they BUILD and RECORD, never compare); (2) no
module that defines a comparer (compare / diff) reads any of them; (3) codes_key_id is recorded on every run. ADR
line (P2-15): any future cross-run comparison must treat a codes_key_id change as R2 rotation, never as appeared /
disappeared queries - adding one makes this test fail on purpose, so the comparer gets its own card and tests."""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
TOKENS = (
    "withheld_rows",
    "withheld_items",
    "handoff_withheld_items",
    "codes_key_id",
    "c:query:",
)
OWNERS = {
    "agents/priya.py",
    "svc_keywords.py",
}  # build + record only (P2-8 finding, ruling 19:55)
COMPARER = ("compare", "diff")


def _modules():
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith(
            ("primitives/codes.py", "vault.py")
        ):  # Wave-1 coder itself, no Priya rows
            continue
        yield rel, p.read_text(encoding="utf-8")


def _comparer_names(text: str) -> list:
    tree = ast.parse(text)
    return [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(c in n.name.lower() for c in COMPARER)
    ]


def test_CODES_e_only_priya_modules_touch_withheld_codes():
    users = {rel for rel, text in _modules() if any(t in text for t in TOKENS)}
    assert users == OWNERS, sorted(users)


def test_CODES_e_no_comparer_reads_two_runs_code_sets():
    for rel, text in _modules():
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(c in fn.name.lower() for c in COMPARER):
                continue
            body = ast.get_source_segment(text, fn) or ""
            assert not any(t in body for t in TOKENS), (rel, fn.name)
    # Priya's own modules define no comparer at all
    for rel, text in _modules():
        if rel in OWNERS:
            assert _comparer_names(text) == [], (rel, _comparer_names(text))


def test_CODES_e_codes_key_id_is_recorded_on_every_run():
    priya = (SRC / "agents/priya.py").read_text(encoding="utf-8")
    svc = (SRC / "svc_keywords.py").read_text(encoding="utf-8")
    assert '"codes_key_id": None if coder is None else coder.key_id' in priya
    assert '"codes_key_id": ledger["gate"]["codes_key_id"]' in svc
