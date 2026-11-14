"""The reviewer's mutants (mutants_rv.MUTANTS, 39 at review + round 2's built-in-abs mutant) in the permanent suite:
each applied to a temporary copy of services/seo-py, Farrah's suite and the reviewer's suite run there; every one
must be killed. Lead lane (CONFORMANCE_LEAD=1): each mutant runs both suites."""

from __future__ import annotations

import json

import pytest

from conformance.farrah.mutate import run_mutant
from mutants_rv import MUTANTS

SMALL = {"RV_N": "300", "RV_FORGE_N": "200", "RV_AC1_N": "60", "RV_AC2_N": "40"}
PREFIX = "services/seo-py/src/"


@pytest.mark.lead_gate
def test_rv_mutants_all_killed():
    survivors, applied = [], 0
    for name, path, old, new in MUTANTS:
        out = run_mutant(path[len(PREFIX):], old, new, ["tests/conformance/farrah", "tests/conformance/farrah_rv",
                                                        "-m", "not lead_gate"], timeout=3000, env_extra=SMALL)
        applied += out["applied"]
        if not (out["applied"] and out["killed"]):
            survivors.append(name)
    print("MEASURE rv_mutants", json.dumps({"n": len(MUTANTS), "applied": applied, "survivors": survivors}))
    assert applied == len(MUTANTS) and survivors == []
