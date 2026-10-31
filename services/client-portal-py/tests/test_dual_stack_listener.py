"""src/dual_stack.py: the IPv6 wildcard is bound dual-stack (golive-combined, CI railway-images).

The Railway images bind ``::``. uvicorn.run binds through asyncio's create_server, which sets IPV6_V6ONLY, so an
IPv4 connection (Docker's port mapping; on Railway an IPv4 health check or private-network peer) was reset. The
launcher now binds the wildcard itself with IPV6_V6ONLY cleared and hands uvicorn the socket; every other host is
passed to uvicorn.run unchanged. Same file in detection-py, finance-py, legal-py and seo-py, and in compliance-py
(ADR 0024: its image is a Railway Python image too; CI railway-images reset its /health the same way until it had it). No test here depends on the machine having IPv6: the socket and uvicorn are fakes, except the last test,
which proves IPv4 reaches the real socket where the kernel has IPv6 and the refusal to start where it does not."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import dual_stack  # noqa: E402


class FakeSocket:
    instances: list = []

    def __init__(self, family, kind, fail_bind=False):
        self.family, self.kind, self.calls, self.closed = family, kind, [], False
        self.fail_bind = fail_bind
        FakeSocket.instances.append(self)

    def setsockopt(self, level, opt, value):
        self.calls.append(("setsockopt", level, opt, value))

    def bind(self, addr):
        self.calls.append(("bind", addr))
        if self.fail_bind:
            raise OSError(98, "Address already in use")

    def close(self):
        self.closed = True


class FakeUvicorn:
    def __init__(self, started=True):
        self.started, self.ran, self.config, self.sockets = started, None, None, None
        outer = self

        class Config:
            def __init__(self, app, **kw):
                outer.config = (app, kw)

        class Server:
            def __init__(self, config):
                self.started = False

            def run(self, sockets=None):
                outer.sockets = sockets
                self.started = outer.started
        self.Config, self.Server = Config, Server

    def run(self, app, **kw):
        self.ran = (app, kw)


@pytest.fixture
def fake_socket(monkeypatch):
    FakeSocket.instances = []
    monkeypatch.setattr(dual_stack.socket, "socket", lambda fam, kind: FakeSocket(fam, kind))
    return FakeSocket


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "::1", "localhost", "10.0.0.5", "[::1]"])
def test_any_other_host_is_not_bound_here(host, fake_socket):
    assert dual_stack.dual_stack_socket(host, 0) is None
    assert fake_socket.instances == []


@pytest.mark.parametrize("host", ["::", "[::]", " :: "])
def test_the_wildcard_is_bound_ipv6_with_v6only_cleared_before_bind(host, fake_socket):
    s = dual_stack.dual_stack_socket(host, 0)
    assert s.family == socket.AF_INET6 and s.kind == socket.SOCK_STREAM and not s.closed
    v6only = ("setsockopt", socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    assert v6only in s.calls and s.calls[-1] == ("bind", ("::", 0))
    assert s.calls.index(v6only) < s.calls.index(("bind", ("::", 0)))      # cleared before the bind
    assert ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in s.calls


def test_a_failed_bind_closes_the_socket_and_the_launcher_exits_1(monkeypatch, capsys):
    FakeSocket.instances = []
    monkeypatch.setattr(dual_stack.socket, "socket", lambda fam, kind: FakeSocket(fam, kind, fail_bind=True))
    uv = FakeUvicorn()
    with pytest.raises(SystemExit) as e:
        dual_stack.serve(uv, "api:app", "::", 0, "svc")
    assert e.value.code == dual_stack.BIND_FAILURE == 1
    assert FakeSocket.instances[0].closed and uv.ran is None and uv.sockets is None
    assert "svc: cannot bind [::]:0" in capsys.readouterr().err


def test_the_wildcard_is_served_on_that_socket_with_the_same_configuration(fake_socket, capsys):
    uv = FakeUvicorn(started=True)
    dual_stack.serve(uv, "api:app", "::", 0, "svc", http="H", timeout_keep_alive=5, limit_concurrency=128)
    sock = fake_socket.instances[0]
    assert uv.ran is None and uv.sockets == [sock] and sock.closed
    assert uv.config == ("api:app", {"host": "::", "port": 0, "http": "H", "timeout_keep_alive": 5,
                                     "limit_concurrency": 128})
    assert "svc: listening on [::]:0 (dual-stack: IPv4 and IPv6)" in capsys.readouterr().err


def test_a_server_that_did_not_start_exits_like_uvicorn(fake_socket):
    uv = FakeUvicorn(started=False)
    with pytest.raises(SystemExit) as e:
        dual_stack.serve(uv, "api:app", "[::]", 0, "svc")
    assert e.value.code == dual_stack.STARTUP_FAILURE == 3 and fake_socket.instances[0].closed


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_any_other_host_goes_to_uvicorn_run_unchanged(host, fake_socket):
    uv = FakeUvicorn()
    dual_stack.serve(uv, "api:app", host, 0, "svc", http="H")
    assert uv.ran == ("api:app", {"host": host, "port": 0, "http": "H"}) and uv.sockets is None
    assert fake_socket.instances == []


def test_the_launcher_serves_through_dual_stack():
    src = (SRC / "api.py").read_text()
    assert "dual_stack.serve(" in src and "uvicorn.run(" not in src


def test_the_railway_services_carry_the_same_dual_stack_module():
    here = (SRC / "dual_stack.py").read_bytes()
    services = SRC.parents[1]
    for svc in ("detection-py", "finance-py", "legal-py", "seo-py", "client-portal-py"):
        assert (services / svc / "src" / "dual_stack.py").read_bytes() == here, svc


# Run in a child process: the services' conftests refuse every socket connect inside a test (no network), and this
# check needs one loopback connect to the real socket. The child imports this service's dual_stack and nothing else.
_REAL_SOCKET_CHILD = """
import socket, sys
sys.path.insert(0, sys.argv[1])
import dual_stack
try:
    socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
except OSError:
    try:
        dual_stack.dual_stack_socket("::", 0)
    except OSError:
        print("no-ipv6 bind-refused")
    else:
        print("no-ipv6 bind-accepted")
    sys.exit(0)
s = dual_stack.dual_stack_socket("::", 0)
v6only = s.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
s.listen(1)
c = socket.create_connection(("127.0.0.1", s.getsockname()[1]), timeout=5)
conn, peer = s.accept()
print("ipv6", v6only, peer[0])
conn.close(); c.close(); s.close()
"""


def test_ipv4_reaches_the_real_wildcard_socket_or_the_launcher_refuses_without_ipv6():
    import subprocess

    r = subprocess.run([sys.executable, "-I", "-B", "-c", _REAL_SOCKET_CHILD, str(SRC)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout.split()
    if out[0] == "no-ipv6":              # no IPv6 in this kernel: binding :: fails, so the service would not start
        assert out == ["no-ipv6", "bind-refused"], out
    else:                                # IPv4 to the IPv6 wildcard: accepted, as a v4-mapped peer, V6ONLY off
        assert out == ["ipv6", "0", "::ffff:127.0.0.1"], out
