"""Fix wave 26b, CI7-1 (CI #7, delivery-py macos-26): ``EgressClient.abort()`` shut the response's socket down and
at once closed it from the aborting thread. On macOS a reader asleep in ``poll`` on that descriptor is not reliably
woken when the close follows the shutdown immediately (plain sockets on the build box: shutdown only 0 of 6000 polls
slept their whole timeout, shutdown then close 201 of 6000), so about one abort in 16 left the LLM call blocked until
its read timeout. A single run cannot show a race of that size: each test here aborts 300 calls.

Live on 127.0.0.1 against a headers-then-silence TLS server, like ``test_live_round19`` (a ``test_live_*`` module: the
conftest's no-network guard exempts it)."""

from __future__ import annotations

import os
import socket
import ssl
import threading
import time

import httpx
import pytest
from test_live_round19 import _blocked_in_a_socket_wait, _cert, _free_port

from zbm_delivery.adapters import egress as E

ROUNDS = 300


def _silent_server(port: int, cert: str, key: str):
    """Complete TLS, read the request, send headers with a large Content-Length and no body, then stay silent and keep
    the connection: the server must not answer the client's shutdown, because a close from the peer wakes the stuck
    reader and hides the defect (seen: 0 of 300 stuck against a server that closed on the client's end of stream).
    ``release()`` closes every held connection, so the process's descriptor count can return to what it was."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    held: list = []
    lock = threading.Lock()
    stopped = threading.Event()

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            if stopped.is_set():
                c.close()
                return
            try:
                tc = ctx.wrap_socket(c, server_side=True)
                tc.recv(65536)
                tc.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
            except OSError:
                c.close()
                continue
            with lock:
                held.append(tc)

    def release():
        with lock:
            while held:
                held.pop().close()

    def stop():
        """End the accept loop: closing a listening socket does not wake ``accept`` on macOS, a connection does."""
        stopped.set()
        try:
            socket.create_connection(srv.getsockname(), timeout=5).close()
        except OSError:
            pass
        th.join(30)                                       # a bound on a stall only
        srv.close()
        release()
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return srv, stop, release


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def _abort_rounds(tmp_path, monkeypatch, *, reader_blocked: bool) -> dict:
    """ROUNDS LLM calls against the silent server, each aborted from this thread — once its reader is asleep in the
    socket wait (``reader_blocked``), or else BEFORE its reader has read at all: the reader is held at the door of
    ``iter_bytes`` until ``abort()`` has returned, so its first read comes after the abort, every round."""
    cert, key = _cert(str(tmp_path))
    srv, stop, release = _silent_server(_free_port(), cert, key)
    port = srv.getsockname()[1]
    socks: list = []                                      # every response socket abort() was handed (kept: ids stay unique)
    resps: list = []
    closed: list = []                                     # every SSLSocket really closed, kept: an id is never reused
    shutdown_socket, real_close = E._shutdown_socket, ssl.SSLSocket._real_close

    def seen_shutdown(resp):
        resps.append(resp)
        socks.append(resp.extensions["network_stream"].get_extra_info("socket"))
        return shutdown_socket(resp)

    def counted_real_close(self):
        closed.append(self)
        return real_close(self)
    monkeypatch.setattr(E, "_shutdown_socket", seen_shutdown)
    monkeypatch.setattr(ssl.SSLSocket, "_real_close", counted_real_close)
    gate = {"at_the_door": threading.Event(), "abort_done": threading.Event()}
    if not reader_blocked:
        iter_bytes = httpx.Response.iter_bytes

        def held_at_the_door(self, *a, **k):
            gate["at_the_door"].set()
            gate["abort_done"].wait(30)                   # a bound on a stall only
            return iter_bytes(self, *a, **k)
        monkeypatch.setattr(httpx.Response, "iter_bytes", held_at_the_door)
    eg = E.EgressClient((f"localhost:{port}",), record=lambda *a, **k: "id", default_timeout_s=60, llm_read_timeout_s=60,
                        env={"SSL_CERT_FILE": cert})
    still_blocked, answers, aborted = 0, [], 0

    def one_round(run_id: str) -> None:
        nonlocal still_blocked, aborted
        out: dict = {}

        def call():
            out["tid"] = threading.get_native_id()
            try:
                eg.request("POST", f"https://localhost:{port}/v1/x", purpose="llm", body=b"{}", run_id=run_id)
                out["r"] = "returned"
            except Exception as exc:  # noqa: BLE001
                out["r"] = f"{type(exc).__name__}: {exc}"
        gate["at_the_door"].clear()
        gate["abort_done"].clear()
        th = threading.Thread(target=call, daemon=True)
        th.start()
        stall = time.monotonic() + 30                     # a bound on a stall only
        while not eg._inflight and time.monotonic() < stall:
            time.sleep(0.0005)                            # yields the interpreter to the reader; not a wait for a state
        assert eg._inflight, "the request never reached the body-read phase"
        if reader_blocked:
            # the reader is SEEN asleep in its socket wait where the kernel shows it (Linux, three polls in a row,
            # as test_live_round19); elsewhere (macOS) a short wait stands in for the observation — the defect
            # appeared with a reader that had been asleep for 20 ms, 50 ms and 500 ms alike
            seen = 0
            while seen < 3 and time.monotonic() < stall:
                state = _blocked_in_a_socket_wait(out["tid"])
                if state is None:
                    time.sleep(0.05)
                    break
                seen = seen + 1 if state else 0
                time.sleep(0.01)
        else:
            assert gate["at_the_door"].wait(30), "the reader never reached its first read"
        aborted += eg.abort(run_id)
        gate["abort_done"].set()
        th.join(5)                                        # a bound on a stall only: a woken reader returns at once
        if th.is_alive():
            still_blocked += 1
        else:
            answers.append(out.get("r", ""))

    try:
        # one call first, not counted: the process opens descriptors of its own on its first connection (name
        # lookup; seen on macOS: two, once per process), which must not read as a leak of the 300 that follow
        one_round("warm-up")
        release()
        still_blocked, aborted = 0, 0
        del answers[:], socks[:], resps[:]
        before = _open_descriptors()
        for i in range(ROUNDS):
            one_round(f"r{i}")
        # every reader has returned (or is counted as still blocked); the server now closes the connections it held,
        # and the descriptor count is read once it has settled — a state, waited for with a stall bound
        release()
        stall = time.monotonic() + 30
        while _open_descriptors() != before and time.monotonic() < stall:
            time.sleep(0.05)
        after = _open_descriptors()
    finally:
        eg.close()
        stop()
    real_closes: dict[int, int] = {}
    for s in closed:
        real_closes[id(s)] = real_closes.get(id(s), 0) + 1
    return {"still_blocked": still_blocked, "aborted": aborted, "answers": answers,
            "descriptors": (before, after),
            "sockets_not_closed": sum(1 for s in socks if s.fileno() != -1),
            "sockets_closed_other_than_once": [(n, c) for n, c in enumerate(real_closes.get(id(s), 0) for s in socks)
                                               if c != 1],
            "responses_not_closed": sum(1 for r in resps if not r.is_closed), "sockets": len(socks)}


def test_300_aborts_of_a_reader_asleep_in_its_socket_wait_wake_it_every_time(tmp_path, monkeypatch):
    got = _abort_rounds(tmp_path, monkeypatch, reader_blocked=True)
    assert got["aborted"] == ROUNDS, got
    assert got["still_blocked"] == 0, f"{got['still_blocked']} of {ROUNDS} aborted calls stayed blocked"
    assert all(a.startswith("EgressFailed") and "aborted" in a for a in got["answers"]), set(got["answers"])


@pytest.mark.parametrize("reader_blocked", [True, False], ids=["reader_asleep", "before_the_reader_reads"])
def test_300_aborts_close_each_response_and_socket_once_and_leave_no_descriptor(tmp_path, monkeypatch, reader_blocked):
    got = _abort_rounds(tmp_path, monkeypatch, reader_blocked=reader_blocked)
    assert got["still_blocked"] == 0, f"{got['still_blocked']} of {ROUNDS} aborted calls stayed blocked"
    assert got["aborted"] == ROUNDS and got["sockets"] == ROUNDS, got
    assert got["responses_not_closed"] == 0 and got["sockets_not_closed"] == 0, got
    assert got["sockets_closed_other_than_once"] == [], got["sockets_closed_other_than_once"]   # (round, closes)
    before, after = got["descriptors"]
    assert after == before, f"open descriptors: {before} before {ROUNDS} aborts, {after} after"
