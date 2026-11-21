"""Portal response-loss regression; synthetic persisted Legal boundary, not real Legal replay."""

import json
import os
import secrets

import httpx
import pytest

from helpers import FakeLegal, call, form_post, make, tok, view
from legal_client import LegalClient


class CommittedThenTimeout(FakeLegal):
    def __init__(self, path, token, service_token, caller_token):
        super().__init__(views={token: view()})
        self.path = path
        self.client = LegalClient(
            "http://synthetic.invalid", service_token, caller_token,
            transport=httpx.MockTransport(self.transport),
        )

    def transport(self, request):
        assert request.url.path == "/legal/v1/portal/acceptances"
        body = json.loads(request.content)
        FakeLegal.accept(self, body)
        # Fsynced synthetic persistence before a lost HTTP response. This is not
        # Legal's anchored journal, nor a crash/restart durability proof.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(body).encode())
            stream.flush()
            os.fsync(stream.fileno())
        raise httpx.ReadTimeout("response lost after fixture persistence", request=request)

    def accept(self, body):
        return self.client.accept(body)


@pytest.fixture
def timeout_case(tmp_path):
    # Runtime-only synthetic credentials; the existing conftest forbids sockets.
    env = {name: secrets.token_hex(32) for name in (
        "PORTAL_LEGAL_SERVICE_TOKEN", "PORTAL_LEGAL_CALLER_TOKEN",
        "PORTAL_EVIDENCE_KEY", "PORTAL_LINK_TAG_KEY",
    )}
    token = tok(secrets.token_hex(32), env["PORTAL_LINK_TAG_KEY"])
    legal = CommittedThenTimeout(
        tmp_path / "synthetic-commit.json", token,
        env["PORTAL_LEGAL_SERVICE_TOKEN"], env["PORTAL_LEGAL_CALLER_TOKEN"],
    )
    try:
        app, *_ = make(legal, env=env)
        page = call(app, "GET", f"/accept/{token}")
        assert page.status == 200
        yield app, legal, token, page
    finally:
        legal.client.close()


def test_before_accept_dispatch_unavailable_does_not_record(timeout_case):
    app, legal, token, page = timeout_case
    legal.unavailable = True
    response = form_post(app, token=token, get=page)
    assert response.status == 503
    assert not legal.path.exists() and legal.accepts == []
    assert "Thank you. Your acceptance is recorded." not in response.text


def _committed_timeout(timeout_case):
    app, legal, token, page = timeout_case
    response = form_post(app, token=token, get=page)
    assert response.status == 503 and response.headers["retry-after"] == "60"
    assert legal.path.exists() and len(legal.accepts) == 1
    stored = json.loads(legal.path.read_text())
    assert stored["request_id"] == legal.accepts[0]["request_id"]
    return response, stored


def test_committed_timeout_original_form_retry_reads_receipt(timeout_case):
    app, legal, token, page = timeout_case
    _, stored = _committed_timeout(timeout_case)
    retry = form_post(app, token=token, get=page)
    assert retry.status == 200 and "Thank you. Your acceptance is recorded." in retry.text
    assert len(legal.accepts) == 1 and len(legal.receipts) == 1
    assert legal.receipts[0][1] == stored["request_id"]
    assert legal.receipts[0][2] == stored["form_sha256"]


def test_timeout_copy_never_claims_no_record_after_fixture_commit(timeout_case):
    response, _ = _committed_timeout(timeout_case)
    assert "Nothing was recorded" not in response.text
    assert "cannot confirm" in response.text.lower()
