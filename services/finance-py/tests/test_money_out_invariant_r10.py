"""AEGIS r10 R10-1 invariant (ADR 0024): every route that sends a client's money out, or commits it, asks Compliance.

Every state-changing Finance route is classified below as MONEY_OUT (sends, spends or commits a client's money: it
must reach Compliance's client activation, ``_activation``), MONEY_IN_GATED (applies a client's money: it must reach
it too, RTM-01) or NOT_CLIENT_MONEY (with the reason). A new route that is in none of the tables fails the first test,
so it cannot skip the question by being forgotten; a classified route whose service method no longer reaches
``_activation`` (statically, through ``self.<method>`` references across the service mixins) fails the second.
The behaviour behind each MONEY_OUT route is exercised end to end in test_compliance_payouts_r10.py,
test_compliance_outflows_r9.py and test_compliance_bank_rtm.py; this file guards the inventory.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SERVICE_MODULES = ("service.py", "svc_books.py", "svc_media.py", "svc_payables.py", "svc_payees.py", "svc_payouts.py",
                   "svc_recon.py", "svc_stripe.py")
TARGET = "_activation"

MONEY_OUT = {
    ("POST", "/fin/v1/payout-handoffs"): "a creator accrual spends the brand's deposit (F2)",
    ("POST", "/fin/v1/payout-runs"): "proposes paying creators from brands' deposits",
    ("POST", "/fin/v1/payout-batches/{batch_id}/decision"): "Andre commits the batch (OFAC warning shown here)",
    ("POST", "/fin/v1/payout-batches/{batch_id}/second-approval"): "the second approver commits the batch",
    ("POST", "/fin/v1/payout-batches/{batch_id}/release"): "sends creators the money",
    ("POST", "/fin/v1/treasury/funding"): "moves restricted cash to the rail for a batch",
    ("POST", "/fin/v1/treasury/funding/{op_id}/decision"): "Andre approves and executes the rail funding",
    ("POST", "/fin/v1/refunds/{campaign_id}"): "proposes returning a deposit",
    ("POST", "/fin/v1/refunds/{refund_id}/decision"): "Andre approves and pays a deposit refund",
    ("POST", "/fin/v1/refunds/{refund_id}/repay"): "Andre pays a deposit refund again",
    ("POST", "/fin/v1/media-buys/{buy_id}/vendor-payments"): "pays a media vendor from the client's prepayment",
    ("POST", "/fin/v1/invoices/{invoice_id}/compliance-hold/release"): "releases held money to the client's account",
    ("POST", "/fin/v1/jobs/{job}/run"): "the accrual job accrues; rail-sync retries transfers; stripe-sessions "
                                        "releases holds",
}
MONEY_IN_GATED = {
    ("POST", "/fin/v1/bank/events"): "applies a wire or ACH to an invoice (RTM-01)",
    ("POST", "/fin/v1/receipts/{receipt_id}/apply"): "Andre applies unapplied money to an invoice (RTM-01)",
    ("POST", "/fin/v1/stripe/events"): "applies a Stripe payment",
    ("POST", "/fin/v1/invoices/{invoice_id}/decision"): "Andre issues an invoice",
    ("POST", "/fin/v1/invoices/{invoice_id}/stripe-checkout"): "opens a payment page",
}
NOT_CLIENT_MONEY = {
    ("POST", "/fin/v1/payees"): "creates a creator payee record",
    ("POST", "/fin/v1/payees/{payee_id}/offboarding-notices"): "payee lifecycle",
    ("POST", "/fin/v1/payees/{payee_id}/callbacks"): "payee callback confirmation",
    ("POST", "/fin/v1/payees/{payee_id}/tax/b-notices"): "tax notice",
    ("POST", "/fin/v1/rate-cards/proposals"): "rate card text",
    ("POST", "/fin/v1/rate-cards/decisions"): "rate card approval",
    ("PUT", "/fin/v1/campaigns/{campaign_id}/commercial-profile"): "profile terms (funding happens on a matched "
                                                                     "deposit, which is gated)",
    ("PUT", "/fin/v1/clients/{client_id}/billing-profile"): "billing terms",
    ("POST", "/fin/v1/invoices"): "a draft (issuing it is gated)",
    ("POST", "/fin/v1/media-buys"): "a draft buy and its invoice (issuing it is gated)",
    ("POST", "/fin/v1/media-buys/{buy_id}/delivery"): "records the vendor's delivery",
    ("POST", "/fin/v1/media-buys/{buy_id}/cancel"): "voids an UNPAID buy (no money moves)",
    ("POST", "/fin/v1/client-receipts/{client_receipt_id}/send"): "a document",
    ("POST", "/fin/v1/receipts/{receipt_id}/return"): "records a return the BANK made (a fact); a sanctions-held "
                                                       "one is flagged OFAC (RTM-05)",
    ("POST", "/fin/v1/rails/{rail}/events"): "records what the payout rail did",
    ("POST", "/fin/v1/disputes"): "records a Stripe dispute (Stripe moves the money)",
    ("POST", "/fin/v1/disputes/{dispute_id}/outcome"): "records a dispute outcome",
    ("POST", "/fin/v1/reconciliations/run"): "reconciliation",
    ("POST", "/fin/v1/breaks/{break_id}/resolution"): "break bookkeeping",
    ("POST", "/fin/v1/treasury/sweeps"): "margin sweep: earned margin only, client deposits (2010) are never "
                                         "sweepable",
    ("POST", "/fin/v1/treasury/sweeps/{op_id}/decision"): "margin sweep approval (same)",
    ("POST", "/fin/v1/treasury/operations/{op_id}/settlement"): "Andre records a bank outcome already approved",
    ("POST", "/fin/v1/treasury/top-ups"): "Andre's operating cash INTO restricted cash",
    ("POST", "/fin/v1/clawbacks/{payee_id}/write-off"): "writes off a creator receivable",
    ("POST", "/fin/v1/exceptions/{exception_id}/decision"): "a limit override; the run and release are gated",
    ("POST", "/fin/v1/close/{entity}/{period}/tasks/{task}"): "month-end close",
    ("POST", "/fin/v1/close/{entity}/{period}/approve"): "month-end close",
    ("POST", "/fin/v1/journal/{entity}/corrections"): "Andre's journal correction (no bank movement)",
    ("POST", "/fin/v1/controls/{control_id}/results"): "control attestation",
    ("POST", "/fin/v1/rules/proposals"): "rules",
    ("POST", "/fin/v1/rules/decisions"): "rules",
    ("POST", "/fin/v1/tax/readiness"): "tax filing readiness",
    ("POST", "/fin/v1/reconcile"): "local-log / ledger reconcile",
}
# the jobs route dispatches by name: these jobs move or commit client money and must each reach Compliance
JOBS_MONEY_OUT = ("job_accrual", "job_rail_sync", "job_stripe_sessions")
# handler helpers that are not the route's action (the founder-refusal audit of an Andre route)
NOT_ACTIONS = ("founder_refused",)


def _routes() -> dict[tuple[str, str], set[str]]:
    """(method, path) -> the ``svc.<name>`` service methods its handler calls (parsed from api.py)."""
    tree = ast.parse((SRC / "api.py").read_text())
    out: dict[tuple[str, str], set[str]] = {}
    for f in ast.walk(tree):
        if not isinstance(f, ast.FunctionDef):
            continue
        for d in f.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) \
                    and d.func.value.id == "app" and d.func.attr in ("post", "put", "patch", "delete") and d.args:
                calls = {n.func.attr for n in ast.walk(f) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                         and n.func.value.id == "svc"}
                out[(d.func.attr.upper(), d.args[0].value)] = calls
    return out


def _graph() -> dict[str, set[str]]:
    """Service method name -> every ``self.<name>`` it references (calls, and dispatch tables), over the mixins."""
    g: dict[str, set[str]] = {}
    for mod in SERVICE_MODULES:
        tree = ast.parse((SRC / mod).read_text())
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef) or not (cls.name.endswith("Mixin") or cls.name == "FinanceService"):
                continue
            for f in cls.body:
                if isinstance(f, ast.FunctionDef):
                    g.setdefault(f.name, set()).update(
                        n.attr for n in ast.walk(f) if isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name) and n.value.id == "self")
    return g


def _reaches(g: dict[str, set[str]], start: str, target: str = TARGET) -> bool:
    seen, todo = set(), [start]
    while todo:
        m = todo.pop()
        if m == target:
            return True
        if m in seen:
            continue
        seen.add(m)
        todo += sorted(g.get(m, ()))
    return False


def test_every_state_changing_route_is_classified():
    routes = set(_routes())
    tables = (MONEY_OUT, MONEY_IN_GATED, NOT_CLIENT_MONEY)
    classified = set().union(*map(set, tables))
    assert routes - classified == set(), ("unclassified route(s): decide whether they move a client's money",
                                          sorted(routes - classified))
    assert classified - routes == set(), ("classified route(s) that no longer exist", sorted(classified - routes))
    for a, b in ((0, 1), (0, 2), (1, 2)):
        assert not set(tables[a]) & set(tables[b])


def test_every_money_route_reaches_the_compliance_check():
    routes, g = _routes(), _graph()
    assert TARGET in g and "client_activation" in str(ast.dump(ast.parse((SRC / "svc_books.py").read_text())))
    missing = []
    for key in sorted(set(MONEY_OUT) | set(MONEY_IN_GATED)):
        methods = routes[key]
        assert methods, (key, "handler calls no service method")
        for m in methods:
            if m == "run_job" or m in NOT_ACTIONS:
                continue
            if not _reaches(g, m):
                missing.append((key, m))
    for m in JOBS_MONEY_OUT + ("_execute_transfer",):
        if not _reaches(g, m):
            missing.append(("job/transfer", m))
    assert not missing, ("money routes that never ask Compliance", missing)


def test_the_jobs_route_dispatches_the_money_jobs():
    src = (SRC / "svc_recon.py").read_text()
    for m in JOBS_MONEY_OUT:
        assert f"self.{m}" in src, m


def test_the_invariant_catches_a_route_that_stops_asking():
    """Self-test: drop the Compliance question from the scheduler's refund proposal and the graph no longer reaches it
    from that route (``repay_refund`` asks twice, before the approval and at every transfer attempt, so dropping one
    of the two is caught by the o4 tests in test_compliance_payouts_r10.py instead)."""
    g = _graph()
    assert _reaches(g, "propose_refund")
    g2 = {k: (v - {"_outflow_reasons"} if k == "propose_refund" else v) for k, v in g.items()}
    assert not _reaches(g2, "propose_refund")
