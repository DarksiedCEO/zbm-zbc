"""ADR 0028: GET /accept/{token}: the page, its words, its headers, and the uniform 404."""

from __future__ import annotations

import hashlib
import re

import pytest

import render as V
from helpers import (DISPLAY, MSA_TEXT, SOW_TEXT, TOKEN, FakeLegal, call, hidden, make, sha, view)
from helpers import tok as mint_tok
from legal_client import Display, Document

CLICKWRAP_B = [
    "Review and accept your agreement with Z Best Media",
    "Before we start, please read and accept two documents for Acme LLC. The Master Services Agreement sets the ground "
    "rules for working together. The Statement of Work describes this project, its price and its timeline. Each "
    "document is shown in full below, and you can download a copy of each before you accept.",
    "Accepting on this page is a legal signature. It has the same effect as signing on paper. Only a person authorized "
    "to sign for Acme LLC should accept.",
    "Z Best Media Master Services Agreement",
    f"Version 1.0 · SHA-256 fingerprint {sha(MSA_TEXT)}",
    "Open the full agreement",
    "I have read the Z Best Media Master Services Agreement, version 1.0, and I agree to it on behalf of Acme LLC.",
    "Statement of Work SOW-2026-001: SEO and AI Visibility Audit",
    f"Version 1.1 · SHA-256 fingerprint {sha(SOW_TEXT)}",
    "Open the full Statement of Work",
    "I have read Statement of Work SOW-2026-001, version 1.1, including its fees, and I agree to it on behalf of "
    "Acme LLC.",
    "Your full name", "Your title", "(for example, Owner, CEO, Managing Member)",
    'By selecting "Accept and sign", I confirm that I am the person named above and that I am authorized to bind '
    "Acme LLC.",
    'By selecting "Accept and sign", you agree to do business with Z Best Media electronically, including receiving '
    "these documents, notices, invoices and reports by email at legal@acme.example. We will record your name, title, "
    "the time, the documents' fingerprints, and your IP address and browser as evidence of this acceptance, and we "
    "will email you a copy of everything you accept. You can ask us for another copy at any time at "
    "legalnotice@zbestmedia.com.",
    "Accept and sign", "Not now", "Download a copy",
]


def test_the_page_carries_the_clickwrap_text_v1_part_b_word_for_word_and_both_documents_in_full():
    app, legal, *_ = make()
    r = call(app, "GET", f"/accept/{TOKEN}")
    assert r.status == 200 and r.headers["content-type"] == "text/html; charset=utf-8"
    text = V.acceptance_page(view(), TOKEN, 0, "", None).text
    for s in CLICKWRAP_B:
        assert s in text, s
        assert V.e(s) in r.text, s
    assert MSA_TEXT in text and V.e(MSA_TEXT) in r.text and V.e(SOW_TEXT) in r.text
    # each box is UNTICKED when the page loads, and has its own label
    boxes = re.findall(r'<input type="checkbox"[^>]*>', r.text)
    assert len(boxes) == 2 and not any("checked" in b for b in boxes)
    assert '<label for="accept_doc_1">' in r.text and '<label for="accept_doc_2">' in r.text
    for fid in ("full_name", "title", "company"):
        assert f'<label for="{fid}">' in r.text and f'id="{fid}"' in r.text
    assert legal.resolves == [TOKEN]


def test_the_page_signs_the_hash_of_its_own_text():
    app, *_ = make()
    r = call(app, "GET", f"/accept/{TOKEN}")
    h = hidden(r.text)
    assert h["page_sha256"] == hashlib.sha256(V.acceptance_page(view(), TOKEN, 0, "", None).text.encode()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", h["form_sig"]) and h["issued"].isdigit()


def test_strict_security_headers_no_script_no_cookie_no_third_party():
    app, *_ = make()
    for path in (f"/accept/{TOKEN}", "/accept/x", "/health", "/nope", "/accept/not-now"):
        r = call(app, "GET", path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp and f"style-src '{V.CSS_HASH}'" in csp and "base-uri 'none'" in csp
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "same-origin"
        assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["strict-transport-security"].startswith("max-age=")
        assert "set-cookie" not in r.headers and "server" not in r.headers
    page = call(app, "GET", f"/accept/{TOKEN}").text
    assert "<script" not in page.lower() and "http://" not in page and "https://" not in page
    assert re.search(r"<style>(.*?)</style>", page).group(1) == V.CSS
    assert 'lang="en"' in page


def test_every_value_is_escaped():
    evil = Display('<script>alert(1)</script> "LLC"', "a@b.example", None, "<b>1</b>", "x' onmouseover='y")
    docs = [Document("client_msa", "1.0", sha("<img src=x onerror=alert(1)>"), "<img src=x onerror=alert(1)>"),
            Document("sow", "1.1", sha(SOW_TEXT), SOW_TEXT)]
    app, *_ = make(FakeLegal(views={TOKEN: view(docs=docs, display=evil)}))
    r = call(app, "GET", f"/accept/{TOKEN}")
    assert r.status == 200
    assert "<script>" not in r.text and "<img" not in r.text and "<b>1" not in r.text
    assert "&lt;script&gt;" in r.text and "&lt;img src=x" in r.text and "x&#x27; onmouseover=&#x27;y" in r.text


@pytest.mark.parametrize("token", ["short", "A" * 42, "A" * 44, "A" * 42 + "!", "A" * 42 + "%2F", "..", "A" * 200])
def test_a_malformed_token_is_the_uniform_404_without_asking_legal(token):
    app, legal, *_ = make()
    r = call(app, "GET", f"/accept/{token}")
    assert r.status == 404 and r.text == V.NOT_FOUND.html()
    assert legal.resolves == []


def test_unknown_used_msa_only_and_unservable_links_are_the_same_404():
    unknown = mint_tok("Z")
    msa_only = mint_tok("M")
    sow_no_msa = mint_tok("S")
    no_sow_title = mint_tok("T")
    legal = FakeLegal(views={
        TOKEN: view(),
        msa_only: view(page="msa_only", docs=[Document("client_msa", "1.0", sha(MSA_TEXT), MSA_TEXT)]),
        sow_no_msa: view(page="new_sow", docs=[Document("sow", "1.2", sha(SOW_TEXT), SOW_TEXT)]),
        no_sow_title: view(display=Display("Acme LLC", "legal@acme.example", None, None, None))})
    legal.used.add(TOKEN)
    app, *_ = make(legal)
    bodies = set()
    for tok in (unknown, TOKEN, msa_only, sow_no_msa, no_sow_title):
        r = call(app, "GET", f"/accept/{tok}")
        assert r.status == 404, tok
        bodies.add((r.body, tuple(sorted(r.headers.items()))))
        for n in (1, 2):
            d = call(app, "GET", f"/accept/{tok}/document/{n}")
            assert d.status == 404 and d.body == r.body
    assert len(bodies) == 1


def test_the_new_sow_page_is_part_c():
    tok = mint_tok("N")
    v = view(page="new_sow", docs=[Document("sow", "1.2", sha(SOW_TEXT), SOW_TEXT)],
             msa_acc=("1.0", "2026-09-02T18:00:00Z"))
    app, *_ = make(FakeLegal(views={tok: v}))
    r = call(app, "GET", f"/accept/{tok}")
    assert r.status == 200
    assert "Review and accept your new Statement of Work" in r.text
    assert ("This Statement of Work is made under the Master Services Agreement, version 1.0, that Acme LLC accepted "
            "on September 2, 2026.") in r.text
    assert "Master Services Agreement, version 1.0, and I agree" not in r.text
    assert len(re.findall(r'<input type="checkbox"', r.text)) == 1 and 'name="accept_doc_1"' in r.text


def test_the_documents_are_served_byte_exact_inline_and_as_a_download():
    app, *_ = make()
    for n, text in ((1, MSA_TEXT), (2, SOW_TEXT)):
        r = call(app, "GET", f"/accept/{TOKEN}/document/{n}")
        assert r.status == 200 and r.body == text.encode("utf-8")
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert r.headers["content-disposition"].startswith("inline;")
        d = call(app, "GET", f"/accept/{TOKEN}/document/{n}/download")
        assert d.body == text.encode("utf-8") and d.headers["content-disposition"].startswith("attachment;")
        assert hashlib.sha256(d.body).hexdigest() == sha(text)
    assert call(app, "GET", f"/accept/{TOKEN}/document/3").status == 404


def test_legal_down_or_a_text_whose_hash_does_not_match_shows_nothing():
    legal = FakeLegal(views={TOKEN: view()}, unavailable=True)
    app, *_ = make(legal)
    r = call(app, "GET", f"/accept/{TOKEN}")
    assert r.status == 503 and "Nothing was recorded" in r.text and MSA_TEXT[:20] not in r.text


def test_rate_limits_per_client_address_per_link_and_for_not_found():
    app, legal, clock, mono = make(env={"PORTAL_RATE_IP_PER_MINUTE": "5", "PORTAL_RATE_TOKEN_PER_HOUR": "7",
                                        "PORTAL_RATE_NOT_FOUND_PER_MINUTE": "3"})
    for _ in range(5):
        assert call(app, "GET", f"/accept/{TOKEN}").status == 200
    r = call(app, "GET", f"/accept/{TOKEN}")
    assert r.status == 429 and int(r.headers["retry-after"]) >= 1
    n = len(legal.resolves)
    # another client address: its own address window, the same link's window (7 per hour)
    assert call(app, "GET", f"/accept/{TOKEN}", client=("198.51.100.2", 1)).status == 200
    assert call(app, "GET", f"/accept/{TOKEN}", client=("198.51.100.3", 1)).status == 200
    assert call(app, "GET", f"/accept/{TOKEN}", client=("198.51.100.4", 1)).status == 429      # 8th for this link
    # AEGIS r3 N3-M1: the per-link limit is charged BEFORE Legal: the 8th request never reaches Legal
    assert len(legal.resolves) == n + 2 and len(app.by_token._hits) == 1
    mono.t += 61
    # not-found answers have a global ceiling (distributed guessing): 3 per minute, then 429
    codes = [call(app, "GET", f"/accept/{mint_tok(c)}", client=(f"192.0.2.{i}", 1)).status
             for i, c in enumerate("BCDEF")]
    assert codes == [404, 404, 404, 429, 429]


def test_a_global_ceiling_on_legal_calls_is_charged_before_calling_legal():
    """AEGIS M-3: a client that can choose its address (a spoofed header) bypasses the per-address limit; every call
    to Legal still counts against one ceiling, charged BEFORE the call, so Legal sees at most that many."""
    app, legal, clock, mono = make(env={"PORTAL_RATE_LEGAL_PER_MINUTE": "4", "PORTAL_RATE_NOT_FOUND_PER_MINUTE": "1000"})
    codes = [call(app, "GET", f"/accept/{mint_tok(str(i))}", {"x-real-ip": f"203.0.113.{i}"}).status
             for i in range(10)]
    assert codes == [404] * 4 + [429] * 6 and len(legal.resolves) == 4
    mono.t += 61
    assert call(app, "GET", f"/accept/{TOKEN}").status == 200


def test_junk_tokens_never_enter_the_per_link_table():
    app, legal, *_ = make(env={"PORTAL_RATE_IP_PER_MINUTE": "999", "PORTAL_RATE_NOT_FOUND_PER_MINUTE": "999",
                               "PORTAL_RATE_LEGAL_PER_MINUTE": "999"})
    for i in range(50):
        call(app, "GET", f"/accept/{('Q%02d' % i) * 14}Q")
        call(app, "GET", f"/accept/short{i}")
    assert len(app.by_token._hits) == 0                      # neither malformed nor unknown links are counted
    call(app, "GET", f"/accept/{TOKEN}")
    assert len(app.by_token._hits) == 1


def test_the_client_address_is_the_edge_header_never_the_left_most_forwarded_entry():
    app, *_ = make()
    peer = ("10.0.0.1", 1)
    ip = lambda h: app._client_ip({"headers": [(k.encode(), v.encode()) for k, v in h.items()], "client": peer})  # noqa: E731
    assert ip({"x-real-ip": "203.0.113.5", "x-forwarded-for": "1.2.3.4, 198.51.100.9"}) == "203.0.113.5"
    assert ip({"x-real-ip": "not an ip", "x-forwarded-for": "1.2.3.4, 198.51.100.9"}) == "198.51.100.9"
    assert ip({"x-forwarded-for": "1.2.3.4, 198.51.100.9"}) == "198.51.100.9"
    assert ip({}) == "10.0.0.1"
    app2, *_ = make(env={"PORTAL_CLIENT_IP_HEADER": "none", "PORTAL_TRUSTED_PROXY_HOPS": "0"})
    assert app2._client_ip({"headers": [(b"x-real-ip", b"203.0.113.5")], "client": peer}) == "10.0.0.1"


def test_the_ip_limit_keys_on_the_edge_address_not_the_proxy():
    app, *_ = make(env={"PORTAL_RATE_IP_PER_MINUTE": "2", "PORTAL_RATE_TOKEN_PER_HOUR": "100"})
    proxy = ("10.0.0.1", 1)
    for client in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
        for _ in range(2):
            assert call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": client}, client=proxy).status == 200
    assert call(app, "GET", f"/accept/{TOKEN}", {"x-real-ip": "203.0.113.1"}, client=proxy).status == 429


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_other_methods_are_405(method):
    app, *_ = make()
    assert call(app, method, f"/accept/{TOKEN}").status == 405
    assert call(app, "POST", f"/accept/{TOKEN}/document/1").status == 405
    assert call(app, method, "/health").status == 405


def test_request_target_and_head_limits():
    app, legal, *_ = make()
    assert call(app, "GET", "/accept/" + "A" * 3000).status == 414
    assert call(app, "GET", f"/accept/{TOKEN}", {"x-pad": "p" * 9000}).status == 431
    assert legal.resolves == []


def test_health_robots_and_not_now_say_nothing_else():
    app, *_ = make()
    h = call(app, "GET", "/health")
    assert h.status == 200 and h.body == b'{"status":"ok"}'      # AEGIS r4 N4-L3: nothing about the link key
    assert call(app, "GET", "/robots.txt").body == b"User-agent: *\nDisallow: /\n"
    n = call(app, "GET", "/accept/not-now")
    assert n.status == 200 and "Nothing was recorded" in n.text and "Your link stays valid until it expires." in n.text
    assert call(app, "GET", "/").status == 404


def test_head_answers_no_body():
    app, *_ = make()
    r = call(app, "HEAD", f"/accept/{TOKEN}")
    assert r.status == 200 and r.body == b"" and int(r.headers["content-length"]) > 1000


def test_a_page_never_logs_the_token_or_the_client(caplog):
    app, *_ = make()
    with caplog.at_level("INFO", logger="client_portal"):
        call(app, "GET", f"/accept/{TOKEN}", {"user-agent": "UA-SECRET-1"}, client=("203.0.113.99", 1))
        call(app, "GET", "/accept/" + mint_tok("Q"))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged and TOKEN not in logged and mint_tok("Q") not in logged
    assert "203.0.113.99" not in logged and "UA-SECRET-1" not in logged and "accept_get" in logged
