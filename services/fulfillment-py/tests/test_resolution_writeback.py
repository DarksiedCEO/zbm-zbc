from agents.resolution_writeback import TerminalEvent, resolve_and_writeback
from fulfillment_schema import ResolutionType, WriteBackStatus
from integrations.system_of_record import InMemorySystemOfRecord, NotConfiguredSystemOfRecord


def test_every_terminal_event_gets_a_resolution_record():
    events = [
        TerminalEvent("call", "call_1", "cust_1", ResolutionType.BOOKED),
        TerminalEvent("appointment", "appt_1", "cust_2", ResolutionType.CONFIRMED_COMPLETE),
        TerminalEvent("task", "task_1", None, ResolutionType.NO_RESOLUTION),
    ]
    records = resolve_and_writeback(events, InMemorySystemOfRecord())
    assert len(records) == 3
    assert {r.entity_id for r in records} == {"call_1", "appt_1", "task_1"}


def test_successful_writeback_is_marked_success():
    events = [TerminalEvent("call", "call_1", "cust_1", ResolutionType.BOOKED)]
    records = resolve_and_writeback(events, InMemorySystemOfRecord())
    assert records[0].write_back_status == WriteBackStatus.SUCCESS


def test_failed_writeback_is_marked_failed_not_swallowed():
    som = InMemorySystemOfRecord()
    som.fail_next = True
    events = [TerminalEvent("call", "call_1", "cust_1", ResolutionType.BOOKED)]
    records = resolve_and_writeback(events, som)
    assert records[0].write_back_status == WriteBackStatus.FAILED
    assert records[0].write_back_detail is not None


def test_no_system_of_record_configured_is_visible_not_silent_success():
    """The core completeness guarantee: when no client CRM is wired, the
    record must say NOT_CONFIGURED — never SUCCESS — so nothing looks
    resolved when it silently wasn't written anywhere."""
    events = [TerminalEvent("appointment", "appt_1", "cust_1", ResolutionType.CONFIRMED_COMPLETE)]
    records = resolve_and_writeback(events, NotConfiguredSystemOfRecord())
    assert records[0].write_back_status == WriteBackStatus.NOT_CONFIGURED
    assert records[0].write_back_status != WriteBackStatus.SUCCESS


def test_no_resolution_type_is_still_recorded_not_dropped():
    """A dead end (customer never reached) must still produce a record —
    silence is never treated as an acceptable outcome in this department."""
    events = [TerminalEvent("task", "task_9", "cust_9", ResolutionType.NO_RESOLUTION)]
    records = resolve_and_writeback(events, InMemorySystemOfRecord())
    assert len(records) == 1
    assert records[0].resolution_type == ResolutionType.NO_RESOLUTION


def test_resolution_ids_are_unique_across_a_batch():
    events = [
        TerminalEvent("call", "call_1", "cust_1", ResolutionType.BOOKED),
        TerminalEvent("call", "call_1", "cust_1", ResolutionType.NO_RESOLUTION),  # same entity, retried
    ]
    records = resolve_and_writeback(events, InMemorySystemOfRecord())
    assert records[0].resolution_id != records[1].resolution_id
