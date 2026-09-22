from agents import customer_dossier
from fixtures_loader import load_appointments, load_call_events, load_dossiers


def test_merges_calls_and_appointments_into_new_and_existing_dossiers():
    existing = load_dossiers()  # seeds cust_f1 with no call/appointment history yet
    calls = load_call_events()
    appts = load_appointments()

    result = customer_dossier.build_or_update(existing, calls, appts)

    # cust_f1 pre-existed and should now have both a call and an appointment folded in.
    assert "call_1001" in result["cust_f1"].call_history
    assert "call_1002" in result["cust_f1"].call_history
    assert "appt_2001" in result["cust_f1"].appointment_history
    assert result["cust_f1"].name == "Devon Carter"  # pre-existing field preserved

    # cust_f2 did not pre-exist and should be created fresh.
    assert "call_1003" in result["cust_f2"].call_history


def test_unmatched_caller_is_skipped_not_crashed_on():
    calls = load_call_events()  # includes call_1005 with customer_id=None
    result = customer_dossier.build_or_update({}, calls, [])
    all_call_ids_in_dossiers = {cid for d in result.values() for cid in d.call_history}
    assert "call_1005" not in all_call_ids_in_dossiers


def test_idempotent_merge_does_not_duplicate_history():
    calls = load_call_events()
    appts = load_appointments()
    once = customer_dossier.build_or_update({}, calls, appts)
    twice = customer_dossier.build_or_update(once, calls, appts)
    for cid, dossier in twice.items():
        assert len(dossier.call_history) == len(set(dossier.call_history))
        assert len(dossier.appointment_history) == len(set(dossier.appointment_history))
    assert once["cust_f1"].call_history == twice["cust_f1"].call_history


def test_phone_numbers_accumulate_without_duplicates():
    calls = load_call_events()
    result = customer_dossier.build_or_update({}, calls, [])
    result_again = customer_dossier.build_or_update(result, calls, [])
    assert result_again["cust_f1"].phone_numbers == result["cust_f1"].phone_numbers


def test_first_and_last_contact_bounds_are_correct():
    calls = load_call_events()
    result = customer_dossier.build_or_update({}, calls, [])
    d = result["cust_f1"]
    assert d.first_contact_at is not None
    assert d.last_contact_at is not None
    assert d.first_contact_at <= d.last_contact_at
