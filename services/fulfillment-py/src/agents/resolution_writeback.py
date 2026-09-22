"""
Agent: Resolution / Write-Back
Single job: for every entity (call, appointment, task) that reached a
terminal state, produce a ResolutionRecord and attempt to write it back
to the client's system of record through the injected SystemOfRecordPort
(ADR 0002, Decision 4). This is the structural implementation of Numa's
"resolve to completion" promise, scoped honestly: every entity actually
HANDED TO THIS FUNCTION gets a record and an attempted write-back, and
NOT_CONFIGURED/FAILED are both visible, never silently equated with
SUCCESS. That is NOT the same claim as "nothing terminal in this
department goes unrecorded" — there is no pipeline this pass that
automatically calls this agent when a call/appointment/task resolves
(README "Known gaps" — no orchestrator layer). An independent review
(Sep 22 2026) correctly flagged an earlier version of this docstring for
implying the stronger, pipeline-level guarantee; corrected here.

Independent review finding (Sep 22 2026, CONFIRMED): resolution_id was
built from the entity type/id plus the event's index WITHIN A SINGLE
BATCH — two separate API calls resolving the same call_1001 both
produced the identical ID "res-call-call_1001-0". Fixed with uuid4, so
IDs are unique across batches and callers, which matters for any real
CRM adapter relying on this ID for idempotent writes/retries.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fulfillment_schema import ResolutionRecord, ResolutionType, WriteBackStatus
from integrations.system_of_record import SystemOfRecordPort

AGENT_ID = "resolution-writeback-v1"


@dataclass(frozen=True)
class TerminalEvent:
    entity_type: str  # "call" | "appointment" | "task"
    entity_id: str
    customer_id: str | None
    resolution_type: ResolutionType


def resolve_and_writeback(
    events: list[TerminalEvent],
    system_of_record: SystemOfRecordPort,
) -> list[ResolutionRecord]:
    records: list[ResolutionRecord] = []

    for ev in events:
        record = ResolutionRecord(
            resolution_id=f"res-{ev.entity_type}-{ev.entity_id}-{uuid.uuid4().hex[:12]}",
            entity_type=ev.entity_type,
            entity_id=ev.entity_id,
            customer_id=ev.customer_id,
            resolution_type=ev.resolution_type,
            write_back_status=WriteBackStatus.PENDING,
        )

        outcome = system_of_record.write_back(record)
        record = record.model_copy(
            update={
                "write_back_status": outcome.status,
                "write_back_detail": outcome.detail,
            }
        )
        records.append(record)

    return records
