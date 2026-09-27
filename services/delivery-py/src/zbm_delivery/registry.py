"""
Process-level registry: the bridge between the service (which owns the recorder, the evidence store, the Docker CLI,
the egress client and the model backend) and the adapter classes deer-flow instantiates BY CLASS PATH with no
reference to our service (``sandbox.use``, ``guardrails.provider.use``, ``models[].use``).

``Runtime`` is installed once per service instance (``install``); ``RunBinding`` is registered per run by the runner
before the first agent turn (spec §C.3.1: the run token is minted by the runner, 32 random bytes, kept in memory
only, never in a prompt, never in the sandbox env — an agent cannot obtain or present it; the runner presents it by
registering the thread). ``lookup(thread_id)`` is what the guardrail and the sandbox provider consult.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

_lock = threading.RLock()
_runtime: Optional["Runtime"] = None
_bindings: dict[str, "RunBinding"] = {}


@dataclass
class Runtime:
    settings: Any
    recorder: Any                 # ledger.Recorder
    docker: Any                   # ports.DockerCli
    chat_backend: Any             # ports.ChatBackend
    egress: Any                   # adapters.egress.EgressClient | None
    policy_seed: dict
    test_seed: dict
    clock: Any
    on_ledger_failure: Callable[[str, str], None]     # (run_id, why) -> marks the run failed
    record: Callable[..., str]    # record(event_id, event_type, actor, subject, payload, summary) -> event id
    resolve_sandbox_path: Callable[[str, str], Optional[str]]   # (run_id, path) -> realpath inside the sandbox
    evidence_root: str = ""


@dataclass
class RunBinding:
    run_id: str
    thread_id: str
    service: str
    principal_user_id: str
    workspace: str
    deadline_at: datetime
    token: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    status: Callable[[], str] = lambda: "running"
    tool_calls: int = 0
    subagents: int = 0
    denies: int = 0
    container_name: Optional[str] = None
    finished: bool = False
    exec_seq: int = 0

    def live(self, now: datetime) -> bool:
        return (not self.finished) and self.status() in ("running", "suite") and now < self.deadline_at


def install(rt: Runtime) -> None:
    global _runtime
    with _lock:
        _runtime = rt


def runtime() -> Runtime:
    with _lock:
        if _runtime is None:
            raise RuntimeError("zbm_delivery runtime not installed (the service builds it; adapters cannot run alone)")
        return _runtime


def runtime_or_none() -> Optional[Runtime]:
    with _lock:
        return _runtime


def bind(binding: RunBinding) -> None:
    with _lock:
        _bindings[binding.thread_id] = binding


def unbind(thread_id: str) -> None:
    with _lock:
        b = _bindings.pop(thread_id, None)
        if b is not None:
            b.finished = True


def lookup(thread_id: Optional[str]) -> Optional[RunBinding]:
    if not thread_id:
        return None
    with _lock:
        return _bindings.get(thread_id)


def by_run(run_id: str) -> Optional[RunBinding]:
    with _lock:
        for b in _bindings.values():
            if b.run_id == run_id:
                return b
    return None


def all_bindings() -> list[RunBinding]:
    with _lock:
        return list(_bindings.values())


def clear() -> None:
    """Tests only: forget every binding (the runtime stays until the next install)."""
    with _lock:
        _bindings.clear()
