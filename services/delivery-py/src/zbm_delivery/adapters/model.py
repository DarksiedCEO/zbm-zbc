"""
The model class deer-flow constructs from ``models[].use`` (spec §C.4, D5; ``models/factory.py:208,343``).

``EgressChatModel(BaseChatModel)`` holds NO provider SDK client. It turns LangChain messages into a neutral
``ChatTurn`` and delegates to the process's ``ChatBackend`` (registry): ``AnthropicMessagesBackend`` or
``OpenAICompatBackend`` build the provider wire request by hand and send it through ``EgressClient.request(
purpose="llm")``; the API key is read from the key reference at call time, sent only as the auth header, never
logged, never in an exception text (G7). With no backend (no key, no provider) the call raises
``LLMNotConfigured`` — the service refuses the run before any worktree exists (§C.4), so this is a backstop.

This file imports only ``BaseChatModel`` from the LangChain model classes (G2): no ``langchain_anthropic``,
``langchain_openai``, ``anthropic`` or ``openai`` anywhere in ``src/``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

from zbm_delivery import registry
from zbm_delivery.adapters.egress import EgressClient, EgressFailed, EgressRefused
from zbm_delivery.ports import ChatAnswer, ChatTurn, LLMNotConfigured, NoChatBackend, VaultUnavailable


# --- LangChain <-> neutral turn -----------------------------------------------------------------------------------------

def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
        return "".join(parts)
    return str(content)


def to_turn(messages: list[BaseMessage], tools: list[dict]) -> ChatTurn:
    out: list[dict] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": _content_text(m.content)})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": _content_text(m.content)})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": _content_text(m.content),
                        "tool_calls": [{"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args") or {}}
                                       for tc in (m.tool_calls or [])]})
        elif isinstance(m, ToolMessage):
            out.append({"role": "tool", "content": _content_text(m.content), "tool_call_id": m.tool_call_id,
                        "name": m.name})
        else:
            out.append({"role": "user", "content": _content_text(m.content)})
    return ChatTurn(messages=out, tools=tools)


class EgressChatModel(BaseChatModel):
    model: str = "engine"
    max_tokens: int = 8192
    temperature: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "zbm-egress"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        specs = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=specs, **kwargs)

    def _backend(self):
        rt = registry.runtime_or_none()
        backend = rt.chat_backend if rt is not None else None
        if backend is None:
            raise LLMNotConfigured("no chat backend installed")
        return backend

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None, run_manager=None,
                  **kwargs: Any) -> ChatResult:
        tools = kwargs.get("tools") or []
        answer: ChatAnswer = self._backend().complete(to_turn(messages, tools))
        msg = AIMessage(content=answer.text or "",
                        tool_calls=[{"id": tc["id"], "name": tc["name"], "args": tc.get("args") or {}, "type": "tool_call"}
                                    for tc in answer.tool_calls],
                        usage_metadata={"input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens,
                                        "total_tokens": answer.input_tokens + answer.output_tokens})
        return ChatResult(generations=[ChatGeneration(message=msg)])


# --- key references ---------------------------------------------------------------------------------------------------

def key_provider(settings, vault, env: Optional[dict] = None) -> Callable[[], str]:
    """A callable that yields the key at call time (never stored on any object that could be logged)."""
    ref = settings.llm_api_key_ref
    if not ref:
        raise LLMNotConfigured("DLV_LLM_API_KEY_REF is not set")
    if ref.startswith("env:"):
        name = ref[4:]

        def from_env() -> str:
            val = (env if env is not None else os.environ).get(name)
            if not val:
                raise LLMNotConfigured("the referenced environment variable is empty")
            return val
        from_env()             # fail at build time, not at the first turn
        return from_env
    if ref.startswith("vault:"):
        try:
            vault.secret(ref[6:])
        except VaultUnavailable as exc:
            raise LLMNotConfigured(str(exc)) from None
        return lambda: vault.secret(ref[6:])
    raise LLMNotConfigured("unknown key reference shape")


# --- wire backends ------------------------------------------------------------------------------------------------------

class _WireBackend:
    fake = False

    def __init__(self, egress: EgressClient, key: Callable[[], str], model: str, base_url: str):
        self.egress = egress
        self._key = key
        self.model = model
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _run_scope() -> tuple[str, Optional[float]]:
        """(run_id, remaining wall clock seconds) of the run whose turn is in flight — the binding whose principal is
        the effective user id (R6: the egress total deadline never exceeds the run's remaining wall clock)."""
        try:
            from datetime import timezone

            from deerflow.runtime.user_context import get_effective_user_id
            uid = get_effective_user_id()
            rt = registry.runtime_or_none()
            for b in registry.all_bindings():
                if b.principal_user_id == uid:
                    remaining = None
                    if rt is not None:
                        now = rt.clock.now().astimezone(timezone.utc)
                        remaining = max(0.0, (b.deadline_at - now).total_seconds())
                    return b.run_id, remaining
        except Exception:  # noqa: BLE001
            pass
        return "-", None

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        run_id, remaining = self._run_scope()
        try:
            resp = self.egress.request("POST", url, purpose="llm", headers=headers, body=body, run_id=run_id,
                                       deadline_s=remaining)
        except EgressRefused as exc:
            raise EgressRefused(f"LLM call refused: {exc}") from None
        except EgressFailed as exc:
            raise EgressFailed(f"LLM call failed: {exc}") from None
        if resp.status_code != 200:
            raise EgressFailed(f"LLM provider answered HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise EgressFailed("LLM provider answered non-JSON") from None
        if not isinstance(data, dict):
            raise EgressFailed("LLM provider answered a non-object")
        return data


class AnthropicMessagesBackend(_WireBackend):
    provider = "anthropic"

    def complete(self, turn: ChatTurn) -> ChatAnswer:
        system = "\n\n".join(m["content"] for m in turn.messages if m["role"] == "system")
        msgs: list[dict] = []
        for m in turn.messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
                msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                    msgs[-1]["content"].append(block)
                else:
                    msgs.append({"role": "user", "content": [block]})
        tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                  "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                 for t in turn.tools if t.get("type") == "function"]
        payload: dict = {"model": self.model, "max_tokens": 8192, "messages": msgs}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        headers = {"x-api-key": self._key(), "anthropic-version": "2023-06-01", "content-type": "application/json"}
        data = self._post(f"{self.base_url}/v1/messages", headers, payload)
        text, calls = [], []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                calls.append({"id": str(block.get("id", "")), "name": str(block.get("name", "")),
                              "args": block.get("input") if isinstance(block.get("input"), dict) else {}})
        usage = data.get("usage") or {}
        return ChatAnswer("".join(text), calls, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))


class OpenAICompatBackend(_WireBackend):
    provider = "openai_compatible"

    def complete(self, turn: ChatTurn) -> ChatAnswer:
        msgs: list[dict] = []
        for m in turn.messages:
            if m["role"] in ("system", "user"):
                msgs.append({"role": m["role"], "content": m["content"]})
            elif m["role"] == "assistant":
                entry: dict = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [{"id": tc["id"], "type": "function",
                                            "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                                           for tc in m["tool_calls"]]
                msgs.append(entry)
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        payload: dict = {"model": self.model, "messages": msgs, "temperature": 0}
        if turn.tools:
            payload["tools"] = turn.tools
        headers = {"authorization": f"Bearer {self._key()}", "content-type": "application/json"}
        data = self._post(f"{self.base_url}/chat/completions", headers, payload)
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices and isinstance(choices[0], dict) else None) or {}
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"id": str(tc.get("id", "")), "name": str(fn.get("name", "")),
                          "args": args if isinstance(args, dict) else {}})
        usage = data.get("usage") or {}
        return ChatAnswer(str(message.get("content") or ""), calls, int(usage.get("prompt_tokens") or 0),
                          int(usage.get("completion_tokens") or 0))


def backend_from_settings(settings, egress: Optional[EgressClient], vault, env: Optional[dict] = None):
    """The production backend for the configured provider, or ``NoChatBackend`` with the reason (never raises)."""
    if settings.llm_provider is None:
        return NoChatBackend("DLV_LLM_PROVIDER is not set")
    if settings.llm_provider == "fake":
        return NoChatBackend("DLV_LLM_PROVIDER=fake: a scripted backend must be injected by the embedding process "
                             "(tests); none was")
    if egress is None or not settings.llm_model:
        return NoChatBackend("egress client or DLV_LLM_MODEL missing")
    try:
        key = key_provider(settings, vault, env)
    except LLMNotConfigured as exc:
        return NoChatBackend(str(exc))
    if settings.llm_provider == "anthropic":
        return AnthropicMessagesBackend(egress, key, settings.llm_model, "https://api.anthropic.com")
    return OpenAICompatBackend(egress, key, settings.llm_model, settings.llm_api_base or "")
