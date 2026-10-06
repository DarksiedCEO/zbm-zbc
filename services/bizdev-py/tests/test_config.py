"""Start-up refuses every problem (fail closed); NOT_BUILT provider settings refuse start."""

import json
import os

import pytest

import config as config_mod
from helpers import CALLERS, SERVICE_TOKEN, base_env, write_key


def _load(**over):
    return config_mod.load(base_env(**over))


@pytest.mark.parametrize("name", sorted(config_mod.NOT_BUILT))
def test_not_built_settings_refuse_start(name):
    with pytest.raises(RuntimeError, match=name):
        _load(**{name: "stripe"})
    _load(**{name: "none"})


def test_service_token_required():
    for bad in (None, "", "short", "x" * 513, "spaces in token " * 3):
        with pytest.raises(RuntimeError):
            _load(NBD_SERVICE_TOKEN=bad)


def test_caller_tokens_validated():
    with pytest.raises(RuntimeError, match="unknown caller"):
        _load(NBD_CALLER_TOKENS=json.dumps({"stripe": "a" * 40}))
    with pytest.raises(RuntimeError, match="distinct"):
        _load(NBD_CALLER_TOKENS=json.dumps({"dashboard": SERVICE_TOKEN}))
    with pytest.raises(RuntimeError, match="distinct"):
        _load(NBD_CALLER_TOKENS=json.dumps({"dashboard": "a" * 40, "hub": "a" * 40}))
    with pytest.raises(RuntimeError):
        _load(NBD_CALLER_TOKENS="[1]")


def test_production_needs_data_dir_and_key(tmp_path):
    with pytest.raises(RuntimeError, match="NBD_DATA_DIR is required"):
        _load(NBD_NON_PRODUCTION=None)
    d = tmp_path / "d"
    with pytest.raises(RuntimeError, match="NBD_PII_HASH_KEY_FILE is required"):
        _load(NBD_NON_PRODUCTION=None, NBD_DATA_DIR=str(d))
    s = _load(NBD_NON_PRODUCTION=None, NBD_DATA_DIR=str(d), NBD_PII_HASH_KEY_FILE=write_key(tmp_path / "k"))
    assert s.non_production is False and s.data_dir_lock is not None


def test_key_file_rules(tmp_path):
    p = tmp_path / "weak.key"
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"00" * 32)
    os.close(fd)
    with pytest.raises(RuntimeError, match="generated key"):
        _load(NBD_PII_HASH_KEY_FILE=str(p))
    loose = tmp_path / "loose.key"
    write_key(loose)
    os.chmod(loose, 0o644)
    with pytest.raises(RuntimeError, match="group or others"):
        _load(NBD_PII_HASH_KEY_FILE=str(loose))


def test_data_dir_permissions(tmp_path):
    d = tmp_path / "open"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(RuntimeError, match="chmod 700"):
        _load(NBD_DATA_DIR=str(d), NBD_PII_HASH_KEY_FILE=write_key(tmp_path / "k"))


def test_outreach_domain_rules():
    with pytest.raises(RuntimeError, match="separate domain"):
        _load(NBD_OUTREACH_DOMAIN="go.zbestmedia.test")
    with pytest.raises(RuntimeError, match="NBD_ZBM_DOMAIN"):
        _load(NBD_ZBM_DOMAIN=None)
    with pytest.raises(RuntimeError, match="POSTAL"):
        _load(NBD_POSTAL_ADDRESS=None)
    with pytest.raises(RuntimeError, match="noreply"):
        _load(NBD_OUTREACH_FROM_LOCAL="noreply")
    s = _load(NBD_OUTREACH_DOMAIN=None, NBD_POSTAL_ADDRESS=None)
    assert s.outreach_domain is None


def test_numeric_settings_bounded():
    for name, bad in (("NBD_DAILY_SEND_CAP", "0"), ("NBD_DAILY_SEND_CAP", "201"), ("NBD_QUEUE_MAX_PER_CALLER", "x"),
                      ("NBD_AGGREGATION_WINDOW_DAYS", "30"), ("NBD_PORT", "80"),
                      ("NBD_DEAL_APPROVAL_THRESHOLD", "10000.01"), ("NBD_DEAL_APPROVAL_THRESHOLD", "10000"),
                      ("NBD_NON_PRODUCTION", "yes")):
        with pytest.raises(RuntimeError):
            _load(**{name: bad})


def test_andre_token_equal_to_a_caller_token_is_not_configured(tmp_path):
    from helpers import Harness, rid
    h = Harness(tmp_path, NBD_ANDRE_APPROVAL_TOKEN=CALLERS["dashboard"])
    assert h.ok(h.get("/status"))["andre_approvals_configured"] is False
    p = h.partner()
    h.rate(p["partner_id"], approve=False)
    r = h.client.post(f"/nbd/v1/partners/{p['partner_id']}/rate/approve",
                      json={"request_id": rid(), "version": 1, "binding_sha256": "0" * 64},
                      headers={**h.headers("dashboard"), "X-Andre-Approval-Token": CALLERS["dashboard"]})
    assert r.status_code == 403 and r.json()["detail"] == "ANDRE_NOT_CONFIGURED"


def test_secret_repr_never_shows_value():
    s = config_mod.Secret(b"top")
    assert "top" not in repr(s) and "top" not in str(s)
