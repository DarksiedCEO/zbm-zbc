"""AEGIS round 5 (Oct 6 2026, NOT BLOCKING): V5-L1 (a second service instance on one data directory in the same
process, scratchpad/aegis-r4c/dbl.py) and V5-I1 (non-regular files at the lock and temporary key paths)."""

from __future__ import annotations

import os

import pytest

import api
import config as config_mod
import store as store_mod
from helpers import Harness, base_env


def _env(d):
    return base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d))


# --------------------------------------------------------------------------------------------------- V5-L1

def test_v5_l1_dbl_a_second_build_in_the_same_process_is_refused(tmp_path):
    d = tmp_path / "d"
    _, svc1 = api.build(_env(d))
    with pytest.raises(store_mod.DataDirBusy, match="another service instance in this process"):
        api.build(_env(d))
    assert svc1._dir_lock is not None and svc1._dir_lock.claimed   # the first instance keeps its claim
    svc1.close()
    _, svc2 = api.build(_env(d))                                   # after close, a new instance may start
    assert svc2._dir_lock.claimed
    svc2.close()


def test_v5_l1_a_second_harness_without_restart_is_refused(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    with pytest.raises(store_mod.DataDirBusy):
        Harness(tmp_path, data_dir=d)
    assert h.restart().svc.integrity["ok"]                         # restart closes first


def test_v5_l1_a_failed_start_gives_the_claim_back(tmp_path):
    d = tmp_path / "d"
    h = Harness(tmp_path, data_dir=str(d), SVC_NON_PRODUCTION=None)
    h.ok(h.chat("I need help"), 201)
    key = (d / "hmac.key").read_bytes()
    h.svc.close()
    os.remove(d / "hmac.key")                                      # a new key: the service's own start refuses
    with pytest.raises(store_mod.StoreCorrupt, match="fingerprint mismatch"):
        Harness(tmp_path, data_dir=str(d), SVC_NON_PRODUCTION=None)
    assert config_mod._HELD[os.path.realpath(d)].claimed is False  # the failed instance gave the claim back
    os.remove(d / "hmac.key")
    fd = os.open(d / "hmac.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, key)
    os.close(fd)
    assert Harness(tmp_path, data_dir=str(d), ledger=h.ledger, SVC_NON_PRODUCTION=None).svc.integrity["ok"]


# --------------------------------------------------------------------------------------------------- V5-I1

@pytest.mark.parametrize("plant", ["symlink", "fifo", "dir"])
def test_v5_i1_a_non_regular_service_lock_refuses_clearly(tmp_path, plant):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    path = d / "service.lock"
    if plant == "symlink":
        (d / "target").write_bytes(b"")
        os.symlink(d / "target", path)
    elif plant == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir()
    with pytest.raises(RuntimeError, match="service.lock in the data directory is not a regular file"):
        config_mod.load(_env(d))


@pytest.mark.parametrize("plant", ["fifo", "dir", "symlink"])
def test_v5_i1_a_non_regular_file_at_a_temp_key_path_refuses_clearly(tmp_path, plant):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    path = d / "hmac.key.999.0123456789abcdef.tmp"
    if plant == "fifo":
        os.mkfifo(path, 0o600)
    elif plant == "dir":
        path.mkdir()
    else:
        os.symlink("/nonexistent", path)
    with pytest.raises(RuntimeError, match="is not a regular file"):
        config_mod.load(_env(d))
