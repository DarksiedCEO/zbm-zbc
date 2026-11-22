"""core-spec/1 M-3 (and U-2): every matrix cell that is STRICT R, placed (a) as a query value and (b) as one path
segment, raw and percent-encoded once: ``url_has_secret`` must be True (the URL is not fetched). Cells that are STRICT
O or X are not judged here. Ids and counts only."""
import random
from urllib.parse import quote

import pytest

from conformance import conftest as C
from conformance import core_matrix as M

pytestmark = pytest.mark.conformance
CI_CELLS = 5000
# what a raw URL cannot hold as written: its own structure characters, and white space, controls, format characters
_STRUCT = "/?#%&=+ "


def raw(cell: str) -> str:
    import unicodedata
    out = []
    for c in cell:
        if c in _STRUCT or unicodedata.category(c)[0] in "CZ":
            out.append(quote(c, safe=""))
        else:
            out.append(c)
    return "".join(out)


def urls(cell: str) -> list:
    r, e = raw(cell), quote(cell, safe="")
    return [("query.raw", f"https://example.test/search?q={r}"), ("query.enc", f"https://example.test/search?q={e}"),
            ("path.raw", f"https://example.test/notes/{r}/view"), ("path.enc", f"https://example.test/notes/{e}/view")]


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def _judge(sg, cells) -> dict:
    miss: dict = {}
    for c in cells:
        for form, u in urls(c.text):
            if not sg.url_has_secret(u):
                k = f"{c.eid}|SP{c.sp:02d}|SEP-{c.sep_class}|{form}"
                miss[k] = miss.get(k, 0) + 1
    return miss


def test_core_M_3_strict_r_cells_in_a_url_are_not_fetched(sg):
    """M-3: 5,000 STRICT-R cells of the CI sample (seeded) in four URL places each."""
    pool = [c for c in M.ci_cells() if c.strict == "R"]
    cells = random.Random(f"{M.SEED}:url").sample(pool, min(CI_CELLS, len(pool)))
    miss = _judge(sg, cells)
    print(f"M-3: cells={len(cells)} urls={4 * len(cells)} misses={sum(miss.values())}")
    assert not miss, dict(sorted(miss.items())[:40])


@pytest.mark.conformance_heavy
def test_core_M_3_heavy_every_strict_r_cell(sg):
    i, n = C.shard()
    miss = _judge(sg, (c for c in M.all_cells(shard=(i, n)) if c.strict == "R"))
    print(f"M-3 heavy shard {i}/{n}: misses={sum(miss.values())}")
    assert not miss, dict(sorted(miss.items())[:40])
