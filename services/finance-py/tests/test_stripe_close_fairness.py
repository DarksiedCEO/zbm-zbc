"""Owned fake-page inventory; no provider or money movement."""
from concurrent.futures import ThreadPoolExecutor
import secrets
import threading

import pytest

import helpers as H
from errors import Unavailable
from ports import StripeSession
from service import Op

FIELD = "close_sweep_order"
MAX_ORDER = (1 << 63) - 1


class Expiry:
    def __init__(self):
        self.calls = []
        self.expired = set()
        self.fail = set()
        self.hook = None

    def expire_session(self, sid):
        self.calls.append(sid)
        if sid in self.fail:
            return StripeSession(False)
        self.expired.add(sid)
        if self.hook:
            self.hook()
        return StripeSession(True, True, sid, "expired")


@pytest.fixture
def world(tmp_path, monkeypatch):
    # Override inherited harness credentials in memory; no new literal credentials.
    monkeypatch.setattr(H, "SERVICE_TOKEN", secrets.token_hex(32))
    monkeypatch.setattr(H, "ANDRE_TOKEN", secrets.token_hex(32))
    monkeypatch.setattr(H, "CALLERS", {k: secrets.token_hex(32) for k in H.CALLERS})
    monkeypatch.setattr(H.Harness.headers, "__defaults__", (None, None, H.SERVICE_TOKEN, None))
    monkeypatch.setattr(H.Harness.post, "__defaults__", (None, None, H.SERVICE_TOKEN, None))
    h = H.Harness(data_dir=str(tmp_path / "data")).ready()
    h.settings.stripe_incoming = True
    p = Expiry()
    h.svc.ports.stripe_in = p
    yield h, p
    h.svc.close()


def seed(h, n, same_time=False):
    op = Op(h.svc, H.rid(), "owned-fixture", "inventory")
    for i in reversed(range(n)):
        iid, sid = f"inv-owned-{i:03}", f"cs_owned{i:03}"
        op.put("invoices", iid, {"invoice_id": iid, "client_id": f"client-owned-{i:03}",
                                 "status": "void", "total": "100.00"})
        op.put("stripe_sessions", sid, {"session_id": sid, "invoice_id": iid, "status": "open",
                "total": "100.00", "methods": ["card"],
                "created_at": f"2026-10-02T17:00:{0 if same_time else i:02}Z"})
    h.svc._commit(op)


def call(h):
    return h.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": H.rid()}, caller="scheduler")


def test_persistent_failures_do_not_starve_later_pages(world):
    h, p = world
    seed(h, 30)
    p.fail = {f"cs_owned{i:03}" for i in range(25)}
    summaries = []
    for _ in range(3):
        r = call(h)
        assert r.status_code == 200
        summaries.append(r.json()["summary"])
        h.clock.advance(minutes=1)
    assert len(p.calls) == 75 and len(set(p.calls)) == 30
    assert all(s["checked"] <= 25 and s["activation_checked"] <= 25 for s in summaries)
    assert len(p.expired) == 5 and summaries[1]["closed"] == 5
    assert all(h.svc.db["stripe_sessions"][sid]["status"] == "open" for sid in p.fail)
    assert not h.svc.entries


def test_restart_and_clock_rollback_preserve_selection(world):
    h, p = world
    seed(h, 30)
    p.fail = {f"cs_owned{i:03}" for i in range(30)}
    assert call(h).status_code == 200
    saved = {k: dict(v) for k, v in h.svc.db["stripe_sessions"].items()}
    h.clock.advance(days=-1)
    restarted = H.Harness(data_dir=h.settings.data_dir, clock=h.clock, ledger=h.ledger, fakes=h.f, stripe_in=p)
    restarted.settings.stripe_incoming = True
    try:
        assert restarted.svc.db["stripe_sessions"] == saved
        assert call(restarted).status_code == 200
        assert set(p.calls[25:30]) == {f"cs_owned{i:03}" for i in range(25, 30)}
        assert not restarted.svc.entries
    finally:
        restarted.svc.close()


def test_equal_timestamps_have_stable_id_order(world):
    h, p = world
    seed(h, 30, same_time=True)
    p.fail = {f"cs_owned{i:03}" for i in range(30)}
    assert call(h).status_code == 200
    assert p.calls == [f"cs_owned{i:03}" for i in range(25)]


@pytest.mark.parametrize("bad", [None, True, -1, "1", 1.5, float("inf"), MAX_ORDER + 1])
def test_malformed_order_refuses_before_provider(world, bad):
    h, p = world
    seed(h, 1)
    h.svc.db["stripe_sessions"]["cs_owned000"][FIELD] = bad
    r = call(h)
    assert r.status_code == 503 and not p.calls
    assert r.json()["outcome"] == "outcome_unknown"


def test_exhausted_order_refuses_without_reset(world):
    h, p = world
    seed(h, 1)
    h.svc.db["stripe_sessions"]["cs_owned000"][FIELD] = MAX_ORDER
    assert call(h).status_code == 503 and not p.calls
    assert h.svc.db["stripe_sessions"]["cs_owned000"][FIELD] == MAX_ORDER


def test_reservation_append_failure_makes_no_provider_call(world):
    h, p = world
    seed(h, 1)
    h.svc.log.fail_next_append = True
    r = call(h)
    assert r.status_code == 503 and not p.calls
    assert FIELD not in h.svc.db["stripe_sessions"]["cs_owned000"]
    assert r.json()["outcome"] == "outcome_unknown"
    assert r.json()["took_effect"] is None


@pytest.mark.parametrize("stage", ["close", "done"])
def test_provider_effect_then_local_failure_is_unknown(world, monkeypatch, stage):
    h, p = world
    seed(h, 1)
    if stage == "close":
        p.hook = lambda: setattr(h.svc.log, "fail_next_append", True)
    else:
        original = h.svc.job_stripe_sessions
        def fail_done(prefix):
            result = original(prefix)
            h.svc.log.fail_next_append = True
            return result
        monkeypatch.setattr(h.svc, "job_stripe_sessions", fail_done)
    r = call(h)
    assert r.status_code == 503 and p.expired == {"cs_owned000"}
    assert r.json()["outcome"] == "outcome_unknown" and r.json()["took_effect"] is None
    assert "nothing took effect" not in r.text
    assert not h.svc.entries


def test_other_job_error_contract_unchanged(world, monkeypatch):
    h, _ = world
    def fail(*args):
        raise Unavailable("fixed local failure")
    monkeypatch.setattr(h.svc, "run_job", fail)
    r = h.post("/fin/v1/jobs/accrual/run", {"request_id": H.rid()}, caller="scheduler")
    assert r.status_code == 503 and r.json() == {"detail": "fixed local failure", "took_effect": False}


def test_overlap_revalidates_and_reserves_next_batch(world, monkeypatch):
    h, p = world
    seed(h, 30)
    p.fail = {f"cs_owned{i:03}" for i in range(30)}
    barrier = threading.Barrier(2)
    original = h.f["compliance"].client_activation
    local = threading.local()
    def simultaneous(cid):
        if not getattr(local, "entered", False):
            local.entered = True
            barrier.wait(timeout=10)
        return original(cid)
    monkeypatch.setattr(h.f["compliance"], "client_activation", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result(15) for f in [pool.submit(call, h), pool.submit(call, h)]]
    assert all(r.status_code == 200 for r in responses)
    assert len(p.calls) == 50 and len(set(p.calls)) == 30
    assert not h.svc.entries


def test_reservation_survives_interruption_before_provider(world, monkeypatch):
    h, p = world
    seed(h, 30)
    before = {k: dict(v) for k, v in h.svc.db["stripe_sessions"].items()}
    original = h.svc._expire_page
    def unavailable(*args):
        raise Unavailable("owned interruption")
    monkeypatch.setattr(h.svc, "_expire_page", unavailable)
    assert call(h).status_code == 503 and not p.calls
    for sid, rec in h.svc.db["stripe_sessions"].items():
        assert {k: v for k, v in rec.items() if k != FIELD} == before[sid]
    restarted = H.Harness(data_dir=h.settings.data_dir, clock=h.clock, ledger=h.ledger, fakes=h.f, stripe_in=p)
    restarted.settings.stripe_incoming = True
    try:
        assert call(restarted).status_code == 200
        assert p.calls[:5] == [f"cs_owned{i:03}" for i in range(25, 30)]
        assert not restarted.svc.entries
    finally:
        restarted.svc.close()
    monkeypatch.setattr(h.svc, "_expire_page", original)


def test_failed_pages_remain_retryable_after_other_pages_close(world):
    h, p = world
    seed(h, 30)
    p.fail = {f"cs_owned{i:03}" for i in range(25)}
    assert call(h).status_code == 200
    assert call(h).json()["summary"]["closed"] == 5
    p.fail.clear()
    assert call(h).json()["summary"]["closed"] == 25
    assert len(p.expired) == 30
    assert call(h).json()["summary"]["checked"] == 0
    assert not h.svc.entries


def test_only_eligible_pages_are_reserved(world):
    h, p = world
    seed(h, 2)
    rec = h.svc.db["stripe_sessions"]["cs_owned000"]
    rec["status"] = "expired"
    before = dict(rec)
    assert call(h).status_code == 200
    assert p.calls == ["cs_owned001"] and rec == before


def test_unavailable_response_does_not_export_cause(world, monkeypatch):
    h, p = world
    secret = secrets.token_hex(32)
    def fail(*args):
        raise Unavailable(secret, leaked=secret, took_effect=False)
    monkeypatch.setattr(h.svc, "run_job", fail)
    r = call(h)
    assert r.status_code == 503 and secret not in r.text
    assert set(r.json()) == {"detail", "took_effect", "outcome"}
    assert r.json()["outcome"] == "outcome_unknown" and r.json()["took_effect"] is None
    assert not p.calls
