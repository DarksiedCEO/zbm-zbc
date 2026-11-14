"""core-spec/1 O-4 I, O-4 MV, S-1: machine values of the sink classes' shapes: 0 differences vs ffd28ca8 (sink
class, strict, client view); ruling and invoice ids accepted by the sink guard. Counts only."""
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


def test_core_O_4_MV_machine_values_no_difference_vs_ffd28ca8(mods):
    items = C.machine()
    base = B.read("machine", "ffd28ca8", C.stream_hash(items))
    diff: dict = {}
    for (cls, v), b in zip(items, base):
        if B.code(C.lanes_machine(mods, v)) != b:
            diff[cls] = diff.get(cls, 0) + 1
    print(f"O-4 MV: values={len(items)} differences vs ffd28ca8={diff}")
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
    bad["violations"] = sum(1 for x in rulings + invoices if sink.violations("audit_requested", {"id": x}))
    print(f"O-4 I: rulings={len(rulings)} invoices={len(invoices)} refused={bad}")
    assert bad == {"ruling": 0, "invoice": 0, "violations": 0}
