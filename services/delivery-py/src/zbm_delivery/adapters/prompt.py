"""
System-prompt middleware: deer-flow assembles its lead-agent system prompt from its own template
(``agents/lead_agent/prompt.py``: skills index, memory, uploads, channel guidance). The engineer's system prompt is
OURS (spec §C.8.4 step 1: "system prompt = §C.9 fork + policy"), so this ``AgentMiddleware`` replaces the system
message on every model call with the assembled fork text (ADR 0011 choice: replace, not prepend — the DF template
carries guidance that contradicts the engine's rules, e.g. installing packages).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage


class ZbmSystemPromptMiddleware(AgentMiddleware):
    name = "zbm-system-prompt"

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], object]):
        return handler(request.override(system_message=SystemMessage(content=self.text)))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[object]]):
        return await handler(request.override(system_message=SystemMessage(content=self.text)))
