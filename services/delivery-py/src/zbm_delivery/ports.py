"""
Outbound ports and their fail-closed stand-ins (spec §D.1, §C.6). Every stand-in answers "unavailable/refused"; the
test fakes live in tests/fakes.py only (G3: no ``Fake*``/``Passing*`` class is importable from ``src/``).

- ``DockerCli``: the argv runner behind the sandbox adapter (``RealDockerCli`` in adapters/sandbox.py).
- ``ChatBackend``: what ``EgressChatModel`` delegates a model call to (the two wire shapes in adapters/model.py;
  ``NoChatBackend`` = LLM_NOT_CONFIGURED).
- ``Vault``: the Cybersecurity (22) key vault — ``NotWiredVault`` answers unavailable.
- ``MemoryPort`` / ``MemoryOff``: the partner's future memory agents plug in here (§C.6); engine runs are off.
- ``Principal``: one per run, never ``"default"`` (§C.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence

_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_\-]{1,120}$")


# --- docker --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    truncated: bool = False


class DockerUnavailable(RuntimeError):
    """The daemon is not reachable (``docker info`` failed). No run starts; nothing is queued (spec C.1.11)."""


class DockerCli(Protocol):
    def run(self, argv: Sequence[str], *, timeout_s: float, stdin: bytes | None = None,
            output_cap: int = 1024 * 1024) -> ExecResult:
        """Run ``docker <argv...>`` (argv[0] is the docker binary; shell=False). Never raises on a non-zero exit."""
        ...


# --- model ---------------------------------------------------------------------------------------------------------

class LLMNotConfigured(RuntimeError):
    """No provider key / backend: the run is refused ``LLM_NOT_CONFIGURED`` before any worktree exists."""


@dataclass
class ChatTurn:
    """One model call: the messages (LangChain-neutral dicts) and the tools the model may call."""

    messages: list[dict]
    tools: list[dict]


@dataclass
class ChatAnswer:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)       # [{"id", "name", "args"}]
    input_tokens: int = 0
    output_tokens: int = 0


class ChatBackend(Protocol):
    provider: str
    model: str
    fake: bool

    def complete(self, turn: ChatTurn) -> ChatAnswer: ...


class NoChatBackend:
    """Stand-in when no provider is configured: every call raises ``LLMNotConfigured``."""

    provider = "unconfigured"
    model = ""
    fake = False

    def __init__(self, why: str = "no LLM provider or key is configured"):
        self.why = why

    def complete(self, turn: ChatTurn) -> ChatAnswer:
        raise LLMNotConfigured(self.why)


# --- vault ---------------------------------------------------------------------------------------------------------

class VaultUnavailable(RuntimeError):
    pass


class Vault(Protocol):
    def secret(self, ref: str) -> str: ...


class NotWiredVault:
    """The Cybersecurity (22) vault is not built; every reference is unavailable (spec D.1)."""

    def secret(self, ref: str) -> str:
        raise VaultUnavailable("vault not wired (DLV_VAULT unset); provider keys are unavailable outside non-production")


# --- identity ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    kind: str          # "service" | "session"
    id: str            # caller name (aegis, andre_session, scheduler)
    tenant: str        # zbm

    def __post_init__(self):
        if self.kind not in ("service", "session"):
            raise ValueError("principal kind must be service or session")
        for v in (self.id, self.tenant):
            if not _PRINCIPAL_RE.fullmatch(v) or v == "default":
                raise ValueError("principal id/tenant must match [A-Za-z0-9_-]{1,120} and never be 'default'")

    def user_id(self, run_id: str) -> str:
        """The deer-flow user id bound to one run. deer-flow's ``_validate_user_id`` (config/paths.py:35) allows only
        ``[A-Za-z0-9_-]``, so the spec's ``tenant:run_id`` shape is written ``tenant--run_id`` (ADR 0011, spec defect)."""
        return f"{self.tenant}--{run_id}"


# --- memory (§C.6) --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    key: str
    text: str


@dataclass(frozen=True)
class MemoryContext:
    available: bool
    facts_sha256: Optional[str] = None
    text: Optional[str] = None


@dataclass(frozen=True)
class Ack:
    available: bool
    ok: bool
    reference: Optional[str] = None


class MemoryPort(Protocol):
    """Contract every future implementation must meet (tested against ``MemoryOff`` and the test ``FakeMemory``):

    1. per-principal namespacing: a fact written under one principal is never returned for another;
    2. record-first: ``memory_fact_recorded`` is on the ledger before the backend write (``remember`` takes the
       recorder callback; a failed record → ``Ack(available=True, ok=False)`` and nothing is written);
    3. text is returned as DATA and injected by the runner behind textguard inside a
       ``--- BEGIN MEMORY (data) ---`` block, never as a "user-managed" instruction (ST-11);
    4. purge is verified: after ``forget(principal, thread_id)`` a ``context()`` returns nothing, and the port
       reports ``memory_purge_verified`` / ``memory_purge_incomplete`` through the recorder (ST-05).
    A partner backend is a ``deerflow.agents.memory.manager.MemoryManager`` subclass reached THROUGH this port; DF's
    own ``memory.enabled`` stays false (D11).
    """

    def context(self, principal: Principal, thread_id: str) -> MemoryContext: ...

    def remember(self, principal: Principal, thread_id: str, facts: list[Fact], request_id: str,
                 record: Any) -> Ack: ...

    def forget(self, principal: Principal, thread_id: str, record: Any) -> Ack: ...


class MemoryOff:
    """The only implementation in this build (D11): nothing is available, nothing is remembered."""

    name = "off"

    def context(self, principal: Principal, thread_id: str) -> MemoryContext:
        return MemoryContext(available=False)

    def remember(self, principal: Principal, thread_id: str, facts: list[Fact], request_id: str, record: Any) -> Ack:
        return Ack(available=False, ok=False)

    def forget(self, principal: Principal, thread_id: str, record: Any) -> Ack:
        return Ack(available=False, ok=False)


MEMORY_BLOCK_BEGIN = "--- BEGIN MEMORY (data) ---"
MEMORY_BLOCK_END = "--- END MEMORY ---"


def memory_block(ctx: MemoryContext) -> str:
    """How memory text enters a prompt: as a fenced DATA block, never as an instruction (ST-11)."""
    if not ctx.available or not ctx.text:
        return ""
    return f"{MEMORY_BLOCK_BEGIN}\n{ctx.text}\n{MEMORY_BLOCK_END}\n"
