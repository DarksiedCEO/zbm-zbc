"""Start-up refusals (ADR 0013 decision 3): every misconfiguration refuses to start."""

from __future__ import annotations

import pytest

import config
from helpers import ANDRE, CALLERS, SERVICE_TOKEN, base_env, secret_file


def load(**over):
    return config.load(base_env(**over))


@pytest.mark.parametrize("outreach,primary", [("zbestmedia.test", None), ("mail.zbestmedia.test", None),
                                              ("ZBESTCLIPS.test", None),
                                              ("zbm-outreach.test", "zbestmedia.test,news.zbm-outreach.test")])
def test_outreach_domain_equal_to_under_or_over_a_primary_refuses_start(outreach, primary):
    over = {"SALES_PRIMARY_DOMAINS": primary} if primary else {}
    with pytest.raises(RuntimeError, match="separate domain"):
        load(SALES_OUTREACH_DOMAIN=outreach, **over)


def test_outreach_domain_without_primary_domains_refuses_start():
    with pytest.raises(RuntimeError, match="SALES_PRIMARY_DOMAINS is not"):
        load(SALES_PRIMARY_DOMAINS=None)


def test_outreach_domain_without_postal_address_refuses_start():
    with pytest.raises(RuntimeError, match="postal address"):
        load(SALES_POSTAL_ADDRESS=None)


@pytest.mark.parametrize("addr", ["short", "No number street, Los Angeles CA", "x" * 201])
def test_postal_address_must_look_like_one(addr):
    with pytest.raises(RuntimeError, match="SALES_POSTAL_ADDRESS"):
        load(SALES_POSTAL_ADDRESS=addr)


def test_no_outreach_domain_is_allowed_and_email_is_then_off():
    s = load(SALES_OUTREACH_DOMAIN=None, SALES_POSTAL_ADDRESS=None)
    assert s.outreach_domain is None


@pytest.mark.parametrize("name", sorted(config.NOT_BUILT))
def test_switches_for_unbuilt_providers_refuse_start(name):
    with pytest.raises(RuntimeError, match="refuses to start rather than pretend"):
        load(**{name: "acme"})


def test_data_dir_required_in_production(tmp_path):
    with pytest.raises(RuntimeError, match="SALES_DATA_DIR is required"):
        load(SALES_NON_PRODUCTION=None)


def test_pii_key_required_in_production(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="SALES_PII_HASH_KEY_FILE is required"):
        load(SALES_NON_PRODUCTION=None, SALES_DATA_DIR=str(d))
    key = secret_file(tmp_path, "pii.key", b"0123456789abcdefghijklmnopqrstuvwxyzABCD")
    s = load(SALES_NON_PRODUCTION=None, SALES_DATA_DIR=str(d), SALES_PII_HASH_KEY_FILE=key)
    assert s.pii_key.reveal().startswith(b"0123") and "0123" not in repr(s.pii_key)


def test_weak_pii_key_refused(tmp_path):
    key = secret_file(tmp_path, "pii.key", b"a" * 40)
    with pytest.raises(RuntimeError, match="generated key"):
        load(SALES_PII_HASH_KEY_FILE=key)


@pytest.mark.parametrize("value", ["10000.01", "25000.00", "10000", "1e4", "-1.00"])
def test_auto_approve_max_bounded_at_ten_thousand(value):
    with pytest.raises(RuntimeError, match="SALES_AUTO_APPROVE_MAX"):
        load(SALES_AUTO_APPROVE_MAX=value)


def test_auto_approve_max_may_be_lower():
    assert str(load(SALES_AUTO_APPROVE_MAX="2500.00").auto_approve_max) == "2500.00"


@pytest.mark.parametrize("sched", ["100,200", "20,10", "20,50", "20,40,80,160,320,640", "0,10", "x"])
def test_warmup_schedule_must_start_low_and_ramp_gently(sched):
    with pytest.raises(RuntimeError, match="SALES_WARMUP_SCHEDULE"):
        load(SALES_WARMUP_SCHEDULE=sched)


def test_daily_cap_bounded():
    with pytest.raises(RuntimeError, match="SALES_DAILY_SEND_CAP"):
        load(SALES_DAILY_SEND_CAP="501")


@pytest.mark.parametrize("local", ["noreply", "no-reply", "donotreply", "Hello", ""])
def test_from_mailbox_must_accept_replies(local):
    if local == "":
        assert load(SALES_OUTREACH_FROM_LOCAL=local).from_local == "hello"
        return
    with pytest.raises(RuntimeError, match="SALES_OUTREACH_FROM_LOCAL"):
        load(SALES_OUTREACH_FROM_LOCAL=local)


def test_service_token_required():
    with pytest.raises(RuntimeError, match="SALES_SERVICE_TOKEN"):
        load(SALES_SERVICE_TOKEN="short")


def test_caller_tokens_distinct_and_known():
    import json
    with pytest.raises(RuntimeError, match="unknown caller"):
        load(SALES_CALLER_TOKENS=json.dumps({"stranger": "x" * 40}))
    with pytest.raises(RuntimeError, match="distinct"):
        load(SALES_CALLER_TOKENS=json.dumps({"hub": SERVICE_TOKEN}))


def test_andre_token_equal_to_a_caller_token_counts_as_not_configured(tmp_path):
    from helpers import Harness, rid
    h = Harness(tmp_path, SALES_ANDRE_APPROVAL_TOKEN=CALLERS["dashboard"])
    r = h.andre("/sales/v1/pricebook/zbm/lines/zbm.creative_production/approve",
                {"request_id": rid(), "version": 1, "price": "100.00"}, token=CALLERS["dashboard"])
    assert r.status_code == 403 and r.json()["detail"] == "ANDRE_TOKEN_NOT_CONFIGURED"
    assert ANDRE != CALLERS["dashboard"]
