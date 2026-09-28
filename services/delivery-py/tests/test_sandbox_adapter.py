"""Sandbox adapter unit certification (spec §C.2): the exact ``docker run`` argv token by token, forbidden flags,
tag-only images, acquire without a run token, truncation, timeouts, record-first, env allowlist, path containment,
lease vs destroy. Everything a machine WITHOUT Docker can prove; tests/test_live_docker.py lists the rest."""

from __future__ import annotations

import os
import tarfile
import io
from datetime import timedelta

import pytest

from helpers import SERVICE_ROOT, Harness, IMAGE

from zbm_delivery import registry
from zbm_delivery.adapters import sandbox as S
from zbm_delivery.policy import WORKSPACE
from zbm_delivery.ports import DockerUnavailable, ExecResult


@pytest.fixture
def h():
    x = Harness(wire_harness=False)
    x.svc._engine = object()
    yield x
    registry.clear()
    x.close()


def _bind(h: Harness, run_id: str = "dlv-run-" + "A" * 26, deadline_s: int = 600) -> registry.RunBinding:
    b = registry.RunBinding(run_id=run_id, thread_id=f"t-{run_id}", service="toy-py", principal_user_id=f"zbm--{run_id}",
                            workspace=WORKSPACE, deadline_at=h.clock.now() + timedelta(seconds=deadline_s))
    registry.bind(b)
    return b


def test_docker_run_argv_token_by_token(h):
    argv = S.ZbmDockerSandboxProvider.run_argv(h.settings, "dlv-run-" + "A" * 26, "/data/sandbox-env/x.env")
    skills = os.path.realpath(str(SERVICE_ROOT / "skills"))
    tools = os.path.realpath(S.TOOLS_SRC_DIR)                    # wave 20 R8: the pinned resolver, read-only
    run_id = "dlv-run-" + "A" * 26
    assert argv == ["run", "-d", "--name", f"dlv-{run_id}", "--label", f"zbm.dlv.run={run_id}", "--user", "65532:65532", "--cap-drop=ALL",
                    "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
                    "--pids-limit", "512", "--memory", "4g", "--cpus", "2", "--network", "none",
                    "--mount", f"type=volume,src=dlv-ws-{run_id},dst={WORKSPACE},volume-label=zbm.dlv.run={run_id}",
                    "--mount", f"type=bind,src={skills},dst=/mnt/skills,ro",
                    "--mount", f"type=bind,src={tools},dst=/mnt/dlv,ro",
                    "--env-file", "/data/sandbox-env/x.env", IMAGE, "sleep", "infinity"]
    assert S.forbidden_run_token(argv) is None
    assert "docker.sock" not in " ".join(argv) and "--privileged" not in argv and "seccomp" not in " ".join(argv)


def test_env_file_is_exactly_the_allowlist(h):
    text = S.ZbmDockerSandboxProvider.env_file_text(h.svc.test_seed)
    lines = dict(ln.split("=", 1) for ln in text.strip().splitlines())
    assert set(lines) == {"PATH", "HOME", "LANG", "TZ", "PYTHONDONTWRITEBYTECODE", "CI", "PYTHONHASHSEED", "NO_COLOR", "FORCE_COLOR",
                          "CARGO_TERM_COLOR", "CARGO_NET_OFFLINE", "GOPROXY", "GOTOOLCHAIN"}
    assert lines["GOPROXY"] == "off" and lines["GOTOOLCHAIN"] == "local" and lines["CARGO_NET_OFFLINE"] == "true"
    assert lines["HOME"] == WORKSPACE and "DLV_SERVICE_TOKEN" not in text and "LEDGER" not in text


def test_acquire_needs_a_live_run_token_and_the_principal(h):
    p = S.ZbmDockerSandboxProvider()
    with pytest.raises(PermissionError, match="no live run token"):
        p.acquire("no-thread", user_id="zbm--x")
    b = _bind(h)
    with pytest.raises(PermissionError, match="principal"):
        p.acquire(b.thread_id, user_id="zbm--other")
    assert h.events("identity_mismatch")
    assert not h.docker.argv_of("run")
    # an expired deadline is not live
    b2 = _bind(h, run_id="dlv-run-" + "B" * 26, deadline_s=-1)
    with pytest.raises(PermissionError):
        p.acquire(b2.thread_id, user_id=b2.principal_user_id)
    # live → docker run, recorded first (crossing_docker_requested precedes the argv)
    sid = p.acquire(b.thread_id, user_id=b.principal_user_id)
    assert sid == f"dlv-{b.run_id}" and h.docker.argv_of("run")
    assert h.events("crossing_docker_requested")
    assert p.acquire(b.thread_id, user_id=b.principal_user_id) == sid       # idempotent for the run
    assert len(h.docker.argv_of("run")) == 1
    # deer-flow's per-turn release keeps the container; destroy removes container + volume
    p.release(sid)
    assert p.get(sid) is not None
    p.destroy(sid, b.run_id)
    assert p.get(sid) is None
    assert any(c[:2] == ["rm", "-f"] for c in h.docker.calls) and any(c[:2] == ["volume", "rm"] for c in h.docker.calls)


def test_daemon_absent_fails_closed(h):
    h.docker.daemon = False
    p = S.ZbmDockerSandboxProvider()
    b = _bind(h)
    with pytest.raises(DockerUnavailable):
        p.acquire(b.thread_id, user_id=b.principal_user_id)
    assert not h.docker.argv_of("run")


def test_exec_argv_env_allowlist_timeout_bound_truncation_and_record_first(h):
    p = S.ZbmDockerSandboxProvider()
    b = _bind(h, deadline_s=50)
    sid = p.acquire(b.thread_id, user_id=b.principal_user_id)
    box = p.get(sid)
    with pytest.raises(ValueError, match="allowlist"):
        box.exec_argv(["true"], env={"AWS_SECRET": "x"})
    with pytest.raises(ValueError):
        box.exec_argv(["true"], env={"bad-name": "x"})
    with pytest.raises(PermissionError):
        box.exec_argv(["true"], cwd="/etc")
    n_before = len(h.events("sandbox_exec_requested"))
    r = box.exec_argv(["echo", "hi"], env={"CI": "1"}, timeout=1000)
    assert r.exit_code == 0 and r.stdout == b"hi\n"
    call = h.docker.argv_of("exec")[-1]
    assert call[:5] == ["exec", "--user", "65532:65532", "-w", WORKSPACE]
    assert call[5:7] == ["--env", "CI=1"] and call[7] == sid and call[8:11] == ["timeout", "-k", "5"]
    assert int(call[11]) == 50                                          # min(timeout, DLV_CMD_TIMEOUT_S, remaining wall clock)
    assert call[12:] == ["echo", "hi"]
    assert len(h.events("sandbox_exec_requested")) == n_before + 1 and h.events("sandbox_exec_completed")
    req = h.events("sandbox_exec_requested")[-1]["payload"]
    assert set(req) >= {"run_id", "seq", "command_sha256", "cwd", "env_names", "argv_len", "timeout_s"} and "hi" not in str(req)
    # truncation at 1 MiB
    big = box.exec_argv(["head", "-c", str(2 * 1024 * 1024), "/dev/zero"])
    assert big.truncated and len(big.stdout) == 1024 * 1024
    assert h.events("sandbox_exec_completed")[-1]["payload"]["truncated"] is True
    # a command past the deadline is killed (exit 124, timed_out)
    b.deadline_at = h.clock.now() + timedelta(seconds=1)
    slow = box.exec_argv(["sleep", "5"])
    assert slow.timed_out and slow.exit_code == 124
    # record-first: a failed record means nothing runs
    h.ledger.fail_all = True
    with pytest.raises(RuntimeError, match="record-first"):
        box.exec_argv(["echo", "no"])
    h.ledger.fail_all = False
    assert not any(c[-2:] == ["echo", "no"] for c in h.docker.argv_of("exec"))
    assert h.svc.runs.get(b.run_id) is None                             # no such run in the store; the failure was reported to the runtime


def test_file_operations_contain_paths(h):
    p = S.ZbmDockerSandboxProvider()
    b = _bind(h)
    sid = p.acquire(b.thread_id, user_id=b.principal_user_id)
    box = p.get(sid)
    # wave 20 R7: file-tool writes are contained to the WRITE ROOTS (services/<service>/, docs/adr/00NN-*.md), not the workspace
    A = f"{WORKSPACE}/services/toy-py/a"
    with pytest.raises(PermissionError, match="write roots|R7"):
        box.write_file(f"{WORKSPACE}/a/b.txt", "hello")
    box.write_file(f"{A}/b.txt", "hello")
    assert box.read_file(f"{A}/b.txt") == "hello"
    box.write_file(f"{A}/b.txt", " world", append=True)
    assert box.read_file(f"{A}/b.txt") == "hello world"
    assert box.read_file(f"{A}/b.txt", 1, 1) == "hello world"
    assert f"{A}/b.txt" in box.list_dir(A)
    for bad in ("/etc/passwd", f"{WORKSPACE}/../etc/passwd", "../x", f"{WORKSPACE}/a/../../x"):
        with pytest.raises(PermissionError):
            box.read_file(bad)
        with pytest.raises(PermissionError):
            box.write_file(bad, "x")
    # a symlink escape resolved inside the sandbox
    box.exec_argv(["ln", "-s", "/etc", f"{A}/link"])
    with pytest.raises(PermissionError, match="symlink"):
        box.read_file(f"{A}/link/hostname")
    matches, _ = box.glob(A, "*.txt")
    assert matches == [f"{A}/b.txt"]
    hits, _ = box.grep(A, "hello", literal=True)
    assert hits and hits[0].line_number == 1
    # the copy-in tar stream is owned by 65532 and never a bind mount of the repo
    data = S.tar_of_dir(str(SERVICE_ROOT / "skills"))
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            assert m.uid == 65532 and m.gid == 65532
    with pytest.raises(PermissionError):
        p.copy_out(sid, "/etc")


def test_execute_command_shape_and_output_tail(h):
    p = S.ZbmDockerSandboxProvider()
    b = _bind(h)
    sid = p.acquire(b.thread_id, user_id=b.principal_user_id)
    box = p.get(sid)
    out = box.execute_command("echo a; exit 3")
    assert out.startswith("a\n") and "[exit code 3]" in out
    call = h.docker.argv_of("exec")[-1]
    assert call[-3:] == ["/bin/bash", "-lc", "echo a; exit 3"]
    assert box.persistent_shell_sessions is False
    assert box.execute_command("") == "Error: empty command"


def test_real_docker_cli_missing_binary_is_an_exec_result_not_an_exception():
    cli = S.RealDockerCli(binary="/nonexistent/docker")
    r = cli.run(["info"], timeout_s=5)
    assert isinstance(r, ExecResult) and r.exit_code == 127 and not S.daemon_available(cli)
