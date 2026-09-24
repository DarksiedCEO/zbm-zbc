"""
Agent: Customer Dossier
Single job: fold CallEvents and Appointments into one CustomerDossier per
customer — Beside's core mechanic (one shared record, not siloed logs).
This agent never decides what to DO about a customer (that's
missed_call_detection / appointment_tracking); it only maintains the
record other agents and a human read from.

Merge policy (encoded here, the agent's rule): idempotent — feeding the
same events through twice must not duplicate history entries. A
customer_id of None on a CallEvent (unmatched caller) is skipped here;
resolving an unknown caller to a customer_id is out of scope for this
agent — that's a matching/identity problem this pass does not solve, and
is named as a known gap.
"""

from __future__ import annotations

from fulfillment_schema import Appointment, CallEvent, CustomerDossier

AGENT_ID = "customer-dossier-v1"


def apply_updates(
    existing: dict[str, CustomerDossier],
    call_events: list[CallEvent],
    appointments: list[Appointment],
) -> dict[str, CustomerDossier]:
    """Return updated COPIES of only the dossiers these events touch (new
    customers included). `existing` and its dossiers are never mutated.

    Fix wave 4: the cost is proportional to the dossiers touched, not to the
    whole store (build_or_update used to deep-copy every dossier on every
    call), and history membership is a set lookup, not a list scan.
    """
    changed: dict[str, CustomerDossier] = {}
    seen: dict[str, tuple[set[str], set[str], set[str]]] = {}

    def touch(customer_id: str) -> tuple[CustomerDossier, tuple[set[str], set[str], set[str]]]:
        d = changed.get(customer_id)
        if d is None:
            base = existing.get(customer_id)
            d = base.model_copy(deep=True) if base is not None else CustomerDossier(customer_id=customer_id)
            changed[customer_id] = d
            seen[customer_id] = (set(d.call_history), set(d.phone_numbers), set(d.appointment_history))
        return d, seen[customer_id]

    for ev in call_events:
        if ev.customer_id is None:
            continue  # unmatched caller — identity resolution is a known gap, not this agent's job
        d, (calls, phones, _) = touch(ev.customer_id)

        if ev.call_id not in calls:
            calls.add(ev.call_id)
            d.call_history.append(ev.call_id)
        if ev.phone_number not in phones:
            phones.add(ev.phone_number)
            d.phone_numbers.append(ev.phone_number)
        if d.first_contact_at is None or ev.started_at < d.first_contact_at:
            d.first_contact_at = ev.started_at
        if d.last_contact_at is None or ev.started_at > d.last_contact_at:
            d.last_contact_at = ev.started_at

    for appt in appointments:
        d, (_, _, appts) = touch(appt.customer_id)
        if appt.appointment_id not in appts:
            appts.add(appt.appointment_id)
            d.appointment_history.append(appt.appointment_id)
        if d.first_contact_at is None or appt.scheduled_at < d.first_contact_at:
            d.first_contact_at = appt.scheduled_at
        if d.last_contact_at is None or appt.scheduled_at > d.last_contact_at:
            d.last_contact_at = appt.scheduled_at

    return changed


def build_or_update(
    existing: dict[str, CustomerDossier],
    call_events: list[CallEvent],
    appointments: list[Appointment],
) -> dict[str, CustomerDossier]:
    """The whole store after the update: `existing` plus apply_updates().
    Untouched dossiers are the same objects as in `existing` (not copies);
    touched ones are new objects, so `existing` is never mutated."""
    return {**existing, **apply_updates(existing, call_events, appointments)}
