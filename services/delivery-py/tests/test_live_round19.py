"""Round 19 R12 (N19-A-6), live on the assigned port range: the reviewer's headers-then-silence TLS server. Before
this wave ``abort()`` closed the response object and the blocked reader returned only when the read timeout struck
(18 s after the abort in the reviewer's run); now the socket is shut down first and the reader returns at once.
A ``test_live_*`` module: the conftest's no-network guard exempts it (the server is 127.0.0.1 on an OS-assigned port, or one of DLV_TEST_PORT_RANGE)."""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import threading
import time

import pytest

from zbm_delivery.adapters import egress as E



def _free_port() -> int:
    """A port of the assigned live range (``DLV_TEST_PORT_RANGE``, wave 21), else OS-assigned (wave 25)."""
    from helpers import free_live_port
    return free_live_port()


def _cert(tmp: str) -> tuple[str, str]:
    cert, key = os.path.join(tmp, "p4.crt"), os.path.join(tmp, "p4.key")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], capture_output=True)
    if r.returncode != 0:
        pytest.skip("openssl is not available to mint the test certificate")
    return cert, key


def _silence_server(port: int, cert: str, key: str):
    """Accept, complete TLS, read the request, send headers with a large Content-Length, then send nothing."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                tc = ctx.wrap_socket(c, server_side=True)
                tc.recv(65536)
                tc.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
                while not stop.is_set():
                    time.sleep(0.1)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
    threading.Thread(target=serve, daemon=True).start()
    return srv, stop


# wave 25 (scout B M3): the kernel calls a socket reader blocks in — Python's socket read with a timeout waits in
# poll/ppoll (or select) and then reads; by architecture, from the Linux syscall tables
_WAIT_SYSCALLS = {"x86_64": {0, 7, 23, 45, 270, 271}, "aarch64": {63, 72, 73, 207}}


def _blocked_in_a_socket_wait(tid: int) -> bool | None:
    """True while thread ``tid`` of this process sleeps in a read/poll syscall (``/proc/self/task/<tid>/syscall``);
    None where that cannot be observed (not Linux, or an architecture not in the table)."""
    calls = _WAIT_SYSCALLS.get(os.uname().machine)
    try:
        with open(f"/proc/self/task/{tid}/stat", encoding="ascii") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        with open(f"/proc/self/task/{tid}/syscall", encoding="ascii") as fh:
            nr = fh.read().split()[0]
    except (OSError, IndexError):
        return None
    if calls is None or not nr.lstrip("-").isdigit():
        return None
    return state == "S" and int(nr) in calls


def test_abort_wakes_a_reader_blocked_in_recv_at_once(tmp_path):
    port = _free_port()
    cert, key = _cert(str(tmp_path))
    srv, stop = _silence_server(port, cert, key)
    try:
        # wave 25 (scout B M2): read timeouts of 60 s, so a reader that the abort did not wake would still be blocked
        # when the 45 s stall bound below ends — ordered by state, not by `after < 2.0` / `< 10` wall-clock bounds
        eg = E.EgressClient((f"localhost:{port}",), record=lambda *a, **k: "id", default_timeout_s=60, llm_read_timeout_s=60,
                            env={"SSL_CERT_FILE": cert})
        out: dict = {}

        def call():
            out["tid"] = threading.get_native_id()
            try:
                eg.request("POST", f"https://localhost:{port}/v1/x", purpose="llm", body=b"{}", run_id="r1")
                out["r"] = "returned"
            except Exception as exc:  # noqa: BLE001
                out["r"] = f"{type(exc).__name__}: {exc}"
        th = threading.Thread(target=call, daemon=True)
        th.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not eg._inflight:
            time.sleep(0.02)
        assert eg._inflight, "the request never reached the body-read phase"
        # wave 25 (scout B M3): the abort is issued once the reader is SEEN asleep in a socket wait (Linux: its
        # syscall, observed on three polls in a row), not after a `sleep(0.5)` that hoped it was — an abort that
        # came first would pass without the blocked reader this test is about. Where the thread's syscall cannot be
        # observed (macOS), the old half-second wait stands, stated as such.
        seen = 0
        stall = time.monotonic() + 30                         # a bound on a stall only
        while seen < 3 and time.monotonic() < stall:
            state = _blocked_in_a_socket_wait(out["tid"])
            if state is None:
                time.sleep(0.5)
                break
            seen = seen + 1 if state else 0
            time.sleep(0.05)
        else:
            assert seen >= 3, "the reader was never seen blocked in a socket wait"
        n = eg.abort("r1")
        th.join(45)                                           # a bound on a stall only
        assert n == 1 and not th.is_alive(), out
        assert out.get("r", "").startswith("EgressFailed") and "aborted" in out["r"], out
        assert not stop.is_set()                              # woken by the abort while the server still sends nothing
    finally:
        stop.set()
        srv.close()
