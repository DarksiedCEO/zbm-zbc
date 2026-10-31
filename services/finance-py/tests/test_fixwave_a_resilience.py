"""Fix wave A (red team Oct 30-31 2026, scratchpad/audit/redteam-chaos.md): crash recovery, write health and the
start-up hold (CH-1, CH-2, CH-5, CH-6; ADR 0025). Every test here fails on feat/compliance-gate-deploy 5c2538b.

The kills of c01_kill_finance_money.py are reproduced in-process: ``Crash`` is a BaseException raised at the exact
point a SIGKILL landed (after a ruling or after the anchor, before the local append), so nothing in the service runs
after it -- no except clause, no settle -- exactly like a killed process. The restart is a new Harness on the same
data directory and ledger (the red team's ``chaoslib`` relaunch)."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time

import pytest

import crash_recovery as CR
import startup_hold as SH
from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i10_evidence_audit as i10

RECONCILE = "/fin/v1/reconcile"


class Crash(BaseException):
    """The process died here (SIGKILL / OOM / host loss)."""


def _durable(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d")).ready()
    x.fund_campaign()
    x.payee()
    x.accrue("sub-1")
    return x


def _restart(x, tmp_path, **env):
    return Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, fakes=x.f, env=env or None)


def _kill_when(x, monkeypatch, event_type):
    """SIGKILL right after the ledger accepted the first event of ``event_type``."""
    real = x.svc.recorder.record

    def record(event_id, et, *a, **k):
        out = real(event_id, et, *a, **k)
        if et == event_type:
            raise Crash(et)
        return out
    monkeypatch.setattr(x.svc.recorder, "record", record)


def _kill_before_append(x, monkeypatch):
    def append(*a, **k):
        raise Crash("append")
    monkeypatch.setattr(x.svc.log, "append_prepared", append)


def _crashes(call):
    """The request died with the process: the Crash propagates, or the ASGI stack turned it into a 500."""
    try:
        r = call()
    except BaseException as exc:  # noqa: BLE001 - Crash, possibly wrapped in an ExceptionGroup by the ASGI stack
        if isinstance(exc, Crash) or (isinstance(exc, BaseExceptionGroup) and exc.subgroup(Crash) is not None):
            return
        raise
    assert r.status_code == 500, (r.status_code, r.text)


def _reconciles(ledger):
    return ledger.of_type(i10.RECONCILE_TYPE)


@pytest.mark.parametrize("where", ["after_ruling"])
def test_ch1_a_kill_inside_a_write_recovers_by_itself_on_start(tmp_path, monkeypatch, where):
    """c01: 3 of 16 kills left Finance refusing to start until Andre's manual reconcile. The interrupted write was
    never acknowledged; the restart now rolls it back (recorded), verifies against the ledger and serves."""
    x = _durable(tmp_path)
    tb = x.ok(x.get("/fin/v1/journal/zbc/trial-balance"))
    if where == "after_ruling":
        _kill_when(x, monkeypatch, "payable_accrued")
    elif where == "after_anchor":
        _kill_when(x, monkeypatch, i10.ANCHOR_TYPE)
    else:
        _kill_before_append(x, monkeypatch)
    _crashes(lambda: x.handoff("sub-2"))
    monkeypatch.undo()
    assert (tmp_path / "d" / "fin_log.jsonl.intent").exists(), "the interrupted write was announced"
    y = _restart(x, tmp_path)                               # used to raise "refusing to start: ... ruling(s) ..."
    rec = _reconciles(x.ledger)
    assert len(rec) == 1 and rec[0]["actor"] == i10.ACTOR, "the rollback is recorded on the ledger"
    assert "Automatic crash recovery" in rec[0]["summary"]
    assert y.ok(y.get("/fin/v1/integrity"))["status"] == "green"
    assert y.ok(y.get("/fin/v1/journal/zbc/trial-balance")) == tb, "nothing of the interrupted write took effect"
    h = y.ok(y.get("/health"))
    assert h["status"] == "ok" and h["write_ok"] is True and h["reconcile_required"] is False
    assert not (tmp_path / "d" / "fin_log.jsonl.intent").exists()
    # the caller's retry books it exactly once
    p = y.accrue("sub-2")
    assert p["status"] in ("accrued", "payable", "pending_release", "approved") or p
    z = _restart(y, tmp_path)
    assert z.ok(z.get("/fin/v1/integrity"))["status"] == "green"
    assert len(_reconciles(x.ledger)) == 1, "a clean restart reconciles nothing"


@pytest.mark.parametrize("where", ["after_anchor", "before_append"])
def test_f2_an_interrupted_write_that_reached_its_anchor_is_never_voided_automatically(tmp_path, monkeypatch, where):
    """AEGIS wave A F-2: after the anchor, a crash and a restored mid-write snapshot look the same; only the second
    can have been acknowledged, so start-up holds with a fixed reason code and Andre's reconcile mode is reachable:
    /livez (Railway's deploy healthcheck) and /health are 200 there (AEGIS F-1)."""
    x = _durable(tmp_path)
    if where == "after_anchor":
        _kill_when(x, monkeypatch, i10.ANCHOR_TYPE)
    else:
        _kill_before_append(x, monkeypatch)
    _crashes(lambda: x.handoff("sub-2"))
    monkeypatch.undo()
    with pytest.raises(SH.StartupHeld) as held:
        _restart(x, tmp_path)
    assert held.value.code == "interrupted_write_anchored"
    assert _reconciles(x.ledger) == []
    y = _restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    assert y.client.get("/livez").status_code == 200
    h = y.ok(y.get("/health"))
    assert h["reconcile_mode"] is True and h["reconcile_required"] is True
    plan = y.ok(y.client.get("/fin/v1/reconcile", headers=y.headers(andre=ANDRE_TOKEN)))
    assert y.ok(y.post("/fin/v1/reconcile", {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                                             "void_lines": plan["voidable"]["lines"],
                                             "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN))
    z = _restart(x, tmp_path)
    assert z.ok(z.get("/fin/v1/integrity"))["status"] == "green"


def test_f2_a_volume_restored_from_a_mid_write_snapshot_never_voids_the_acknowledged_write(tmp_path, monkeypatch):
    """AEGIS test_A1: the snapshot holds W in the intent journal and not in the log; W then commits and is
    acknowledged; the volume is restored. It used to be rolled back automatically ("never acknowledged")."""
    x = _durable(tmp_path)
    snap = tmp_path / "snap"
    real = x.svc.recorder.record

    def record(event_id, et, *a, **k):
        out = real(event_id, et, *a, **k)
        if et == i10.ANCHOR_TYPE and not snap.exists():
            shutil.copytree(tmp_path / "d", snap)
        return out
    monkeypatch.setattr(x.svc.recorder, "record", record)
    r = x.handoff("sub-2")
    monkeypatch.undo()
    assert r.status_code in (200, 201), r.text
    x.svc.close()
    shutil.rmtree(tmp_path / "d")
    shutil.copytree(snap, tmp_path / "d")
    with pytest.raises(SH.StartupHeld) as held:
        _restart(x, tmp_path)
    assert held.value.code == "interrupted_write_anchored"
    assert _reconciles(x.ledger) == []


def test_f2_a_cloned_volume_is_a_generation_mismatch(tmp_path):
    """A second instance booted on a copy of the volume writes its own lease (the volume generation marker); the
    original volume no longer holds the ledger's latest lease: hold, code volume_generation_mismatch."""
    x = _durable(tmp_path)
    x.svc.close()
    shutil.copytree(tmp_path / "d", tmp_path / "clone")
    Harness(data_dir=str(tmp_path / "clone"), ledger=x.ledger, clock=x.clock, fakes=x.f).svc.close()
    with pytest.raises(SH.StartupHeld) as held:
        _restart(x, tmp_path)
    assert held.value.code == "volume_generation_mismatch"


def test_ch1_recovery_is_idempotent_when_the_restart_itself_is_killed(tmp_path, monkeypatch):
    x = _durable(tmp_path)
    _kill_when(x, monkeypatch, "payable_accrued")
    _crashes(lambda: x.handoff("sub-2"))
    monkeypatch.undo()
    # the first restart dies right after its own reconcile event reached the ledger
    real = x.ledger.record_event

    def once(event_id, department, event_type, *a, **k):
        real(event_id, department, event_type, *a, **k)
        if event_type == i10.RECONCILE_TYPE:
            raise Crash("restart killed")
    monkeypatch.setattr(x.ledger, "record_event", once)
    with pytest.raises(Crash):
        _restart(x, tmp_path)
    monkeypatch.undo()
    y = _restart(x, tmp_path)
    assert y.ok(y.get("/fin/v1/integrity"))["status"] == "green"
    assert y.ok(y.get("/health"))["status"] == "ok"


def test_ch1_real_divergence_still_refuses_with_a_clear_reason(tmp_path):
    """A ruling this log never announced (another instance, a forged event, a restored older volume) is NOT crash
    residue: start-up still refuses, as StartupHeld (the hold keeps the process up and shows the reason)."""
    x = _durable(tmp_path)
    x.ledger.record_event("fin-je-" + "ab" * 20, "finance", "journal_entry_posted", "intel_01_journal", "x",
                          {"forged": 1}, "forged ruling")
    with pytest.raises(SH.StartupHeld, match="refusing to start: 1 ruling"):
        _restart(x, tmp_path)
    assert _reconciles(x.ledger) == []


def test_ch1_a_truncated_log_is_divergence_not_residue(tmp_path):
    x = _durable(tmp_path)
    p = tmp_path / "d" / "fin_log.jsonl"
    lines = p.read_bytes().splitlines()
    p.write_bytes(b"\n".join(lines[:-1]) + b"\n")
    with pytest.raises(SH.StartupHeld, match="truncated"):
        _restart(x, tmp_path)


def test_ch2_a_ledger_blip_mid_write_is_reconciled_while_running_and_never_arms_a_refusal(tmp_path):
    """c02: the ledger refused after the rulings (k=4..8); /health said ok, and the NEXT ordinary deploy refused to
    start. Now /health is 503 degraded with the pending write, the ledger watch rolls it back once the ledger answers,
    and a restart (with or without that pass) starts."""
    x = _durable(tmp_path)
    x.ledger.fail_on_type = i10.ANCHOR_TYPE
    r = x.handoff("sub-2")
    assert r.status_code == 503
    x.ledger.fail_on_type = None
    h = x.get("/health")
    assert h.status_code == 503, h.text
    reasons = h.json()["degraded_reasons"]
    assert any(r.startswith("pending_unreconciled_writes") for r in reasons), reasons
    assert any(r.startswith("ledger_write_failing") for r in reasons), reasons
    # another write succeeds meanwhile (the orphan stays pending; it used to be invisible)
    x.payee("clip-b")
    assert x.get("/health").status_code == 503
    # a deploy now (before the watch ran) starts: the orphan is crash residue of this log
    y = _restart(x, tmp_path)
    assert y.ok(y.get("/health"))["status"] == "ok"
    assert y.ok(y.get("/fin/v1/integrity"))["status"] == "green"


def test_ch2_the_ledger_watch_settles_a_pending_write_without_a_restart(tmp_path):
    x = _durable(tmp_path)
    x.ledger.fail_on_type = i10.ANCHOR_TYPE
    assert x.handoff("sub-2").status_code == 503
    x.ledger.fail_on_type = None
    x.payee("clip-b")
    assert x.ok(x.get("/fin/v1/integrity"))["status"] == "red"
    assert x.svc.settle_pending() is True
    assert x.ok(x.get("/fin/v1/integrity"))["status"] == "green"
    assert x.ok(x.get("/health"))["status"] == "ok"
    assert len(_reconciles(x.ledger)) == 1
    x.accrue("sub-2")                                         # the retry books it once
    assert x.ok(x.get("/fin/v1/integrity"))["status"] == "green"


def test_ch2_watch_backs_off_while_the_ledger_is_down_and_logs_an_alert(tmp_path, caplog):
    x = _durable(tmp_path)
    x.ledger.fail_on_type = i10.ANCHOR_TYPE
    assert x.handoff("sub-2").status_code == 503
    x.ledger.fail_on_type = None
    x.ledger.fail_all = True
    w = CR.LedgerWatch("finance-py", x.svc.settle_pending, x.svc.probe_ledger, lambda: len(x.svc.intent))
    with caplog.at_level("WARNING", logger="zbm.alert"):
        assert w.step() is False
    alerts = [json.loads(r.getMessage()) for r in caplog.records if r.name == "zbm.alert"]
    assert any(a["alert"] == "ledger_unreachable" for a in alerts), alerts
    assert "ledger_unreachable" in x.get("/health").json()["degraded_reasons"]
    x.ledger.fail_all = False
    assert w.step() is True
    assert x.ok(x.get("/health"))["status"] == "ok"


def test_ch6_health_is_503_while_the_ledger_cannot_be_reached(tmp_path):
    """c05: with the ledger refused or blackholed, Finance /health answered 200 in 0.01 s."""
    x = _durable(tmp_path)
    assert x.ok(x.get("/health"))["status"] == "ok"
    x.ledger.fail_all = True
    assert x.svc.probe_ledger() is False
    h = x.get("/health")
    assert h.status_code == 503 and h.json()["status"] == "degraded" and h.json()["write_ok"] is False
    x.ledger.fail_all = False
    assert x.svc.probe_ledger() is True
    assert x.get("/health").status_code == 200


def test_ch6_a_refused_event_does_not_mark_the_ledger_unwritable():
    assert CR.cannot_write(Exception("ledger unreachable: ConnectError")) is True
    assert CR.cannot_write(Exception("ledger answered HTTP 500 (the ledger may have recorded it)")) is True
    assert CR.cannot_write(Exception("ledger refused the event: HTTP 400")) is False
    assert CR.cannot_write(Exception("ledger already holds a DIFFERENT record under this event id (409)")) is False


def test_ch5_ledger_down_at_start_is_transient_and_the_hold_retries(tmp_path):
    """c05: with the ledger unreachable Finance exited rc=1 and Railway's ON_FAILURE budget ran out. The build
    error is now StartupTransient; ``supervise`` holds (503 with the reason) and retries until the ledger is back."""
    x = _durable(tmp_path)
    x.svc.close()
    x.ledger.fail_all = True
    with pytest.raises(SH.StartupTransient, match="cannot be verified against the evidence ledger"):
        _restart(x, tmp_path)
    attempts, waits = [], []

    def build():
        attempts.append(1)
        if len(attempts) < 3:
            raise SH.StartupTransient("refusing to start: the local log cannot be verified against the evidence "
                                      "ledger (simulated)")
        x.ledger.fail_all = False
        return _restart(x, tmp_path).app
    ready = []
    hold = SH.supervise("finance-py", build, on_ready=ready.append, sleep=waits.append, background=False)
    assert isinstance(hold, SH.HoldingApp) and hold.app is not None and ready == [hold.app]
    assert waits == [1.0, 2.0], "exponential backoff"
    assert hold.attempts == 2


def test_ch5_the_holding_app_answers_503_with_the_reason():
    hold = SH.HoldingApp("finance-py")
    hold.reason = "refusing to start: 1 ruling(s) ... (first: fin-je-" + "cd" * 20 + ")"
    hold.code = "ledger_unavailable_at_start"

    async def call(path, method="GET"):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)
        await hold({"type": "http", "path": path, "method": method, "headers": []}, receive, send)
        return sent[0]["status"], json.loads(sent[1]["body"])
    st, body = asyncio.run(call("/health"))
    assert st == 503 and body["status"] == "starting"
    assert body["degraded_reasons"] == ["ledger_unavailable_at_start"]          # AEGIS F-7: a code, never an id
    assert "fin-je-" not in json.dumps(body)
    st, body = asyncio.run(call("/livez"))
    assert st == 200 and body["status"] == "alive"                             # AEGIS F-1: Railway activates it
    st, body = asyncio.run(call("/fin/v1/stripe/events", "POST"))
    assert st == 503 and body["took_effect"] is False


def test_ch5_a_configuration_error_still_raises():
    def build():
        raise RuntimeError("rules seed SHA-256 x does not match the expected y; refusing to start")
    with pytest.raises(RuntimeError, match="seed"):
        SH.supervise("finance-py", build, background=False)


def test_shared_modules_are_identical_across_the_three_services():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ("crash_recovery.py", "startup_hold.py"):
        copies = {s: (root / s / "src" / name).read_bytes() for s in ("finance-py", "legal-py", "compliance-py")}
        assert len(set(copies.values())) == 1, f"{name} differs between {sorted(copies)}"


def test_f1_reconcile_mode_is_deployable_livez_and_health_are_200(tmp_path):
    """AEGIS test_A6: in reconcile mode /health was 503, so Railway (healthcheckPath) never activated Andre's
    recovery deploy. Railway now checks /livez; /health is 200 in reconcile mode with the flags."""
    x = _durable(tmp_path)
    x.ledger.record_event("fin-je-" + "ab" * 20, "finance", "journal_entry_posted", "intel_01_journal", "x",
                          {"forged": 1}, "forged ruling")
    x.svc.close()
    y = _restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    assert y.client.get("/livez").status_code == 200
    h = y.get("/health")
    assert h.status_code == 200 and h.json()["reconcile_required"] is True, h.text


def test_f3_a_plain_read_is_not_delayed_behind_a_slow_ledger_read(tmp_path):
    """AEGIS test_A8: the watch read /ledger/entries under the service lock (a plain read waited 9.7 s)."""
    x = _durable(tmp_path)
    x.ledger.fail_on_type = "payable_accrued"
    x.ledger.fail_on_type = None
    real_record = x.svc.recorder.record

    def flaky(event_id, et, *a, **k):
        if et == i10.ANCHOR_TYPE:
            from ledger import LedgerRecordError
            raise LedgerRecordError("ledger response lost (test)")       # unknown: stays pending
        return real_record(event_id, et, *a, **k)
    x.svc.recorder.record = flaky
    assert x.handoff("sub-2").status_code == 503
    x.svc.recorder.record = real_record
    assert len(x.svc.intent)
    real = x.svc.recorder.client.entries
    in_read, release = threading.Event(), threading.Event()

    def blocked():
        in_read.set()
        release.wait(60)                                          # the ledger read hangs until released
        return real()
    x.svc.recorder.client.entries = blocked
    t = threading.Thread(target=x.svc.settle_pending)
    t.start()
    assert in_read.wait(30)
    done = {}
    reader = threading.Thread(target=lambda: done.setdefault("r", x.get("/fin/v1/journal/zbc/trial-balance")))
    reader.start()
    reader.join(30)
    finished_while_blocked = "r" in done and not release.is_set()
    release.set()
    t.join()
    reader.join()
    assert finished_while_blocked, "a plain read was queued behind the watch's ledger read (service lock held)"
    assert done["r"].status_code == 200


def test_f3_the_watch_does_not_act_on_a_read_older_than_a_commit(tmp_path):
    x = _durable(tmp_path)
    x.ledger.fail_on_type = i10.ANCHOR_TYPE
    assert x.handoff("sub-2").status_code == 503
    x.ledger.fail_on_type = None
    real = x.svc.recorder.client.entries

    def read_then_commit():
        out = real()
        x.payee("clip-z")                                            # the log moves while the read is in flight
        return out
    x.svc.recorder.client.entries = read_then_commit
    assert x.svc.settle_pending() is False                           # stale read: nothing applied this pass
    x.svc.recorder.client.entries = real
    assert x.svc.settle_pending() is True
