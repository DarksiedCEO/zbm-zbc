"""AEGIS r3 (ADR 0028): the per-link limit before Legal, the dead-link cache, the log fingerprint, the format version,
the distinct-secrets check, IPv4-mapped addresses and blank glyphs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import unicodedata

import pytest

import app as A
import config as config_mod
from helpers import (CP, ENV, LINK_TAG_KEY, TOKEN, FakeLegal, call, form_post, hidden, make, tok, view)
from ratelimit import DeadLinks, Once

USED = tok("a link the client already used")


def _fp(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()[:8]


# --- N3-M1: a dead link costs at most one Legal call per window --------------------------------------------------

def test_a_dead_link_is_asked_about_once_then_remembered_for_fifteen_minutes():
    app, legal, clock, mono = make(FakeLegal(views={TOKEN: view()}, used={USED}),
                                   env={"PORTAL_RATE_TOKEN_PER_HOUR": "999", "PORTAL_RATE_NOT_FOUND_PER_MINUTE": "999"})
    codes = {call(app, "GET", f"/accept/{USED}", client=(f"198.51.100.{i % 200}", 1)).status for i in range(300)}
    assert codes <= {404, 429} and 404 in codes
    assert legal.resolves.count(USED) == 1
    mono.t += A.DEAD_LINK_TTL_S - 1
    call(app, "GET", f"/accept/{USED}", client=("192.0.2.9", 1))
    assert legal.resolves.count(USED) == 1
    mono.t += 2                                                     # the window is over: Legal is asked again
    call(app, "GET", f"/accept/{USED}", client=("192.0.2.10", 1))
    assert legal.resolves.count(USED) == 2


def test_a_flood_on_one_used_link_leaves_another_client_unaffected():
    """AEGIS probe4: a used link replayed from many addresses must not spend the global Legal ceiling."""
    app, legal, *_ = make(FakeLegal(views={TOKEN: view()}, used={USED}),
                          env={"PORTAL_RATE_LEGAL_PER_MINUTE": "5", "PORTAL_RATE_NOT_FOUND_PER_MINUTE": "100000",
                               "PORTAL_RATE_TOKEN_PER_HOUR": "999"})
    for i in range(2000):
        call(app, "GET", f"/accept/{USED}", client=(f"10.{i // 250}.{i % 250}.1", 1))
    assert legal.resolves.count(USED) == 1
    assert call(app, "GET", f"/accept/{TOKEN}", client=("203.0.113.50", 1)).status == 200
    assert form_post(app, client=("203.0.113.50", 1)).status == 200


def test_the_per_link_limit_is_charged_before_legal_and_names_the_link_by_fingerprint(caplog):
    app, legal, *_ = make(env={"PORTAL_RATE_TOKEN_PER_HOUR": "3"})
    caplog.set_level(logging.INFO)
    for i in range(3):
        assert call(app, "GET", f"/accept/{TOKEN}", client=(f"198.51.100.{i}", 1)).status == 200
    for i in range(20):
        assert call(app, "GET", f"/accept/{TOKEN}", client=(f"192.0.2.{i}", 1)).status == 429
    assert len(legal.resolves) == 3
    warned = [r.getMessage() for r in caplog.records if "rate-limited" in r.getMessage()]
    assert len(warned) == 1 and _fp(TOKEN) in warned[0] and len(_fp(TOKEN)) == 8      # once a minute, not per hit
    assert all(TOKEN not in r.getMessage() and TOKEN[:20] not in r.getMessage() for r in caplog.records)


def test_a_post_on_a_limited_link_never_reaches_legal():
    app, legal, *_ = make(env={"PORTAL_RATE_TOKEN_PER_HOUR": "1"})
    page = call(app, "GET", f"/accept/{TOKEN}")
    assert page.status == 200
    assert form_post(app, get=page).status == 429 and legal.accepts == [] and legal.receipts == []
    assert len(legal.resolves) == 1


def test_the_dead_link_log_line_names_the_fingerprint_never_the_token(caplog):
    app, legal, *_ = make(FakeLegal(views={TOKEN: view()}, used={USED}))
    caplog.set_level(logging.INFO)
    for _ in range(5):
        call(app, "GET", f"/accept/{USED}")
    dead = [r.getMessage() for r in caplog.records if "not usable at Legal" in r.getMessage()]
    assert len(dead) == 1 and _fp(USED) in dead[0]
    assert all(USED not in r.getMessage() for r in caplog.records)


def test_a_lost_confirmation_still_comes_back_after_the_link_is_remembered_dead():
    """The client's own submission used the link; a refresh of the page caches it dead; the re-POST of the same form
    still gets Legal's receipt (charged against the per-link limit and the Legal ceiling)."""
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    assert form_post(app, get=page).status == 200
    assert call(app, "GET", f"/accept/{TOKEN}").status == 404       # used: Legal says 404; remembered
    n = len(legal.resolves)
    again = form_post(app, get=page)
    assert again.status == 200 and len(legal.resolves) == n and legal.receipts


def test_a_not_found_from_accept_marks_the_link_dead():
    """The link was live when the POST resolved it, and gone by the time Legal recorded (a race, a revoke)."""
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    legal.accept_kind = "not_found"
    assert form_post(app, get=page).status == 404 and len(legal.accepts) == 1
    n = len(legal.resolves)
    call(app, "GET", f"/accept/{TOKEN}")
    assert len(legal.resolves) == n


def test_junk_never_enters_the_dead_or_per_link_tables():
    app, legal, *_ = make()
    for i in range(200):
        raw = b"\x01" + hashlib.sha256(str(i).encode()).digest() + bytes(16)
        call(app, "GET", "/accept/" + base64.urlsafe_b64encode(raw).decode().rstrip("="))
    assert len(app.dead._until) == 0 and len(app.by_token._hits) == 0 and legal.resolves == []


def test_the_link_tables_are_bounded():
    app, *_ = make()
    assert app.by_token.max_keys == 5_000 and app.dead.max_keys == 20_000
    d = DeadLinks(900, max_keys=3, clock=lambda: 0.0)
    for k in "abcde":
        d.add(k)
    assert "a" not in d and "b" not in d and all(k in d for k in "cde")
    once = Once(60, max_keys=2, clock=lambda: 0.0)
    assert once("x") and not once("x") and once("y") and once("z") and once("x")       # x was evicted


# --- N3-L4: the format version byte -------------------------------------------------------------------------------

def _v(version: int, seed: str = "v") -> str:
    rnd = hashlib.sha256(seed.encode()).digest()
    ver = bytes([version])
    tag = hmac.new(LINK_TAG_KEY.encode(), b"zbm-link\x1f" + ver + b"\x1f" + rnd, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(ver + rnd + tag).decode().rstrip("=")


def test_a_link_in_another_format_version_is_refused_and_logged(caplog):
    # AEGIS r4 N4-L3: a log line only (anyone can send such a token); the public /health stays {"status": "ok"}
    app, legal, *_ = make()
    caplog.set_level(logging.WARNING)
    for i in range(3):
        assert call(app, "GET", f"/accept/{_v(2, str(i))}").status == 404
    assert legal.resolves == []
    assert json.loads(call(app, "GET", "/health").body) == {"status": "ok"}
    warned = [r.getMessage() for r in caplog.records if "format version 2" in r.getMessage()]
    assert len(warned) == 1


def test_the_key_id_is_logged_at_startup_never_the_key(caplog):
    # AEGIS r4 N4-L3: the public /health no longer carries it
    caplog.set_level(logging.INFO)
    app, *_ = make()
    key_id = hashlib.sha256(LINK_TAG_KEY.encode()).hexdigest()[:8]
    assert any(key_id in r.getMessage() for r in caplog.records)
    assert all(LINK_TAG_KEY not in r.getMessage() for r in caplog.records)
    assert call(app, "GET", "/health").body == b'{"status":"ok"}'


# --- N3-L2: the distinct-secrets check and IPv4-mapped addresses ---------------------------------------------------

@pytest.mark.parametrize("other", ["PORTAL_LEGAL_SERVICE_TOKEN", "PORTAL_LEGAL_CALLER_TOKEN", "PORTAL_EVIDENCE_KEY"])
def test_the_link_tag_key_must_differ_from_every_other_portal_secret(other):
    with pytest.raises(RuntimeError, match="must differ"):
        config_mod.load({**ENV, "PORTAL_LINK_TAG_KEY": ENV[other]})


def test_an_ipv4_mapped_ipv6_address_counts_as_its_ipv4_address():
    assert A.rate_key("::ffff:203.0.113.5") == "203.0.113.5" == A.rate_key("203.0.113.5")
    assert A.rate_key("2001:db8:1:2:3:4:5:6") == A.rate_key("2001:db8:1:2:ff::1") == "2001:db8:1:2::/64"
    assert A.rate_key("not an address") == "not an address"
    app, *_ = make(env={"PORTAL_RATE_IP_PER_MINUTE": "2", "PORTAL_RATE_TOKEN_PER_HOUR": "999"})
    assert call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": "203.0.113.5"}).status == 200
    assert call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": "::ffff:203.0.113.5"}).status == 200
    assert call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": "::ffff:cb00:7105"}).status == 429     # same address


# --- N3-L3: blank glyphs -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cp", [0x1D159] + list(range(0x1D173, 0x1D17B)))
def test_musical_blanks_are_refused_before_legal(cp):
    app, legal, *_ = make()
    assert form_post(app, fields={"full_name": f"Pat{chr(cp)}Q"}).status == 422 and legal.accepts == []


@pytest.mark.parametrize("name", ["́̂", "́", "--", "‍́"])
def test_a_name_with_no_letter_or_digit_is_refused_before_legal(name):
    app, legal, *_ = make()
    assert form_post(app, fields={"full_name": name}).status == 422 and legal.accepts == []


def test_the_blank_and_visible_rules_are_legals():
    extra = list(range(0x1D100, 0x1D200)) + [0xE0001, 0xE0101, 0x1BCA0, 0xFFA0, 0xFEFF]
    for cp in list(range(0, 0x3200)) + extra:
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        assert A._invisible(unicodedata.normalize("NFKC", ch)) == CP.has_invisible(ch), hex(cp)
        assert A._has_visible(ch) == CP.has_visible(ch), hex(cp)
    for s in ("Pat", "́", "́a", "1", "ㅤ"):
        assert A._has_visible(s) == CP.has_visible(s)


def test_a_form_still_round_trips_for_an_ordinary_name():
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    assert "form_sig" in hidden(page.text)
    assert form_post(app, get=page, fields={"full_name": "José Ñúñez-O'Brien"}).status == 200
