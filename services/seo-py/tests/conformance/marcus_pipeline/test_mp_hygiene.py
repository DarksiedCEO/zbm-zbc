"""marcus-pipeline-spec/1 TB-MP-1..5 (tests bite), G with the pinned mutants (CB-23, AC-14), WR-MP-1 (CB-22), and the
certification table generated from the test ids (SUITE_PLAN 2.1)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conformance.marcus_pipeline.mutate import CALLS, SEO_PY, extras, generated, run_mutant

HERE = Path(__file__).resolve().parent
REPO = SEO_PY.parents[1]
SPEC_ITEMS = (["B-MP-01", "B-MP-02", "B-MP-03", "B-MP-04"] + [f"B-MP-{n}" for n in (10, 11, 12, 13, 15)]
              + [f"B-MP-{n}" for n in range(20, 28)] + [f"B-MP-{n}" for n in range(30, 37)]
              + [f"B-MP-{n}" for n in range(40, 44)] + ["D-MP-01", "D-MP-02", "W-MP-1", "W-MP-2"]
              + [f"TB-MP-{n}" for n in range(1, 6)] + [f"A-MP-0{n}" for n in range(1, 5)]
              + ["AC-1", "AC-2", "AC-3", "AC-4", "AC-6", "AC-9", "AC-10", "AC-12", "AC-13", "AC-14"]
              + ["O-MP-1", "O-MP-2", "O-MP-3", "WR-MP-1", "G"] + [f"CB-{n}" for n in range(1, 24)])
WR_FAMILIES = ("wat-hostility", "index-tamper-and-crash", "pii-in-urls-and-anchors")
SCOPE = ("src/marcus/__init__.py", "src/marcus/release_watcher.py", "src/marcus/wat_fetcher.py",
         "src/marcus/link_extractor.py", "src/marcus/index_builder.py", "src/marcus/graph_importer.py",
         "src/marcus/index_store.py")


def _ids():
    out = []
    for p in sorted(HERE.glob("test_*.py")):
        out += re.findall(r"^def (test_\w+)", p.read_text(), re.M)
    return out


def test_G_certification_table_every_item_has_a_test_and_every_test_names_an_item():
    ids = _ids()
    norm = lambda item: item.replace("-", "_")                                # noqa: E731
    missing = [i for i in SPEC_ITEMS if not any(f"_{norm(i)}_" in f"_{t}_" for t in ids)]
    assert missing == [], missing
    pat = re.compile(r"(B_MP_\d\d|D_MP_0\d|W_MP_\d|TB_MP_\d|A_MP_0\d|AC_\d+|O_MP_\d|WR_MP_1|CB_\d+|G)_")
    assert [t for t in ids if not pat.search(t[5:] + "_")] == []


@pytest.mark.parametrize("entry", CALLS["bite"], ids=[e["id"] for e in CALLS["bite"]])
def test_TB_MP_1_TB_MP_2_TB_MP_3_TB_MP_4_TB_MP_5_each_removed_guard_makes_its_named_test_fail(entry):
    r = run_mutant(entry["file"], entry["old"], entry["new"], entry["tests"], extra=extras(entry))
    assert r["applied"], r
    assert r["killed"], (entry["id"], r)


@pytest.mark.parametrize("entry", CALLS["pinned"], ids=[e["id"] for e in CALLS["pinned"]])
def test_G_AC_14_pinned_mutants_are_killed(entry):
    r = run_mutant(entry["file"], entry["old"], entry["new"], entry["tests"], extra=extras(entry))
    assert r["applied"], r
    assert r["killed"], (entry["id"], r)


@pytest.mark.lead_gate
@pytest.mark.parametrize("file", [s[len("src/"):] for s in SCOPE if not s.endswith("__init__.py")])
def test_G_AC_14_generated_mutants_lead(file):
    muts = generated(file, 24)
    assert len(muts) >= 20, len(muts)
    equivalent = {e["old"]: e for e in CALLS.get("equivalent", []) if e["file"] == file}
    tests = ["tests/conformance/marcus_pipeline"]
    survivors = []
    for old, new, op in muts:
        if old in equivalent:
            assert equivalent[old]["proof"]
            continue
        r = run_mutant(file, old, new, tests)
        if r["applied"] and not r["killed"]:
            survivors.append((op, old.strip()[:80]))
    assert survivors == [], survivors


def test_G_scope_new_modules_ascii_only_and_no_secret_shaped_literal():
    from agents import secret_guard
    for rel in SCOPE + ("src/svc_link_index.py",):
        text = (SEO_PY / rel).read_text(encoding="utf-8")
        assert text.isascii(), rel
    for p in HERE.glob("*.py"):
        assert p.read_text(encoding="utf-8").isascii(), p.name
    for rel in SCOPE:
        for s in re.findall(r'"([^"\n]{12,})"', (SEO_PY / rel).read_text()):
            if any(ch in s for ch in "[\\("):                 # a regular expression, not a value
                continue
            assert secret_guard.check(s) is None or s.startswith(("{", "marcus", "seo")), (rel, s[:20])


def test_G_adr_exists_and_names_the_spec():
    adrs = list((REPO / "docs" / "adr").glob("*marcus-pipeline*.md"))
    assert len(adrs) == 1
    text = adrs[0].read_text()
    assert "marcus-pipeline-spec/1" in text and "GP-4" in text and "INDEX_KEY_UNAVAILABLE" in text


@pytest.mark.parametrize("family", WR_FAMILIES)
def test_WR_MP_1_CB_22_war_room_families_present(family):
    lib = json.loads((REPO / "devtools" / "warroom" / "scenarios" / "seo-py.json").read_text())
    ids = {s["id"] for s in lib["scenarios"]}
    assert any(i == family or i.startswith(family + "-") for i in ids), family


@pytest.mark.lead_gate
def test_WR_MP_1_seeds_1_to_5_pass():
    for seed in range(1, 6):
        r = subprocess.run([sys.executable, str(REPO / "devtools" / "warroom" / "run.py"), "--department", "seo-py",
                            "--seed", str(seed)], cwd=REPO, capture_output=True, text=True, timeout=7200)
        assert r.returncode == 0, (seed, r.stdout[-2000:])


def test_CB_23_suite_twice_same_count_is_a_gate_step():
    """CB-23 G-1..G-9 are gate steps (two full runs, the CI lane twice, ruff, gitleaks, mutants): recorded in the
    build notes and the final report; this test pins the lane split they rely on."""
    conf = (HERE / "conftest.py").read_text()
    assert "CONFORMANCE_LEAD" in conf and "lead_gate" in conf
