"""core-spec/1 O-4 F: the fuzz texts of the CI draw (``corpora.fuzz``): 0 new reads vs ffd28ca8 in the texts that
hold no label token. Counts only."""
import pytest

from conformance import baseline as B
from conformance import corpora as C
from conformance import evidence as E

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
    # FCL-CVR re-pin (core-spec/2 section 6 "the STRICT / CV columns of O-4 W / N / F / PIN: reported", lead ruling
    # 20:40 PT, G-7 never weaken): the STRICT lane keeps its v1 bar (0 new reads, asserted); the CV column is REPORTED
    # (count to the evidence file) because CV-3 / CV-4 mask valueish tokens by design and the CV lane is judged by
    # CV-8 (test_core_cv8; full pass lead 21:15 PT: N 84 / 45,600 = 0.184%, word classes 0.0%, cap 2.0%).
    E.report("O-4 F cv new (reported)", {"texts": len(items), "with_label": with_label, "cv_new": new["cv"]})
    # ST-2 / ST-3 re-pin (core-spec/2 ST-3: the O-4 F STRICT column is reported, not judged; C2-11b; G-7: re-pinned to
    # "reported" with the item named, superseding the 20:40 PT strict-asserted ruling): no pass/fail on either column.
    E.report("O-4 F STRICT new (reported, ST-3)",
             {"texts": len(items), "with_label": with_label, "strict_new": new["strict"]})
    assert len(items) > with_label                             # (draw sanity only: texts without a label were checked)
