"""Property certification (spec §F Property): for every non-empty subset of the eight unavailable inputs, the run is
refused naming the input (start-up refusal for the pinned/manifest/repo/evidence inputs; a 503 for the ledger, the
Docker daemon and the LLM key) and afterwards there is no worktree, no container argv, no evidence file and no run.
2^8 - 1 = 255 subsets."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import tempfile

import pytest

from helpers import SERVICE_ROOT, SITE_PACKAGES, Harness, base_env, make_repo, two_findings

from zbm_delivery import config as C
from zbm_delivery import gate as G

INPUTS = ("docker", "llm_key", "ledger", "git_repo", "config_pin", "prompts_manifest", "skills_manifest", "evidence_dir")
SUBSETS = [s for n in range(1, 9) for s in itertools.combinations(INPUTS, n)]
assert len(SUBSETS) == 255
STARTUP = {"git_repo", "config_pin", "prompts_manifest", "skills_manifest", "evidence_dir"}
NAMES = {"docker": "SANDBOX_UNAVAILABLE", "llm_key": "LLM_NOT_CONFIGURED", "ledger": "ledger", "git_repo": "DLV_REPO_PATH",
         "config_pin": "deer-flow config", "prompts_manifest": "prompts", "skills_manifest": "skills", "evidence_dir": "evidence"}


@pytest.fixture(scope="module")
def tampered_roots():
    """Copies of prompts/skills/config with one byte changed (start-up refusals) — built once."""
    tmp = tempfile.mkdtemp(prefix="dlv-prop-")
    shutil.copytree(SERVICE_ROOT / "prompts", os.path.join(tmp, "prompts"))
    p = os.path.join(tmp, "prompts", "engine.system.md")
    with open(p, "ab") as fh:
        fh.write(b"\n")
    shutil.copytree(SERVICE_ROOT / "skills", os.path.join(tmp, "skills"))
    with open(os.path.join(tmp, "skills", "custom", "extra.md"), "w") as fh:
        fh.write("x")
    shutil.copytree(SERVICE_ROOT / "config", os.path.join(tmp, "config"))
    p = os.path.join(tmp, "config", "deerflow.engine.yaml")
    with open(p, "ab") as fh:
        fh.write(b"\n# tampered\n")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("absent", SUBSETS, ids=["+".join(s) for s in SUBSETS])
def test_property_refusal_leaves_nothing_behind(absent, tampered_roots):
    absent = set(absent)
    tmp = tempfile.mkdtemp(prefix="dlv-p-")
    try:
        repo, base_sha = make_repo(tmp)
        extra = {}
        if "config_pin" in absent:
            extra["DLV_DEERFLOW_CONFIG"] = os.path.join(tampered_roots, "config", "deerflow.engine.yaml")
        if "prompts_manifest" in absent:
            extra["DLV_PROMPTS_DIR"] = os.path.join(tampered_roots, "prompts")
        if "skills_manifest" in absent:
            extra["DLV_SKILLS_ROOT"] = os.path.join(tampered_roots, "skills")
        if "git_repo" in absent:
            extra["DLV_REPO_PATH"] = os.path.join(tmp, "no-such-repo")
        env = base_env(tmp, repo, llm="none" if "llm_key" in absent else "fake", extra=extra)
        data_dir = env["DLV_DATA_DIR"]
        if "evidence_dir" in absent:
            os.makedirs(data_dir, exist_ok=True)
            with open(os.path.join(data_dir, "evidence"), "w") as fh:
                fh.write("not a directory")
        settings = C.load(env)
        startup = absent & STARTUP
        if startup:
            with pytest.raises(RuntimeError) as exc:
                if startup & {"config_pin", "prompts_manifest", "skills_manifest", "git_repo"}:
                    G.run(settings, env, site_packages=SITE_PACKAGES)
                Harness(tmp=tmp, docker="docker" not in absent, llm="none" if "llm_key" in absent else "fake",
                        ledger_ok="ledger" not in absent, extra_env=extra, wire_harness=False, site_packages=SITE_PACKAGES)
            msg = str(exc.value)
            assert any(NAMES[i] in msg for i in startup), (absent, msg)
        else:
            h = Harness(tmp=tmp, docker="docker" not in absent, llm="none" if "llm_key" in absent else "fake",
                        ledger_ok="ledger" not in absent, wire_harness=False, site_packages=SITE_PACKAGES)
            try:
                h.svc._engine = object()                    # the engine is present; only the inputs are absent
                r = h.post("/dlv/v1/fix-runs", two_findings(h.base_sha))
                assert r.status_code == 503, (absent, r.text)
                body = r.json()
                if "docker" in absent or "llm_key" in absent:
                    assert body["reasons"][0]["code"] in ("SANDBOX_UNAVAILABLE", "LLM_NOT_CONFIGURED"), (absent, body)
                    assert body["took_effect"] is False
                else:
                    assert "ledger" in body["detail"] and body["took_effect"] is False
                assert h.svc.runs == {}
                assert not h.docker.argv_of("run")
            finally:
                h.close()
        # nothing left behind
        assert os.listdir(os.path.join(tmp, "worktrees")) == []
        ev = os.path.join(data_dir, "evidence")
        assert not os.path.isdir(ev) or os.listdir(ev) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_subset_count_is_255():
    assert len(SUBSETS) == 255 and len({json.dumps(s) for s in SUBSETS}) == 255
