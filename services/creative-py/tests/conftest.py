import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

# api.py fails closed (raises at import time) without CREATIVE_SERVICE_TOKEN.
# Fixed test-only values, never used outside tests. The ledger env vars are
# deliberately NOT set: the module-level app must default to "ledger not
# configured" and every test that needs a ledger injects a fake.
TEST_SERVICE_TOKEN = "test-shared-secret-do-not-use-in-production"
TEST_FOUNDER_TOKEN = "test-andre-approval-token-do-not-use"
# Fix wave 25 (D1): always the test token, never one the shell exports (test_fix25_test_token.py).
os.environ["CREATIVE_SERVICE_TOKEN"] = TEST_SERVICE_TOKEN
os.environ.pop("LEDGER_SERVICE_URL", None)
os.environ.pop("LEDGER_SERVICE_TOKEN", None)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

# Per-actor credentials (fix wave 2, N4): one test-only token per default actor.
TEST_ACTOR_TOKENS = {a: f"test-actor-token-{a}-do-not-use" for a in (
    "zbm_brief_writer", "zbm_creative_lead", "zbm_creative_quality", "zbm_placement_spec", "zbc_rulebook_writer",
    "zbc_campaign_rulebook", "zbc_platform_rules", "zbc_clip_human_reviewer", "rights_desk")}
ACTOR_HEADER = "X-Creative-Actor-Token"
_AUTO = object()


def _DEFAULT_DRAFTER(path: str) -> str | None:
    """Tests that omit actor_id when drafting act as the default writer
    intelligence (the server used to default to it; now it must be proven)."""
    import re

    if path == "/zbm/briefs":
        return "zbm_brief_writer"
    if re.fullmatch(r"/zbc/campaigns/[^/]+/(rulebooks(/\d+)?|revisions)", path):
        return "zbc_rulebook_writer"
    return None


@pytest.fixture
def clock():
    from shared.clock import FixedClock

    return FixedClock(NOW)


@pytest.fixture
def ledger():
    from shared.ledger import FakeLedgerClient

    return FakeLedgerClient()


@pytest.fixture
def registry():
    from shared.registry import seeded_registry

    return seeded_registry()


@pytest.fixture
def rights():
    from shared.rights import RightsRegistry

    return RightsRegistry()


@pytest.fixture
def actors():
    from shared.actors import ActorRegistry

    return ActorRegistry()


@pytest.fixture
def recorder(ledger):
    from shared.ledger import EvidenceRecorder

    return EvidenceRecorder(ledger)


class Api:
    """A TestClient bound to an app built with injected fakes."""

    def __init__(self, *, ledger, clock, departments=None, actors=None, founder_token=TEST_FOUNDER_TOKEN,
                 actor_tokens=None, **build_kw):
        from fastapi.testclient import TestClient

        from api import build_app

        self.ledger = ledger
        self.clock = clock
        if actor_tokens is None:  # every actor in the registry gets a test credential
            actor_tokens = {**{a: f"test-actor-token-{a}-do-not-use" for a in (actors.actors if actors else ())},
                            **TEST_ACTOR_TOKENS}
        self.actor_tokens = dict(actor_tokens)
        self.app = build_app(service_token=TEST_SERVICE_TOKEN, ledger=ledger, founder_token=founder_token,
                             clock=clock, departments=departments, actors=actors, actor_tokens=self.actor_tokens,
                             **build_kw)
        self.client = TestClient(self.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
        self.zbm = self.app.state.zbm
        self.zbc = self.app.state.zbc

    def _actor_headers(self, json, as_actor, path=None) -> dict:
        """Test convenience: act as `as_actor`, or (by default) as the actor the
        body names — the SERVER only ever trusts the credential."""
        if as_actor is _AUTO:
            as_actor = json.get("actor_id") if isinstance(json, dict) else None
            if as_actor is None and path is not None:
                as_actor = _DEFAULT_DRAFTER(path)
        tok = self.actor_tokens.get(as_actor) if as_actor else None
        return {ACTOR_HEADER: tok} if tok else {}

    def post(self, path, json=None, andre=None, as_actor=_AUTO, **kw):
        headers = self._actor_headers(json, as_actor, path)
        if andre is not None:
            headers["X-Andre-Approval-Token"] = andre
        return self.client.post(path, json=json, headers=headers, **kw)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def put(self, path, json=None, as_actor=_AUTO, **kw):
        return self.client.put(path, json=json, headers=self._actor_headers(json, as_actor, path), **kw)


@pytest.fixture
def make_api(ledger, clock):
    def _make(**kw):
        return Api(ledger=kw.pop("ledger", ledger), clock=kw.pop("clock", clock), **kw)

    return _make


@pytest.fixture
def api(make_api):
    return make_api()


def port_range(default: range) -> range:
    """The ports real-socket tests may bind: `default`, or the range in
    CREATIVE_TEST_PORTS ("lo-hi", inclusive) so a run can stay inside the
    port range its operator was given (fix wave 9)."""
    spec = os.environ.get("CREATIVE_TEST_PORTS")
    if not spec:
        return default
    lo, hi = (int(x) for x in spec.split("-"))
    return range(lo, hi + 1)


_HANDED_OUT: list[int] = []


def free_port() -> int:
    """A free local port: OS-assigned, or — with CREATIVE_TEST_PORTS set —
    the next free one of that range not handed out lately (two calls in a
    row never return the same port)."""
    import socket

    spec = os.environ.get("CREATIVE_TEST_PORTS")
    if not spec:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    ports = list(port_range(range(0)))
    recent = set(_HANDED_OUT[-(len(ports) // 2):])
    for port in ports:
        if port in recent:
            continue
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # fix wave 22 (G3): TIME_WAIT is free
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        _HANDED_OUT.append(port)
        return port
    raise RuntimeError(f"no free port in CREATIVE_TEST_PORTS={spec}")


def start_serve(extra_env: dict | None = None, attempts: int = 5, timeout: float = 30.0):
    """Fix wave 25 (scout A C5/C6; R-HYGIENE L2): `python3 serve.py` on a port from `free_port()` (OS-assigned, or
    CREATIVE_TEST_PORTS), accepted only once THIS child has logged its own bind on it ("Uvicorn running on ..."):
    a port picked free a moment earlier can be another process's, and its /health would answer for it. A child that
    exits before announcing (e.g. it lost the port) is reaped and another port is tried; any other failure kills
    and reaps the child before raising. Returns (proc, port). The child's output goes to an unlinked temp file
    (nothing is left behind); stop it with `stop_serve`."""
    import subprocess
    import tempfile
    import time

    import httpx

    last = None
    for _ in range(attempts):
        port = free_port()
        env = {**os.environ, "CREATIVE_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "CREATIVE_PORT": str(port),
               **(extra_env or {})}
        env.pop("LEDGER_SERVICE_URL", None)
        env.pop("LEDGER_SERVICE_TOKEN", None)
        out = tempfile.TemporaryFile()
        proc = subprocess.Popen([sys.executable, "serve.py"], cwd=SRC, env=env, stdout=out, stderr=subprocess.STDOUT)
        proc._creative_out = out  # closed by stop_serve
        announced = f"Uvicorn running on http://127.0.0.1:{port}".encode()
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None and announced not in os.pread(out.fileno(), os.fstat(out.fileno()).st_size, 0):
                if time.monotonic() > deadline:
                    raise RuntimeError("creative-py did not start: " + _tail(out))
                time.sleep(0.05)
            if proc.poll() is None:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=10).status_code != 200:
                    raise RuntimeError("creative-py's /health did not answer 200")
                return proc, port
            last = _tail(out)
        except BaseException:
            stop_serve(proc)
            raise
        stop_serve(proc)
    raise RuntimeError(f"creative-py could not bind a port in {attempts} attempts: {last}")


def _tail(out) -> str:
    return os.pread(out.fileno(), 4000, max(0, os.fstat(out.fileno()).st_size - 4000)).decode(errors="replace")


def stop_serve(proc) -> None:
    import subprocess

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    out = getattr(proc, "_creative_out", None)
    if out is not None:
        out.close()
