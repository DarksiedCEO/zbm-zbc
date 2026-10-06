"""Start-up refusals (ADR 0015 decision 3): every problem refuses start, a missing optional piece is visibly off."""

from __future__ import annotations

import json

import pytest

import config as config_mod
from helpers import CALLERS, SERVICE_TOKEN, base_env, write_key


def load(**over):
    return config_mod.load(base_env(**over))


def test_defaults_load():
    s = load()
    assert s.port == 8480 and s.bind_addr == "127.0.0.1" and f"{s.auto_approve_max:f}" == "5000.00"
    assert s.daily_send_cap == 50 and s.outreach_domain == "creators-outreach.test"
    assert "***" in repr(s.pii_key)


@pytest.mark.parametrize("token", [None, "", "short", "x" * 31, "a b" * 20, "é" * 40])
def test_service_token_required(token):
    with pytest.raises(RuntimeError, match="INF_SERVICE_TOKEN"):
        load(INF_SERVICE_TOKEN=token) if token is not None else config_mod.load(
            {k: v for k, v in base_env().items() if k != "INF_SERVICE_TOKEN"})


@pytest.mark.parametrize("name", sorted(config_mod.NOT_BUILT))
def test_every_not_built_switch_refuses_start(name):
    with pytest.raises(RuntimeError, match=name):
        load(**{name: "stripe"})
    load(**{name: "none"})                       # an explicit "none" is the unset value


def test_production_needs_a_data_dir_and_a_key(tmp_path):
    with pytest.raises(RuntimeError, match="INF_DATA_DIR is required"):
        load(INF_NON_PRODUCTION=None)
    with pytest.raises(RuntimeError, match="INF_PII_HASH_KEY_FILE is required"):
        load(INF_DATA_DIR=str(tmp_path / "d"))   # non-production with a durable log still needs a real key
    assert not (tmp_path / "d").exists()         # a refused start never creates the directory


def test_key_file_rules(tmp_path):
    weak = tmp_path / "weak.key"
    weak.write_text("00" * 32)
    weak.chmod(0o600)
    with pytest.raises(RuntimeError, match="generated key"):
        load(INF_PII_HASH_KEY_FILE=str(weak))
    loose = tmp_path / "loose.key"
    write_key(loose)
    loose.chmod(0o644)
    with pytest.raises(RuntimeError, match="group or others"):
        load(INF_PII_HASH_KEY_FILE=str(loose))
    good = write_key(tmp_path / "good.key")
    assert len(load(INF_PII_HASH_KEY_FILE=good).pii_key.reveal()) == 32


@pytest.mark.parametrize("value,ok", [("5000.00", True), ("4999.99", True), ("0.01", True), ("5000.01", False),
                                      ("10000.00", False), ("5000", False), ("5e3", False), ("-1.00", False),
                                      ("5000.0", False)])
def test_auto_approve_max_never_above_5000(value, ok):
    if ok:
        assert f"{load(INF_AUTO_APPROVE_MAX=value).auto_approve_max:f}" == value
    else:
        with pytest.raises(RuntimeError, match="INF_AUTO_APPROVE_MAX"):
            load(INF_AUTO_APPROVE_MAX=value)


def test_outreach_domain_must_be_separate_from_both_brands():
    with pytest.raises(RuntimeError, match="separate domain"):
        load(INF_OUTREACH_DOMAIN="go.zbestmedia.test")
    with pytest.raises(RuntimeError, match="separate domain"):
        load(INF_OUTREACH_DOMAIN="zbestclips.test")
    with pytest.raises(RuntimeError, match="both set"):
        load(INF_ZBC_DOMAIN=None)
    with pytest.raises(RuntimeError, match="different domains"):
        load(INF_ZBC_DOMAIN="www.zbestmedia.test")
    with pytest.raises(RuntimeError, match="separate domain"):
        load(INF_PRIMARY_DOMAINS="creators-outreach.test")
    s = load(INF_OUTREACH_DOMAIN=None, INF_POSTAL_ADDRESS=None)
    assert s.outreach_domain is None


def test_postal_address_required_with_outreach():
    with pytest.raises(RuntimeError, match="postal address"):
        load(INF_POSTAL_ADDRESS=None)
    with pytest.raises(RuntimeError, match="INF_POSTAL_ADDRESS"):
        load(INF_POSTAL_ADDRESS="No number St")


@pytest.mark.parametrize("local", ["noreply", "no-reply", "donotreply", "Creators", "a b"])
def test_from_mailbox_must_accept_replies(local):
    with pytest.raises(RuntimeError, match="INF_OUTREACH_FROM_LOCAL"):
        load(INF_OUTREACH_FROM_LOCAL=local)


def test_caller_tokens():
    with pytest.raises(RuntimeError, match="unknown caller"):
        load(INF_CALLER_TOKENS=json.dumps({"sales_agent": "x" * 40}))
    with pytest.raises(RuntimeError, match="distinct"):
        load(INF_CALLER_TOKENS=json.dumps({"hub": "x" * 40, "dashboard": "x" * 40}))
    with pytest.raises(RuntimeError, match="distinct"):
        load(INF_CALLER_TOKENS=json.dumps({"hub": SERVICE_TOKEN}))
    with pytest.raises(RuntimeError, match="printable"):
        load(INF_CALLER_TOKENS=json.dumps({"hub": "short"}))
    assert set(load().caller_tokens) == set(CALLERS)


@pytest.mark.parametrize("name,value", [("INF_DAILY_SEND_CAP", "0"), ("INF_DAILY_SEND_CAP", "201"),
                                        ("INF_QUEUE_MAX_PER_CALLER", "0"), ("INF_PORT", "80"),
                                        ("INF_NON_PRODUCTION", "yes"), ("INF_ANDRE_APPROVAL_TOKEN", "short")])
def test_bounds(name, value):
    with pytest.raises(RuntimeError, match=name):
        load(**{name: value})


def test_andre_token_equal_to_a_caller_token_counts_as_not_configured(tmp_path):
    from helpers import Harness
    h = Harness(tmp_path, INF_ANDRE_APPROVAL_TOKEN=CALLERS["dashboard"])
    st = h.ok(h.get("/status"))
    assert st["andre_approvals_configured"] is False
