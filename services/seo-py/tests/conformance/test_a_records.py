"""A-26 (unbounded collections), A-29 (JSON-LD hostility), A-33 (over-limit numbers in outputs), A-32 (licence)."""

from __future__ import annotations

import json
import os
import re
import tracemalloc
from pathlib import Path

import pytest

from agents_run import codes, run3
from cert_web import ORG_LD, Web, page, site, sitemap
from common import check_ran

SRC = Path(__file__).resolve().parents[2] / "src"
REPO = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("kind", ["lastmods", "links", "headings", "jsonld_blocks", "canonicals", "locs"])
def test_A_26_every_collection_bounded(kind):
    web = site(Web())
    if kind == "lastmods":
        web.route("/sitemap.xml", sitemap([f"http://site.test/{i}" for i in range(100_000)], lastmod="2026-13-99"),
                  ctype="application/xml")
    elif kind == "links":
        web.route("/", page(body="".join(f"<a href='/l{i}'>x</a>" for i in range(5000)), ld=ORG_LD))
    elif kind == "headings":
        web.route("/", page(body="".join("<h3>x</h3><h5>y</h5>" for _ in range(400)), ld=ORG_LD))
    elif kind == "jsonld_blocks":
        web.route("/", page(head="".join("<script type='application/ld+json'>{bad</script>" for _ in range(60)),
                            ld=ORG_LD))
    elif kind == "canonicals":
        web.route("/", page(canonical=None, head="".join(f"<link rel='canonical' href='/c{i}'>" for i in range(3000)),
                            ld=ORG_LD))
    else:
        web.route("/sitemap.xml", sitemap(["relative/%d" % i for i in range(60_000)]), ctype="application/xml")
    tracemalloc.start()
    c, s, d, r, _, _ = run3(web)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak <= 96 * 2 ** 20, (kind, peak)
    for env in (s, d, r):
        for f in env["findings"]:
            for v in (f.get("detail") or {}).values():
                if isinstance(v, list):
                    assert len(v) <= 50, (kind, f["code"], len(v))
    check_ran("A-26", 6)


def test_A_29_jsonld_hostility_and_caps():
    blocks = ["{bad"] * 270 + ['{"@type":"Organization","name":"x","sameAs":["https://e.test"]}']
    head = "".join(f"<script type='application/ld+json'>{b}</script>" for b in blocks)
    web = site(Web())
    web.route("/", page(head=head, ld=None))
    c, _, _, r, _, _ = run3(web)
    summ = c.osei.summary()
    assert "STRUCTURED_DATA_INVALID_JSON" in codes(r)
    assert len(summ["quarantined"]) + summ["quarantined_dropped"] >= 30
    for ld in ('\ufeff{"@type":"Organization","name":"x"}', '{"@type":"Organization","name":"a","name":"b"}',
               '{"@type":"Organization","name":NaN}', '{"@type":"Organization","n":' + "9" * 5000 + "}"):
        web = site(Web())
        web.route("/", page(ld=ld))
        run3(web)
    for n in (262_144, 262_145):
        web = site(Web())
        ld = '{"@type":"Organization","name":"' + "a" * (n - 34) + '"}'
        ld = ld + " " * (n - len(ld))
        web.route("/", page(ld=ld))
        _, _, _, r, _, _ = run3(web, max_bytes=2 * 1024 * 1024)
        assert ("STRUCTURED_DATA_BLOCK_TOO_LARGE" in codes(r)) is (n > 262_144), n
    check_ran("A-29", 20)


def test_A_33_big_numbers_are_accepted_by_the_sink_guard():
    import sink_guard
    for v in (10 ** 18, -1, 2 ** 63 - 1, 0, 2 ** 64, 1.5e300):
        rec = {"status": v, "count": v, "bytes": v, "hll": [v] * 4}
        assert sink_guard.violations("audit_completed", rec) == [], v
    check_ran("A-33", 10)


# A-32: permissive licences and the owner-approved list (CC-BY-4.0, Unicode-3.0, MIT-CMU, CNRI-Python, PDFium);
# Public-Domain = self-authored data
ALLOWED_LICENCES = {"CC-BY-4.0", "Unicode-3.0", "MIT-CMU", "CNRI-Python", "PDFium", "MIT", "BSD-2-Clause",
                    "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "ISC", "Public-Domain"}


def test_A_32_no_forbidden_data_in_the_build_and_every_source_noticed():
    banned = ["path_words.txt", "schema_types.py"]
    for b in banned:
        assert not list(SRC.rglob(b)), b
    for p in SRC.rglob("*"):
        if p.is_file() and p.suffix in (".txt", ".dat", ".json", ".py"):
            head = p.read_bytes()[:4000].decode("utf-8", "replace").lower()
            assert "wordfreq" not in head and "nltk" not in head and "cc by-sa" not in head, p
    notices = (REPO / "services" / "seo-py" / "THIRD_PARTY_NOTICES.md")
    assert notices.exists()
    text = notices.read_text(encoding="utf-8")
    for data in [p for p in SRC.rglob("*") if p.is_file() and p.suffix in (".txt", ".dat", ".json")]:
        assert data.name in text, f"{data.name} has no THIRD_PARTY_NOTICES entry"
    for m in re.finditer(r"- Licen[cs]e:\s*([A-Za-z0-9.\-]+)", text):
        assert m.group(1) in ALLOWED_LICENCES, m.group(1)
    check_ran("A-32", 1)
    assert os and json
