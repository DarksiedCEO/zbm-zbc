"""
Tool receipts middleware (spec §C.3.4). ``ZbmToolReceiptMiddleware`` is a LangChain ``AgentMiddleware`` passed through
``DeerFlowClient(middlewares=[...])`` (client.py:189, merged in ``build_middlewares``). It wraps every tool call's
result path and records ``tool_result_recorded {tool_call_id, result_sha256, bytes, truncated}`` AFTER the tool ran
and BEFORE the model sees the result; a failed record marks the run ``failed`` and raises so the turn stops.
Only hashes and sizes are recorded — never the result text.
"""

from __future__ import annotations

import hashlib
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from zbm_delivery import registry
from zbm_delivery.ledger import derived_id

ACTOR = "intel_03_guardrail"
RESULT_CAP = 20_000


def _text_of(result: Any) -> str:
    if isinstance(result, ToolMessage):
        c = result.content
        return c if isinstance(c, str) else str(c)
    if isinstance(result, Command):
        return str(getattr(result, "update", "") or "")
    return str(result)


class ZbmToolReceiptMiddleware(AgentMiddleware):
    name = "zbm-tool-receipts"

    def __init__(self, thread_id: str):
        super().__init__()
        self.thread_id = thread_id
        self.receipts = 0

    def _record(self, request: ToolCallRequest, result: Any) -> Any:
        rt = registry.runtime()
        binding = registry.lookup(self.thread_id)
        if binding is None:
            raise RuntimeError("tool receipt: no run bound to this thread")
        text = _text_of(result)
        raw = text.encode("utf-8", "surrogatepass")
        truncated = len(raw) > RESULT_CAP
        self.receipts += 1
        call_id = str(request.tool_call.get("id") or "")[:64]
        payload = {"run_id": binding.run_id, "tool_call_id": call_id, "tool": str(request.tool_call.get("name") or "")[:64],
                   "result_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "truncated": truncated,
                   "seq": self.receipts}
        try:
            rt.record(derived_id("tr", binding.run_id, self.receipts, call_id, payload["result_sha256"]),
                      "tool_result_recorded", ACTOR, binding.run_id, payload,
                      f"Tool result {self.receipts} recorded ({binding.run_id})")
        except Exception as exc:  # noqa: BLE001 - unrecorded result never reaches the model
            rt.on_ledger_failure(binding.run_id, f"tool_result_recorded record failed: {type(exc).__name__}")
            raise RuntimeError("tool result could not be recorded; the turn stops (record-first)") from None
        if truncated and isinstance(result, ToolMessage):
            result = ToolMessage(content=text[:RESULT_CAP] + "\n[result truncated at 20000 characters]",
                                 tool_call_id=result.tool_call_id, name=result.name, id=result.id,
                                 status=getattr(result, "status", "success"))
        return result

    def wrap_tool_call(self, request: ToolCallRequest,
                       handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        return self._record(request, handler(request))

    async def awrap_tool_call(self, request: ToolCallRequest,
                              handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]) -> ToolMessage | Command:
        return self._record(request, await handler(request))
