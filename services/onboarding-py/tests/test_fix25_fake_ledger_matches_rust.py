"""
Fix wave 25 (scout A X8): onboarding-py has its own fake ledger
(tools/fake_ledger_server.py; test_fix_wave5's live `Stack` runs against it) and
says it "validates exactly like" ledger-rust — but no test checked that, unlike
creative-py's devtools fake (creative tests/test_fix_wave_1.py
test_f16_devtools_fake_server_matches_ledger_rust). Here one set of accept and
reject cases is put to all three validators onboarding-py relies on and they
must agree: the REAL ledger-rust binary (POST /ledger/events, 201 vs 400), the
fake's `validate`, and the client's own mirror `ledger.ledger_rust_accepts`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from conftest import start_live
from ledger import ledger_rust_accepts
from test_fix_wave5 import _stop, _wait_health

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fake-vs-rust-test-token-padded-to-32b"  # ledger-rust needs >= 32 bytes (sweep F-12)
GOOD = dict(event_id="onb-x", department="onboarding", event_type="client_started", actor="zbm_onboarding",
            subject_id="client_0001", payload_sha256="0123456789abcdef" * 4, summary="ok")

RUST_REJECTS = [
    {"summary": "a\u0085b"},                 # C1 NEL (Rust char::is_control is Unicode Cc)
    {"summary": "a\u009fb"},                 # C1 APC
    {"summary": "a\x7fb"},                   # DEL
    {"summary": "tab\there"},                # C0
    {"summary": "\ud800"},                   # lone surrogate: not a Unicode scalar value (serde refuses it)
    {"summary": "é" * 281},                  # 281 scalar values
    {"summary": ""},
    {"subject_id": "client_0001\n"},         # trailing newline (Python's `$` would allow it)
    {"event_id": "onb-x\n"},
    {"actor": "zbm_onboarding\n"},
    {"subject_id": "c" * 129},
    {"event_id": "e" * 129},
    {"subject_id": "clïent"},                # ids are ASCII only
    {"department": "Onboarding"},            # slugs are lowercase
    {"event_type": "client-started"},        # '-' is not a slug byte
    {"actor": "a" * 65},
    {"payload_sha256": "0123456789ABCDEF" * 4},   # lowercase hex only
    {"payload_sha256": "0" * 63},
    {"payload_sha256": "g" * 64},
    {"summary": 5},                          # not a string
    {"extra": "field"},                      # unknown fields are refused (deny_unknown_fields)
    {"summary": None, "_drop": "summary"},   # a missing field
]
RUST_ACCEPTS = [
    {},
    {"summary": "é" * 280},                  # 280 scalar values, 560 bytes
    {"summary": "line sep"},            # U+2028 is Zl, not Cc
    {"summary": "pipes | and emoji \U0001F600"},
    {"summary": " "},
    {"subject_id": "c" * 128},
    {"event_id": "e" * 128},
    {"actor": "a" * 64},
    {"event_id": "A.b_c:d-9"},
]


def _body(over: dict) -> dict:
    over = dict(over)
    drop = over.pop("_drop", None)
    b = {**GOOD, **over}
    if drop:
        del b[drop]
    return b


def _fake_validate():
    # (its TOKEN is read at import with a default; it refuses to SERVE without one, not to load)
    spec = importlib.util.spec_from_file_location("onb_fake_ledger_server", ROOT / "tools" / "fake_ledger_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate


@pytest.fixture(scope="module")
def real_ledger(ledger_bin, tmp_path_factory):
    env = dict(os.environ, LEDGER_SERVICE_TOKEN=TOKEN,
               LEDGER_LOG_PATH=str(tmp_path_factory.mktemp("rust-ledger") / "ledger.jsonl"))
    # fix wave 26b (scout C5-6): the ledger is this module's only once IT holds the port (conftest.start_live)
    proc, port = start_live(lambda p: subprocess.Popen([str(ledger_bin)], env={**env, "LEDGER_PORT": str(p)},
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        _wait_health(port, proc, "ledger-rust")
        yield port
    finally:
        _stop(proc)


def _post(port: int, body: dict, n: int) -> int:
    if "event_id" in body and body["event_id"] == GOOD["event_id"]:
        body = {**body, "event_id": f"onb-x{n}"}         # every accepted case is a NEW event (201, not 200/409)
    # json.dumps escapes non-ASCII (ensure_ascii), so a lone surrogate goes out as the JSON escape "\ud800" — the
    # wire form a client can send — where httpx's json= would refuse to encode it at all (E-A review: it raised
    # UnicodeEncodeError before reaching the ledger)
    r = httpx.post(f"http://127.0.0.1:{port}/ledger/events", content=json.dumps(body).encode(), timeout=10,
                   headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    return r.status_code


def test_x8_the_three_validators_agree_on_every_case_and_ledger_rust_is_the_reference(real_ledger):
    validate = _fake_validate()
    disagreements = []
    for n, (case, want_ok) in enumerate([(c, False) for c in RUST_REJECTS] + [(c, True) for c in RUST_ACCEPTS]):
        body = _body(case)
        rust = _post(real_ledger, body, n)
        seen = {"ledger-rust": rust, "fake": validate(body) is None, "client mirror": ledger_rust_accepts(body)}
        if rust != (201 if want_ok else 400) or seen["fake"] != want_ok or seen["client mirror"] != want_ok:
            disagreements.append((case, want_ok, seen))
    assert disagreements == [], disagreements
