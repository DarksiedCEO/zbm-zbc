"""Auth on every route, separation of duties by caller identity, request limits, strict input, idempotency."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, Harness, rid

BAD_BEARERS = [None, "Bearer wrong", "Basic abc", f"Bearer {SERVICE_TOKEN}x", "Bearer t\xf6ken".encode("latin-1"),
               "Bearer ☃☃".encode("utf-8")]


def _concrete(path: str) -> str:
    for k, v in (("{payee_id}", "clip-a"), ("{doc_id}", "fin-rc-X"), ("{version}", "1"), ("{campaign_id}", "camp-1"),
                 ("{client_id}", "client-1"), ("{invoice_id}", "fin-inv-X"), ("{receipt_id}", "fin-rct-X"),
                 ("{rail}", "stripe"), ("{dispute_id}", "fin-dsp-X"), ("{refund_id}", "fin-rfd-X"), ("{job}", "accrual"),
                 ("{batch_id}", "fin-bat-X"), ("{payable_id}", "fin-pay-X"), ("{exception_id}", "fin-exc-X"),
                 ("{break_id}", "fin-brk-X"), ("{op_id}", "fin-trx-X"), ("{entity}", "zbc"), ("{period}", "2026-09"),
                 ("{task}", "preclose_review"), ("{control_id}", "FC-05"), ("{tax_year}", "2026"),
                 ("{buy_id}", "fin-mb-X"), ("{client_receipt_id}", "fin-crc-X")):
        path = path.replace(k, v)
    return path


def all_routes(h):
    out = []
    for r in h.app.routes:
        if isinstance(r, APIRoute) and r.path != "/health":
            for m in r.methods:
                out.append((m, _concrete(r.path)))
    return sorted(out)


def test_route_inventory_matches_the_spec(h):
    paths = {p for _, p in all_routes(h)}
    for need in ("/fin/v1/payout-handoffs", "/fin/v1/payees", "/fin/v1/payees/clip-a/tax-status",
                 "/fin/v1/payees/clip-a/rail-status", "/fin/v1/payees/clip-a/payout-identity",
                 "/fin/v1/payees/clip-a/open-items", "/fin/v1/payees/clip-a/offboarding-notices",
                 "/fin/v1/payees/clip-a/callbacks", "/fin/v1/payees/clip-a/tax/b-notices",
                 "/fin/v1/rate-cards/fin-rc-X/versions/1", "/fin/v1/rate-cards/fin-rc-X/versions/1/document",
                 "/fin/v1/rate-cards/proposals", "/fin/v1/rate-cards/decisions", "/fin/v1/campaigns/camp-1/commercial-profile",
                 "/fin/v1/campaigns/camp-1/budget", "/fin/v1/clients/client-1/billing-readiness", "/fin/v1/invoices",
                 "/fin/v1/invoices/fin-inv-X/decision", "/fin/v1/bank/events", "/fin/v1/rails/stripe/events",
                 "/fin/v1/disputes", "/fin/v1/disputes/fin-dsp-X/outcome", "/fin/v1/refunds/camp-1",
                 "/fin/v1/jobs/accrual/run", "/fin/v1/payout-runs", "/fin/v1/payout-batches/fin-bat-X",
                 "/fin/v1/payout-batches", "/fin/v1/payout-batches/fin-bat-X/decision",
                 "/fin/v1/payout-batches/fin-bat-X/release", "/fin/v1/exceptions", "/fin/v1/exceptions/fin-exc-X/decision",
                 "/fin/v1/reconciliations/run", "/fin/v1/breaks", "/fin/v1/breaks/fin-brk-X/resolution",
                 "/fin/v1/treasury", "/fin/v1/treasury/sweeps", "/fin/v1/treasury/funding",
                 "/fin/v1/clawbacks/clip-a/write-off", "/fin/v1/close/zbc/2026-09/tasks/preclose_review",
                 "/fin/v1/close/zbc/2026-09/approve", "/fin/v1/journal/zbc/entries", "/fin/v1/journal/zbc/trial-balance",
                 "/fin/v1/controls", "/fin/v1/controls/FC-05/results", "/fin/v1/rules", "/fin/v1/rules/proposals",
                 "/fin/v1/rules/decisions", "/fin/v1/tax/readiness", "/fin/v1/tax/1099/2026", "/fin/v1/audit/export",
                 # media billing (ADR 0009 amendment, Oct 5 2026)
                 "/fin/v1/media-buys", "/fin/v1/media-buys/fin-mb-X", "/fin/v1/media-buys/fin-mb-X/vendor-payments",
                 "/fin/v1/media-buys/fin-mb-X/delivery", "/fin/v1/media-buys/fin-mb-X/cancel",
                 "/fin/v1/client-receipts/fin-crc-X", "/fin/v1/client-receipts/fin-crc-X/send"):
        assert need in paths, need
    assert len(all_routes(h)) >= 60


@pytest.mark.parametrize("bearer", BAD_BEARERS)
def test_every_route_needs_the_bearer_401_never_500(h, bearer):
    for method, path in all_routes(h):
        headers = {"X-FIN-Caller-Token": CALLERS["scheduler"], "X-Andre-Approval-Token": ANDRE_TOKEN}
        if bearer is not None:
            headers["Authorization"] = bearer
        r = h.client.request(method, path, headers=headers, json={"request_id": "x"} if method in ("POST", "PUT") else None)
        assert r.status_code == 401, (method, path, r.status_code)


def test_docs_are_off_and_health_is_open(h):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(p).status_code == 404
    hl = h.ok(h.client.get("/health"))
    assert hl["rules_version"] is None and hl["in_memory"] is True and hl["rules_pinned"] is True
    assert hl["entities"] == ["zbc", "zbm"]


# (route, method, allowed callers) — every other caller is refused 403 (FIN-19)
CALLER_ROUTES = [
    ("POST", "/fin/v1/payout-handoffs", {"creative_production"}),
    ("POST", "/fin/v1/payees", {"onboarding", "clipper_network"}),
    ("GET", "/fin/v1/payees/clip-a/tax-status", {"compliance_38", "clipper_network"}),
    ("GET", "/fin/v1/payees/clip-a/rail-status", {"compliance_38"}),
    ("GET", "/fin/v1/payees/clip-a/payout-identity", {"verification_integrity"}),
    ("GET", "/fin/v1/payees/clip-a/open-items", {"clipper_network"}),
    ("POST", "/fin/v1/payees/clip-a/offboarding-notices", {"clipper_network"}),
    ("GET", "/fin/v1/campaigns/camp-1/budget", {"clipper_network", "creative_production"}),
    ("GET", "/fin/v1/clients/client-1/billing-readiness", {"onboarding"}),
    ("POST", "/fin/v1/invoices", {"onboarding", "scheduler"}),
    ("POST", "/fin/v1/bank/events", {"bank_feed"}),
    ("POST", "/fin/v1/rails/stripe/events", {"rail_gateway"}),
    ("POST", "/fin/v1/refunds/camp-1", {"scheduler"}),
    ("POST", "/fin/v1/jobs/accrual/run", {"scheduler"}),
    ("POST", "/fin/v1/payout-runs", {"scheduler"}),
    ("POST", "/fin/v1/payout-batches/fin-bat-X/release", {"scheduler"}),
    ("POST", "/fin/v1/reconciliations/run", {"scheduler"}),
    ("POST", "/fin/v1/treasury/sweeps", {"scheduler"}),
    ("POST", "/fin/v1/treasury/funding", {"scheduler"}),
    ("POST", "/fin/v1/close/zbc/2026-09/tasks/preclose_review", {"scheduler"}),
    ("POST", "/fin/v1/client-receipts/fin-crc-X/send", {"scheduler"}),
]


def test_separation_of_duties_by_caller_identity(hr):
    for method, path, allowed in CALLER_ROUTES:
        for name in CALLERS:
            r = hr.client.request(method, path, headers=hr.headers(name), json={"request_id": rid()}
                                  if method == "POST" else None)
            if name in allowed:
                assert r.status_code != 403, (path, name, r.text)
            else:
                assert r.status_code == 403, (path, name, r.status_code)
        r = hr.client.request(method, path, headers=hr.headers(None), json={"request_id": rid()} if method == "POST" else None)
        assert r.status_code == 403, path


ANDRE_ROUTES = ["/fin/v1/payees/clip-a/callbacks", "/fin/v1/payees/clip-a/tax/b-notices", "/fin/v1/rate-cards/proposals",
                "/fin/v1/rate-cards/decisions", "/fin/v1/invoices/fin-inv-X/decision",
                "/fin/v1/disputes/fin-dsp-X/outcome", "/fin/v1/refunds/fin-rfd-X/decision",
                "/fin/v1/payout-batches/fin-bat-X/decision", "/fin/v1/exceptions/fin-exc-X/decision",
                "/fin/v1/breaks/fin-brk-X/resolution", "/fin/v1/treasury/sweeps/fin-trx-X/decision",
                "/fin/v1/treasury/funding/fin-trx-X/decision", "/fin/v1/treasury/top-ups",
                "/fin/v1/clawbacks/clip-a/write-off", "/fin/v1/close/zbc/2026-09/approve", "/fin/v1/journal/zbc/corrections",
                "/fin/v1/rules/proposals", "/fin/v1/rules/decisions", "/fin/v1/tax/readiness", "/fin/v1/reconcile",
                "/fin/v1/receipts/fin-rct-X/apply", "/fin/v1/media-buys", "/fin/v1/media-buys/fin-mb-X/vendor-payments",
                "/fin/v1/media-buys/fin-mb-X/delivery", "/fin/v1/media-buys/fin-mb-X/cancel"]


def test_andre_routes_refuse_every_other_identity(hr):
    for path in ANDRE_ROUTES:
        for tok in [None, SERVICE_TOKEN, *CALLERS.values(), "x" * 40]:
            r = hr.post(path, {"request_id": rid()}, andre=tok, caller="scheduler")
            assert r.status_code == 403, (path, tok, r.status_code)
        r = hr.client.put("/fin/v1/campaigns/camp-1/commercial-profile", json={"request_id": rid()},
                          headers=hr.headers(caller="scheduler"))
        assert r.status_code == 403
    assert hr.get("/fin/v1/tax/1099/2026").status_code == 403
    assert hr.get("/fin/v1/rate-cards/x/versions/1/document").status_code == 403


def test_andre_token_equal_to_a_caller_or_service_token_refuses_to_start():
    import json
    import config as config_mod
    from helpers import base_env
    for over in ({"FIN_ANDRE_APPROVAL_TOKEN": SERVICE_TOKEN}, {"FIN_ANDRE_APPROVAL_TOKEN": CALLERS["scheduler"]},
                 {"FIN_SECOND_APPROVER_TOKEN": ANDRE_TOKEN},
                 {"FIN_CALLER_TOKENS": json.dumps({**CALLERS, "bank_feed": CALLERS["scheduler"]})}):
        with pytest.raises(RuntimeError):
            config_mod.load(base_env(**over))


def test_request_limits(h):
    hd = h.headers("scheduler")
    assert h.client.get("/fin/v1/rules?x=" + "a" * 5000, headers=hd).status_code == 414
    assert h.client.get("/fin/v1/rules", headers={**hd, "X-Big": "a" * 17000}).status_code == 431
    big = {"request_id": "r", "pad": "a" * 20000}
    assert h.client.post("/fin/v1/payout-runs", json=big, headers=hd).status_code == 413
    r = h.client.post("/fin/v1/payout-runs", content=b"request_id=r", headers={**hd, "Content-Type": "text/plain"})
    assert r.status_code == 415
    deep = "[" * 40 + "]" * 40
    r = h.client.post("/fin/v1/payout-runs", content=deep.encode(), headers={**hd, "Content-Type": "application/json"})
    assert r.status_code == 422
    r = h.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe", "extra": 1}, caller="scheduler")
    assert r.status_code == 422 and "extra" in r.text


def test_validation_errors_are_bounded_and_do_not_echo(hr):
    r = hr.post("/fin/v1/invoices", {"request_id": "bad id with spaces " + "Z" * 300, "entity": "zbm"}, caller="onboarding")
    assert r.status_code == 422 and "ZZZZZZZZZZ" not in r.text and len(r.text) < 4000


def test_idempotency_same_body_same_answer_different_body_409(hr):
    hr.fund_campaign()
    body = {"request_id": "dup-1", "payee_id": "clip-a", "kind": "clipper", "declared_country": "US",
            "callback_contact_ref": "vault:c"}
    a = hr.ok(hr.post("/fin/v1/payees", body, caller="clipper_network"))
    b = hr.ok(hr.post("/fin/v1/payees", body, caller="clipper_network"))
    assert a == b and a["request_id"] == "dup-1" and len(a["facts_sha256"]) == 64
    c = hr.post("/fin/v1/payees", {**body, "declared_country": "GB"}, caller="clipper_network")
    assert c.status_code == 409
    # the same request id from ANOTHER caller is another request
    d = hr.post("/fin/v1/payees", body, caller="onboarding")
    assert d.status_code == 200
    hr.clock.advance(minutes=16)
    e = hr.post("/fin/v1/payees", body, caller="clipper_network")
    assert e.status_code == 409 and "15 minutes" in e.text


def test_stored_answer_survives_restart(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d")).ready()
    x.fund_campaign()
    body = {"request_id": "dup-2", "payee_id": "clip-b", "kind": "clipper", "declared_country": "US",
            "callback_contact_ref": "vault:c"}
    a = x.ok(x.post("/fin/v1/payees", body, caller="clipper_network"))
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, fakes=x.f)
    assert y.ok(y.post("/fin/v1/payees", body, caller="clipper_network")) == a
    assert y.post("/fin/v1/payees", {**body, "declared_country": "CA"}, caller="clipper_network").status_code == 409


def test_unknown_job_and_bad_ids(hr):
    assert hr.post("/fin/v1/jobs/nope/run", {"request_id": rid()}, caller="scheduler").status_code == 404
    assert hr.get("/fin/v1/payees/has space").status_code in (404, 422)
    assert hr.get("/fin/v1/payees/123-45-6789").status_code == 422
