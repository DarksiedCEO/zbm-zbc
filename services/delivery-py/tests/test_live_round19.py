"""Round 19 R12 (N19-A-6), live on the assigned port range: the reviewer's headers-then-silence TLS server. Before
this wave ``abort()`` closed the response object and the blocked reader returned only when the read timeout struck
(18 s after the abort in the reviewer's run); now the socket is shut down first and the reader returns at once.
A ``test_live_*`` module: the conftest's no-network guard exempts it (the server is 127.0.0.1 on 18800-18849)."""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import threading
import time

import pytest

from zbm_delivery.adapters import egress as E

PORTS = range(18800, 18850)


def _free_port() -> int:
    for port in PORTS:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 18800-18849 (the assigned live range)")


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


def test_abort_wakes_a_reader_blocked_in_recv_at_once(tmp_path):
    port = _free_port()
    cert, key = _cert(str(tmp_path))
    srv, stop = _silence_server(port, cert, key)
    try:
        eg = E.EgressClient((f"localhost:{port}",), record=lambda *a, **k: "id", default_timeout_s=20, llm_read_timeout_s=20,
                            env={"SSL_CERT_FILE": cert})
        out: dict = {}
        t0 = time.monotonic()

        def call():
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
        time.sleep(0.5)                                       # the reader is now blocked in recv with nothing arriving
        n = eg.abort("r1")
        t_abort = time.monotonic()
        th.join(15)
        after = time.monotonic() - t_abort
        assert n == 1 and not th.is_alive(), out
        assert out.get("r", "").startswith("EgressFailed") and "aborted" in out["r"], out
        assert after < 2.0, f"the reader returned {after:.1f}s after the abort (the read timeout is 20 s)"
        assert time.monotonic() - t0 < 10
    finally:
        stop.set()
        srv.close()
