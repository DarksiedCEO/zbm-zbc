"""Fix wave A (red team Oct 30-31 2026, scratchpad/audit/redteam-access.md) checks that span the Python services:
RTA-4 (no query string, id or token in uvicorn access logs) and the shared files' copies. Fails on 5c2538b (no
access_redact module; the launchers passed no log_config)."""

from __future__ import annotations

import io
import logging
import logging.config
import re
from pathlib import Path

import pytest

import access_redact
import serve

SERVICES = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("target,want", [
    ("/compliance/v1/sanctions/status?subject_id=client-ACME-123", "/compliance/v1/sanctions/status?<redacted>"),
    ("/legal/v1/obligations?party_ref=party-9&status=open", "/legal/v1/obligations?<redacted>"),
    ("/fin/v1/invoices/inv-123/status", "/fin/v1/invoices/:id/status"),
    ("/compliance/v1/register%26tok%3D" + "ab12" * 16, "/compliance/v1/:id"),
    ("/health", "/health"),
    ("/compliance/v1/clients/client-1/activation", "/compliance/v1/clients/:id/activation"),
])
def test_rta4_redact_target(target, want):
    assert access_redact.redact_target(target) == want


def test_rta4_the_finance_launcher_logs_no_query_string_or_id():
    """lateral_probe: a token sent in a URL path landed in five services' access logs; subject ids in query strings
    reach Railway's logs on every call."""
    cfg = serve.uvicorn_kwargs()["log_config"]
    logging.config.dictConfig(cfg)
    logger = logging.getLogger("uvicorn.access")
    out = io.StringIO()
    for h in logger.handlers:
        h.setStream(out)
    secret = "tok-" + "9f" * 16
    logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "GET",
                f"/compliance/v1/sanctions/status?subject_id=client-123&t={secret}", "1.1", 200)
    logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "GET", f"/fin/v1/invoices/{secret}/status", "1.1", 403)
    for h in logger.handlers:
        h.setStream(__import__("sys").stdout)
    text = out.getvalue()
    assert "client-123" not in text and secret not in text, text
    assert "/compliance/v1/sanctions/status?<redacted>" in text and "/fin/v1/invoices/:id/status" in text


def test_rta4_every_python_launcher_uses_the_redacting_log_config():
    launchers = sorted(SERVICES.glob("*/src/serve.py")) + [SERVICES / "delivery-py/src/zbm_delivery/serve.py",
                                                            SERVICES / "fulfillment-py/src/api.py"]
    missing = [str(p.relative_to(SERVICES)) for p in launchers
               if "access_redact.log_config()" not in p.read_text()]
    assert not missing, missing


def test_rta4_access_redact_copies_are_identical():
    copies = sorted(SERVICES.glob("*/src/access_redact.py")) + [SERVICES / "delivery-py/src/zbm_delivery/access_redact.py"]
    assert len(copies) >= 17
    assert len({p.read_bytes() for p in copies}) == 1, [str(p) for p in copies]


def test_rta4_no_launcher_logs_headers():
    pat = re.compile(r"log\w*\.(info|warning|error|debug)\([^)]*(authorization|headers|token)", re.I)
    hits = [str(p) for p in SERVICES.glob("*/src/*.py") if pat.search(p.read_text())]
    assert not hits, hits


# --- RTA-5: rotation window (current + next) and Andre's token held as a SHA-256 ------------------------------------

def _sha(t):
    import hashlib
    return hashlib.sha256(t.encode()).hexdigest()


def test_rta5_rotation_window_accepts_current_and_next_tokens():
    import json as _json

    import config as config_mod
    from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, Harness
    nxt_service = "next-fin-service-token-openssl-rand-hex-32-aa"
    nxt_sched = "next-fin-caller-scheduler-token-rand-hex-32-bb"
    h = Harness(env={"FIN_SERVICE_TOKEN_NEXT": nxt_service,
                     "FIN_CALLER_TOKENS_NEXT": _json.dumps({"scheduler": nxt_sched})})
    for bearer, caller in ((SERVICE_TOKEN, CALLERS["scheduler"]), (nxt_service, CALLERS["scheduler"]),
                           (SERVICE_TOKEN, nxt_sched), (nxt_service, nxt_sched)):
        r = h.client.get("/fin/v1/integrity", headers={"Authorization": f"Bearer {bearer}",
                                                        "X-FIN-Caller-Token": caller})
        assert r.status_code == 200, (bearer[:8], caller[:8], r.text)
    bad = h.client.get("/fin/v1/integrity", headers={"Authorization": "Bearer not-a-token-of-this-service-000000",
                                                      "X-FIN-Caller-Token": nxt_sched})
    assert bad.status_code == 401
    assert "next-fin" not in repr(h.settings), "no token in a repr"
    with pytest.raises(RuntimeError, match="NEXT"):
        config_mod.load({**h.env, "FIN_SERVICE_TOKEN_NEXT": CALLERS["onboarding"]})
    with pytest.raises(RuntimeError, match="unknown caller"):
        config_mod.load({**h.env, "FIN_CALLER_TOKENS_NEXT": _json.dumps({"nobody": "x" * 40})})
    assert ANDRE_TOKEN


def test_rta5_andres_token_can_be_held_as_a_hash_only():
    import config as config_mod
    from helpers import ANDRE_TOKEN, SERVICE_TOKEN, Harness
    h = Harness(env={"FIN_ANDRE_APPROVAL_TOKEN": "", "FIN_ANDRE_APPROVAL_TOKEN_SHA256": _sha(ANDRE_TOKEN)})
    assert h.settings.andre_token is None
    h.approve_rules()                                    # Andre's real token is accepted against the stored hash
    r = h.client.get("/fin/v1/reconcile", headers={"Authorization": f"Bearer {SERVICE_TOKEN}",
                                                    "X-Andre-Approval-Token": "wrong-andre-token-0000000000000000"})
    assert r.status_code == 403
    # a rotation of Andre's token: the next hash is accepted too
    nxt = "next-andre-approval-token-rand-hex-32-cc"
    h2 = Harness(env={"FIN_ANDRE_APPROVAL_TOKEN": "", "FIN_ANDRE_APPROVAL_TOKEN_SHA256": _sha(ANDRE_TOKEN),
                      "FIN_ANDRE_APPROVAL_TOKEN_NEXT_SHA256": _sha(nxt)})
    for tok in (ANDRE_TOKEN, nxt):
        r = h2.client.get("/fin/v1/reconcile", headers={"Authorization": f"Bearer {SERVICE_TOKEN}",
                                                         "X-Andre-Approval-Token": tok})
        assert r.status_code != 403, r.text
    with pytest.raises(RuntimeError, match="SHA256"):
        config_mod.load({**h.env, "FIN_ANDRE_APPROVAL_TOKEN_SHA256": "ABC"})
    with pytest.raises(RuntimeError, match="distinct"):
        config_mod.load({**h.env, "FIN_ANDRE_APPROVAL_TOKEN_SHA256": _sha(SERVICE_TOKEN)})
