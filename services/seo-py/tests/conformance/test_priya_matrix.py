"""priya-spec/1 section 2 (D01-D24 x X0-X12 x Y; M-1, M-3, Z-1 at the function level): every cell's values are
dropped, raw and stored key. CI lane: N = 5 per cell; heavy lane: N = 200 per cell. One test per D item."""

from __future__ import annotations

import collections

import priya_gen as g
import pytest
from priya_common import define, dropped

D_IDS = tuple(f"D{i:02d}" for i in range(1, 25))
N_CI, N_HEAVY = 5, 200


def _run(d: str, n: int):
    cells = [c for c in g.cells() if c[1] == d]
    assert cells, d
    kept = collections.Counter()
    total = 0
    for cell in cells:
        for k in range(n):
            total += 1
            if not dropped(g.make(cell, g.cell_rng(cell, k))):
                kept[":".join(cell)] += 1
    assert total == n * len(cells)
    assert sum(kept.values()) == 0, (d, total, dict(kept))     # ids and counts only


for _d in D_IDS:
    define(globals(), f"test_{_d}_matrix_ci", lambda d=_d: _run(d, N_CI), (pytest.mark.conformance,))
    define(globals(), f"test_{_d}_matrix_heavy", lambda d=_d: _run(d, N_HEAVY), (pytest.mark.conformance_heavy,))


@pytest.mark.conformance
def test_M1_matrix_shape():
    cells = g.cells()
    assert len(cells) == len(set(cells)) >= 1250
    assert {c[0] for c in cells} == set(g.CLASSES)
    assert {c[1] for c in cells} == set(D_IDS)
    assert {c[2] for c in cells} == set(g.CONTEXTS)
    assert len(cells) * N_HEAVY >= 250000


@pytest.mark.conformance
def test_M1_generators_are_pure_functions_of_seed_and_index():
    cells = g.cells()[::97]
    a = [g.make(c, g.cell_rng(c, 3)) for c in cells]
    b = [g.make(c, g.cell_rng(c, 3)) for c in cells]
    assert a == b


@pytest.mark.conformance
def test_M3_Z1_every_cell_alone_and_in_context_drops_ci():
    kept = [":".join(c) for c, k, q in g.matrix(1) if not dropped(q)]
    assert kept == [], kept[:20]
