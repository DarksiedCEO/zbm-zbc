"""SUITE_PLAN registry (G-7, only growing): every priya-spec/1 item has a registered test, every registered test
exists, and the number of Priya conformance tests does not fall below the committed floor."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.conformance
HERE = Path(__file__).resolve().parent
SPEC_ITEMS = (["Z-1", "Z-2"] + [f"C{i}" for i in range(1, 8)] + [f"D{i:02d}" for i in range(1, 25)]
              + ["M-1", "M-2", "M-3", "CP-1", "CP-2", "CP-3", "CP-4", "LP-1", "LP-2", "LP-3", "OQ-1", "OQ-2", "OQ-3",
                 "OQ-4", "OQ-5", "OI-1", "OI-2", "OI-3", "PF-1", "PF-2", "PF-3", "G-6", "G-9"])


def _tests_in(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    if path.name == "test_priya_matrix.py":                     # one test per D item, defined in a loop
        names |= {f"test_D{i:02d}_matrix_{lane}" for i in range(1, 25) for lane in ("ci", "heavy")}
    return names


def test_registry_covers_every_priya_item_and_every_node_exists():
    reg = json.loads((HERE / "registry.json").read_text())
    assert reg["spec"] == "priya-spec/1"
    assert set(SPEC_ITEMS) <= set(reg["items"]), set(SPEC_ITEMS) - set(reg["items"])
    nodes = {n for v in reg["items"].values() for n in v}
    for node in nodes:
        f, name = node.split("::")
        assert name in _tests_in((HERE / f).resolve()), node
    count = sum(len(_tests_in(p)) for p in HERE.glob("test_priya_*.py"))
    assert count >= reg["floor"], (count, reg["floor"])
