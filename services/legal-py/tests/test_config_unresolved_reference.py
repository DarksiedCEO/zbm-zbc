"""AEGIS night review M-2: a Railway variable reference left unresolved reaches the service as its literal text.
``${{LEGAL_ONBOARDING_CALLER_TOKEN}}`` is >= 32 printable characters and is public in the repo (.railway/railway.ts),
so it passed the token length rule and would have been accepted as the onboarding caller token. Every token setting
legal-py loads now refuses a value holding ``${{`` or ``{{`` at config load, naming the setting, never the value."""

from __future__ import annotations

import json

import pytest

import config as config_mod
from helpers import CALLERS, base_env

LITERAL_REF = "${{LEGAL_ONBOARDING_CALLER_TOKEN}}"
BARE_REF = "{{LEGAL_ONBOARDING_CALLER_TOKEN_PADDING}}"
COMP = {"LEGAL_COMPLIANCE_URL": "http://compliance.railway.internal:1", "LEGAL_COMPLIANCE_TOKEN": "c" * 40,
        "LEGAL_COMPLIANCE_CALLER_TOKEN": "d" * 40}


def _refused(env: dict, setting: str, secret: str) -> str:
    with pytest.raises(RuntimeError) as exc:
        config_mod.load(env)
    msg = str(exc.value)
    assert setting in msg and "unresolved Railway reference" in msg and "refusing to start" in msg
    assert secret not in msg
    return msg


def test_the_literal_onboarding_reference_passes_the_length_rule_so_length_alone_accepted_it():
    assert len(LITERAL_REF) >= config_mod.TOKEN_MIN and all(0x21 <= ord(c) <= 0x7e for c in LITERAL_REF)


@pytest.mark.parametrize("ref", [LITERAL_REF, BARE_REF])
def test_caller_token_holding_an_unresolved_reference_is_refused_at_config_load(ref):
    callers = dict(CALLERS, onboarding=ref)
    msg = _refused(base_env(LEGAL_CALLER_TOKENS=json.dumps(callers)), "LEGAL_CALLER_TOKENS", ref)
    assert "onboarding" in msg


@pytest.mark.parametrize("setting", ["LEGAL_SERVICE_TOKEN", "LEGAL_ANDRE_APPROVAL_TOKEN"])
def test_accepted_single_token_holding_an_unresolved_reference_is_refused(setting):
    ref = "${{" + setting + "_PADDED_TO_LENGTH}}"
    assert len(ref) >= config_mod.TOKEN_MIN
    _refused(base_env(**{setting: ref}), setting, ref)


@pytest.mark.parametrize("setting", ["LEDGER_SERVICE_TOKEN", "LEGAL_COMPLIANCE_TOKEN", "LEGAL_COMPLIANCE_CALLER_TOKEN"])
def test_outbound_peer_token_holding_an_unresolved_reference_is_refused(setting):
    ref = "${{" + setting + "}}"
    env = dict(COMP) if setting.startswith("LEGAL_COMPLIANCE") else \
        {"LEDGER_SERVICE_URL": "http://ledger.railway.internal:8080"}
    env[setting] = ref
    _refused(base_env(**env), setting, ref)


def test_a_normal_token_set_still_loads_including_the_onboarding_caller_and_peers():
    s = config_mod.load(base_env(LEDGER_SERVICE_URL="http://ledger.railway.internal:8080",
                                 LEDGER_SERVICE_TOKEN="l" * 64, **COMP))
    assert s.caller_tokens["onboarding"] == CALLERS["onboarding"]
    assert s.compliance_token == "c" * 40 and s.ledger_token == "l" * 64


def test_a_short_caller_token_still_gets_the_length_message_unchanged():
    callers = dict(CALLERS, onboarding="short")
    with pytest.raises(RuntimeError, match=r"^LEGAL_CALLER_TOKENS: the onboarding token must be >= 32 printable ASCII"):
        config_mod.load(base_env(LEGAL_CALLER_TOKENS=json.dumps(callers)))


# --- the optional compliance_38 caller (added to the deployed map on this branch; AEGIS r12 follow-up) -------------------

@pytest.mark.parametrize("value", ["", "${{compliance.COMPLIANCE_LEGAL_CALLER_TOKEN}}"])
def test_an_unset_compliance_38_entry_is_skipped_and_its_route_answers_503(value, caplog):
    from helpers import Harness
    callers = dict(CALLERS, compliance_38=value)
    settings = config_mod.load(base_env(LEGAL_CALLER_TOKENS=json.dumps(callers)))
    assert "compliance_38" not in settings.caller_tokens and settings.callers_not_configured == ("compliance_38",)
    x = Harness(env={"LEGAL_CALLER_TOKENS": json.dumps(callers)})
    r = x.post("/legal/v1/register/CQ-01/invalidate", {"request_id": "r-1", "source_ref": "src-1",
                                                        "detected_change_sha256": "a" * 64}, caller=value or "nobody")
    assert r.status_code == 503 and "compliance_38" in r.text, r.text
    assert x.client.get("/health").status_code == 200                 # Legal is up; every other caller still works
    assert x.get("/legal/v1/documents/client_msa", caller="finance_31").status_code in (200, 404)


@pytest.mark.parametrize("caller", ["finance_31", "onboarding", "hub", "clipper_network"])
def test_every_pre_existing_caller_stays_strict(caller):
    for value in ("", "${{x.Y_PADDED_TO_THE_MINIMUM_LENGTH}}"):
        callers = dict(CALLERS, **{caller: value})
        with pytest.raises(RuntimeError):
            config_mod.load(base_env(LEGAL_CALLER_TOKENS=json.dumps(callers)))


def test_a_set_compliance_38_token_is_checked_like_any_other():
    callers = dict(CALLERS, compliance_38="short")
    with pytest.raises(RuntimeError, match="compliance_38 token must be"):
        config_mod.load(base_env(LEGAL_CALLER_TOKENS=json.dumps(callers)))
