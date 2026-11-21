"""Portal launcher: bounded initial/reused request heads without timing assertions.

The actual launcher chooses the protocol. A real h11 parser and asyncio loop use
an in-memory transport; advancing the loop clock delivers real scheduled timers.
No external provider or credentials are used. The live control exercises api.main
on one loopback connection, not a load test.
"""
from __future__ import annotations

import asyncio
import secrets
from contextlib import closing, contextmanager

import pytest
from uvicorn.server import ServerState

import api
import config
import dual_stack


class Transport:
    def __init__(self):
        self.closed = self.eof = False
        self.output = bytearray()
        self.protocol = None

    def get_extra_info(self, name, default=None):
        return default

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.output.extend(data)

    def close(self):
        self.closed = True

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.eof = True

    def set_protocol(self, protocol):
        self.protocol = protocol

    def resume_reading(self):
        pass

    def pause_reading(self):
        pass


async def application(scope, receive, send):
    while True:
        message = await receive()
        if message['type'] == 'http.disconnect':
            return
        if not message.get('more_body', False):
            break
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-length', b'2')]})
    await send({'type': 'http.response.body', 'body': b'ok'})


@contextmanager
def launched_protocol(monkeypatch):
    env = {name: secrets.token_hex(32) for name in (
        'PORTAL_LEGAL_SERVICE_TOKEN', 'PORTAL_LEGAL_CALLER_TOKEN',
        'PORTAL_EVIDENCE_KEY', 'PORTAL_LINK_TAG_KEY')}
    env.update(PORTAL_LEGAL_URL='http://localhost:0', PORTAL_PUBLIC_ORIGIN='http://localhost:0')
    settings = config.load(env)
    monkeypatch.setattr(api.config_mod, 'load', lambda: settings)
    monkeypatch.setattr(api, 'create_app', lambda settings: application)
    chosen = {}

    def capture(uvicorn_mod, app, host, port, name, **kwargs):
        chosen['config'] = uvicorn_mod.Config(app, host=host, port=port, **kwargs)
    monkeypatch.setattr(dual_stack, 'serve', capture)
    assert api.main() == 0
    cfg = chosen['config']
    cfg.load()
    loop = asyncio.new_event_loop()
    now = [loop.time()]
    monkeypatch.setattr(loop, 'time', lambda: now[0])
    state, transport = ServerState(), Transport()
    protocol = cfg.http_protocol_class(config=cfg, server_state=state, app_state={}, _loop=loop)
    protocol.connection_made(transport)

    def advance(seconds=0):
        now[0] += seconds
        loop.run_until_complete(asyncio.sleep(0))

    try:
        yield protocol, transport, state, advance
    finally:
        transport.closed = True
        protocol.connection_lost(None)
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


@pytest.mark.parametrize('partial', [b'', b'GET /health HTTP/1.1\r\nHost: local'])
def test_initial_head_deadline_frees_the_slot_and_bounds_drain(monkeypatch, partial):
    with launched_protocol(monkeypatch) as (p, t, state, advance):
        if partial:
            p.data_received(partial)
        advance(11)
        assert t.eof or t.closed, 'incomplete initial request was not closed'
        assert p not in state.connections, 'deadline must release its concurrency slot before draining'
        advance(2)
        assert t.closed, 'the graceful drain must also end'


def test_partial_progress_does_not_extend_the_head_deadline(monkeypatch):
    with launched_protocol(monkeypatch) as (p, t, state, advance):
        p.data_received(b'GET /health HTTP/1.1\r\n')
        advance(9)
        p.data_received(b'Host: local')
        advance(2)
        assert t.eof or t.closed


def test_reused_connection_gets_a_fresh_absolute_head_deadline(monkeypatch):
    with launched_protocol(monkeypatch) as (p, t, state, advance):
        p.data_received(b'GET /health HTTP/1.1\r\nHost: local\r\n\r\n')
        advance()
        assert b'200 OK' in t.output and not t.closed
        advance(4)
        p.data_received(b'GET /health HTTP/1.1\r\nHost:')
        advance(7)
        assert t.eof or t.closed, 'keep-alive cancellation must not cancel the request-head deadline'


def test_complete_head_leaves_body_deadline_to_the_application(monkeypatch):
    with launched_protocol(monkeypatch) as (p, t, state, advance):
        p.data_received(b'POST / HTTP/1.1\r\nHost: local\r\nContent-Length: 2\r\n\r\nx')
        advance(11)
        assert not t.closed and not t.eof
        p.data_received(b'y')
        advance()
        assert b'200 OK' in t.output and t.output.endswith(b'ok')


def test_pipelined_head_is_not_closed_while_its_body_is_pending(monkeypatch):
    with launched_protocol(monkeypatch) as (p, t, state, advance):
        p.data_received(b'GET / HTTP/1.1\r\nHost: local\r\n\r\n'
                        b'POST / HTTP/1.1\r\nHost: local\r\nContent-Length: 2\r\n\r\nx')
        advance()
        assert t.output.count(b'200 OK') == 1
        advance(11)
        assert not t.closed and not t.eof
        p.data_received(b'y')
        advance()
        assert t.output.count(b'200 OK') == 2


def test_disconnect_cancels_pending_head_and_drain_work(monkeypatch):
    with launched_protocol(monkeypatch) as (p, t, state, advance):
        p.data_received(b'GET / HTTP/1.1\r\n')
        t.closed = True
        p.connection_lost(None)
        after_disconnect = (t.eof, t.closed)
        advance(20)
        assert p not in state.connections
        assert (t.eof, t.closed) == after_disconnect, 'disconnect must cancel pending deadline callbacks'
        assert all(handle.cancelled() for handle in p.loop._scheduled)
        assert not getattr(state, '_zbm_draining', set())


@pytest.mark.live
@pytest.mark.parametrize('reuse', [False, True])
def test_real_launcher_expires_one_incomplete_head_and_remains_healthy(reuse):
    import http.client
    import subprocess
    import sys
    import time

    from test_launcher import SRC, _env, _free_port

    port = _free_port()
    env = _env(PORTAL_PORT=str(port), PORTAL_BIND_ADDR='127.0.0.1',
               **{name: secrets.token_hex(32) for name in (
                   'PORTAL_LEGAL_SERVICE_TOKEN', 'PORTAL_LEGAL_CALLER_TOKEN',
                   'PORTAL_EVIDENCE_KEY', 'PORTAL_LINK_TAG_KEY')})
    proc = subprocess.Popen([sys.executable, '-m', 'api'], cwd=SRC, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                conn.connect()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail('portal listener did not start')
                time.sleep(0.05)
        if reuse:
            conn.request('GET', '/health')
            response = conn.getresponse()
            assert response.status == 200 and response.read() == b'{"status":"ok"}'
            assert response.getheader('server') is None
        conn.sock.sendall(b'GET /health HTTP/1.1\r\nHost: local')
        assert conn.sock.recv(1) == b'', 'incomplete request must receive EOF without an application response'
        with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=5)) as control:
            control.request('GET', '/health')
            response = control.getresponse()
            assert response.status == 200 and response.read() == b'{"status":"ok"}'
    finally:
        conn.close()
        proc.terminate()
        try:
            output, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
            raise
    assert proc.returncode in (0, -15), output
