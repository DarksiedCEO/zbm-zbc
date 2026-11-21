"""Actual Scheduler, synthetic controls/watcher. No network or provider calls."""
import threading
import time
from datetime import timedelta
import pytest
from scheduler import Scheduler

class Service:
    def __init__(self):
        self.enter=threading.Event();self.release=threading.Event()
        self.watch_enter=threading.Event();self.watch_release=threading.Event()
        self.controls=0;self.watches=0
    def run_internal_controls(self,request):
        self.enter.set()
        if not self.release.wait(2):raise AssertionError('fixture controls release timed out')
        self.controls+=1
    def watcher_run(self,request):
        self.watch_enter.set()
        if not self.watch_release.wait(2):raise AssertionError('fixture watcher release timed out')
        self.watches+=1
    def readiness(self):
        return {'as_of':'2026-11-01','register_version':None,'gate_blockers':['synthetic'],
                'expired':[],'us_web_client_activation':{'blocking_now':['synthetic'],'expiring':[]}}

def make(service,**kwargs):return Scheduler(service,watcher=True,out=lambda _:None,**kwargs)

def wait_paused(s):
    end=time.monotonic()+1
    while not s.backup_state()['pause_requested'] and time.monotonic()<end:time.sleep(.001)
    assert s.backup_state()['pause_requested']

def test_default_tick_behavior_and_startup_pause():
    svc=Service();svc.release.set();svc.watch_release.set();s=make(svc)
    assert s.tick() is None and (svc.controls,svc.watches)==(1,1)
    p=make(svc,startup_paused=True)
    assert p.tick() is False and (svc.controls,svc.watches)==(1,1)
    assert p.backup_state()['pause_requested']
    assert not p.backup_state()['ready_for_backup']


def test_drain_waits_controls_and_following_watcher():
    svc=Service();s=make(svc);worker=threading.Thread(target=s.tick);worker.start()
    drain=None;result=[]
    try:
        assert svc.enter.wait(1)
        drain=threading.Thread(target=lambda:result.append(s.pause_and_drain(1.5)));drain.start()
        wait_paused(s)
        assert s.tick() is False
        with pytest.raises(RuntimeError):s.resume()
        with pytest.raises(RuntimeError):s.pause_and_drain(.1)
        svc.release.set();assert svc.watch_enter.wait(1)
        assert drain.is_alive() and s.backup_state()['inflight_ticks']==1
        svc.watch_release.set();worker.join(1);drain.join(1)
        assert not worker.is_alive() and not drain.is_alive() and result==[True]
        assert (svc.controls,svc.watches)==(1,1)
        assert s.backup_state()['inflight_ticks']==0
        assert s.tick() is False
        s.resume();s.last_controls=None
        assert s.tick() is None and svc.controls==2
    finally:
        svc.release.set();svc.watch_release.set();worker.join(3)
        if drain is not None:drain.join(3)


def test_timeout_remains_paused_until_explicit_resume():
    svc=Service();s=make(svc);worker=threading.Thread(target=s.tick);worker.start()
    try:
        assert svc.enter.wait(1)
        assert s.pause_and_drain(.01) is False
        assert s.backup_state()['pause_requested'] and s.tick() is False
        with pytest.raises(RuntimeError):s.resume()
        svc.release.set();svc.watch_release.set();worker.join(1)
        assert s.backup_state()['pause_requested'] and s.tick() is False
        assert s.pause_and_drain(.1)
    finally:svc.release.set();svc.watch_release.set();worker.join(3)


def test_live_paused_separate_from_dead_and_stale_liveness():
    svc=Service();svc.release.set();svc.watch_release.set();s=make(svc,startup_paused=True).start()
    try:
        assert s.alive() and s.pause_and_drain(.1)
        assert s.backup_state()['ready_for_backup'] and svc.controls==0
        s.started_at=s._now()-timedelta(seconds=3*s.interval.total_seconds())
        assert not s.liveness()['healthy']  # pause does not silently erase existing SLA status
    finally:s.stop();s._thread.join(2)
    assert not s.alive() and not s.backup_state()['ready_for_backup']
    assert not s.liveness()['healthy']
    with pytest.raises(RuntimeError):s.resume()


def test_invalid_timeout_and_boot_flag():
    s=make(Service())
    for value in (-1,0,True,float('nan'),float('inf'),301,'1'):
        with pytest.raises(ValueError):s.pause_and_drain(value)
    with pytest.raises(ValueError):make(Service(),startup_paused='yes')
