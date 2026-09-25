"""Auth, docs and startup: same fail-closed pattern as fulfillment-py, tested against the real module app."""

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN

SRC = Path(__file__).resolve().parents[1] / "src"
anon = TestClient(app)
authed = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
wrong = TestClient(app, headers={"Authorization": "Bearer not-the-real-token"})

PROTECTED = [("get", "/registry/rows"), ("post", "/zbm/briefs"), ("post", "/zbc/clips"),
             ("post", "/zbc/campaigns/c1/rulebooks/1/sign"), ("put", "/registry/rows/x")]


def test_health_is_open():
    r = anon.get("/health")
    assert r.status_code == 200 and r.json()["service"] == "creative-py"


def test_protected_routes_reject_missing_and_wrong_token():
    for method, path in PROTECTED:
        assert getattr(anon, method)(path, **({"json": {}} if method != "get" else {})).status_code == 401, path
        assert getattr(wrong, method)(path, **({"json": {}} if method != "get" else {})).status_code == 401, path


def test_malformed_header_rejected():
    r = TestClient(app, headers={"Authorization": TEST_SERVICE_TOKEN}).get("/registry/rows")
    assert r.status_code == 401


def test_non_ascii_bearer_token_is_401_not_500():
    r = TestClient(app, headers={"Authorization": b"Bearer caf\xc3\xa9"}).get("/registry/rows")
    assert r.status_code == 401


def test_correct_token_accepted():
    assert authed.get("/registry/rows").status_code == 200


def test_docs_redoc_openapi_disabled():
    for path in ("/docs", "/redoc", "/openapi.json"):
        # fix wave 9 (AEGIS round 8 L4): authentication comes first on every path but /health, so an
        # anonymous caller gets 401 (it learns nothing, not even that the docs are off); with the token, 404
        assert anon.get(path).status_code == 401, path
        assert authed.get(path).status_code == 404, path


def test_module_app_defaults_to_unconfigured_ledger_so_decisions_are_refused():
    assert authed.get("/health").json()["ledger_configured"] is False
    from samples import zbm_requirements

    # fix wave 2 (N4): the module app has no actor credentials either -> actor actions refused first
    r = authed.post("/zbm/briefs", json={"requirements": zbm_requirements()})
    assert r.status_code == 403 and "not configured" in r.json()["detail"]
    from samples import ZBC_ASSETS

    r = authed.post("/zbc/campaigns/c1/rights-check", json={"assets": ZBC_ASSETS})
    assert r.status_code == 503 and r.json()["took_effect"] is False


def test_service_refuses_to_start_without_token():
    env = {k: v for k, v in os.environ.items() if k != "CREATIVE_SERVICE_TOKEN"}
    p = subprocess.run([sys.executable, "-c", "import api"], cwd=SRC, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "CREATIVE_SERVICE_TOKEN is not set" in p.stderr


def test_default_bind_address_is_loopback():
    import serve  # noqa: F401  (import only; does not start a server)
    src = (SRC / "serve.py").read_text()
    assert 'os.environ.get("CREATIVE_BIND_ADDR", "127.0.0.1")' in src
    assert "0.0.0.0" not in src.replace('never 0.0.0.0 by default', '')
