"""
devtools/live_run.py's ledger leak scan (ledger_leaks). The live run's "nothing personal, no amount and no name on the
ledger" check matched its probes against json.dumps of every ledger entry, and ledger-rust's recorded_at carries
nanoseconds: an entry recorded at hh:mm:12.50x holds "12.50", the probe for the partner rate, so the check failed
about once in seventy runs with nothing leaked (CI runs 38045011479 and 38110484058). The probes now run over the
fields the department wrote; the five fields the ledger writes itself are held to their exact shape instead.

Every input here is a fixed literal: no clock, no network, no process.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

LIVE_RUN = Path(__file__).resolve().parents[1] / "devtools" / "live_run.py"
PROBES = ("pat@livewest.test", "Live West", "6000.00", "12.50", "Sounds good", "Live Acme")


@pytest.fixture(scope="module")
def live_run():
    spec = importlib.util.spec_from_file_location("bizdev_live_run_under_test", LIVE_RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def entry(**over) -> dict:
    e = {"kind": "event", "seq": 7, "event_id": "nb-evd-" + "1" * 40, "department": "bizdev",
         "event_type": "rate_approved", "actor": "dashboard", "subject_id": "partner:nb-par-" + "2" * 40,
         "payload_sha256": "3" * 64, "summary": "rate_approved partner:nb-par-" + "2" * 40,
         "recorded_at": "2026-10-11T04:09:12.501234567Z", "prev_hash": "4" * 64, "hash": "5" * 64}
    e.update(over)
    return e


def test_a_timestamp_holding_a_probe_is_not_a_leak(live_run):
    # the CI failure: "12.50" inside the nanosecond timestamp
    assert "12.50" in entry()["recorded_at"]
    assert live_run.ledger_leaks([entry()], PROBES) == []


@pytest.mark.parametrize("ts", ["2026-10-11T04:09:12Z", "2026-10-11T04:09:12.5Z", "2026-10-11T04:09:12.501Z",
                                "2026-10-11T04:09:12.501234Z", "2026-10-11T04:09:12.501234567Z"])
def test_every_precision_the_ledger_writes_is_accepted(live_run, ts):
    assert live_run.ledger_leaks([entry(recorded_at=ts)], PROBES) == []


@pytest.mark.parametrize("field", ["summary", "subject_id", "event_id", "actor", "event_type", "department",
                                   "payload_sha256"])
@pytest.mark.parametrize("probe", PROBES)
def test_a_probe_in_any_field_the_department_wrote_is_a_leak(live_run, field, probe):
    leaks = live_run.ledger_leaks([entry(), entry(**{field: f"x {probe} y"})], PROBES)
    assert leaks == [f"{probe!r} is on the ledger"]


@pytest.mark.parametrize("ts", ["12.50", "2026-10-11T04:09:12.50 rate 12.50Z", "2026-10-11T04:09:12.501234567+00:00",
                                "2026-10-11 04:09:12Z", "2026-10-11T04:09:12.5012345678Z", "2026-10-11T04:09:12.Z",
                                "2026-10-11T04:09:١٢Z", "", None, 1760155752])
def test_a_recorded_at_not_of_its_exact_shape_is_refused(live_run, ts):
    leaks = live_run.ledger_leaks([entry(recorded_at=ts)], PROBES)
    assert len(leaks) == 1 and "recorded_at" in leaks[0] and "RFC 3339" in leaks[0], leaks


@pytest.mark.parametrize("over, field", [({"hash": "Live West"}, "hash"), ({"prev_hash": "5" * 63}, "prev_hash"),
                                         ({"hash": "A" * 64}, "hash"), ({"seq": "7"}, "seq"), ({"seq": True}, "seq"),
                                         ({"seq": -1}, "seq"), ({"kind": "12.50"}, "kind")])
def test_the_other_ledger_written_fields_are_held_to_their_shape(live_run, over, field):
    leaks = live_run.ledger_leaks([entry(**over)], PROBES)
    assert len(leaks) == 1 and f": {field} " in leaks[0], leaks


def test_a_finding_entry_and_an_empty_ledger_pass(live_run):
    assert live_run.ledger_leaks([entry(kind="finding")], PROBES) == []
    assert live_run.ledger_leaks([], PROBES) == []
