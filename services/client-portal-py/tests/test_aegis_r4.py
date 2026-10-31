"""AEGIS r4 (ADR 0028): canonical tokens only, only Legal's FINAL 404s are remembered as dead, a Legal 503 is never
remembered, and the public /health says nothing about the link key."""

from __future__ import annotations

import base64
import hashlib
import json
import logging

import httpx

import app as A
from helpers import LINK_TAG_KEY, TOKEN, FakeLegal, call, form_post, make, tok, view
from legal_client import Gone, LegalClient


def _spellings(t: str) -> list:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    raw = base64.urlsafe_b64decode(t + "==")
    return [t[:-1] + c for c in alphabet if base64.urlsafe_b64decode(t[:-1] + c + "==") == raw]


# --- N4-L1 ---------------------------------------------------------------------------------------------------------

def test_only_the_minted_spelling_of_a_link_is_a_link():
    alts = [t for t in _spellings(TOKEN) if t != TOKEN]
    assert len(alts) == 15
    app, legal, *_ = make(env={"PORTAL_RATE_TOKEN_PER_HOUR": "999"})
    for t in alts:
        assert call(app, "GET", f"/accept/{t}").status == 404
        assert not A.LT.valid(t, LINK_TAG_KEY.encode())
    assert legal.resolves == [] and len(app.by_token._hits) == 0 and len(app.dead._until) == 0
    assert call(app, "GET", f"/accept/{TOKEN}").status == 200


# --- N4-L2 / N4-L5 -------------------------------------------------------------------------------------------------

def test_a_legal_503_is_never_remembered_as_dead():
    app, legal, *_ = make()
    legal.unavailable = True
    assert call(app, "GET", f"/accept/{TOKEN}").status == 503
    page = None
    legal.unavailable = False
    page = call(app, "GET", f"/accept/{TOKEN}")
    assert page.status == 200 and len(legal.resolves) == 2 and len(app.dead._until) == 0
    legal.unavailable = True
    assert form_post(app, get=page).status == 503
    legal.unavailable = False
    assert len(app.dead._until) == 0 and form_post(app, get=page).status == 200


def test_a_404_that_is_not_final_is_not_remembered():
    """Legal could not serve the link's documents right now (a blob missing, rules not loaded): the client sees the
    404, and the next request asks Legal again."""
    legal = FakeLegal(views={TOKEN: view()}, transient={TOKEN})
    app, *_ = make(legal)
    assert call(app, "GET", f"/accept/{TOKEN}").status == 404
    legal.transient.clear()
    assert call(app, "GET", f"/accept/{TOKEN}").status == 200
    assert len(legal.resolves) == 2 and len(app.dead._until) == 0


def test_an_accept_404_that_is_not_final_is_not_remembered():
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    legal.accept_kind, legal.accept_final = "not_found", False
    assert form_post(app, get=page).status == 404
    assert len(app.dead._until) == 0
    n = len(legal.resolves)
    call(app, "GET", f"/accept/{TOKEN}")
    assert len(legal.resolves) == n + 1


def test_a_final_404_is_remembered():
    used = tok("used for r4")
    app, legal, *_ = make(FakeLegal(views={TOKEN: view()}, used={used}))
    for _ in range(3):
        assert call(app, "GET", f"/accept/{used}").status == 404
    assert legal.resolves.count(used) == 1


def _client(status, payload):
    def handler(req):
        return httpx.Response(status, json=payload)
    return LegalClient("http://legal.railway.internal:8420", "s" * 40, "c" * 40, transport=httpx.MockTransport(handler))


def test_the_adapter_reads_legals_final_flag():
    assert _client(404, {"detail": "not found", "final": True}).resolve(TOKEN) == Gone(final=True)
    assert _client(404, {"detail": "not found"}).resolve(TOKEN) == Gone(final=False)
    assert _client(404, {"detail": "not found", "final": "yes"}).resolve(TOKEN) == Gone(final=False)
    assert _client(404, {"detail": "not found", "final": True}).accept({"token": TOKEN}).final is True
    assert _client(404, {"detail": "not found"}).accept({"token": TOKEN}).final is False


# --- N4-L3 ---------------------------------------------------------------------------------------------------------

def test_the_public_health_says_nothing_about_links_even_after_foreign_versions(caplog):
    caplog.set_level(logging.INFO)
    app, legal, *_ = make()
    rnd = hashlib.sha256(b"v2").digest()
    raw = bytes([2]) + rnd + bytes(16)
    other = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    for _ in range(3):
        assert call(app, "GET", f"/accept/{other}").status == 404
    h = call(app, "GET", "/health")
    assert h.status == 200 and json.loads(h.body) == {"status": "ok"}
    msgs = [r.getMessage() for r in caplog.records]
    key_id = hashlib.sha256(LINK_TAG_KEY.encode()).hexdigest()[:8]
    assert any("link tags" in m and "version 1" in m and key_id in m for m in msgs)       # at startup, in the log
    assert len([m for m in msgs if "format version 2" in m]) == 1
    assert all(LINK_TAG_KEY not in m for m in msgs)
