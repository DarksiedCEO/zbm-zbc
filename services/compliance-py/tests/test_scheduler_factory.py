"""Real Compliance service and Scheduler factory, synthetic Ledger only."""
import threading
import pytest
import api
import config as config_mod
from clock import FixedClock
from fakes import FakeLedgerClient
from helpers import base_env, NOW


def service(tmp_path, enabled=True):
    cfg = config_mod.load(base_env(COMPLIANCE_DATA_DIR=str(tmp_path / 'store'),
                                  COMPLIANCE_SCHEDULER_ENABLED='1' if enabled else '0'))
    ledger = FakeLedgerClient()
    return api.build_service(cfg, FixedClock(NOW), ledger=ledger), cfg, ledger


def cleanup(svc, worker):
    if worker is not None:
        worker.stop()
        worker._thread.join(3)
        assert not worker.alive()
    svc.close()


def test_factory_paused_start_stays_live_and_requires_explicit_resume(tmp_path):
    svc, cfg, ledger = service(tmp_path)
    worker = None
    try:
        before = ledger.calls
        worker = api.start_scheduler(svc, cfg, startup_paused=True)
        assert worker is svc.scheduler and worker.alive()
        assert worker.backup_state()['ready_for_backup']
        assert worker.tick() is False and ledger.calls == before
        worker.resume()
        assert not worker.backup_state()['pause_requested']
        worker.tick()
        assert ledger.calls > before
    finally:
        cleanup(svc, worker)


def test_factory_default_enabled_scheduler_still_executes_actual_controls(tmp_path, monkeypatch):
    svc, cfg, ledger = service(tmp_path)
    done = threading.Event()
    original = svc.run_internal_controls
    def controls(request):
        result = original(request)
        done.set()
        return result
    monkeypatch.setattr(svc, 'run_internal_controls', controls)
    worker = None
    try:
        before = ledger.calls
        worker = api.start_scheduler(svc, cfg)
        assert done.wait(3)
        assert not worker.backup_state()['pause_requested'] and ledger.calls > before
    finally:
        cleanup(svc, worker)


def test_disabled_scheduler_remains_absent_even_with_paused_option(tmp_path):
    svc, cfg, ledger = service(tmp_path, False)
    try:
        before = ledger.calls
        assert api.start_scheduler(svc, cfg, startup_paused=True) is None
        assert svc.scheduler is None and ledger.calls == before
    finally:
        svc.close()


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('invalid', [1, 'true', None])
def test_factory_rejects_nonboolean_pause_before_thread_start(tmp_path, enabled, invalid):
    svc, cfg, ledger = service(tmp_path, enabled)
    try:
        before = ledger.calls
        with pytest.raises(ValueError, match='startup_paused must be boolean'):
            api.start_scheduler(svc, cfg, startup_paused=invalid)
        assert svc.scheduler is None and ledger.calls == before
    finally:
        svc.close()
