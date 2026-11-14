"""core-spec/1 O-4 F: the fuzz texts of the CI draw (``corpora.fuzz``): 0 new reads vs ffd28ca8 in the texts that
hold no label token. Counts only."""
import pytest

from conformance import baseline as B
from conformance import corpora as C

pytestmark = pytest.mark.conformance


@pytest.fixture(scope="module")
def mods():
    from agents import callum
    from agents import secret_guard

    import sink_guard
    import svc_audits
    import svc_tenants
    return {"sg": secret_guard, "sink": sink_guard, "tenants": svc_tenants, "audits": svc_audits, "callum": callum}


def test_core_O_4_F_fuzz_no_new_read_without_a_label_token(mods):
    items = C.fuzz()
    base = B.read("fuzz", "ffd28ca8", C.stream_hash(items))
    new = {"strict": 0, "cv": 0}
    with_label = 0
    for (has, t), b in zip(items, base):
        if has:
            with_label += 1
            continue
        got = C.lanes_text(mods["sg"], t)
        for lane, g, x in zip(("strict", "cv"), got, b.split("|")):
            if g and x == "0":
                new[lane] += 1
    print(f"O-4 F: texts={len(items)} with a label token={with_label} new reads without one={new}")
    assert new == {"strict": 0, "cv": 0}
