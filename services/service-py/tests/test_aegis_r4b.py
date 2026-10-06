"""AEGIS round 4b (Oct 6 2026, NOT BLOCKING): one regression per finding, each the reviewer's scenario
(scratchpad/aegis-service-r4b/p6b_race.py, p2b_deny.py)."""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config as config_mod
from helpers import Harness, base_env, rid

SRC = Path(__file__).resolve().parents[1] / "src"


def _race_worker(d, gate, done, q):
    gate.wait()
    try:
        q.put(("ok", config_mod._generated_key(d).hex()))
    except BaseException as e:  # noqa: BLE001 - every outcome is reported to the parent
        q.put(("err", f"{type(e).__name__}: {e}"))
    done.wait()                        # every process holds what it got until all have tried (concurrent starts)


# --------------------------------------------------------------------------------------------------- V4b-I1

def test_v4b_i1_concurrent_first_starts_exactly_one_wins_one_key_clear_error(tmp_path):
    ctx = mp.get_context("fork")
    for t in range(15):
        d = str(tmp_path / f"d{t}")
        os.makedirs(d, mode=0o700)
        n = 5
        gate, done, q = ctx.Barrier(n), ctx.Barrier(n), ctx.Queue()
        ps = [ctx.Process(target=_race_worker, args=(d, gate, done, q)) for _ in range(n)]
        for p in ps:
            p.start()
        res = [q.get(timeout=60) for _ in range(n)]
        for p in ps:
            p.join(timeout=60)
        oks = [r[1] for r in res if r[0] == "ok"]
        errs = [r[1] for r in res if r[0] == "err"]
        assert len(oks) == 1                                                  # exactly one start per data dir
        assert all(e.startswith("RuntimeError") and "another service-py process holds this data directory" in e
                   for e in errs)                                             # never a crash with the wrong error
        assert sorted(n for n in os.listdir(d)) == ["hmac.key", "service.lock"]
        assert config_mod._generated_key(d).hex() == oks[0]                   # never two keys
        config_mod._HELD.pop(os.path.realpath(d)).release()


def test_v4b_i1_a_second_process_on_a_running_services_data_dir_refuses(tmp_path):
    d = tmp_path / "data"
    h = Harness(tmp_path, data_dir=str(d), SVC_NON_PRODUCTION=None)
    key = (d / "hmac.key").read_bytes()
    env = {**os.environ, **base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d))}
    r = subprocess.run([sys.executable, "-B", "-c", "import config; config.load()"], cwd=SRC, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "another service-py process holds this data directory" in r.stderr
    assert (d / "hmac.key").read_bytes() == key and h.svc.integrity["ok"]


def test_v4b_i1_the_lock_is_taken_before_the_key_and_the_log(tmp_path, monkeypatch):
    order = []
    real_hold, real_gen = config_mod.hold_data_dir, config_mod._generated_key
    monkeypatch.setattr(config_mod, "hold_data_dir", lambda d: order.append("lock") or real_hold(d))
    monkeypatch.setattr(config_mod, "_generated_key", lambda d: order.append("key") or real_gen(d))
    config_mod.load(base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(tmp_path / "d")))
    assert order[0] == "lock" and order.index("lock") < order.index("key")


# --------------------------------------------------------------------------------------------------- V4b-I2

def test_v4b_i2_stale_temp_key_files_are_removed_even_when_the_key_exists(tmp_path):
    d = tmp_path / "d"
    key = config_mod.load(base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d))).hmac_key
    config_mod._HELD.pop(os.path.realpath(d)).release()
    for name in ("hmac.key.tmp", "hmac.key.4242.0011223344556677.tmp", "hmac.key.1.ab.tmp"):
        (d / name).write_bytes(b"stale")
    (d / "unrelated.tmp").write_bytes(b"keep")
    assert config_mod.load(base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d))).hmac_key == key
    assert sorted(os.listdir(d)) == ["hmac.key", "service.lock", "unrelated.tmp"]


# --------------------------------------------------------------------------------------------------- V4b-L1

R4B_NEAR_MISSES = ["do you keep my phone number", "im done with you", "can i get compensated",
                   "do you give my email to others", "how do i deactivate", "can you pull my videos",
                   "can my clips come down", "can you discontinue my plan", "can i downgrade"]


@pytest.mark.parametrize("question", R4B_NEAR_MISSES)
def test_v4b_l1_the_reviewers_near_misses_are_refused(h, question):
    r = h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "nm-x", "brands": ["zbm"], "channels": ["chat"],
                                       "title": "t", "answer": "a", "questions": [question]})
    assert r.status_code == 422 and r.json()["detail"] == "QUESTION_DENIED"


def test_v4b_l1_andre_is_warned_about_near_miss_terms_at_save_and_approval(h):
    saved = h.ok(h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "plans", "brands": ["zbm"],
                                                "channels": ["chat"], "title": "Plans", "answer": "See the portal.",
                                                "questions": ["How do I change my plan?", "What are your hours?"]}),
                 201)
    assert saved["warnings"] == [{"question": "How do I change my plan?", "terms": ["change", "plan"]}]
    appr = h.approve("kb/articles", saved)
    assert appr["warnings"] == saved["warnings"]
    assert h.ok(h.get("/svc/v1/kb/articles"))[0]["warnings"] == saved["warnings"]


def test_v4b_l1_a_one_typo_near_miss_is_warned():
    import service
    assert "wavie" in service.question_warnings("Can the fee be wavie?")
    assert service.question_warnings("What are your hours?") == []
