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


def _ends(days=1):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_rta5_rotation_window_accepts_current_and_next_tokens():
    import json as _json

    import config as config_mod
    from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, Harness
    nxt_service = "next-fin-service-token-openssl-rand-hex-32-aa"
    nxt_sched = "next-fin-caller-scheduler-token-rand-hex-32-bb"
    h = Harness(env={"FIN_SERVICE_TOKEN_NEXT": nxt_service, "FIN_ROTATION_ENDS_AT": _ends(),
                     "FIN_CALLER_TOKENS_NEXT": _json.dumps({"scheduler": nxt_sched})})
    assert h.client.get("/health").json()["rotation_in_progress"] is True
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
                      "FIN_ANDRE_APPROVAL_TOKEN_NEXT_SHA256": _sha(nxt), "FIN_ROTATION_ENDS_AT": _ends()})
    for tok in (ANDRE_TOKEN, nxt):
        r = h2.client.get("/fin/v1/reconcile", headers={"Authorization": f"Bearer {SERVICE_TOKEN}",
                                                         "X-Andre-Approval-Token": tok})
        assert r.status_code != 403, r.text
    with pytest.raises(RuntimeError, match="SHA256"):
        config_mod.load({**h.env, "FIN_ANDRE_APPROVAL_TOKEN_SHA256": "ABC"})
    with pytest.raises(RuntimeError, match="distinct"):
        config_mod.load({**h.env, "FIN_ANDRE_APPROVAL_TOKEN_SHA256": _sha(SERVICE_TOKEN)})


def test_f11_a_rotation_window_has_an_explicit_end():
    """AEGIS wave A F-11: nothing expired a *_NEXT value, and a plaintext Andre token beside a different hash both
    worked."""
    import json as _json

    import config as config_mod
    import token_rotation as TR
    from helpers import ANDRE_TOKEN, Harness
    h = Harness()
    nxt = "next-fin-service-token-openssl-rand-hex-32-aa"
    with pytest.raises(RuntimeError, match="ROTATION_ENDS_AT"):
        config_mod.load({**h.env, "FIN_SERVICE_TOKEN_NEXT": nxt})
    with pytest.raises(RuntimeError, match="14 days"):
        config_mod.load({**h.env, "FIN_SERVICE_TOKEN_NEXT": nxt, "FIN_ROTATION_ENDS_AT": _ends(30)})
    with pytest.raises(RuntimeError, match="ROTATION_ENDS_AT"):
        config_mod.load({**h.env, "FIN_SERVICE_TOKEN_NEXT": nxt, "FIN_ROTATION_ENDS_AT": "2026-11-07T00:00:00"})
    # the next value is refused once the window has ended
    t = [1000.0]
    s = TR.Secret(["current-token-0000000000000000000000"], next_plaintexts=[nxt], next_until=2000.0, clock=lambda: t[0])
    assert s.matches(nxt) and s.matches("current-token-0000000000000000000000")
    t[0] = 2000.0
    assert not s.matches(nxt) and s.matches("current-token-0000000000000000000000")
    # a plaintext Andre token and a hash of ANOTHER token: refused at start (the old plaintext would keep working)
    with pytest.raises(RuntimeError, match="different"):
        config_mod.load({**h.env, "FIN_ANDRE_APPROVAL_TOKEN_SHA256": _sha("another-andre-token-0000000000000000")})
    # the same token as plaintext and hash (the move to the hash): fine
    config_mod.load({**h.env, "FIN_ANDRE_APPROVAL_TOKEN_SHA256": _sha(ANDRE_TOKEN)})
    assert "rotation_in_progress" not in h.client.get("/health").json()
    assert _json


def test_n4_an_expired_rotation_window_never_stops_a_start(caplog):
    """AEGIS round 2 N-4/N-8: after <P>_ROTATION_ENDS_AT any restart (an OOM, a host move, a redeploy for another
    variable) refused to start: a crash loop of the money path. Now the service starts, the *_NEXT values are ignored
    (the current tokens keep working), every start logs a warning, /health shows `rotation_expired` (a warning the
    heartbeat alerts on), and `rotation_in_progress` is false once the window has ended."""
    import time as _time

    import token_rotation as TR
    import config as config_mod
    from helpers import CALLERS, SERVICE_TOKEN, Harness
    nxt = "next-fin-service-token-openssl-rand-hex-32-aa"
    with caplog.at_level(logging.WARNING):
        h = Harness(env={"FIN_SERVICE_TOKEN_NEXT": nxt, "FIN_ROTATION_ENDS_AT": _ends(-1)})
    assert any("ROTATION_ENDS_AT" in r.getMessage() and "expired" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]
    assert nxt not in caplog.text
    hb = h.client.get("/health")
    assert hb.status_code == 200, hb.text
    body = hb.json()
    assert body.get("rotation_expired") is True and "rotation_expired" in body.get("warnings", []), body
    assert not body.get("rotation_in_progress"), body
    hdr = {"X-FIN-Caller-Token": CALLERS["scheduler"]}
    assert h.client.get("/fin/v1/integrity", headers={"Authorization": f"Bearer {nxt}", **hdr}).status_code == 401
    assert h.client.get("/fin/v1/integrity", headers={"Authorization": f"Bearer {SERVICE_TOKEN}", **hdr}).status_code == 200
    # a window that ends while the service runs: in progress before, expired (not in progress) after (N-8)
    r = TR.Rotation(service_next=nxt, ends_at=_time.time() + 3600)
    assert r.in_progress and not r.expired
    r = TR.Rotation(service_next=nxt, ends_at=_time.time() - 1)
    assert r.expired and not r.in_progress
    assert not TR.Rotation(ends_at=_time.time() - 1).expired          # no next value: nothing to expire
    # a garbage ROTATION_ENDS_AT is a deploy-time error even without a NEXT value (N-8: it was accepted silently)
    with pytest.raises(RuntimeError, match="ROTATION_ENDS_AT"):
        config_mod.load({**h.env, "FIN_ROTATION_ENDS_AT": "garbage"})
