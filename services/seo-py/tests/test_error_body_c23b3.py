"""C2-3b3 (lead rulings 18:20 PT F3b2-1..F3b2-3; SPEC_CORE_v2 T-1 / T-4 / T-5): the 401 bodies and the ASGI
input-limit bodies carry one closed reason code (no prose); an unknown reason is a code bug -> 500 INTERNAL_ERROR.
Synthetic values only (gen_values_v2 seed 0 and helpers.derived)."""
from __future__ import annotations

import asyncio
import json

import pytest

import api
import reasons
import sink_fields as sf
import sink_guard as sg
from conformance import gen_values_v2 as vg
from errors import Conflict, Forbidden, Invalid, NotFound, SeoError, Unavailable
from reasons import R

SEED = vg.SEEDS[0]
_WORDS = vg.prose(SEED, 400)
PROSE = [" ".join(_WORDS[i:i + 5]) for i in range(0, 400, 5)]
VALUES = vg.values(SEED, 80)
AUTH_CODES = ("AUTH_MISSING", "AUTH_INVALID")
LIMIT = {414: "URI_TOO_LONG", 431: "HEADERS_TOO_LARGE", 413: "BODY_TOO_LARGE", 400: "BAD_REQUEST",
         415: "UNSUPPORTED_MEDIA_TYPE", 408: "REQUEST_TIMEOUT", 422: "BODY_REFUSED"}


def _code_body(out: dict, code: str) -> None:
    assert out == {"detail": code}, out
    assert sg.classify(out["detail"]) == "code"


def test_the_new_codes_are_closed_reasons_with_a_typed_error_row():
    for c in AUTH_CODES + tuple(LIMIT.values()):
        assert c in reasons.CODES and R(c) == c and sg.classify(c) == "code"
        assert sf.SINK_FIELDS[("error", c, "/detail")] and sf.error_body(c, {}) == ({"detail": c}, None)
    assert api.LIMIT_CODES == LIMIT and (api.AUTH_MISSING, api.AUTH_INVALID) == AUTH_CODES


@pytest.fixture()
def h(tmp_path):
    import helpers
    return helpers.Harness(tmp_path)


def test_401_bodies_are_AUTH_MISSING_or_AUTH_INVALID_without_prose(h):
    texts = []
    missing = [{}, {"Authorization": ""}, {"Authorization": "Basic x"}, {"Authorization": "bearer x"}]
    missing += [{"Authorization": "".join(ch for ch in v if ch.isascii() and ch.isprintable()).strip()[:60] or "x"}
                for v in VALUES[:30] if not str(v).startswith("Bearer ")]
    for hd in missing:
        r = h.client.get("/seo/v1/status", headers=hd)
        assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
        _code_body(r.json(), "AUTH_MISSING")
        texts.append(r.text)
    for i, v in enumerate(VALUES[:30]):
        tok = "".join(ch for ch in v if ch.isascii() and ch.isalnum())[:40] or f"t{i}"
        r = h.client.get("/seo/v1/status", headers={"Authorization": "Bearer " + tok + "z" * (i % 3)})
        assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
        _code_body(r.json(), "AUTH_INVALID")
        texts.append(r.text)
    text = "\n".join(texts)
    assert "token" not in text.lower().replace("auth_", "") and "header" not in text and "Bearer" not in text


async def _call(app, scope, body_messages, delay=None):
    sent = []
    queue = list(body_messages)

    async def receive():
        if delay is not None:
            await asyncio.sleep(delay)
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body), body


async def _inner(scope, receive, send):   # never reached by a refused request
    raise AssertionError("route reached")


def _scope(path="/seo/v1/x", headers=(), raw=None):
    return {"type": "http", "path": path, "raw_path": raw or path.encode(), "query_string": b"",
            "headers": list(headers), "method": "POST"}


def _cases():
    js = (b"content-type", b"application/json")
    deep = b"[" * (api.MAX_JSON_DEPTH + 1) + b"]" * (api.MAX_JSON_DEPTH + 1)
    many = b"[" + b"0," * (api.MAX_JSON_MEMBERS + 1) + b"0]"
    return [
        (414, _scope(path="/" + "a" * api.MAX_TARGET_BYTES), [], None),
        (431, _scope(headers=[(b"x-pad", b"p" * api.MAX_HEAD_BYTES)]), [], None),
        (413, _scope(headers=[js, (b"content-length", str(api.MAX_BODY_BYTES + 1).encode())]), [], None),
        (413, _scope(headers=[js, (b"transfer-encoding", b"chunked")]),
         [{"type": "http.request", "body": b" " * (api.MAX_BODY_BYTES + 1), "more_body": False}], None),
        (400, _scope(headers=[js, (b"content-length", b"12a")]), [], None),
        (415, _scope(headers=[(b"content-length", b"2")]), [{"type": "http.request", "body": b"{}"}], None),
        (415, _scope(headers=[(b"content-type", b"text/plain")]), [{"type": "http.request", "body": b"{}"}], None),
        (408, _scope(headers=[js, (b"content-length", b"2")]), [{"type": "http.request", "body": b"{}"}], 0.2),
        (422, _scope(headers=[js]), [{"type": "http.request", "body": deep}], None),
        (422, _scope(headers=[js]), [{"type": "http.request", "body": many}], None),
    ]


def test_every_input_limit_body_is_its_closed_code(monkeypatch):
    monkeypatch.setattr(api, "BODY_READ_TIMEOUT_S", 0.05)
    limits = api.InputLimits(_inner)
    seen = set()
    for status, scope, msgs, delay in _cases():
        got, out, raw = asyncio.run(_call(limits, scope, msgs, delay))
        assert got == status, (got, status)
        _code_body(out, LIMIT[status])
        assert b"refused" not in raw and b"bytes" not in raw and b"JSON" not in raw
        seen.add(status)
    assert seen == set(LIMIT)


def test_the_limit_bodies_through_the_real_app(h):
    r = h.client.get("/seo/v1/" + "a" * api.MAX_TARGET_BYTES)
    assert r.status_code == 414
    _code_body(r.json(), "URI_TOO_LONG")
    r = h.client.post("/seo/v1/tenants", content=b"{}", headers={**h.headers(andre=True), "content-type": "text/plain"})
    assert r.status_code == 415
    _code_body(r.json(), "UNSUPPORTED_MEDIA_TYPE")


@pytest.mark.parametrize("cls", [Conflict, Invalid, NotFound, Forbidden, Unavailable])
def test_an_unknown_reason_is_500_INTERNAL_ERROR_whatever_the_exception_status(h, cls):
    app = h.client.app
    while not hasattr(app, "exception_handlers"):
        app = app.app
    domain = app.exception_handlers[SeoError]
    api.ERROR_REFUSED.clear()
    for p in PROSE[:20]:
        r = domain(None, cls(p))
        assert r.status_code == 500 and json.loads(r.body) == {"detail": "INTERNAL_ERROR"}
        assert p not in r.body.decode()
    assert api.ERROR_REFUSED == {("INTERNAL_ERROR", "unknown_reason"): 20}
    ok = domain(None, cls(R("INVALID")))                    # a catalogue reason keeps its own status
    assert ok.status_code == cls(R("INVALID")).status_code != 500
    api.ERROR_REFUSED.clear()
