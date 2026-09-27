"""L1 (spec §C.2, §F Live): the properties only a machine WITH a Docker daemon can prove. Each test is skipped with
the reason printed (pytest.ini -rs) when ``docker info`` fails; the report must list them as "not provable here",
never as passed. When Docker is present the tests run the REAL adapter against a real container."""

from __future__ import annotations

import os
import subprocess
from datetime import timedelta

import pytest

from helpers import Harness

from zbm_delivery import registry
from zbm_delivery.adapters import sandbox as S
from zbm_delivery.policy import WORKSPACE

PROPERTIES = [
    "the container runs as uid 65532 (id -u inside)",
    "seccomp filtering is on (/proc/self/status Seccomp: 2, the default profile; never unconfined)",
    "no external route: curl / python socket connect to any external host fails inside the container (--network internal)",
    "/var/run/docker.sock is absent inside the container",
    "a write outside /mnt/user-data/workspace and /tmp fails (--read-only root)",
    "a process past the deadline is killed (timeout -k 5 <secs> + the host-side backstop)",
    "the named volume dlv-ws-<run_id> is gone after destroy (docker volume rm)",
]


def _docker_reason() -> str | None:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker CLI unavailable: {type(exc).__name__}"
    if r.returncode != 0 or not r.stdout.strip():
        return "docker daemon not reachable: " + (r.stderr.strip().splitlines() or ["docker info failed"])[0][:160]
    if not os.environ.get("DLV_LIVE_SANDBOX_IMAGE"):
        return "DLV_LIVE_SANDBOX_IMAGE (a digest-pinned image built from docker/sandbox.Dockerfile) is not set"
    return None


def _skip_unless_docker():
    reason = _docker_reason()
    if reason:
        pytest.skip("Docker live: not provable here — " + reason + "; unproven properties: " + "; ".join(PROPERTIES))


@pytest.fixture
def live():
    _skip_unless_docker()
    image = os.environ["DLV_LIVE_SANDBOX_IMAGE"]
    registry_host = image.split("/", 1)[0]
    h = Harness(wire_harness=False, extra_env={"DLV_SANDBOX_IMAGE": image, "DLV_IMAGE_REGISTRY": registry_host},
                gate_report=None)
    h.svc.docker = S.RealDockerCli()
    registry.runtime().docker = h.svc.docker
    b = registry.RunBinding(run_id="dlv-run-" + "L" * 26, thread_id="t-live", service="toy-py", principal_user_id="zbm--live",
                            workspace=WORKSPACE, deadline_at=h.clock.now() + timedelta(seconds=120))
    registry.bind(b)
    p = S.ZbmDockerSandboxProvider()
    sid = p.acquire(b.thread_id, user_id=b.principal_user_id)
    try:
        yield p, p.get(sid), sid, b
    finally:
        p.destroy(sid, b.run_id)
        registry.clear()
        h.close()


def test_l1_uid_seccomp_socket_readonly(live):
    p, box, sid, b = live
    assert box.exec_argv(["id", "-u"]).stdout.strip() == b"65532"
    status = box.exec_argv(["cat", "/proc/self/status"]).stdout.decode()
    assert "Seccomp:\t2" in status
    assert box.exec_argv(["test", "-e", "/var/run/docker.sock"]).exit_code != 0
    assert box.exec_argv(["touch", "/usr/x"]).exit_code != 0
    assert box.exec_argv(["touch", f"{WORKSPACE}/x"]).exit_code == 0
    assert box.exec_argv(["touch", "/tmp/x"]).exit_code == 0


def test_l1_no_external_route(live):
    p, box, sid, b = live
    r = box.exec_argv(["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"])
    assert r.exit_code != 0


def test_l1_deadline_kills_and_volume_is_gone(live):
    p, box, sid, b = live
    b.deadline_at = b.deadline_at.__class__.now(b.deadline_at.tzinfo) + timedelta(seconds=2)
    r = box.exec_argv(["sleep", "30"])
    assert r.timed_out
    p.destroy(sid, b.run_id)
    out = subprocess.run(["docker", "volume", "ls", "-q"], capture_output=True, text=True, timeout=30).stdout
    assert f"dlv-ws-{b.run_id}" not in out
