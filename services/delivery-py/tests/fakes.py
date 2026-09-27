"""
TEST-ONLY fakes (spec §F, G3: they live here, never in src/). Each has a switch so a test can make exactly one
gate input absent.

- ``FakeLedgerClient``: the finance-py double (field rules, idempotency, ``fail_all`` / ``fail_on_type``).
- ``FakeDockerCli``: the argv-level double of the Docker CLI (spec C.2). It records EVERY argv the adapter emits.
  ``docker run`` creates a local directory standing in for the run's volume; ``docker exec`` runs the argv locally
  in that directory with deer-flow's virtual prefix ``/mnt/user-data/workspace`` mapped to it (and mapped back in
  the output); ``docker cp`` moves tar streams; ``docker rm``/``volume rm`` remove the directory; ``docker info``
  reports the configured daemon state. It does NOT enforce the container's isolation — that is exactly what only a
  machine with Docker can prove (tests/test_live_docker.py).
- ``FakeChatModel``: the scripted deterministic model behind ``EgressChatModel`` (a ``ChatBackend``), driven by a
  JSON scenario: a flat list of answers, each ``{"tool_calls": [...]}`` or ``{"text": "..."}``; an
  ``{"advance_clock_s": N}`` entry moves the harness clock before the next answer (S6).
- ``FakeMemory``: proves the ``MemoryPort`` contract (A9).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zbm_delivery.ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, event_field_problems, payload_sha256  # noqa: E402
from zbm_delivery.policy import WORKSPACE  # noqa: E402
from zbm_delivery.ports import Ack, ChatAnswer, ChatTurn, ExecResult, Fact, MemoryContext, Principal  # noqa: E402


@dataclass
class FakeLedgerClient:
    events: list = field(default_factory=list)
    fail_all: bool = False
    fail_on_type: Optional[str] = None
    readable: bool = True
    calls: int = 0
    by_id: dict = field(default_factory=dict)

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        self.calls += 1
        if self.fail_all or (self.fail_on_type and event_type == self.fail_on_type):
            raise LedgerNotRecorded("simulated ledger outage (test double)")
        bad = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
        if bad:
            raise LedgerNotRecorded(f"HTTP 400 (invalid {bad}, test double)")
        entry = {"event_id": event_id, "department": department, "event_type": event_type, "actor": actor,
                 "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        e = self.by_id.get(event_id)
        if e is not None:
            if {k: e[k] for k in entry} == entry:
                return
            raise LedgerConflict("409 (test double)")
        rec = {**entry, "payload": payload}
        self.events.append(rec)
        self.by_id[event_id] = rec

    def verify(self) -> bool:
        return not self.fail_all

    def entries(self) -> list:
        if self.fail_all or not self.readable:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} for e in self.events]

    def of_type(self, t: str) -> list:
        return [e for e in self.events if e["event_type"] == t]


class FakeDockerCli:
    """See the module docstring. ``daemon=False`` simulates ``Cannot connect to the Docker daemon``."""

    def __init__(self, root: str, daemon: bool = True):
        self.root = os.path.abspath(root)
        self.daemon = daemon
        self.calls: list[list[str]] = []
        self.containers: dict[str, str] = {}      # name -> local volume dir
        self.exec_fail_next: Optional[int] = None
        os.makedirs(self.root, exist_ok=True)

    # --- helpers ------------------------------------------------------------------------------------------------------

    def _vol(self, name: str) -> str:
        return os.path.join(self.root, "vol", name)

    def _map_in(self, s: str, vol: str) -> str:
        return s.replace(WORKSPACE, vol)

    def _map_out(self, b: bytes, vol: str) -> bytes:
        return b.replace(vol.encode(), WORKSPACE.encode())

    def run(self, argv, *, timeout_s: float, stdin: bytes | None = None, output_cap: int = 1024 * 1024) -> ExecResult:
        argv = [str(a) for a in argv]
        self.calls.append(list(argv))
        if not self.daemon:
            return ExecResult(1, b"", b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
        if argv[0] == "info":
            return ExecResult(0, b"29.4.3\n", b"")
        if argv[0] == "run":
            name = argv[argv.index("--name") + 1]
            vol = self._vol(name)
            os.makedirs(vol, exist_ok=True)
            self.containers[name] = vol
            cid = hashlib.sha256(name.encode()).hexdigest()
            return ExecResult(0, (cid + "\n").encode(), b"")
        if argv[0] == "rm":
            name = argv[-1]
            self.containers.pop(name, None)
            return ExecResult(0, b"", b"")
        if argv[0] == "volume":
            name = argv[-1].replace("dlv-ws-", "dlv-")
            vol = self._vol(name)
            if os.path.isdir(vol):
                shutil.rmtree(vol)
            return ExecResult(0, b"", b"")
        if argv[0] == "cp":
            return self._cp(argv, stdin)
        if argv[0] == "exec":
            return self._exec(argv, timeout_s, output_cap)
        return ExecResult(125, b"", b"unknown docker subcommand (test double)\n")

    def _cp(self, argv, stdin) -> ExecResult:
        src, dst = argv[1], argv[2]
        if src == "-":
            name, _, path = dst.partition(":")
            vol = self.containers.get(name)
            if vol is None:
                return ExecResult(1, b"", b"no such container\n")
            dest = self._map_in(path, vol)
            os.makedirs(dest, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(stdin or b""), mode="r") as tar:
                tar.extractall(dest, filter="data")
            return ExecResult(0, b"", b"")
        name, _, path = src.partition(":")
        vol = self.containers.get(name)
        if vol is None:
            return ExecResult(1, b"", b"no such container\n")
        local = self._map_in(path, vol)
        if not os.path.exists(local):
            return ExecResult(1, b"", b"no such path\n")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(local, arcname=os.path.basename(local.rstrip("/")))
        return ExecResult(0, buf.getvalue(), b"")

    def _exec(self, argv, timeout_s, output_cap) -> ExecResult:
        i = 1
        cwd = WORKSPACE
        env = {}
        while argv[i].startswith("-"):
            if argv[i] == "--user":
                i += 2
            elif argv[i] == "-w":
                cwd = argv[i + 1]
                i += 2
            elif argv[i] == "--env":
                k, _, v = argv[i + 1].partition("=")
                env[k] = v
                i += 2
            else:
                i += 1
        name = argv[i]
        cmd = argv[i + 1:]
        vol = self.containers.get(name)
        if vol is None:
            return ExecResult(1, b"", b"no such container\n")
        secs = timeout_s
        if cmd[:3] == ["timeout", "-k", "5"]:
            secs = float(cmd[3])
            cmd = cmd[4:]
        cmd = [self._map_in(a, vol) for a in cmd]
        if cmd[0] in ("pytest",):
            cmd = [sys.executable, "-m", "pytest", *cmd[1:]]
        elif cmd[0] in ("python", "python3"):
            cmd = [sys.executable, *cmd[1:]]
        local_cwd = self._map_in(cwd, vol)
        if not os.path.isdir(local_cwd):
            return ExecResult(1, b"", b"cwd missing\n")
        if self.exec_fail_next is not None:
            code, self.exec_fail_next = self.exec_fail_next, None
            return ExecResult(code, b"", b"simulated exec failure\n")
        full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": vol, "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1", **env}
        try:
            r = subprocess.run(cmd, cwd=local_cwd, capture_output=True, timeout=secs, env=full_env)
        except subprocess.TimeoutExpired as exc:
            return ExecResult(124, self._map_out(exc.stdout or b"", vol), self._map_out(exc.stderr or b"", vol), True)
        except OSError as exc:
            return ExecResult(127, b"", f"{type(exc).__name__}\n".encode())
        out, err = self._map_out(r.stdout, vol), self._map_out(r.stderr, vol)
        truncated = len(out) > output_cap
        return ExecResult(r.returncode, out[:output_cap], err[:output_cap], False, truncated)

    # --- inspection ---------------------------------------------------------------------------------------------------

    def argv_of(self, sub: str) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == sub]

    def exec_commands(self) -> list[str]:
        """The ``/bin/bash -lc <cmd>`` strings the adapter asked the container to run."""
        out = []
        for c in self.argv_of("exec"):
            if "-lc" in c:
                out.append(c[c.index("-lc") + 1])
        return out


class FakeChatModel:
    """A ``ChatBackend`` driven by a scenario (see the module docstring)."""

    provider = "fake"
    model = "fake-engineer"
    fake = True

    def __init__(self, scenario: list[dict], clock=None):
        self.scenario = list(scenario)
        self.clock = clock
        self.calls: list[ChatTurn] = []
        self.n = 0

    @classmethod
    def from_file(cls, path: str, clock=None) -> "FakeChatModel":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh), clock)

    def complete(self, turn: ChatTurn) -> ChatAnswer:
        self.calls.append(turn)
        while self.n < len(self.scenario) and "advance_clock_s" in self.scenario[self.n]:
            if self.clock is not None:
                self.clock.advance(seconds=self.scenario[self.n]["advance_clock_s"])
            self.n += 1
        if self.n >= len(self.scenario):
            return ChatAnswer("BLOCKED: scenario exhausted", [], 1, 1)
        step = self.scenario[self.n]
        self.n += 1
        calls = []
        for k, tc in enumerate(step.get("tool_calls", [])):
            calls.append({"id": f"call-{self.n}-{k}", "name": tc["name"], "args": tc.get("args", {})})
        return ChatAnswer(step.get("text", ""), calls, int(step.get("tokens_in", 10)), int(step.get("tokens_out", 5)))

    def seen_tool_results(self) -> list[str]:
        out = []
        for t in self.calls:
            for m in t.messages:
                if m["role"] == "tool":
                    out.append(m["content"])
        return out


class FakeMemory:
    """A backend that honours the MemoryPort contract (per-principal, record-first, purge verified)."""

    name = "fake"

    def __init__(self):
        self.store: dict[tuple[str, str], list[Fact]] = {}
        self.purges: list[str] = []

    def _key(self, principal: Principal, thread_id: str) -> tuple[str, str]:
        return (f"{principal.tenant}:{principal.id}", thread_id)

    def context(self, principal: Principal, thread_id: str) -> MemoryContext:
        facts = self.store.get(self._key(principal, thread_id), [])
        if not facts:
            return MemoryContext(available=True, facts_sha256=None, text=None)
        text = "\n".join(f"{f.key}: {f.text}" for f in facts)
        return MemoryContext(available=True, facts_sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)

    def remember(self, principal: Principal, thread_id: str, facts: list[Fact], request_id: str, record) -> Ack:
        try:
            record("memory_fact_recorded", {"principal_sha256": hashlib.sha256(self._key(principal, thread_id)[0].encode()).hexdigest(),
                                           "count": len(facts), "request_id": request_id})
        except Exception:  # noqa: BLE001 - not recorded → not written
            return Ack(available=True, ok=False)
        self.store.setdefault(self._key(principal, thread_id), []).extend(facts)
        return Ack(available=True, ok=True, reference=request_id)

    def forget(self, principal: Principal, thread_id: str, record) -> Ack:
        self.store.pop(self._key(principal, thread_id), None)
        ok = not self.context(principal, thread_id).text
        record("memory_purge_verified" if ok else "memory_purge_incomplete", {"thread_id": thread_id})
        self.purges.append(thread_id)
        return Ack(available=True, ok=ok)


SECRET_RE = re.compile(r"sk-test-[A-Za-z0-9]{20,}")
