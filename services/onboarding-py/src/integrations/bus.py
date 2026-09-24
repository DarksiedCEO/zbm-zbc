"""
Shared event bus (spec: "Publish events to a shared bus; questions go as
request -> ruling"). No bus exists in the monorepo yet, and the consumers
Onboarding publishes to (the risk watcher, AEGIS) are not built. The
default ``InProcessEventBus`` keeps published events in memory so they are
inspectable; nothing downstream receives them. Every publish is ALSO a
ledger crossing, recorded by the service before it publishes.
"""

from __future__ import annotations

from typing import Protocol

from onboarding_schema import DomainEvent


class EventBus(Protocol):
    def publish(self, event: DomainEvent) -> None: ...


class InProcessEventBus:
    delivered_downstream = False

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    def publish(self, event: DomainEvent) -> None:
        self.events.append(event)
