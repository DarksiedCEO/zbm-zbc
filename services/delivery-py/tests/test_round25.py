"""
Fix wave 25 — AEGIS round 24 findings (Oct 1, 2026), delivery (H8).

N24-D-1  `_closed_admissions` (the recorded answer of every admission refused after its RED check ran, or cancelled
         while it ran) was an unbounded map, re-materialised from the local log at every start. It is now kept like
         `idem`: the most recent CLOSED_ADMISSIONS_MAX, oldest first out, at start too. A cancel made while the
         admission's containers run is honoured when they end whatever the bounded map has forgotten meanwhile; an
         answer that has gone out of it is not needed for safety — a replay of that admission runs its RED containers
         again under the same admission id and is refused (measured: its sandbox-exec completion record collides with
         the first attempt's; a collision, not a rule) — never admitted without a RED verdict recorded for it.
         The log itself is not compacted: its lines are anchored in the evidence ledger, like every other record's.
N24-D-2  a binary file under src was "shown" in the report's complete source diff as "Binary files ... differ" —
         its content never — while the header said every source file was there "in full". A binary change under
         src now fails the round (`binary_src_change`), and a source diff that still carries content git cannot show
         as text (a binary, a submodule pointer) fails the run before any report is written.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from helpers import SERVICE_ROOT, Harness, finding, findings_doc, flat, review_body, rid, scenario_s1, two_findings
from test_round23 import _p1, _states, _whys
from test_round23 import DONE
from test_round24 import HANG_PATH, HANG_RT, OLD

from zbm_delivery import service as SV
from zbm_delivery.engine import srcdiff
from helpers import replace


# ====================================================================== N24-D-1: bounded closed admissions

def test_closed_admission_answers_are_bounded_like_the_idempotency_records(monkeypatch):
    """Applying more admission_closed records than the cap (the start-up replay of the local log does exactly this)
    keeps the most recent CLOSED_ADMISSIONS_MAX. b51f307: a plain dict, every one kept."""
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 3, raising=False)
    h = Harness(scenario=[])
    try:
        for i in range(10):
            h.svc._apply("admission_closed", {"admission_id": f"adm-{i}", "status": 409, "reason": "r", "body": {}})
        assert list(h.svc._closed_admissions) == ["adm-7", "adm-8", "adm-9"], list(h.svc._closed_admissions)
        h.svc._apply("admission_closed", {"admission_id": "adm-8", "status": 409, "reason": "r", "body": {}})
        assert list(h.svc._closed_admissions) == ["adm-7", "adm-9", "adm-8"]       # the most recent use kept
    finally:
        h.close()


def test_a_cancel_made_while_the_red_check_runs_is_honoured_even_when_the_map_keeps_nothing(monkeypatch):
    """The cap at its extreme (0: no answer is kept): the operator's cancel still refuses the review whose containers
    were running — the bounded map is a convenience for replays, never what enforces the cancel."""
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 0, raising=False)
    h = Harness(scenario=scenario_s1() + scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run1)["status"] == "awaiting_review"
        nf = finding("N9-1", line=15, class_hint="argument_validation",
                     reproduction=f"run {HANG_PATH}::test_clamp_hangs: clamp(5, 3, 0) answers 3", expected="ValueError",
                     observed="3", reproduction_test={"path": HANG_PATH, "content": HANG_RT})
        body = {"request_id": rid(), "review_ref": "r25-h8", "sha256": "d" * 64, "verdict": "fail", "reopened": [],
                "new_findings": [nf]}
        res = {}
        th = threading.Thread(target=lambda: res.setdefault("a", h.post(f"/dlv/v1/fix-runs/{run1}/review", body)))
        th.start()
        t0 = time.monotonic()
        while not h.events("reproduction_red_check_started") and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        adm = h.events("reproduction_red_check_started")[0]["payload"]["admission_id"]
        rc = h.post(f"/dlv/v1/fix-runs/{adm}/cancel", {"request_id": rid(), "reason": "stop the pending review"},
                    caller="andre_session")
        assert rc.status_code == 200, rc.text
        assert adm not in h.svc._closed_admissions                    # nothing kept by the map
        th.join(120)
        a = res["a"]
        assert a.status_code == 409 and "cancelled" in a.text, a.text
        h.svc.wait_idle(240)
        assert not h.events("fix_run_reviewed")
        assert not [r for r in h.svc.runs.values() if "N9-1" in (r.get("finding_ids") or [])]
        assert not h.svc._cancelled_running                           # and nothing left behind
    finally:
        h.svc.wait_idle(240)
        h.close()


def test_a_replay_of_an_admission_whose_answer_is_no_longer_kept_is_refused_never_admitted(monkeypatch):
    """What the bound costs, shown (wave 25 E-B, measured): a refused admission's answer pushed out of the map, the
    same review replayed — its RED containers RUN AGAIN under the same admission id (not "nothing runs", as with the
    answer kept), and it is refused: measured 422 `reproduction_red_unverified`, because the re-run's sandbox-exec
    completion record collides with the first attempt's (same id, other elapsed time/output). That collision is what
    refuses it, not a rule; the guarantee is only that nothing is admitted without a RED verdict recorded for it."""
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 0, raising=False)
    green = ("from toy import calc\n\n\ndef test_clamp_is_green_on_base():\n"
             "    assert calc.clamp(5, 3, 0) == 3\n")
    h = Harness(scenario=scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run1)["status"] == "awaiting_review"
        nf = finding("N9-3", line=15, class_hint="argument_validation",
                     reproduction=f"run {HANG_PATH}::test_clamp_is_green_on_base: clamp(5, 3, 0) answers 3",
                     expected="ValueError", observed="3", reproduction_test={"path": HANG_PATH, "content": green})
        body = {"request_id": rid(), "review_ref": "r25-h8b", "sha256": "e" * 64, "verdict": "fail", "reopened": [],
                "new_findings": [nf]}
        first = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert first.status_code == 422 and first.json()["code"] == "reproduction_not_red", first.text
        assert not h.svc._closed_admissions
        boxes = len([e for e in h.events("engine_box_started") if e["payload"].get("tag") == "admission"])
        again = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert again.status_code in (409, 422, 503), again.text
        assert len([e for e in h.events("engine_box_started") if e["payload"].get("tag") == "admission"]) > boxes
        h.svc.wait_idle(240)
        assert h.run(run1)["status"] == "awaiting_review" and not h.events("fix_run_reviewed")
        assert not [r for r in h.svc.runs.values() if "N9-3" in (r.get("finding_ids") or [])]
    finally:
        h.svc.wait_idle(240)
        h.close()


# ====================================================================== N24-D-2: nothing un-showable reaches a report

def test_binary_and_submodule_changes_are_what_a_diff_cannot_show(tmp_path):
    """Against a real git repository: a binary file added and changed, a submodule pointer, a text change — only the
    text change is shown as text; the others are named."""
    def git(*a):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always",
                               *a], cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "blob.bin").write_bytes(b"\x00\x01\x02")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    (tmp_path / "src" / "a.py").write_text("x = 2\n")
    (tmp_path / "src" / "blob.bin").write_bytes(b"\x00\x01\x03")
    (tmp_path / "src" / "new.bin").write_bytes(b"PK\x03\x04\x00\x00")
    git("add", "-A")
    git("update-index", "--add", "--cacheinfo", f"160000,{base},src/vendored")     # a submodule pointer (gitlink)
    git("commit", "-qm", "change")
    diff = git("diff", "--no-renames", "--no-color", "--no-ext-diff", base, "HEAD")
    assert srcdiff.binary_paths(diff) == ["src/blob.bin", "src/new.bin", "src/vendored (submodule)"], diff
    text_only = git("diff", "--no-renames", "--no-color", "--no-ext-diff", base, "HEAD", "--", "src/a.py")
    assert srcdiff.binary_paths(text_only) == []
    assert srcdiff.binary_paths('Binary files "a/src/\\303\\244.bin" and "b/src/\\303\\244.bin" differ\n') == [
        '"a/src/\\303\\244.bin" and "b/src/\\303\\244.bin"']      # a quoted name is never dropped
    assert srcdiff.is_binary_file(str(tmp_path / "src" / "new.bin"))
    assert not srcdiff.is_binary_file(str(tmp_path / "src" / "a.py"))


BINARY_FIX = flat([{"tool_calls": [{"name": "write_file", "args": {
    "path": "/mnt/user-data/workspace/services/toy-py/src/toy/table.bin", "content": "\x00\x01lookup\x00"}}]},
    replace("src/toy/calc.py", OLD, "    if whole == 0:\n        return 0.0\n" + OLD)])


def test_a_binary_file_written_under_src_fails_the_round():
    """b51f307: the binary went into the commit and the report showed "Binary files ... differ" under a header that
    said every source file was there in full."""
    h = Harness(scenario=_p1(BINARY_FIX), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        h.svc.wait_idle(240)
        assert "binary_src_change" in _whys(h), _whys(h)
        st = _states(h, run_id)
        assert st["N1-2"] not in DONE, st
        failed = [e["payload"] for e in h.events("round_failed") if e["payload"].get("why") == "binary_src_change"]
        assert failed and "src/toy/table.bin" in " ".join(failed[0]["paths"]), failed
    finally:
        h.close()


def test_the_report_header_says_exactly_what_the_diff_shows():
    from zbm_delivery.engine import report as RP
    lines = RP.source_diff_section({"src_diff_sha256": "0" * 64, "src_diff_evidence_id": "ev", "base_sha": "b" * 40},
                                   lambda ev: "diff --git a/x b/x\n")
    head = " ".join(lines[:3])
    assert "in full as text" in head and "binary_src_change" in head, head


# ====================================================================== scout B H1: the Docker live job's input survives

_ENV_READ = re.compile(r"""os\.environ(?:\.get\(\s*|\[\s*)["'](DLV_[A-Z0-9_]+)["']""")


def test_every_dlv_variable_a_test_reads_survives_the_conftest_scrub():
    """The class behind scout B H1: conftest.py removes every DLV_* variable except SUITE_SETTINGS, so a DLV_* name a
    test (or a test helper) reads from os.environ that is not in SUITE_SETTINGS can never be set from outside — the
    test sees it unset on every machine. 0f017a7: DLV_LIVE_SANDBOX_IMAGE (test_live_docker.py) was such a name."""
    import conftest
    read = {}
    for p in sorted(Path(__file__).resolve().parent.glob("*.py")):
        for m in _ENV_READ.finditer(p.read_text(encoding="utf-8")):
            read.setdefault(m.group(1), set()).add(p.name)
    assert {"DLV_LIVE_SANDBOX_IMAGE", "DLV_TEST_PORT_RANGE", "DLV_LIVE_LOG_DIR"} <= set(read), read
    scrubbed = {k: sorted(v) for k, v in read.items() if k not in conftest.SUITE_SETTINGS}
    assert not scrubbed, f"read by the suite but removed by conftest.py before any test runs: {scrubbed}"


_FAKE_DOCKER = '''#!{python}
import json, os, sys
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["info"]:
    print("29.4.3")
    sys.exit(0)
sys.stderr.write("fake docker (wave 25 wiring probe): every verb but info is refused\\n")
sys.exit(1)
'''


def test_the_docker_live_job_input_reaches_the_live_tests(tmp_path):
    """Scout B H1: the CI job delivery-docker-live sets DLV_LIVE_SANDBOX_IMAGE and fails when a live Docker test is
    skipped; conftest.py removed the variable before the tests read it, so all three skipped "is not set" on every
    machine and the job could never pass. Here the variable is set from OUTSIDE on a child pytest of
    tests/test_live_docker.py, with a fake `docker` first on PATH that answers `info` (a daemon is "reachable") and
    refuses everything else: the tests must get past the skip and reach the real adapter, which asks the fake to
    `run` the image the variable names. What the fake cannot prove — the nine container properties — only a real
    daemon can (the CI job); this proves only that the job's input arrives."""
    image = "127.0.0.1:5000/zbm/dlv-sandbox@sha256:" + "a" * 64
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker-argv.jsonl"
    shim = bindir / "docker"
    shim.write_text(_FAKE_DOCKER.format(python=sys.executable, log=str(log)), encoding="utf-8")
    shim.chmod(0o755)
    junit = tmp_path / "live-docker.xml"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update({"DLV_LIVE_SANDBOX_IMAGE": image, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHONDONTWRITEBYTECODE": "1"})
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "tests/test_live_docker.py",
                        f"--junitxml={junit}"], cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=600)
    out = r.stdout[-3000:] + r.stderr[-2000:]
    import xml.etree.ElementTree as ET
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    skipped = [c.get("name") for c in cases if c.find("skipped") is not None]
    assert len(cases) == 3 and not skipped, out           # the CI job's own check: three cases, none skipped
    assert "is not set" not in out, out
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    runs = [c for c in calls if c[:1] == ["run"]]
    assert runs and all(image in c for c in runs), calls  # the real adapter asked for exactly the job's image
    # and they failed on the fake's refusal (exit 1: errors, never "passed" by a fake that proves nothing)
    assert r.returncode == 1 and "docker run failed" in out, out


# ====================================================================== scout B M1: the Docker double kills the whole box

_LEADER = r'''
import os, subprocess, sys, time
d = sys.argv[1]
hold = "import os,sys,time; open(sys.argv[1]+'/hold.pid','w').write(str(os.getpid())); time.sleep(20); open(sys.argv[1]+'/hold.survived','w').write('1')"
loose = "import os,sys,time; open(sys.argv[1]+'/loose.pid','w').write(str(os.getpid())); time.sleep(20); open(sys.argv[1]+'/loose.survived','w').write('1')"
subprocess.Popen([sys.executable, "-c", hold, d])                                                  # holds the exec's pipes
subprocess.Popen([sys.executable, "-c", loose, d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # does not
while not (os.path.exists(d + "/hold.pid") and os.path.exists(d + "/loose.pid")):
    time.sleep(0.01)
open(d + "/ready", "w").write("1")
time.sleep(60)
'''


def _gone(pid: int, within_s: float = 15.0) -> bool:
    """True once ``pid`` no longer exists (polled; a state, not a sleep)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < within_s:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True                               # a zombie: dead, waiting for whoever reaps it
        except OSError:
            pass
        time.sleep(0.05)
    return False


def _box(tmp_path):
    from fakes import FakeDockerCli
    d = FakeDockerCli(str(tmp_path / "docker"))
    assert d.run(["run", "--name", "dlv-m1", "img"], timeout_s=30).exit_code == 0
    marks = tmp_path / "marks"
    marks.mkdir()
    return d, marks


def test_the_docker_double_kills_every_process_of_a_command_past_its_deadline(tmp_path):
    """Scout B M1: on the deadline the double killed only the process it started (`node --test`, `cargo test`, `go
    test`), so a per-file test process / test binary outlived the suite (pid 19025, `node tests/zzz_hang.test.ts`),
    and for one that still held the exec's pipes `communicate()` waited for it. The real sandbox kills the whole
    container; the double now kills the command's whole process group. Shown with a leader that starts one child
    holding its pipes and one that does not: past the 3 s deadline both are gone and neither lived to its 20 s mark."""
    d, marks = _box(tmp_path)
    r = d.run(["exec", "dlv-m1", "timeout", "-k", "5", "3", "python", "-c", _LEADER, str(marks)], timeout_s=60)
    assert r.timed_out and (marks / "ready").exists(), (r, sorted(p.name for p in marks.iterdir()))
    for name in ("hold", "loose"):
        pid = int((marks / f"{name}.pid").read_text())
        assert _gone(pid), f"{name} child {pid} outlived its command's deadline"
        assert not (marks / f"{name}.survived").exists(), f"{name} child ran to its 20 s mark"


def test_docker_kill_on_the_double_kills_every_process_the_box_runs(tmp_path):
    """Scout B M1 (the `kill` verb, fakes.py:174-178 at 0f017a7): it killed only the direct child of each running
    exec. Now the whole group of every command the container runs."""
    d, marks = _box(tmp_path)
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("r", d.run(
        ["exec", "dlv-m1", "timeout", "-k", "5", "120", "python", "-c", _LEADER, str(marks)], timeout_s=200)))
    th.start()
    t0 = time.monotonic()
    while not (marks / "ready").exists() and time.monotonic() - t0 < 60:
        time.sleep(0.02)
    assert (marks / "ready").exists()
    assert d.run(["kill", "dlv-m1"], timeout_s=30).exit_code == 0
    th.join(60)
    assert not th.is_alive() and res["r"].exit_code != 0, res
    for name in ("hold", "loose"):
        pid = int((marks / f"{name}.pid").read_text())
        assert _gone(pid), f"{name} child {pid} outlived `docker kill`"
        assert not (marks / f"{name}.survived").exists()


# ====================================================================== scout B Low: a setting nothing reads is gone

def test_dlv_live_port_range_is_not_a_service_setting(tmp_path):
    """Scout B (Low): config.py parsed DLV_LIVE_PORT_RANGE into Settings.live_port_range — read by nothing — and
    refused to start on a malformed value of a variable that changed nothing. The live tests' range is the suite's
    DLV_TEST_PORT_RANGE (tests/helpers.py); the service has no such setting."""
    import dataclasses
    from helpers import base_env
    from zbm_delivery import config as C
    assert "live_port_range" not in {f.name for f in dataclasses.fields(C.Settings)}
    env = dict(base_env(str(tmp_path), str(tmp_path / "repo")), DLV_LIVE_PORT_RANGE="not-a-range")
    C.load(env)                                       # 0f017a7: RuntimeError("DLV_LIVE_PORT_RANGE must be lo-hi")


# ====================================================================== scout C C6-3: no temp dir at import

_IMPORT_ONLY = r'''
import sys
sys.path.insert(0, "src")
import zbm_delivery.gitport, zbm_delivery.adapters.sandbox, zbm_delivery.engine.loop, zbm_delivery.api  # noqa: E401,F401
print("IMPORTED", flush=True)
sys.stdin.read()
'''


def test_importing_the_service_creates_nothing_in_the_temp_dir(tmp_path):
    """Scout C C6-3: gitport made its isolation dir (`dlv-git-*`) at IMPORT, in whatever TMPDIR the process had, and
    removed it only at a normal exit — a child killed by a signal (SIGKILL; SIGTERM in an entrypoint without the
    serve handler) left one behind for every process that merely imported the package, git used or not (scout C saw
    147 in /tmp). Now nothing is made until the first git command needs it. Shown by importing every module the
    service's entrypoint imports in a child with a private TMPDIR and SIGKILLing it."""
    tmpdir = tmp_path / "t"
    tmpdir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update({"TMPDIR": str(tmpdir), "PYTHONDONTWRITEBYTECODE": "1"})
    p = subprocess.Popen([sys.executable, "-c", _IMPORT_ONLY], cwd=SERVICE_ROOT, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline()
        assert line.strip() == "IMPORTED", line + p.stderr.read()
        left_while_alive = sorted(x.name for x in tmpdir.iterdir())
    finally:
        p.kill()
        p.communicate()
    left = sorted(x.name for x in tmpdir.iterdir())
    assert left_while_alive == [] and left == [], (left_while_alive, left)


# (C6-3's other half, "the isolation dir is made on first use", is superseded by fix wave 26b C6-3-res: git now needs
# no directory at all — tests/test_round26b.py `test_the_git_isolation_is_no_hooks_and_a_home_that_does_not_exist`.)


# ====================================================================== scout B M5: the ADR's pin table is the files'

def test_adr_0011_pinned_hash_table_matches_every_file_it_names():
    """Scout B M5: the "Pinned hashes" table of ADR 0011 went stale for two rows (the test-commands seed and uv.lock)
    while the files changed — nothing compared them. Every row naming a file of this service (first backticked path)
    must carry that file's sha256 now; a row naming a file that no longer exists fails too."""
    import hashlib
    adr = next((SERVICE_ROOT.parents[1] / "docs" / "adr").glob("0011-*.md")).read_text(encoding="utf-8")
    table = adr.split("### Pinned hashes", 1)[1].split("\n### ", 1)[0]
    rows = re.findall(r"^\| `([^`]+)`[^|]*\| `([0-9a-f]{64})`", table, re.M)
    assert len(rows) >= 10, rows
    wrong = []
    for rel, want in rows:
        p = SERVICE_ROOT / rel
        got = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "missing"
        if got != want:
            wrong.append(f"{rel}: ADR {want[:12]}…, file {got[:12]}")
    assert not wrong, wrong


# ====================================================================== wave 25 E-B: DLV_MAX_FINDINGS is applied

def test_dlv_max_findings_caps_the_findings_document():
    """Spec D12 / §B.1 ("1..`DLV_MAX_FINDINGS` findings", strict schema → 422): config.py parsed DLV_MAX_FINDINGS
    (1..200) into a setting nothing read, so DLV_MAX_FINDINGS=2 still admitted a three-finding document (the request
    model's own cap of 200 was the only one). Now a document over the cap is the same 422 schema answer a 201-finding
    document gets, and nothing is created; at the cap it is admitted."""
    h = Harness(scenario=[], extra_env={"DLV_MAX_FINDINGS": "2"})
    try:
        three = findings_doc(h.base_sha, [finding("N1-1", line=6), finding("N1-2", line=11), finding("N1-3", line=15)])
        n_events = len(h.events())
        r = h.post("/dlv/v1/fix-runs", three)
        assert r.status_code == 422, r.text
        assert any(e["loc"][-1] == "findings" and "DLV_MAX_FINDINGS" in e["msg"] for e in r.json()["detail"]), r.text
        assert not h.svc.runs and len(h.events()) == n_events, [e["event_type"] for e in h.events()[n_events:]]
        ok = h.post("/dlv/v1/fix-runs", two_findings(h.base_sha))
        assert ok.status_code == 202, ok.text
    finally:
        h.svc.wait_idle(240)
        h.close()


def test_dlv_max_findings_caps_the_run_a_failing_review_would_open():
    """D12 is per RUN: a failing review opens a child run holding the reopened findings plus the new ones, which the
    request model capped at 200 EACH (400 together) and DLV_MAX_FINDINGS not at all. Now a review whose child run
    would carry more than DLV_MAX_FINDINGS findings is a 422 before anything is admitted or run."""
    h = Harness(scenario=scenario_s1(), extra_env={"DLV_MAX_FINDINGS": "2"})
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run1)["status"] == "awaiting_review"
        nf = finding("N9-1", line=15, class_hint="argument_validation",
                     reproduction=f"run {HANG_PATH}::test_clamp_hangs: clamp(5, 3, 0) answers 3", expected="ValueError",
                     observed="3", reproduction_test={"path": HANG_PATH, "content": HANG_RT})
        body = review_body(h, run1, verdict="fail", reopened=("N1-1", "N1-2"), new_findings=(nf,))
        n_events = len(h.events())
        r = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert r.status_code == 422, r.text
        assert any("DLV_MAX_FINDINGS" in e["msg"] for e in r.json()["detail"]), r.text
        assert len(h.events()) == n_events, [e["event_type"] for e in h.events()[n_events:]]
        assert h.run(run1)["status"] == "awaiting_review"
    finally:
        h.svc.wait_idle(240)
        h.close()
