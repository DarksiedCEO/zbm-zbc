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


def build_or_update(
    existing: dict[str, CustomerDossier],
    call_events: list[CallEvent],
    appointments: list[Appointment],
) -> dict[str, CustomerDossier]:
    dossiers: dict[str, CustomerDossier] = {
        cid: d.model_copy(deep=True) for cid, d in existing.items()
    }

    for ev in call_events:
        if ev.customer_id is None:
            continue  # unmatched caller — identity resolution is a known gap, not this agent's job
        d = dossiers.setdefault(ev.customer_id, CustomerDossier(customer_id=ev.customer_id))

        if ev.call_id not in d.call_history:
            d.call_history.append(ev.call_id)
        if ev.phone_number not in d.phone_numbers:
            d.phone_numbers.append(ev.phone_number)
        if d.first_contact_at is None or ev.started_at < d.first_contact_at:
            d.first_contact_at = ev.started_at
        if d.last_contact_at is None or ev.started_at > d.last_contact_at:
            d.last_contact_at = ev.started_at

    for appt in appointments:
        d = dossiers.setdefault(appt.customer_id, CustomerDossier(customer_id=appt.customer_id))
        if appt.appointment_id not in d.appointment_history:
            d.appointment_history.append(appt.appointment_id)
        if d.first_contact_at is None or appt.scheduled_at < d.first_contact_at:
            d.first_contact_at = appt.scheduled_at
        if d.last_contact_at is None or appt.scheduled_at > d.last_contact_at:
            d.last_contact_at = appt.scheduled_at

    return dossiers
