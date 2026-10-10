"""AEGIS review of Wave F (038fa93..0928711), creative-py: the reviewer's signature probes, ported. The memory-growth
probe is replaced by ``test_wave_f_restart.test_f1_memory_deltas_stay_the_same_size_as_the_store_grows`` (bytes)."""
from __future__ import annotations

import pytest

from conftest import TEST_FOUNDER_TOKEN
from flows import C
from samples import CAMPAIGN
from shared.ledger import LedgerNotRecorded
from test_wave_f_restart import Box, _kit_to_draft


@pytest.fixture
def box(make_api, tmp_path):
    b = Box(make_api, tmp_path)
    yield b
    b.close()


def test_probe_sign_record_landed_but_reply_lost(box):
    """The ledger records the signature, the reply is lost (UNKNOWN): after restart, the signature is applied once."""
    _kit_to_draft(box)
    led = box.led
    orig = type(led).record_event

    def landed_then_fail(self, event_id, department, event_type, *a, **k):
        r = orig(self, event_id, department, event_type, *a, **k)
        if event_type == "campaign_kit_signed_by_andre" and not getattr(self, "_done", False):
            self._done = True
            raise LedgerNotRecorded("reply lost")
        return r
    type(led).record_event = landed_then_fail
    try:
        box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN)
    finally:
        type(led).record_event = orig
    box.restart(check=False)
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"
    n = len(led.of_type("campaign_kit_signed_by_andre"))
    box.restart()
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"
    assert len(led.of_type("campaign_kit_signed_by_andre")) == n == 1


def test_probe_resolution_line_fails_at_start_then_resign(box):
    _kit_to_draft(box)
    box.led.fail_type, box.led.countdown = "log_anchor", 2       # decision line owed
    r = box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN)
    assert r.status_code == 503
    box.led.fail_type, box.led.countdown = "log_anchor", 1       # the resolution line's anchor fails at start
    box.restart(check=False)
    box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN)
    box.restart(check=False)
    assert len(box.led.of_type("campaign_kit_signed_by_andre")) == 1
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"
    box.restart()


def test_probe_ledger_down_at_start_with_open_intent(box):
    _kit_to_draft(box)
    box.led.fail_type, box.led.countdown = "log_anchor", 2
    assert box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN).status_code == 503
    box.api.app.state.close()
    box.led.fail_all = True
    try:
        with pytest.raises(RuntimeError, match="cannot be read"):
            box._new()
    finally:
        box.led.fail_all = False
    box.api = box._new()
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"
