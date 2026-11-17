"""core-spec/1 O-4 I, O-4 MV, S-1: machine values of the sink classes' shapes: 0 differences vs ffd28ca8 (sink
class, strict, client view); ruling and invoice ids accepted by the sink guard. Counts only."""
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


def test_core_O_4_MV_machine_values_no_difference_vs_ffd28ca8(mods):
    items = C.machine()
    base = B.read("machine", "ffd28ca8", C.stream_hash(items))
    # FCL-CVR re-pin (core-spec/2 section 6 O-4 MV: "SINK lane (0 differences vs ffd28ca8; STRICT and CV columns
    # reported: CV now masks UUID / SHA by design)", lead ruling 20:40 PT, G-7 never weaken): the SINK class AND the
    # STRICT verdict stay asserted == ffd28ca8 for every value; only the CV column is REPORTED (evidence file), the CV
    # lane being judged by CV-8 (test_core_cv8; full pass lead 21:15 PT, cap 2.0%).
    diff: dict = {}
    cv_diff: dict = {}
    for (cls, v), b in zip(items, base):
        got = B.code(C.lanes_machine(mods, v)).split("|")
        want = b.split("|")
        assert len(got) == len(want) == 3, (cls, len(got), len(want))
        if got[:2] != want[:2]:
            diff[cls] = diff.get(cls, 0) + 1
        if got[2] != want[2]:
            cv_diff[cls] = cv_diff.get(cls, 0) + 1
    print(f"O-4 MV: values={len(items)} sink+strict differences vs ffd28ca8={diff}")
    E.report("O-4 MV cv differences vs ffd28ca8 (reported)", {"values": len(items), **cv_diff})
    assert not diff


def test_core_O_4_I_ruling_and_invoice_ids_accepted_by_the_sink_guard(mods):
    """O-4 I / S-1 at the guard: 2,000 ruling ids (compliance-py format) and 2,000 invoice ids (finance-py format)
    are machine values of their class and pass the ledger and log sinks (0 refused)."""
    import random

    import helpers
    import test_review_2b26d980 as r2b
    sink = mods["sink"]
    rng = random.Random(f"{C.SEED}:ids")
    rulings = [r2b.ruling_id(rng) for _ in range(2000)]
    invoices = [helpers.invoice_id(rng.randrange(1, 10 ** 9)) for _ in range(2000)]
    bad = {"ruling": sum(1 for x in rulings if sink.classify(x) != "ruling_id"),
           "invoice": sum(1 for x in invoices if sink.classify(x) != "invoice_id")}
    # C2-2e re-pin (CL-5, T-3): /id is not a field of audit_requested -> exactly the ``untyped`` refusal, no class one
    bad["violations"] = sum(1 for x in rulings + invoices
                            if sink.violations("audit_requested", {"id": x}) != [("/id", "untyped")])
    print(f"O-4 I: rulings={len(rulings)} invoices={len(invoices)} refused={bad}")
    assert bad == {"ruling": 0, "invoice": 0, "violations": 0}
