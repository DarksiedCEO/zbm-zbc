"""marcus-pipeline-spec/1 section 2: the WAT Fetcher (B-MP-10..15) and A-MP-01 (hostile WAT), A-MP-03 (paths)."""

from __future__ import annotations

import json
import os
import tracemalloc

import pytest

import config as config_mod
from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import all_rows, build, job, kinds_logged, post_job, watch
from helpers import base_env, rid

C1 = "CC-MAIN-2026-40"
REGISTRY = {"cc_wat_fetcher": {
    "assignment": "deliver the WAT files of one crawl (or one targeted subset) to the Link Extractor, verified",
    "inputs": ["one recorded crawl release", "its wat.paths list or a targeted path list (path B)",
               "the cc_wat port (v1: the fixture directory SEO_MARCUS_FIXTURE_DIR, tests only)"],
    "outputs": ["verified WAT record streams for the Link Extractor", "wat_file_done records {path hash, records, bytes}"],
    "name_pending": True, "proposed_name": "Felix"}}


# ---------------------------------------------------------------------------------------------- B-MP-10

def test_B_MP_10_fixture_dir_only_in_test_settings(tmp_path):
    d = tmp_path / "fx"
    d.mkdir()
    s = config_mod.load(base_env(SEO_MARCUS_FIXTURE_DIR=str(d)))
    assert s.marcus_fixture_dir == str(d)
    prod = base_env(SEO_NON_PRODUCTION=None, SEO_MARCUS_FIXTURE_DIR=str(d))
    with pytest.raises(RuntimeError) as e:
        config_mod.load(prod)
    assert "SEO_MARCUS_FIXTURE_DIR" in str(e.value)
    for bad in ("relative/dir", str(tmp_path / "missing")):
        with pytest.raises(RuntimeError):
            config_mod.load(base_env(SEO_MARCUS_FIXTURE_DIR=bad))
    link = tmp_path / "link"
    link.symlink_to(d)
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SEO_MARCUS_FIXTURE_DIR=str(link)))
    assert config_mod.load(base_env()).marcus_fixture_dir is None


def test_B_MP_10_without_the_fixture_the_wat_port_is_not_connected(make_mp):
    h = make_mp(fixture=False)
    h.svc.cc_releases.update({"crawl:" + C1: {"id": C1, "kind": "crawl", "published": None}})
    j = build(h)
    assert j["outcome"] == "NOT_CONNECTED" and "cc_wat" in j["not_connected"]
    assert h.svc.index_snapshots == {}


def test_B_MP_10_input_is_one_recorded_release(mp):
    r = post_job(mp, "cc_index_builder", crawl_ids=[C1])
    assert r.status_code == 409 and r.json()["detail"] == "RELEASE_NOT_RECORDED"
    watch(mp)
    j = build(mp)
    assert j["outcome"] == "OK" and j["crawl_ids"] == [C1]


def test_B_MP_10_path_B_targeted_list(mp):
    watch(mp)
    paths = (mp.fx / "crawl-data" / C1 / "wat.paths").read_text().split()
    j = build(mp, paths=paths[:1])
    assert j["outcome"] == "OK" and j["facts"]["files"] == 1


# ---------------------------------------------------------------------------------------------- B-MP-11 / A-MP-03

BAD_PATHS = ["../" + gen_cc.wat_path(C1, 0), "/" + gen_cc.wat_path(C1, 0), "/etc/passwd",
             "https://data.commoncrawl.org/" + gen_cc.wat_path(C1, 0), "//evil.test/" + gen_cc.wat_path(C1, 0),
             gen_cc.wat_path("CC-MAIN-2026-44", 0), gen_cc.wat_path(C1, 0).replace("/wat/", "/warc/"),
             gen_cc.wat_path(C1, 0).replace(".gz", ".gz/../../x"), gen_cc.wat_path(C1, 0) + "\n",
             gen_cc.wat_path(C1, 0).replace("crawl-data", "crawl-data/./"), "crawl-data/" + C1 + "/%2e%2e/x",
             gen_cc.wat_path(C1, 0).replace("/", "\\"), gen_cc.wat_path(C1, 0) + "\x00", ""]


@pytest.mark.parametrize("i", range(len(BAD_PATHS)))
def test_B_MP_11_A_MP_03_targeted_paths_outside_the_grammar_refused(mp, i):
    watch(mp)
    r = post_job(mp, "cc_index_builder", crawl_ids=[C1], paths=[gen_cc.wat_path(C1, 0), BAD_PATHS[i]])
    assert r.status_code == 422 and r.json()["detail"] == "PATH_NOT_ALLOWED", r.text
    assert "wat_file_done" not in kinds_logged(mp)


def test_B_MP_11_A_MP_03_a_bad_line_in_wat_paths_refuses_the_list_and_reads_nothing(mp, monkeypatch):
    from marcus import wat_fetcher
    watch(mp)
    opened = []
    real = wat_fetcher.FixtureWatSource.open
    monkeypatch.setattr(wat_fetcher.FixtureWatSource, "open", lambda self, p: opened.append(p) or real(self, p))
    pf = mp.fx / "crawl-data" / C1 / "wat.paths"
    pf.write_text(pf.read_text() + "../../outside.warc.wat.gz\n")
    j = build(mp)
    assert j["outcome"] == "BLOCKED" and j["facts"]["reason"] == "PATH_NOT_ALLOWED"
    assert opened == []


def test_A_MP_03_symlinks_in_the_fixture_directory_are_never_followed(mp, tmp_path):
    watch(mp)
    outside = tmp_path / "outside.gz"
    outside.write_bytes(gen_cc.gz([gen_cc.line(gen_cc.page(__import__("random").Random(1)).record())]))
    p0 = mp.fx / gen_cc.wat_path(C1, 0)
    p0.unlink()
    p0.symlink_to(outside)                                             # a file link
    seg = (mp.fx / gen_cc.wat_path(C1, 1)).parent
    os.rename(seg, str(seg) + ".real")
    seg.symlink_to(str(seg) + ".real")                                 # a directory link
    j = build(mp)
    assert j["outcome"] == "PARTIAL"
    assert j["facts"]["files_quarantined"] == 2 and j["facts"]["quarantine_reasons"] == {"PATH_NOT_ALLOWED": 2}
    assert j["facts"]["records"] == 0


@pytest.mark.parametrize("n", [50])
def test_A_MP_03_generated_smuggling_fast(mp, n):
    import random
    from marcus import wat_fetcher
    rng = random.Random(303)
    pieces = ["..", ".", "", "%2e%2e", "~", "segments", "wat", "..%2f", "\\", "/", "C:", "data.commoncrawl.org"]
    for _ in range(n):
        p = gen_cc.wat_path(C1, rng.randint(0, 9)).split("/")
        p.insert(rng.randrange(len(p) + 1), rng.choice(pieces))
        cand = "/".join(p)
        if cand == gen_cc.wat_path(C1, 0):
            continue
        with pytest.raises(wat_fetcher.PathNotAllowed):
            wat_fetcher.check_path(C1, cand)


# ---------------------------------------------------------------------------------------------- B-MP-12 / A-MP-01

def _hostile_build(mp, cases):
    root = mp.fx
    paths = []
    for i, (_name, data, _want) in enumerate(cases):
        p = gen_cc.wat_path(C1, 100 + i)
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_bytes(data)
        paths.append(p)
    watch(mp)
    return build(mp, paths=paths)


@pytest.mark.parametrize("seed", [1])
def test_B_MP_12_A_MP_01_AC_4_hostile_wat_quarantined_or_skipped_and_the_job_continues(mp, seed):
    cases = gen_cc.hostile_cases(seed, 30)
    j = _hostile_build(mp, cases)
    q = sum(1 for _, _, w in cases if w.startswith("quarantine:"))
    assert j["outcome"] == "PARTIAL"
    assert j["facts"]["files"] == len(cases) and j["facts"]["files_quarantined"] == q
    want = {}
    for _, _, w in cases:
        if w.startswith("quarantine:"):
            want[w.split(":")[1]] = want.get(w.split(":")[1], 0) + 1
    assert j["facts"]["quarantine_reasons"] == want
    for counter in ("records_malformed", "records_too_large", "records_duplicate"):
        n = sum(1 for _, _, w in cases if w == "skip:" + counter)
        assert j["facts"][counter] >= n, counter
    qrec = [r["data"] for r in mp.svc.log.iter_records() if r["kind"] == "wat_file_quarantined"]
    assert len(qrec) == q and all(set(r) >= {"path_hash", "reason", "size"} for r in qrec)


def test_B_MP_12_decoded_cap_and_memory_bound_on_a_long_line(mp, tmp_path):
    from marcus import wat_fetcher
    assert wat_fetcher.DECODED_CAP == 2 * 1024 ** 3 and wat_fetcher.PEAK_BYTES == 64 * 1024 * 1024
    p = tmp_path / "long.gz"
    gen_cc.big_line_file(p, 96 * 1024 * 1024)
    tracemalloc.start()
    try:
        with open(p, "rb") as fh:
            kinds = [x if isinstance(x, str) else "line" for x in wat_fetcher.gz_lines(fh)]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert kinds == [wat_fetcher.TOO_LARGE]
    assert peak <= 64 * 1024 * 1024, peak


@pytest.mark.lead_gate
def test_A_MP_01_lead_300_cases_and_1GiB_line_under_2_min_cpu(mp, tmp_path):
    import time
    from marcus import wat_fetcher
    j = _hostile_build(mp, gen_cc.hostile_cases(2, 300))
    assert j["outcome"] == "PARTIAL"
    p = tmp_path / "giant.gz"
    gen_cc.big_line_file(p, 1024 ** 3)
    t0 = time.process_time()
    tracemalloc.start()
    try:
        with open(p, "rb") as fh:
            out = list(wat_fetcher.gz_lines(fh))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    cpu = time.process_time() - t0
    assert out == [wat_fetcher.TOO_LARGE] and peak <= 64 * 1024 * 1024 and cpu <= 120, (peak, cpu)


# ---------------------------------------------------------------------------------------------- B-MP-13

class Crash(BaseException):
    """The process dies: nothing after this point runs (no handler records anything)."""


def test_B_MP_13_restart_resumes_after_the_last_recorded_file_and_never_double_counts(mpd):
    truth = gen_cc.default_fixture(mpd.fx)[C1][1]
    watch(mpd)
    mpd.svc._mp_hook = lambda step: (_ for _ in ()).throw(Crash()) if step == "file_done:0" else None
    with pytest.raises(Crash):
        mpd.svc.link_index_start("dashboard", {"request_id": rid(), "service": "cc_index_builder",
                                               "crawl_ids": [C1]})
    first = [j for j in mpd.svc.link_index_jobs.values() if j["service"] == "cc_index_builder"][0]
    done = [r for r in mpd.svc.log.iter_records() if r["kind"] == "wat_file_done"]
    assert len(done) == 1
    h2 = mpd.restart()
    h2.svc.run_job("interrupted-audits", {"request_id": rid()})
    assert h2.svc.link_index_jobs[first["job_id"]]["status"] == "interrupted"
    j = job(h2, "cc_index_builder", resume=first["job_id"])
    assert j["outcome"] == "OK" and j["facts"]["files_resumed"] == 1 and j["facts"]["files"] == 2
    done = [r["data"] for r in h2.svc.log.iter_records() if r["kind"] == "wat_file_done"]
    assert len(done) == 2 and len({d["path_hash"] for d in done}) == 2       # each file counted once
    kept = sorted(json.dumps(r, sort_keys=True) for r in all_rows(h2, j["snapshot_id"]))
    want = sorted(json.dumps(r, sort_keys=True) for r in truth if r["dst"].split("/")[2].endswith(
        ("acme.test", "globex.test")))
    assert kept == want
    assert j["facts"]["records"] == 24                                       # 2 files x 12 pages, once each


def test_B_MP_15_registry_byte_for_byte():
    import svc_manager
    for agent, block in REGISTRY.items():
        shown = {**svc_manager.REGISTRY[agent], **svc_manager.NAMES[agent]}       # naming beside the block (Q-MP-1)
        assert set(svc_manager.REGISTRY[agent]) == {"assignment", "inputs", "outputs"}
        assert json.dumps(shown, sort_keys=True) == json.dumps(block, sort_keys=True)
        assert agent in svc_manager.AGENTS and agent not in svc_manager.NOT_BUILT_AGENTS
