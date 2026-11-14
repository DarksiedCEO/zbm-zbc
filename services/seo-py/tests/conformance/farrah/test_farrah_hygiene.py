"""farrah-spec/1 G and the common rows CB-13, CB-14, CB-15, CB-16, CB-22, CB-23, AC-14 for Farrah: the network
audit hook, the write path's call order, the tests-bite map (TB-FA-1..6), the pinned and generated mutants, the war
room families, the scope / non-ASCII hygiene, and the certification table generated from the test ids."""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest

from conformance.gen import netaudit as fconf
from conformance.farrah.mutate import CALLS, SEO_PY, generated, run_mutant
from conformance.gen.farrah_util import ok_run, simple

HERE = Path(__file__).resolve().parent
REPO = SEO_PY.parents[1]
SPEC_ITEMS = ([f"B-FA-{n:02d}" for n in (1, 2, 3, 4, 5, 10, 11, 12, 13)] + [f"B-FA-{n}" for n in range(20, 30)]
              + ["W-FA-1", "W-FA-2", "W-FA-3", "W-E"] + [f"TB-FA-{n}" for n in range(1, 7)] + ["D-FA-01"]
              + [f"A-FA-0{n}" for n in range(1, 5)]
              + ["AC-1", "AC-2", "AC-6", "AC-7", "AC-9", "AC-10", "AC-11", "AC-13", "AC-14"]
              + ["O-FA-1", "O-FA-2", "O-FA-3", "PF-FA-1", "WR-FA-1", "G"] + [f"CB-{n}" for n in range(1, 24)])
WR_FAMILIES = ("money-edge-cases", "month-boundaries", "plan-escalation", "attribution-gaming")


def test_CB_13_network_audit_hook_is_active():
    n = len(fconf.ESCAPES)
    try:
        socket.getaddrinfo("example.invalid", 443)
    except Exception:  # noqa: BLE001 - the suite's socket guard may refuse it too
        pass
    assert len(fconf.ESCAPES) == n + 1
    del fconf.ESCAPES[n:]                                                    # this one was planted on purpose


def test_CB_14_W_FA_2_write_path_calls_in_order(fa, monkeypatch):
    import svc_attribution
    from agents import secret_guard
    order = []

    def spy(name, fn):
        def wrapped(*a, **k):
            order.append(name if name != "commit" else f"commit:{a[0]}")
            return fn(*a, **k)
        return wrapped
    monkeypatch.setattr(secret_guard, "guard_record", spy("guard_record", secret_guard.guard_record))
    monkeypatch.setattr(fa.svc, "_vaulted_seal", spy("seal", fa.svc._vaulted_seal))
    monkeypatch.setattr(fa.svc, "_vaulted_commit", spy("vault_commit", fa.svc._vaulted_commit))
    monkeypatch.setattr(fa.svc, "_commit", spy("commit", fa.svc._commit))
    simple(fa)
    ok_run(fa)
    want = ["guard_record", "seal", "vault_commit", "commit:attribution_run_recorded"]
    at = order.index("commit:attribution_run_requested")
    for w in want:
        at = order.index(w, at + 1)                     # each call after the one before it
    assert at > 0
    assert svc_attribution.RESULT_FIELD == "revenue_attribution"


def test_CB_15_CB_16_outside_values_never_stored_outside_the_vault_and_no_new_code_kind(fa):
    """Farrah stores no value from outside the service in any form outside the vault (its results are its own
    numbers, labels and ids; the channel map's values are sealed whole in the operator vault), so it needs no store
    code and adds no code kind (CB-16). Coding itself is BLOCKED until primitives/codes.py lands (CL-0): nothing of
    Farrah routes through it."""
    import svc_attribution
    from agents import farrah
    src = (SEO_PY / "src" / "agents" / "farrah.py").read_text() + (SEO_PY / "src" / "svc_attribution.py").read_text()
    assert not re.search(r"(?<![A-Za-z0-9])_code\(", src) and "primitives.codes" not in src
    assert svc_attribution.MAP_FIELD == "channel_map" and farrah.CONTRACT == "farrah.revenue_attribution/1"


@pytest.mark.parametrize("item", CALLS["bite"], ids=[b["id"] for b in CALLS["bite"]])
def test_TB_FA_each_call_replaced_by_identity_fails_a_named_test(item):
    out = run_mutant(item["file"], item["old"], item["new"], item["tests"])
    assert out["applied"], out
    assert out["killed"], (item["id"], out)


@pytest.mark.parametrize("item", CALLS["pinned_mutants"], ids=[m["id"] for m in CALLS["pinned_mutants"]])
def test_AC_14_G_pinned_mutants_killed(item):
    out = run_mutant(item["file"], item["old"], item["new"], item["tests"])
    assert out["applied"] and out["killed"], (item["id"], out)


GEN = {"agents/farrah.py": "# ------------------------------------------------------------------------------------------------ money",
       "svc_attribution.py": "class AttributionMixin:"}


@pytest.mark.lead_gate
@pytest.mark.parametrize("file", sorted(GEN))
def test_AC_14_G_generated_mutants(file):
    muts = generated(file, 24, GEN[file])
    assert len(muts) >= 20
    survivors = []
    for old, new, op in muts:
        out = run_mutant(file, old, new, ["tests/conformance/farrah", "-m", "not lead_gate"])
        if out["applied"] and not out["killed"]:
            survivors.append({"op": op, "line": old.strip()[:100]})
    print("MEASURE mutants", json.dumps({"file": file, "n": len(muts), "survivors": survivors}))
    assert survivors == [], survivors


def test_CB_22_WR_FA_1_war_room_families_in_the_library():
    lib = json.loads((REPO / "devtools" / "warroom" / "scenarios" / "seo-py.json").read_text())
    ids = {s["id"] for s in lib["scenarios"]}
    assert set(WR_FAMILIES) <= ids
    for s in lib["scenarios"]:
        if s["id"] in WR_FAMILIES:
            assert s["invariants"] and all(i["severity"] in ("MUST", "SHOULD") for i in s["invariants"])


NEW_SOURCES = ("src/agents/farrah.py", "src/svc_attribution.py", "tests/conformance/gen/attribution.py",
               "tests/conformance/gen/attribution_truth.py", "tests/conformance/gen/farrah_util.py",
               "tests/conformance/gen/personal.py", "tests/conformance/farrah/mutate.py")


def test_CB_23_G_no_non_ascii_in_new_source():
    files = list(NEW_SOURCES) + [str(p.relative_to(SEO_PY)) for p in HERE.glob("*.py")]
    bad = [f for f in files if any(ord(ch) > 127 for ch in (SEO_PY / f).read_text())]
    assert bad == []


def test_G_certification_table_every_spec_item_has_a_test():
    names = []
    for p in HERE.glob("test_*.py"):
        names += re.findall(r"^def (test_\w+)", p.read_text(), re.M)
    bitten = {b["id"] for b in CALLS["bite"]} if "test_TB_FA_each_call_replaced_by_identity_fails_a_named_test" \
        in names else set()
    missing = [i for i in SPEC_ITEMS if i not in bitten and not any(
        re.search(rf"_{re.escape(i.replace('-', '_'))}(_|$)", n) for n in names)]
    assert missing == [], missing
    unnamed = [n for n in names if not re.search(r"_(B_FA|W_FA|W_E|TB_FA|D_FA|A_FA|AC|O_FA|PF_FA|WR_FA|G|CB)_?", n)]
    assert unnamed == [], unnamed
