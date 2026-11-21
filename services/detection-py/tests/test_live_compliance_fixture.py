"""The local live fixture must exercise the real Compliance HTTP client, including refusals."""
import importlib.util
import secrets
from pathlib import Path

import httpx
import pytest

from store_connector.compliance import HttpCompliance
from store_connector.config import Secret
from store_connector.reasons import StoreError


def fixture_module():
    path = Path(__file__).resolve().parents[1] / "devtools" / "live_compliance.py"
    assert path.exists(), "live harness needs an authenticated loopback Compliance fixture"
    spec = importlib.util.spec_from_file_location("live_compliance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_client_observes_allow_refuse_expired_and_unavailable():
    mod = fixture_module()
    with mod.ComplianceFixture() as fixture:
        client = HttpCompliance(fixture.url, Secret(fixture.service_token), Secret(fixture.caller_token))
        fixture.set_mode("lr-connected", "allow")
        assert client.require_allow("lr-connected")["jurisdiction"] == "US-CA"
        for mode, code in (("refuse", "COMPLIANCE_NOT_ACTIVATED"),
                           ("expired", "COMPLIANCE_NOT_ACTIVATED"),
                           ("unavailable", "COMPLIANCE_UNAVAILABLE")):
            fixture.set_mode("lr-connected", mode)
            with pytest.raises(StoreError) as exc:
                client.require_allow("lr-connected")
            assert exc.value.code == code
        # A refused client cannot get another registered client's allowed answer.
        fixture.set_mode("another-client", "allow")
        with pytest.raises(StoreError):
            client.require_allow("unknown-client")
    assert not fixture.thread.is_alive()


@pytest.mark.parametrize("bad_header", ["authorization", "x-compliance-caller-token"])
def test_fixture_requires_both_runtime_tokens(bad_header):
    mod = fixture_module()
    with mod.ComplianceFixture() as fixture:
        fixture.set_mode("lr-connected", "allow")
        headers = {"authorization": "Bearer " + fixture.service_token,
                   "x-compliance-caller-token": fixture.caller_token}
        headers[bad_header] = secrets.token_hex(32)
        with httpx.Client(trust_env=False) as client:
            response = client.get(fixture.url + "/compliance/v1/clients/lr-connected/activation", headers=headers)
        assert response.status_code == 403
        assert "ALLOW" not in response.text


def test_socket_audit_rejects_unexpected_loopback_and_external_destinations():
    mod = fixture_module()
    first, second, other = secrets.SystemRandom().sample(range(1024, 65536), 3)
    allowed = {first, second}
    good = [{"event": "socket.connect", "address": ["127.0.0.1", first]},
            {"event": "socket.getaddrinfo", "host": "127.0.0.1", "port": second}]
    assert mod.unexpected_socket_events(good, allowed) == []
    bad = [{"event": "socket.connect", "address": ["127.0.0.1", other]},
           {"event": "socket.connect", "address": ["203.0.113.1", first]},
           {"event": "socket.getaddrinfo", "host": "shop.myshopify.com", "port": first},
           {"event": "socket.connect", "address": "/tmp/other.sock"}]
    assert mod.unexpected_socket_events(bad, allowed) == bad
