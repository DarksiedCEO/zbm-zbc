"""Transient dependency retries preserve requests and never relax refusal semantics."""
import json
import secrets

import httpx
import pytest

from clients import HttpCompliance


def reply(method, allow=True):
    if method == "POST":
        return {"answers": [{"who": "person", "class": "operate" if allow else "refuse"}]}
    return {"client_id": "owned-client", "lane": "client", "decision": "ALLOW" if allow else "REFUSE",
            "current": allow, "seed_pinned": True, "ruling_id": "owned-ruling" if allow else None,
            "jurisdiction": "US-CA" if allow else None, "reasons": [], "sanctions_grounded": False}


def call(method, handler):
    client = HttpCompliance("https://owned.invalid", secrets.token_hex(32), secrets.token_hex(32),
                            transport=httpx.MockTransport(handler))
    return client.jurisdiction("US", "US-CA") if method == "POST" else client.client_activation("owned-client")


def capture(req):
    return req.method, str(req.url), req.content


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("failure", ["connect", "503"])
def test_transient_retry_preserves_request_and_recovers(method, failure):
    seen = []

    def handler(req):
        seen.append(capture(req))
        if len(seen) == 1:
            if failure == "connect":
                raise httpx.ConnectError("owned fixture unavailable", request=req)
            return httpx.Response(503, json={"detail": "owned fixture unavailable"})
        return httpx.Response(200, json=reply(method))

    answer = call(method, handler)
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0][0] == method
    if method == "POST":
        body = json.loads(seen[0][2])
        assert body["request_id"].startswith("fin-jur-")
        assert body["person"]["declared_region"] == "US-CA"
        assert answer.status == "operate"
    else:
        assert seen[0][2] == b""
        assert answer.allowed is True
    assert answer.available is True


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_persistent_503_stays_bounded_and_unavailable(method):
    seen = []

    def handler(req):
        seen.append(capture(req))
        return httpx.Response(503, json={"detail": "still unavailable"})

    answer = call(method, handler)
    assert len(seen) == 2 and seen[0] == seen[1]
    assert answer.available is False


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_permanent_refusal_is_not_retried(method):
    seen = []

    def handler(req):
        seen.append(capture(req))
        return httpx.Response(403, json={"detail": "refused"})

    assert call(method, handler).available is False
    assert len(seen) == 1


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_recovered_dependency_refusal_is_not_promoted_to_allow(method):
    seen = []

    def handler(req):
        seen.append(capture(req))
        return httpx.Response(503, json={"detail": "unavailable"}) if len(seen) == 1 else httpx.Response(
            200, json=reply(method, allow=False))

    answer = call(method, handler)
    assert len(seen) == 2 and seen[0] == seen[1]
    assert answer.available is True
    if method == "POST":
        assert answer.status == "blocked"
    else:
        assert answer.allowed is False
