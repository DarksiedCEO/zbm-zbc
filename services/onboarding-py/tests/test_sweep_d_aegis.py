"""AEGIS re-review of bug sweep D (37a2830), onboarding-py: O-1 (1099 key split by invisible / variant characters),
O-2 (a payment whose line was owed at a restart was not counted: totals short of the ledger), O-3 (one payment id
under two creator ids counted twice). Ported from the reviewer's probes (aegis-ocfd/probe); each FAILS on 37a2830."""

from __future__ import annotations

import pytest

from conftest import client_for, make_service
from ledger import FakeLedgerClient, LedgerWriteError
from service import person_key
from store import DataDirLock, RecordLog
from test_sweep_d import _app

DOB = __import__("datetime").date(2000, 1, 1)


@pytest.mark.parametrize("variant", [
    "Pat​ Young", "Pat Y­oung", "Pat⁠Young".replace("⁠", "⁠ "), "Pаt Young",
    "PAT YOUNG", " Pat  Young ", "Pat Young", "Ｐat Young",
])
def test_o1_every_spelling_of_one_name_is_one_person(variant):
    assert person_key(variant, DOB) == person_key("Pat Young", DOB)


@pytest.mark.parametrize("a,b", [("O’Brien", "O'Brien"), ("OʼBrien", "O'Brien"), ("Mary–Jane", "Mary-Jane"),
                                 ("Mary‑Jane", "Mary-Jane"), ("Mary−Jane", "Mary-Jane")])
def test_o1_apostrophe_and_dash_variants_fold(a, b):
    assert person_key(a, DOB) == person_key(b, DOB)


def test_o1_format_characters_are_refused_at_the_schema_and_totals_aggregate():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    assert c.post("/zbc/creators/applications", json=_app()).status_code == 201
    for bad in ("Pat​ Young", "Pat Y­oung", "Pat ‮Young"):
        assert c.post("/zbc/creators/applications", json=_app(creator_id="clip_x", legal_name=bad)).status_code == 422
    assert c.post("/zbc/creators/applications", json=_app(creator_id="clip_2", legal_name="Pаt YOUNG")).status_code == 201
    c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "1500.00"})
    r = c.post("/zbc/creators/clip_2/payments", json={"request_id": "p2", "amount_usd": "600.00"}).json()
    assert r["paid_to_date_usd"] == "2100.00" and r["form_1099_required"] is True, r


class NthAnchorFails(FakeLedgerClient):
    """Refuses the n-th ``log_anchor`` write from now (1 = the next one)."""

    def __init__(self):
        super().__init__()
        self.countdown = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
        if event_type == "log_anchor" and self.countdown:
            self.countdown -= 1
            if self.countdown == 0:
                raise LedgerWriteError("simulated outage for one anchor")
        return super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)


def test_o2_a_payment_whose_line_was_owed_at_a_restart_is_counted_from_the_ledger(tmp_path):
    d = str(tmp_path / "d")
    led = NthAnchorFails()
    lock = DataDirLock(d)
    svc = make_service(all_fakes=True, ledger=led, log=RecordLog(d), dir_lock=lock)
    c = client_for(svc)
    assert c.post("/zbc/creators/applications", json=_app()).status_code == 201
    assert c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "550.00"}).status_code == 200
    led.countdown = 2                                  # the intent's anchor passes; the operation line's anchor fails
    r = c.post("/zbc/creators/clip_1/payments", json={"request_id": "p2", "amount_usd": "550.00"})
    assert r.status_code == 503 and r.json()["evidence"] == "pending" and "do not repeat" in r.json()["retry"]
    svc.close()                                        # restart before the owed line is written
    svc2 = make_service(all_fakes=True, ledger=led, log=RecordLog(d), dir_lock=lock)
    c2 = client_for(svc2)
    assert c2.post("/zbc/creators/applications", json=_app()).status_code == 201
    r = c2.post("/zbc/creators/clip_1/payments", json={"request_id": "p3", "amount_usd": "1.00"}).json()
    tracked = [e for e in led.events if e["event_type"] == "creator_payment_tracked"]
    assert len(tracked) == 3 and r["paid_to_date_usd"] == "1101.00", r
    # the retried payment id is answered, never counted again
    again = c2.post("/zbc/creators/clip_1/payments", json={"request_id": "p2", "amount_usd": "550.00"})
    assert again.status_code == 200 and len([e for e in led.events if e["event_type"] == "creator_payment_tracked"]) == 3
    svc2.close()
    lock.release()


def test_o2_an_intent_whose_record_never_reached_the_ledger_is_not_counted(tmp_path):
    d = str(tmp_path / "d")

    class RecordFails(FakeLedgerClient):
        arm = False

        def record_event(self, eid, dep, et, *a, **k):
            if self.arm and et == "creator_payment_tracked":
                self.arm = False
                raise LedgerWriteError("refused")
            return super().record_event(eid, dep, et, *a, **k)
    led = RecordFails()
    lock = DataDirLock(d)
    svc = make_service(all_fakes=True, ledger=led, log=RecordLog(d), dir_lock=lock)
    c = client_for(svc)
    c.post("/zbc/creators/applications", json=_app())
    led.arm = True
    assert c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "550.00"}).status_code == 503
    svc.close()
    svc2 = make_service(all_fakes=True, ledger=led, log=RecordLog(d), dir_lock=lock)
    c2 = client_for(svc2)
    c2.post("/zbc/creators/applications", json=_app())
    assert c2.post("/zbc/creators/clip_1/payments", json={"request_id": "p9", "amount_usd": "1.00"}).json()[
        "paid_to_date_usd"] == "1.00"
    svc2.close()
    lock.release()


def test_o2_unresolved_intent_with_an_unreadable_ledger_refuses_start(tmp_path):
    d = str(tmp_path / "d")
    led = NthAnchorFails()
    lock = DataDirLock(d)
    svc = make_service(all_fakes=True, ledger=led, log=RecordLog(d), dir_lock=lock)
    c = client_for(svc)
    c.post("/zbc/creators/applications", json=_app())
    led.countdown = 2
    assert c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "5.00"}).status_code == 503
    svc.close()
    led.fail = True
    with pytest.raises(RuntimeError, match="cannot be read"):
        make_service(all_fakes=True, ledger=led, log=RecordLog(d), dir_lock=lock)
    lock.release()


def test_o3_one_payment_id_under_two_creator_ids_counts_once():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    c.post("/zbc/creators/applications", json=_app())
    c.post("/zbc/creators/applications", json=_app(creator_id="clip_2"))
    assert c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "500.00"}).status_code == 200
    assert c.post("/zbc/creators/clip_2/payments", json={"request_id": "p1", "amount_usd": "500.00"}).status_code == 409
    r = c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "500.00"})
    assert r.status_code == 200 and r.json()["paid_to_date_usd"] == "500.00"


def test_p4_unknown_outcome_payment_then_retry_records_once():
    class Unknown(FakeLedgerClient):
        arm = False

        def record_event(self, eid, dep, et, actor, sid, payload, summary):
            out = super().record_event(eid, dep, et, actor, sid, payload, summary)
            if self.arm and et == "creator_payment_tracked":
                self.arm = False
                raise LedgerWriteError("timeout after write", "unknown")
            return out
    led = Unknown()
    svc = make_service(all_fakes=True, ledger=led)
    c = client_for(svc)
    c.post("/zbc/creators/applications", json=_app())
    led.arm = True
    assert c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "500.00"}).json()["proceeded"] == "unknown"
    r = c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "500.00"})
    assert r.status_code == 200 and r.json()["paid_to_date_usd"] == "500.00"
    assert len([e for e in led.events if e["event_type"] == "creator_payment_tracked"]) == 1
