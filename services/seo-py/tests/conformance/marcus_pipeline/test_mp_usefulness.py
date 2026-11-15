"""marcus-pipeline-spec/1 A-MP-02 (hostile URLs) and O-MP-1..3 (over-tightening and usefulness)."""

from __future__ import annotations

import json
import random

import pytest

import url_corpus
from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import all_rows, build, watch

C1 = "CC-MAIN-2026-40"


def _hostile_urls(n, seed=2):
    rng = random.Random(seed)
    schemes = ["http", "https", "HTTP", "ftp", "javascript", "data", "file", "ws", "wss", "chrome", "about", "blob",
               "view-source", "jar", "intent", "gopher", "ldap", "dict", "", "http"]
    hosts = ["127.0.0.1", "[::1]", "0x7f.1", "2130706433", "10.0.0.1", "[::ffff:7f00:1]", "169.254.169.254",
             "\u0430cme.test", "xn--cme-5cd.test", "acme.test.evil.test", "ac\u200dme.test", "ACME.TEST.",
             "acme.test:99999", "acme.test:0", "a..b.test", "-x-.test", "\uff41cme.test", "acme.te\u0455t"]
    paths = ["/../../../etc/passwd", "/a/./b/../c", "/%2e%2e/%2e%2e/x", "/%00x", "/%0d%0aSet-Cookie:x", "/\u202eabc",
             "/a%E2%80%AEb", "/" + "a" * 65536, "/%ZZ", "/\x7f", "/a b", "/\\x", ""]
    out = []
    for _ in range(n):
        s = rng.choice(schemes)
        u = f"{s}://{rng.choice(hosts)}{rng.choice(paths)}"
        if rng.random() < 0.2:
            u += "?" + rng.choice(["a=%00", "x=" + "q" * 70000, "utm_source=z", "b=\u202e"])
        if rng.random() < 0.1:
            u = rng.choice(["//acme.test/x", "\\\\acme.test\\x", "https:acme.test", "https:///x", " https://acme.test",
                            "https://acme.test\n/x"])
        out.append(u)
    return out


def _check_hostile(n):
    from marcus import link_extractor
    urls = _hostile_urls(n)
    links = [{"path": "A@/href", "url": u} for u in urls]
    recs = [gen_cc.line({"Envelope": {"WARC-Header-Metadata": {"WARC-Type": "response",
                                                                "WARC-Target-URI": "https://src.example/p"},
                                      "Payload-Metadata": {"HTTP-Response-Metadata": {"HTML-Metadata": {
                                          "Links": links[i:i + 100]}}}}}) for i in range(0, len(links), 100)]
    c = link_extractor.Counts()
    rows = list(link_extractor.extract_lines(iter(recs), c))
    dropped = sum(v for k, v in c.items() if k.startswith(("links_scheme:", "urls_", "links_dropped")))
    assert len(rows) + dropped == len(urls) - c.get("links_over_cap", 0), (len(rows), dropped)
    for r in rows:
        d = r["dst"]
        assert d.startswith(("http://", "https://")) and len(d) <= 2048
        assert d.isascii() and not any(ch in d for ch in " \x00\r\n\t\\") and "/../" not in d and "/./" not in d
        host = d.split("/")[2]
        assert host == host.lower() and "@" not in host and not host.endswith(".")
        assert "%e2%80%ae" not in d.lower() and "\u202e" not in d


def test_A_MP_02_hostile_urls_normalised_or_dropped_and_counted_never_fetched(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("lookup")))
    _check_hostile(2000)


@pytest.mark.lead_gate
def test_A_MP_02_lead_20000():
    _check_hostile(20_000)


def _corpus_check(mp, n_files, pages):
    files, truth = gen_cc.corpus(77, n_files, pages)
    gen_cc.write_crawl(mp.fx, C1, files)
    watch(mp)
    j = build(mp)
    kept = all_rows(mp, j["snapshot_id"])
    want = [r for r in truth if r["dst"].split("/")[2].split(":")[0].endswith(("acme.test", "globex.test"))]
    return j, kept, want, truth


def test_O_MP_1_realistic_corpus_0_lost_well_formed_links(mp):
    from marcus import link_extractor
    files, truth = gen_cc.corpus(77, 20, 25)
    lines = [gen_cc.line(p.record()) for f in files for p in f]
    rows = list(link_extractor.extract_lines(iter(lines), link_extractor.Counts()))
    assert rows == truth                                                  # every well-formed link, exactly
    j, kept, want, _ = _corpus_check(mp, 20, 25)
    key = lambda r: json.dumps(r, sort_keys=True)                         # noqa: E731
    assert sorted(map(key, kept)) == sorted(map(key, want))


@pytest.mark.lead_gate
def test_O_MP_1_lead_200_files(mp):
    j, kept, want, _ = _corpus_check(mp, 200, 25)
    assert len(kept) == len(want)
    key = lambda r: json.dumps(r, sort_keys=True)                         # noqa: E731
    assert sorted(map(key, kept)) == sorted(map(key, want))               # exact rows, as the fast test (MR2-F4)


def test_O_MP_2_ordinary_urls_keep_every_segment_core_keeps():
    """Differential against the core lane (``secret_guard.safe_url``) on core-spec/1's O-4 U corpus
    (tests/url_corpus.py): a segment differs only where the core lane or ``prompt_guard.personal_data`` read it."""
    from agents import prompt_guard, secret_guard
    from marcus import link_extractor
    unexplained, explained = [], 0
    for u in url_corpus.corpus():
        norm, _ = link_extractor.normalise(u)
        if norm is None:
            continue
        ours = link_extractor.scrub_url(norm)
        core = secret_guard.safe_url(norm)
        if ours is None:
            assert secret_guard.inspect_url(norm)["host_kind"] is not None or prompt_guard.personal_data(
                secret_guard.inspect_url(norm)["host"]) is not None
            continue
        a = ours.split("?")[0].split("/")[3:]
        b = core.split("?")[0].split("/")[3:]
        if len(a) != len(b):
            unexplained.append(("length", u[:40]))
            continue
        for x, y in zip(a, b):
            if x == y:
                continue
            if x == "{withheld}" and y not in ("{secret}", "{withheld}") and prompt_guard.personal_data(
                    __import__("urllib.parse", fromlist=["unquote"]).unquote(y)) is not None:
                explained += 1
                continue
            unexplained.append((x[:12], y[:12]))
    assert unexplained == [], unexplained[:10]


def test_O_MP_3_withheld_anchors_at_most_half_a_percent_on_ordinary_anchors():
    from marcus import link_extractor
    rng = random.Random(33)
    files, truth = gen_cc.corpus(78, 20, 25)
    anchors = [ln.anchor for f in files for p in f for ln in p.links] + [
        " ".join(rng.choice(gen_cc.WORDS) for _ in range(rng.randint(1, 6))) for _ in range(5000)] + [
        f"Top {rng.randint(3, 20)} {rng.choice(gen_cc.WORDS)} tips for {rng.choice(('2025', '2026'))}"
        for _ in range(500)]
    withheld = [a for a in anchors if link_extractor.scrub_anchor(a) == "{withheld}"]
    rate = len(withheld) / len(anchors)
    print(f"O-MP-3 measured: {len(withheld)} / {len(anchors)} = {rate:.4%}; rules: "
          f"{sorted({link_extractor.anchor_rule(a) for a in withheld})}")
    assert rate <= 0.005, (rate, withheld[:10])
