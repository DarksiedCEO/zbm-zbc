"""SPEC section 3 and 0.1: the codes primitive (B-CD-01..06), D-01 H / W / R and O-TPL."""

from __future__ import annotations

import re
import time

import pytest

import vault
from common import check_ran, rng, seed_note, seeds
from gen import plants
from oracles import templates as tpl
from primitives import codes
from primitives import safe_url as su

CODE = re.compile(r"\{c:([a-z]+):([0-9a-f]{16})\}")


def _coder(tenant="zbm", key=None):
    return vault.Coder(key or vault.ephemeral_vault_key(), tenant)


def test_B_CD_01_code_shape_determinism_and_kinds():
    c = _coder()
    with codes.coding(c, own=("site.test",)):
        a = codes.code("seg", "hello")
        assert CODE.fullmatch(a) and a.startswith("{c:seg:")
        assert all(codes.code("seg", "hello") == a for _ in range(1000))
        assert codes.code("text", "hello") != a                  # the kind is part of the HMAC input
    assert set(codes.KINDS) >= {"host", "seg", "path", "query", "rule", "text", "jsonld", "type", "lang", "date",
                                "ctype", "ns", "root", "ua", "ref", "entity"}
    with pytest.raises(ValueError):
        with codes.coding(c, own=()):
            codes.code("ip", "x")                                 # D-04: an IP is never stored, not even coded


def test_B_CD_01_two_tenants_never_share_a_code():
    key = vault.ephemeral_vault_key()
    a, b = _coder("zbm", key), _coder("acme", key)
    r = rng(7, "cd01")
    vals = [plants.uniq(r) + str(i) for i in range(10_000)]
    ca = {a.code("seg", v) for v in vals}
    cb = {b.code("seg", v) for v in vals}
    assert len(ca) == len(cb) == 10_000 and not ca & cb


@pytest.mark.lead_gate
def test_B_CD_01_A_22_no_collision_in_one_million_values():
    c = _coder()
    t0 = time.perf_counter()
    seen = {c.code("seg", "v%07d" % i) for i in range(1_000_000)}
    per = (time.perf_counter() - t0) / 1_000_000
    assert len(seen) == 1_000_000
    assert per <= 2e-6 * 4, per                                  # A-22: <= 2 us a code on the reference box (R-13)


def test_B_CD_01_the_key_never_leaves_the_vault_module():
    c = _coder()
    assert "redacted" in repr(c) and not hasattr(c, "__dict__")
    assert not any(isinstance(getattr(c, s, None), (bytes, bytearray)) for s in dir(c) if not s.startswith("_"))
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(c)


def test_B_CD_02_key_id_is_the_vault_key_id():
    import base64
    import os
    key = vault.parse_vault_key(base64.urlsafe_b64encode(os.urandom(32)).decode(), "k-one")
    assert _coder(key=key).key_id == "k-one"


OWN = ("site.test",)


def test_B_CD_03_stored_url_form():
    with codes.coding(_coder(), own=OWN):
        assert su.safe_url("http://site.test/") == "http://site.test/"
        assert su.safe_url("https://www.site.test/robots.txt") == "https://www.site.test/robots.txt"
        assert su.safe_url("HTTP://Site.Test./") == "http://site.test/"
        assert CODE.fullmatch(su.safe_url("http://blog.site.test/").split("//", 1)[1].rstrip("/"))   # Q-02
        assert CODE.fullmatch(su.safe_url("http://other.test/").split("//", 1)[1].rstrip("/"))
        assert su.safe_url("http://site.test:8080/").startswith("http://site.test:8080/")
        assert su.safe_url("http://site.test:80/") == "http://site.test/"
        for scheme in ("javascript", "data", "file", "ftp", "mailto", "tel"):
            assert su.safe_url(f"{scheme}:something-{plants.uniq(rng(1, scheme))}") == scheme + ":"
        assert su.safe_url("gopher://x/") == "other:"
        m = plants.uniq(rng(2, "q"))
        s = su.safe_url(f"http://{m}@site.test/a?{m}=1#{m}")
        assert m not in s and "?" not in s and "#" not in s and "@" not in s
        assert su.safe_url("http://[::1/") in ("{unreadable}", "http://{host}")


def _gen_path(r) -> str:
    parts = []
    for _ in range(r.randint(1, 5)):
        k = r.random()
        if k < 0.1:
            parts.append(r.choice(sorted(tpl.WELL_KNOWN)))
        elif k < 0.15:
            parts.append(r.choice(("post", "page", "product")) + "-sitemap" + r.choice(("", "2", "17")) + ".xml")
        elif k < 0.25:
            parts.append(str(r.randint(0, 10 ** r.randint(1, 9))))
        elif k < 0.3:
            parts.append(plants.shape_value("uuid", r))
        elif k < 0.35:
            parts.append(plants.uniq(r) + r.choice(("@", "(at)", "[at]")) + "x.test")
        elif k < 0.45:
            parts.append(plants.uniq(r) + "." + r.choice(("pdf", "jpg", "css", "docx", "zzz")))
        elif k < 0.5:
            parts.append(plants.shape_value(r.choice(("hex32", "base64_40", "key_prefix")), r).replace("/", "x"))
        elif k < 0.55:
            parts.append(plants.uniq(r) + ";" + r.choice(("a=1", "jsessionid=9")))
        elif k < 0.6:
            parts.append("k=" + str(r.randint(0, 99)))
        elif k < 0.65:
            parts.append("")
        else:
            parts.append(plants.uniq(r)[:r.randint(2, 10)] + r.choice(("", "-" + plants.uniq(r)[:4], "2")))
    return "/" + "/".join(parts)


@pytest.mark.parametrize("seed", seeds())
def test_B_CD_04_path_shapes_equal_the_reference_O_TPL(seed):
    r = rng(seed, "cd04")
    ran = 0
    with codes.coding(_coder(), own=OWN):
        for i in range(3000):
            p = _gen_path(r)
            want = tpl.template(p)
            got = su.safe_path(p)
            got_n = CODE.sub(lambda m: "{c:" + m.group(1) + "}", got)
            if len(want) <= 300:
                assert got_n == want, (p, got, want, seed_note(seed, i))
            ran += 1
    check_ran("B-CD-04", ran)


def test_B_CD_04_every_well_known_name_readable_in_every_position():
    names = sorted(tpl.WELL_KNOWN) + ["post-sitemap.xml", "page-sitemap2.xml", "sitemap-posts-1.xml",
                                      "sitemap_index.xml.gz", "wp-sitemap-posts-post-1.xml", "sitemap-12.xml"]
    with codes.coding(_coder(), own=OWN):
        for n in names:
            for p in (f"/{n}", f"/x/{n}", f"/{n}/y", f"/{n.upper()}"):
                assert n.lower() in su.safe_path(p).lower(), (p, su.safe_path(p))
        for bad in ("/sitemap-" + plants.uniq(rng(3, "s")) + ".xml", "/sitemapx.xml"):
            assert "sitemap" not in su.safe_path(bad).replace("{file}.xml", "")


def test_B_CD_04_at_most_300_characters():
    with codes.coding(_coder(), own=OWN):
        p = "/" + "/".join(plants.uniq(rng(4, str(i))) for i in range(40))
        assert len(su.safe_path(p)) <= 300


def test_B_CD_05_text_is_coded_with_length_kept():
    with codes.coding(_coder(), own=OWN):
        t = codes.code("text", "  Some   title  ")
        assert t == codes.code("text", "Some title")              # whitespace collapsed before coding
        assert codes.measure_text("  Some   title ") == {"length": 10, "empty": False}


LOOKS = ["site.test.evil.test", "evilsite.test", "xn--site-8ya.test", "site.tes", "www-site.test",
         "evil.test%2esite.test", "site.test.", "SITE.TEST",
         "s\u0456te.test", "wwww.site.test", "site-test.test", "test", "www.site.test.", "a.b.site.test"]


def _ref_same_site(host: str, own) -> bool:
    h = host.lower().rstrip(".")
    if ":" in h or "@" in h or "%" in h:
        return False
    try:
        h = h.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return any(h == d or h == "www." + d or h.endswith("." + d) for d in own)


def test_B_CD_06_same_site_and_lookalikes():
    from cert_web import Web, ctx
    c = ctx(Web())
    r = rng(5, "cd06")
    hosts = LOOKS + ["x%d.site.test" % i for i in range(60)] + [plants.uniq(r) + ".test" for _ in range(60)] + \
        [plants.uniq(r) + "site.test" for _ in range(64)] + ["site.test", "www.site.test"]
    for h in hosts:
        assert bool(c.same_site(h)) is _ref_same_site(h, OWN), h
    check_ran("B-CD-06", len(hosts))
    with codes.coding(_coder(), own=OWN):
        for h in LOOKS:
            if not _ref_same_site(h, OWN) or h.lower().rstrip(".") not in OWN + ("www.site.test",):
                s = su.safe_url(f"http://{h}/")
                assert "site.test/" not in s.replace("://site.test/", "") or h.lower().rstrip(".") in \
                    ("site.test", "www.site.test"), (h, s)


def test_B_CD_01_the_kind_is_part_of_the_hmac_input():
    with codes.coding(_coder(), own=OWN):
        a = CODE.fullmatch(codes.code("seg", "same")).group(2)
        b = CODE.fullmatch(codes.code("text", "same")).group(2)
        c = CODE.fullmatch(codes.code("host", "same")).group(2)
    assert len({a, b, c}) == 3


def test_B_CD_01_evidence_values_are_bounded():
    with codes.coding(_coder(), own=OWN) as ev:
        short = codes.code("text", "x" * 1024)
        long = codes.code("text", "y" * 1025)
    assert ev.values.get(short) == "x" * 1024 and long not in ev.values and ev.truncated == 1


def test_B_CD_04_names_like_but_not_well_known_are_not_readable():
    with codes.coding(_coder(), own=OWN):
        for bad in ("/post-sitemap.xmlzz", "/sitemap.xml.bak", "/robots.txtx", "/xsitemap.xml"):
            assert "sitemap" not in su.safe_path(bad) and "robots" not in su.safe_path(bad), bad
