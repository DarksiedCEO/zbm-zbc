"""Fix wave 26b (CI #3, delivery-py 3.13 macos-26): the argv-level Docker double runs the adapter's commands on the
host, and on a host without GNU coreutils/findutils (macOS) the two GNU-only spellings the adapter emits failed —
``mv -f -T`` (every ``put_bytes``: the engine's ini write, every file-tool write) and ``find … -printf '%p\\n'``
(``ls``) — so every harness run ended in HARNESS_ERROR "write failed (mv)" before the model's first call, and the
adversarial tests saw no tool decisions at all. The real sandbox is Debian (GNU tools), so production never saw it.
The double now gives those two spellings their GNU meaning on any host; these tests pin that meaning."""

from __future__ import annotations

import os

from fakes import FakeDockerCli

WS = "/mnt/user-data/workspace"


def _box(tmp_path):
    d = FakeDockerCli(str(tmp_path / "docker"))
    assert d.run(["run", "--name", "dlv-g", "img"], timeout_s=30).exit_code == 0
    vol = d.containers["dlv-g"]
    return d, vol


def _exec(d, *cmd):
    return d.run(["exec", "dlv-g", *cmd], timeout_s=30)


def test_the_double_runs_gnu_mv_no_target_directory_on_any_host(tmp_path):
    d, vol = _box(tmp_path)
    os.makedirs(os.path.join(vol, "stage"))
    with open(os.path.join(vol, "stage", "f"), "w") as fh:
        fh.write("new")
    with open(os.path.join(vol, "dest"), "w") as fh:
        fh.write("old")
    r = _exec(d, "mv", "-f", "-T", "--", f"{WS}/stage/f", f"{WS}/dest")
    assert r.exit_code == 0, r
    assert open(os.path.join(vol, "dest")).read() == "new"
    assert not os.path.exists(os.path.join(vol, "stage", "f"))


def test_the_doubles_mv_t_never_moves_into_a_directory(tmp_path):
    # GNU -T: the destination is the name itself, never "into" it — a directory there (an R7 swap) is refused
    d, vol = _box(tmp_path)
    os.makedirs(os.path.join(vol, "stage"))
    with open(os.path.join(vol, "stage", "f"), "w") as fh:
        fh.write("x")
    os.makedirs(os.path.join(vol, "dest"))
    r = _exec(d, "mv", "-f", "-T", "--", f"{WS}/stage/f", f"{WS}/dest")
    assert r.exit_code != 0, r
    assert os.listdir(os.path.join(vol, "dest")) == []
    assert os.path.exists(os.path.join(vol, "stage", "f"))


def test_the_double_runs_gnu_find_printf_path_on_any_host(tmp_path):
    d, vol = _box(tmp_path)
    os.makedirs(os.path.join(vol, "a", "b"))
    open(os.path.join(vol, "a", "f.txt"), "w").close()
    r = _exec(d, "find", f"{WS}/a", "-maxdepth", "1", "-mindepth", "1", "-printf", "%p\n")
    assert r.exit_code == 0, r
    assert sorted(r.stdout.decode().splitlines()) == [f"{WS}/a/b", f"{WS}/a/f.txt"]


# ====================================================================== CI #3 macos-26: the service could not start
def test_macos_corefoundation_encoding_name_is_allowed_on_darwin_only_and_only_in_its_shape():
    """CPython on macOS gets ``__CF_USER_TEXT_ENCODING`` written into its own environment by CoreFoundation at start
    (``env -i python -c 'import os; print(sorted(os.environ))'`` → ``['LC_CTYPE', '__CF_USER_TEXT_ENCODING']``), so the
    launcher refused every start on macOS (CI #3: four L2 errors, two failures). Same class as LC_CTYPE (written by
    CPython's PEP 538 coercion): allowed — but on darwin only, and only as the three-hex-field value CF writes."""
    from zbm_delivery import config as C
    name = "__CF_USER_TEXT_ENCODING"
    assert C.env_problems({name: "0x1F5:0x0:0x0"}, platform="darwin") == []
    assert C.env_problems({name: "0x1F5:0x0:0x0"}, platform="linux") == [name]
    for bad in ("", "x", "0x1F5:0x0", "0x1F5:0x0:0x0;rm", "0x1F5:0x0:0x0\n", "1F5:0:0"):
        assert C.env_problems({name: bad}, platform="darwin") == [f"{name} (value is not CoreFoundation's 0xH:0xH:0xH)"], bad


def test_env_problems_defaults_to_this_platform():
    import sys
    from zbm_delivery import config as C
    assert C.env_problems({"__CF_USER_TEXT_ENCODING": "0x1F5:0x0:0x0"}) == ([] if sys.platform == "darwin" else ["__CF_USER_TEXT_ENCODING"])


# ====================================================================== N25-D-4: a recorded request replays as recorded
def test_a_recorded_fix_run_replays_its_answer_after_dlv_max_findings_is_lowered():
    """AEGIS r25 N25-D-4: DLV_MAX_FINDINGS was checked in the route BEFORE the idempotency lookup, so a request that
    was admitted and recorded, replayed identically after the cap was lowered (and the service restarted), got 422
    instead of its recorded answer. The cap now applies to new requests only: after the lookup, before anything is
    recorded. (The route reads the setting per request, so lowering it on the live settings object is the restart.)"""
    from helpers import Harness, two_findings
    h = Harness(scenario=[], extra_env={"DLV_MAX_FINDINGS": "2"})
    try:
        doc = two_findings(h.base_sha)
        first = h.post("/dlv/v1/fix-runs", doc)
        assert first.status_code == 202, first.text
        h.settings.max_findings = 1
        n_events = len(h.events())
        again = h.post("/dlv/v1/fix-runs", doc)
        assert again.status_code == 202, again.text
        assert again.json() == first.json()
        assert len(h.events()) == n_events                            # a replay records nothing
        other = dict(doc, request_id=doc["request_id"] + "-new")       # a NEW request is still capped
        r = h.post("/dlv/v1/fix-runs", other)
        assert r.status_code == 422 and "DLV_MAX_FINDINGS" in r.text, r.text
        assert len(h.events()) == n_events
    finally:
        h.svc.wait_idle(240)
        h.close()


# ====================================================================== N25-D-1: non-UTF-8 source is not "text in full"
def _git_repo(tmp_path):
    import subprocess

    def git(*a, text=True):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=tmp_path, check=True,
                              capture_output=True, text=text).stdout
    git("init", "-q")
    return git


def test_non_utf8_source_in_a_diff_is_named_and_utf8_text_is_not(tmp_path):
    """AEGIS r25 N25-D-1: gitport decoded git's output with errors="replace", so a Latin-1 byte in a source file
    reached the report as U+FFFD under the header "in full as text" — the bytes were bound only by the 7-hex index
    line. Like a binary change (wave 25 H8), a source file whose diff is not UTF-8 cannot be shown faithfully, so it is
    named; a UTF-8 file that legitimately contains U+FFFD is not."""
    from zbm_delivery.engine import srcdiff
    from zbm_delivery.gitport import GitPort
    git = _git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "gone.py").write_bytes(b"N = '\xe9t\xe9'\n")          # removed below: its old lines count too
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    (tmp_path / "src" / "a.py").write_text("x = '� ok'\n")             # valid UTF-8, a literal replacement char
    (tmp_path / "src" / "latin1.py").write_bytes(b"NAME = 'caf\xe9'\n")
    (tmp_path / "src" / "gone.py").unlink()
    git("add", "-A")
    git("commit", "-qm", "change")
    head = git("rev-parse", "HEAD").strip()
    gp = GitPort(str(tmp_path), record=lambda *a, **k: "")
    raw = gp.range_diff_raw(str(tmp_path), base, head, ["src/a.py", "src/gone.py", "src/latin1.py"])
    assert isinstance(raw, bytes) and b"caf\xe9" in raw
    assert srcdiff.non_utf8_paths(raw) == ["src/gone.py", "src/latin1.py"]
    assert srcdiff.non_utf8_paths(gp.range_diff_raw(str(tmp_path), base, head, ["src/a.py"])) == []
    assert srcdiff.is_non_utf8_file(str(tmp_path / "src" / "latin1.py"))
    assert not srcdiff.is_non_utf8_file(str(tmp_path / "src" / "a.py"))
    assert not srcdiff.is_non_utf8_file(str(tmp_path / "src" / "missing.py"))


def test_a_non_utf8_file_written_under_src_fails_the_round():
    """The agent's way to a non-UTF-8 source file (the file tool writes UTF-8): bash. The round fails as a binary
    change does, naming the file, and the finding is not done."""
    from helpers import Harness, flat, two_findings
    from test_round23 import DONE, _p1, _states, _whys
    from test_round24 import OLD
    from helpers import replace
    latin1 = flat([{"tool_calls": [{"name": "bash", "args": {
        "command": "printf 'NAME = \"caf\\351\"\\n' > /mnt/user-data/workspace/services/toy-py/src/toy/names.py"}}]},
        replace("src/toy/calc.py", OLD, "    if whole == 0:\n        return 0.0\n" + OLD)])
    h = Harness(scenario=_p1(latin1), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        h.svc.wait_idle(240)
        assert "binary_src_change" in _whys(h), (_whys(h), h.run(run_id).get("reasons"))
        assert _states(h, run_id)["N1-2"] not in DONE
        failed = [e["payload"] for e in h.events("round_failed") if e["payload"].get("why") == "binary_src_change"]
        assert failed and "src/toy/names.py" in " ".join(failed[0]["paths"]), failed
    finally:
        h.close()


# ====================================================================== N24-D-1-res / N25-D-2: a cancel is for good
def test_a_cancelled_admission_replayed_after_its_answer_left_the_map_is_refused_as_cancelled(monkeypatch):
    """N24-D-1-res (E-B) and AEGIS r25 N25-D-2: the bounded map was the only memory of a cancel once its containers
    had ended, so a cancelled review replayed after its answer left the map ran its RED containers again and was
    judged afresh (N25-D-2: a concurrent replay even cleared the first attempt's cancel marker, and the cancelled
    review was recorded `reviewed_fail` with a child run). Cancelled admission ids are now kept apart, never evicted
    (one per operator cancel), and rebuilt from the local log at start: a replay gets the cancel's 409 and nothing
    runs. Shown with the map at its extreme (keeps nothing)."""
    import threading
    import time
    from helpers import Harness, finding, rid, scenario_s1, two_findings
    from test_round24 import HANG_PATH, HANG_RT
    from zbm_delivery import service as SV
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 0, raising=False)
    h = Harness(scenario=scenario_s1() + scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run1)["status"] == "awaiting_review"
        nf = finding("N9-4", line=15, class_hint="argument_validation",
                     reproduction=f"run {HANG_PATH}::test_clamp_hangs: clamp(5, 3, 0) answers 3", expected="ValueError",
                     observed="3", reproduction_test={"path": HANG_PATH, "content": HANG_RT})
        body = {"request_id": rid(), "review_ref": "r26b-cancel", "sha256": "f" * 64, "verdict": "fail", "reopened": [],
                "new_findings": [nf]}
        res = {}
        th = threading.Thread(target=lambda: res.setdefault("a", h.post(f"/dlv/v1/fix-runs/{run1}/review", body)))
        th.start()
        t0 = time.monotonic()
        while not h.events("reproduction_red_check_started") and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        adm = h.events("reproduction_red_check_started")[0]["payload"]["admission_id"]
        rc = h.post(f"/dlv/v1/fix-runs/{adm}/cancel", {"request_id": rid(), "reason": "stop it"}, caller="andre_session")
        assert rc.status_code == 200, rc.text
        th.join(120)
        assert res["a"].status_code == 409 and "cancelled" in res["a"].text, res["a"].text
        assert adm not in h.svc._closed_admissions                     # the map kept nothing
        boxes = len([e for e in h.events("engine_box_started") if e["payload"].get("tag") == "admission"])
        again = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert again.status_code == 409 and "cancelled" in again.text, again.text
        assert len([e for e in h.events("engine_box_started") if e["payload"].get("tag") == "admission"]) == boxes
        h.svc.wait_idle(240)
        assert not h.events("fix_run_reviewed")
        assert not [r for r in h.svc.runs.values() if "N9-4" in (r.get("finding_ids") or [])]
    finally:
        h.svc.wait_idle(240)
        h.close()


def test_cancelled_admission_ids_survive_the_start_up_replay_of_the_log(monkeypatch):
    from helpers import Harness
    from zbm_delivery import service as SV
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 1, raising=False)
    h = Harness(scenario=[])
    try:
        h.svc._apply("admission_closed", SV.DeliveryService._cancelled_answer("adm-c", "req-c", "f" * 64))
        for i in range(5):
            h.svc._apply("admission_closed", {"admission_id": f"adm-{i}", "status": 422, "reason": "r", "body": {}})
        assert "adm-c" not in h.svc._closed_admissions and "adm-c" in h.svc._cancelled_admissions
        assert not {f"adm-{i}" for i in range(5)} & h.svc._cancelled_admissions     # refusals are not cancels
    finally:
        h.close()


# ====================================================================== DLV-HOST: the double keeps no host cargo state
def test_the_docker_double_gives_cargo_a_session_home_not_the_hosts(monkeypatch):
    """E-B DLV-HOST: the double ran the toy-rs suite with the host's ~/.cargo as CARGO_HOME (registry, caches, the
    host's cargo config — host state read, and writable). CARGO_HOME is now the session's own, removed with the session
    root like GOCACHE (DLV_TEST_CARGO_HOME keeps one elsewhere); only the toolchain itself (RUSTUP_HOME) is the host's,
    used read-only, as the image's is."""
    import os
    import tempfile
    import _tmproot
    from fakes import FakeDockerCli
    monkeypatch.delenv("CARGO_HOME", raising=False)
    monkeypatch.delenv("DLV_TEST_CARGO_HOME", raising=False)
    env = FakeDockerCli.toolchain_env()
    session = os.path.realpath(_tmproot.SESSION_TMP)
    assert os.path.commonpath([os.path.realpath(env["CARGO_HOME"]), session]) == session, env["CARGO_HOME"]
    assert os.path.isdir(env["CARGO_HOME"])
    assert env["CARGO_HOME"] != os.path.join(os.path.expanduser("~"), ".cargo")
    keep = tempfile.mkdtemp()
    monkeypatch.setenv("DLV_TEST_CARGO_HOME", keep)
    assert FakeDockerCli.toolchain_env()["CARGO_HOME"] == keep


# ====================================================================== C6-3-res: git needs no directory of its own
_GIT_THEN_WAIT = r'''
import subprocess, sys
sys.path.insert(0, "src")
from zbm_delivery.gitport import GitPort
repo = sys.argv[1]
sha = GitPort(repo, record=lambda *a, **k: "").rev_parse("HEAD")
print("GIT-DONE", sha, flush=True)
sys.stdin.read()
'''


def test_a_process_killed_after_its_git_commands_leaves_nothing_in_its_temp_dir(tmp_path):
    """E-B C6-3-res: since C6-3 the isolation dir (`dlv-git-*`: a private HOME and an empty hooks dir) was made at the
    first git command and removed only by atexit, so a process SIGKILLed after it left the dir in its TMPDIR. git
    needs no directory for either: hooks are off with `core.hooksPath=/dev/null`, and HOME is `/nonexistent` (with
    GIT_CONFIG_GLOBAL=/dev/null and no system config, git reads nothing there). Shown: a child runs a real git command
    through GitPort with a private TMPDIR, is SIGKILLed, and the TMPDIR is empty."""
    import os
    import subprocess
    import sys
    from helpers import SERVICE_ROOT
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (["git", "init", "-q"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                                         "--allow-empty", "-m", "base"]):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    tmpdir = tmp_path / "t"
    tmpdir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update({"TMPDIR": str(tmpdir), "PYTHONDONTWRITEBYTECODE": "1"})
    p = subprocess.Popen([sys.executable, "-c", _GIT_THEN_WAIT, str(repo)], cwd=SERVICE_ROOT, env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline()
        assert line.startswith("GIT-DONE "), line + p.stderr.read()
        while_alive = sorted(x.name for x in tmpdir.iterdir())
    finally:
        p.kill()
        p.communicate()
    after = sorted(x.name for x in tmpdir.iterdir())
    assert while_alive == [] and after == [], (while_alive, after)


def test_the_git_isolation_is_no_hooks_and_a_home_that_does_not_exist():
    from zbm_delivery import gitport
    home, hooks = gitport._isolation()
    assert (home, hooks) == ("/nonexistent", "/dev/null") and not os.path.exists(home)
    assert gitport.git_env()["HOME"] == home and gitport.git_env()["XDG_CONFIG_HOME"].startswith(home + "/")
    assert gitport.isolation_args()[:2] == ("-c", "core.hooksPath=/dev/null")
