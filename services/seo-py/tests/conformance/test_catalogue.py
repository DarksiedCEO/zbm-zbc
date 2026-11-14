"""SUITE_PLAN 5: the suite only grows. (a) ids in catalogue.lock are in catalogue.json; (b) every entry names a test
that exists; (d) every SPEC item id is referenced by at least one conformance test name; (e) the SPEC N table covers
every item; no xfail / skip marker in the fast tier."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE.parents[1] / "docs" / "spec" / "wave1-spec-1.md"
ITEM = re.compile(r"^\| ((?:B-[A-Z]{2}|A|D|O)-\d\d) \|", re.M)


def _spec_items() -> list:
    ids = ITEM.findall(SPEC.read_text(encoding="utf-8"))
    ids += [f"D-0{i}" for i in range(1, 9)]
    return sorted(set(ids))


def _tests() -> dict:
    out = {}
    for f in HERE.glob("test_*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                out[f"{f.name}::{node.name}"] = node
    return out


def _names_ids(name: str) -> set:
    return {m.replace("_", "-") for m in re.findall(r"(?:B_[A-Z]{2}|A|D|O)_\d\d", name)}


def test_every_spec_item_has_a_test():
    have = set()
    for k in _tests():
        have |= _names_ids(k.split("::")[1])
    missing = [i for i in _spec_items() if i not in have]
    assert not missing, missing


def test_catalogue_entries_name_existing_tests_and_lock_is_a_subset():
    cat = json.loads((HERE / "catalogue.json").read_text(encoding="ascii"))
    tests = _tests()
    for e in cat["entries"]:
        assert e["test"] in tests, e["id"]
        assert e["status"] in ("live", "equivalent-with-proof")
        if e["status"] == "equivalent-with-proof":
            assert e.get("proof")
    lock = (HERE / "catalogue.lock").read_text(encoding="ascii").split()
    n, ids = int(lock[0]), lock[1:]
    assert len(ids) == n and set(ids) <= {e["id"] for e in cat["entries"]}
    assert len(cat["entries"]) >= n


def test_spec_n_table_covers_every_item():
    cat = json.loads((HERE / "catalogue.json").read_text(encoding="ascii"))
    assert set(_spec_items()) <= set(cat["spec_n"])


def test_no_xfail_or_skip_in_the_fast_tier():
    for k, node in _tests().items():
        marks = {ast.unparse(d) for d in node.decorator_list}
        if any("lead_gate" in m for m in marks):
            continue
        assert not any("xfail" in m or "skip" in m for m in marks), k
