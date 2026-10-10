"""The LLM persona port (ADR 0018). Personas that an LLM writes (an angry customer, a confused grandparent, a
scammer) are a planned input to the war room; no model is connected. The port says so and returns nothing: it never
fabricates personas, and a run never counts them. The deterministic personas in the scenario libraries are what
the gate runs on."""

from __future__ import annotations

NOT_CONNECTED = "NOT_CONNECTED"


class LLMPersonaPort:
    connected = False

    def status(self) -> str:
        return NOT_CONNECTED

    def generate(self, department: str, count: int, seed: int) -> dict:
        return {"status": NOT_CONNECTED, "department": department, "personas": [],
                "detail": "no LLM is wired to the war room; nothing was generated"}
