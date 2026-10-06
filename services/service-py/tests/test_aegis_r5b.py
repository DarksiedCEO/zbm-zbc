"""AEGIS round 5 follow-up (Oct 6 2026, NOT BLOCKING): V5r-L1 (a refused second build must not touch the running
instance's files; scratchpad/aegis-r5/leak.py, last block) and V5r-Info (a closed instance never writes)."""

from __future__ import annotations

import os

import pytest

import api
import config as config_mod
import store as store_mod
from errors import Unavailable
from helpers import Harness, base_env


def _env(d):
    return base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d))


# --------------------------------------------------------------------------------------------------- V5r-L1

def test_v5r_l1_a_refused_second_build_leaves_the_running_instances_files_alone(tmp_path):
    d = tmp_path / "d"
    _, s1 = api.build(_env(d))
    inflight = d / "bodies" / "x.tmp"
    inflight.write_text("inflight")                                 # the running instance's body being written
    with pytest.raises(store_mod.DataDirBusy):
        api.build(_env(d))
    assert inflight.exists()
    s1.close()


def test_v5r_l1_a_build_that_fails_before_the_service_gives_the_claim_back(tmp_path):
    d = tmp_path / "d"
    _, s1 = api.build(_env(d))
    s1.close()
    with open(d / "service_log.jsonl", "ab") as fh:
        fh.write(b"\n")                                             # RecordLog refuses (after the claim was taken)
    with pytest.raises(store_mod.StoreCorrupt, match="empty line"):
        api.build(_env(d))
    assert config_mod._HELD[os.path.realpath(d)].claimed is False


# --------------------------------------------------------------------------------------------------- V5r-Info

def test_v5r_info_a_closed_instance_never_writes(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    h.ok(h.chat("I need help"), 201)
    n = len(h.svc.log)
    h.svc.close()
    r = h.chat("more")
    assert r.status_code == 503 and r.json()["detail"] == "SERVICE_CLOSED"
    with pytest.raises(Unavailable) as e:
        h.svc._commit("job_ran", {"effects": [{"op": "job_ran", "job": "x"}]}, "scheduler")
    assert e.value.reason == "SERVICE_CLOSED"
    rec, line = h.svc.log.prepare("job_ran", "2026-10-06T18:00:00Z", {"effects": [], "actor": "scheduler"})
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        h.svc.log.append_prepared(rec, line)
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        h.svc.log.write_pending(line)
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        h.svc.bodies.put("text")
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        h.svc.bodies.delete("0" * 64)
    assert len(h.svc.log) == n
    assert h.restart().svc.integrity["ok"]                          # a new instance writes again
