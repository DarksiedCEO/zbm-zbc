"""AEGIS night review M-2: a Railway variable reference left unresolved reaches the service as its literal text.
``${{FIN_ONBOARDING_CALLER_TOKEN}}`` is exactly 32 printable characters and is public in the repo (.railway/railway.ts),
so it passed the token length rule and would have been accepted as the onboarding caller token. Every token setting
finance-py loads now refuses a value holding ``${{`` or ``{{`` at config load, naming the setting, never the value."""

from __future__ import annotations

import json

import pytest

import config as config_mod
from helpers import CALLERS, base_env

LITERAL_REF = "${{FIN_ONBOARDING_CALLER_TOKEN}}"
BARE_REF = "{{FIN_ONBOARDING_CALLER_TOKEN_PADDING}}"         # the same marker without the dollar sign
PEER = {"FIN_LEGAL_URL": "http://legal.railway.internal:8420", "FIN_LEGAL_TOKEN": "p" * 40,
        "FIN_LEGAL_CALLER_TOKEN": "q" * 40}


def _refused(env: dict, setting: str, secret: str) -> str:
    with pytest.raises(RuntimeError) as exc:
        config_mod.load(env)
    msg = str(exc.value)
    assert setting in msg and "unresolved Railway reference" in msg and "refusing to start" in msg
    assert secret not in msg                                    # the value is never echoed
    return msg


def test_the_literal_onboarding_reference_is_32_printable_chars_so_length_alone_accepted_it():
    assert len(LITERAL_REF) == config_mod.TOKEN_MIN and all(0x21 <= ord(c) <= 0x7e for c in LITERAL_REF)


@pytest.mark.parametrize("ref", [LITERAL_REF, BARE_REF])
def test_caller_token_holding_an_unresolved_reference_is_refused_at_config_load(ref):
    callers = dict(CALLERS, onboarding=ref)
    msg = _refused(base_env(FIN_CALLER_TOKENS=json.dumps(callers)), "FIN_CALLER_TOKENS", ref)
    assert "onboarding" in msg                                  # the caller NAME, so the operator knows which one


@pytest.mark.parametrize("setting", ["FIN_SERVICE_TOKEN", "FIN_ANDRE_APPROVAL_TOKEN", "FIN_SECOND_APPROVER_TOKEN"])
def test_accepted_single_token_holding_an_unresolved_reference_is_refused(setting):
    ref = "${{" + setting + "_PADDED_TO_LENGTH}}"
    assert len(ref) >= config_mod.TOKEN_MIN
    _refused(base_env(**{setting: ref}), setting, ref)


@pytest.mark.parametrize("setting", ["LEDGER_SERVICE_TOKEN", "FIN_LEGAL_TOKEN", "FIN_LEGAL_CALLER_TOKEN",
                                     "FIN_VI_TOKEN", "FIN_COMPLIANCE_CALLER_TOKEN"])
def test_outbound_peer_token_holding_an_unresolved_reference_is_refused(setting):
    ref = "${{" + setting + "}}"
    env = dict(PEER)
    if setting.startswith("FIN_VI"):
        env = {"FIN_VI_URL": "http://vi.railway.internal:1", "FIN_VI_TOKEN": "v" * 40, "FIN_VI_CALLER_TOKEN": "w" * 40}
    if setting.startswith("FIN_COMPLIANCE"):
        env = {"FIN_COMPLIANCE_URL": "http://c.railway.internal:1", "FIN_COMPLIANCE_TOKEN": "c" * 40,
               "FIN_COMPLIANCE_CALLER_TOKEN": "d" * 40}
    if setting == "LEDGER_SERVICE_TOKEN":
        env = {"LEDGER_SERVICE_URL": "http://ledger.railway.internal:8080"}
    env[setting] = ref
    _refused(base_env(**env), setting, ref)


def test_a_normal_token_set_still_loads_including_the_onboarding_caller_and_peers():
    s = config_mod.load(base_env(LEDGER_SERVICE_URL="http://ledger.railway.internal:8080",
                                 LEDGER_SERVICE_TOKEN="l" * 64, **PEER))
    assert s.caller_tokens["onboarding"] == CALLERS["onboarding"]
    assert s.legal_token == "p" * 40 and s.ledger_token == "l" * 64


def test_a_short_caller_token_still_gets_the_length_message_unchanged():
    callers = dict(CALLERS, onboarding="short")
    with pytest.raises(RuntimeError, match=r"^FIN_CALLER_TOKENS: the onboarding token must be >= 32 printable ASCII"):
        config_mod.load(base_env(FIN_CALLER_TOKENS=json.dumps(callers)))
