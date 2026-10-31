"""AEGIS r2 on ADR 0028 (aegis-client-acceptance-r2.md @591161b0): the portal side.

- N-M1: a token whose tag is wrong (any junk, forged or mistyped link) is refused locally, spending no budget and never
  asking Legal: a junk flood from any number of addresses leaves a real link servable. Only tag-valid tokens count
  toward the Legal-call ceiling. IPv6 clients are counted per /64. With every client behind one address (no client
  header), junk still cannot fill that address's bucket.
- N-L1: one company normalization on both sides (legal-py's ``norm_company``).
- N-L3: the same invisible-character rule as legal-py.
- N-L4: the receipt lookup carries the hash of the form signature the page issued.
- N-L5: the confirmation's time-zone label comes from the zone Legal names.
- N-L6: the page shown must be Legal's page exactly (503 otherwise), and a forged document block is refused.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

import app as A
import render as V
from helpers import (CP, LEGAL_LT, LINK_TAG_KEY, MSA_TEXT, SOW_TEXT, TOKEN, FakeLegal, call, form_post, hidden, make,
                     sha, tok, view)
from legal_client import LegalUnavailable, parse_view


def _junk(i: int) -> str:
    raw = hashlib.sha256(str(i).encode()).digest() + bytes(16)       # a right-shaped token with a wrong tag
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_the_portal_and_legal_mint_and_check_the_same_tokens():
    here = Path(__file__).resolve().parents[1] / "src" / "link_tag.py"
    legal = Path(__file__).resolve().parents[2] / "legal-py" / "src" / "link_tag.py"
    assert here.read_bytes() == legal.read_bytes()                  # one function, byte-identical in both services
    t = LEGAL_LT.mint(LINK_TAG_KEY.encode())
    assert A.LT.valid(t, LINK_TAG_KEY.encode()) and not A.LT.valid(t, b"another-key-" * 4)
    assert A.LT.valid(TOKEN, LINK_TAG_KEY.encode())


def test_a_junk_flood_spends_nothing_and_never_reaches_legal():
    app, legal, clock, mono = make(env={"PORTAL_RATE_LEGAL_PER_MINUTE": "5", "PORTAL_RATE_IP_PER_MINUTE": "5",
                                        "PORTAL_RATE_NOT_FOUND_PER_MINUTE": "5"})
    codes = {call(app, "GET", f"/accept/{_junk(i)}", {"x-real-ip": f"203.0.113.{i % 7}"}).status for i in range(600)}
    codes |= {call(app, "POST", f"/accept/{_junk(i)}", {"content-type": "application/x-www-form-urlencoded"}).status
              for i in range(50)}
    assert codes == {404} and legal.resolves == [] and legal.accepts == [] and legal.receipts == []
    assert len(app.legal_calls._hits) == 0 and len(app.by_ip._hits) == 0 and len(app.not_found._hits) == 0
    assert call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": "203.0.113.1"}).status == 200


def test_one_shared_address_is_not_a_bucket_junk_can_fill():
    """No client header and no forwarded hop (every client keys on the edge's address): junk is refused before the
    per-address limit is charged, so a flood cannot lock real clients out."""
    app, legal, *_ = make(env={"PORTAL_CLIENT_IP_HEADER": "none", "PORTAL_TRUSTED_PROXY_HOPS": "0",
                               "PORTAL_RATE_IP_PER_MINUTE": "30"})
    for i in range(500):
        call(app, "GET", f"/accept/{_junk(i)}", client=("10.0.0.1", 1))
    assert call(app, "GET", f"/accept/{TOKEN}", client=("10.0.0.1", 1)).status == 200


def test_tag_valid_unknown_links_still_count_toward_the_legal_ceiling():
    app, legal, clock, mono = make(env={"PORTAL_RATE_LEGAL_PER_MINUTE": "3", "PORTAL_RATE_NOT_FOUND_PER_MINUTE": "99"})
    codes = [call(app, "GET", f"/accept/{tok(str(i))}", {"x-real-ip": f"198.51.100.{i}"}).status for i in range(6)]
    assert codes == [404, 404, 404, 429, 429, 429] and len(legal.resolves) == 3


def test_ipv6_clients_are_counted_per_64():
    app, *_ = make(env={"PORTAL_RATE_IP_PER_MINUTE": "3", "PORTAL_RATE_TOKEN_PER_HOUR": "999"})
    codes = [call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": f"2001:db8:1:2::{i:x}"}).status for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": "2001:db8:1:3::1"}).status == 200   # another /64
    assert A.rate_key("2001:db8:1:2:aaaa::1") == A.rate_key("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert A.rate_key("203.0.113.9") == "203.0.113.9"


@pytest.mark.parametrize("typed,name", [("ＡＣＭＥ, l.l.c.", "Acme LLC"), ("acme llc", "Acme LLC"),
                                        ("Acme Inc", "Acme LLC"), ("AcmeTM LLC", "Acme LLC")])
def test_one_company_normalization_on_both_sides(typed, name):
    assert (A._norm_company(typed) == A._norm_company(name)) == (CP.norm_company(typed) == CP.norm_company(name))
    assert A._norm_company(typed) == CP.norm_company(typed)


def test_the_invisible_rule_is_legals():
    for cp in list(range(0, 0x3000)) + [0x3164, 0xFFA0, 0xFEFF, 0xE0001, 0xE0101, 0x1BCA0, 0x1D173, 0x2800]:
        ch = chr(cp)
        if 0xD800 <= cp <= 0xDFFF:
            continue
        assert A._invisible(__import__("unicodedata").normalize("NFKC", ch)) == CP.has_invisible(ch), hex(cp)


@pytest.mark.parametrize("ch", ["ㅤ", "ᅟ", "ᅠ", "ﾠ", "⠀", "͏", "\U000e0101"])
def test_blank_fillers_are_refused_before_legal(ch):
    app, legal, *_ = make()
    assert form_post(app, fields={"full_name": f"Pat{ch}Q"}).status == 422 and legal.accepts == []


def test_the_receipt_lookup_carries_the_hash_of_this_pages_form():
    app, legal, *_ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    form_sig = hidden(page.text)["form_sig"]
    form_post(app, get=page)
    assert legal.accepts[0]["form_sha256"] == hashlib.sha256(form_sig.encode()).hexdigest()
    assert form_post(app, get=page).status == 200                  # the same form: the receipt
    assert legal.receipts[-1][2] == legal.accepts[0]["form_sha256"]


def test_the_time_zone_label_comes_from_legals_zone():
    app, legal, *_ = make(FakeLegal(views={TOKEN: view(time_zone="America/New_York")}))
    r = form_post(app)
    assert r.status == 200 and "(Eastern Time)" in r.text and "Pacific" not in r.text
    assert "October 1, 2026 at 1:05 PM (Eastern Time)" in r.text


def test_a_laid_out_page_that_differs_from_legals_is_never_shown_or_recorded(monkeypatch):
    app, legal, *_ = make()
    real = V.acceptance_page

    def drops_a_block(view_, *a, **k):
        pg = real(view_, *a, **k)
        pg.text_parts = pg.text_parts[:-1]                         # the layout lost the last line
        return pg
    page = call(app, "GET", f"/accept/{TOKEN}")
    monkeypatch.setattr(V, "acceptance_page", drops_a_block)
    assert call(app, "GET", f"/accept/{TOKEN}").status == 503
    assert form_post(app, get=page).status == 503 and legal.accepts == []


def test_a_forged_document_block_is_refused_even_when_its_hash_is_recomputed():
    v = view()
    blocks = [dict(b) for b in v.blocks]
    next(b for b in blocks if b["k"] == "doc_text")["text"] = "a different agreement"
    text = CP.page_text(blocks)
    data = {"invitation_id": v.invitation_id, "page": v.page, "expires_at": v.expires_at,
            "display": {"client_legal_name": "Acme LLC", "notice_email": "legal@acme.example",
                        "contact_first_name": "Pat", "sow_number": "SOW-2026-001",
                        "sow_title": "SEO and AI Visibility Audit"},
            "documents": [{"doc_id": d.doc_id, "version": d.version, "doc_sha256": d.doc_sha256, "text": d.text,
                           "label": d.label} for d in v.documents],
            "msa_acceptance": None, "blocks": blocks, "page_sha256": hashlib.sha256(text.encode()).hexdigest()}
    with pytest.raises(LegalUnavailable):
        parse_view(data)
