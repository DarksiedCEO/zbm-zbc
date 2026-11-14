"""
The O-01 corpus: realistic ordinary sites built at run time, each with its ground truth computed from the SPEC rules
and the independent oracles (never from src/). Languages: words from generated syllables over each script's letter
range (Unicode blocks; no word list bundled). CMS shapes: the robots.txt defaults the CMSes document.
"""

from __future__ import annotations

import json
import random

from cert_web import Web

LANGS = {  # tag -> (first, last) code point of letters used for generated words
    "en": (0x61, 0x7A), "de": (0x61, 0x7A), "fr": (0x61, 0x7A), "es": (0x61, 0x7A), "pt": (0x61, 0x7A),
    "it": (0x61, 0x7A), "pl": (0x61, 0x7A), "tr": (0x61, 0x7A), "nl": (0x61, 0x7A), "sv": (0x61, 0x7A),
    "id": (0x61, 0x7A), "cs": (0x61, 0x7A), "ru": (0x430, 0x44F), "uk": (0x430, 0x44F), "ja": (0x3041, 0x3093),
    "zh": (0x4E00, 0x4FFF), "ko": (0xAC00, 0xAD00), "ar": (0x627, 0x64A), "he": (0x5D0, 0x5EA),
    "hi": (0x915, 0x939), "th": (0xE01, 0xE2E)}
CMS = {
    "wordpress": "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n",
    "shopify": "User-agent: *\nDisallow: /admin\nDisallow: /cart\nDisallow: /orders\nDisallow: /checkouts/\n"
               "Disallow: /*?*oseid=*\nDisallow: /search\n",
    "squarespace": "User-agent: *\nDisallow: /config\nDisallow: /search\nDisallow: /account$\n",
    "wix": "User-agent: *\nDisallow: /*?lightbox=\n",
    "nextjs": "User-agent: *\nAllow: /\n",
    "magento": "User-agent: *\nDisallow: /checkout/\nDisallow: /customer/\nDisallow: /catalogsearch/\n",
    "static": "",
}
SITEMAP_NAMES = {"wordpress": "/wp-sitemap.xml", "shopify": "/sitemap.xml", "squarespace": "/sitemap.xml",
                 "wix": "/sitemap.xml", "nextjs": "/sitemap.xml", "magento": "/sitemap.xml",
                 "static": "/sitemap.xml"}
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def word(r: random.Random, lang: str, n=None) -> str:
    a, b = LANGS[lang]
    return "".join(chr(r.randint(a, b)) for _ in range(n or r.randint(3, 8)))


def text(r: random.Random, lang: str, words: int) -> str:
    sep = "" if lang in ("ja", "zh", "th") else " "
    return sep.join(word(r, lang) for _ in range(words))


def _page(r, lang, title, canonical, h1, ld, desc=True) -> str:
    body = "".join(f"<p>{text(r, lang, 40)}</p>" for _ in range(4))
    d = f"<meta name='description' content='{text(r, lang, 12)}'>" if desc else ""
    return (f"<!doctype html><html lang='{lang}'><head><meta charset='utf-8'><title>{title}</title>{d}"
            f"<link rel='canonical' href='{canonical}'><script type='application/ld+json'>{ld}</script></head>"
            f"<body><h1>{h1}</h1><h2>{text(r, lang, 3)}</h2>{body}<h2>{text(r, lang, 2)}</h2>"
            f"<h3>{text(r, lang, 2)}</h3></body></html>")


def build(seed_r: random.Random, idx: int) -> dict:
    """One ordinary site: {web, paths, truth: list of (agent, code, severity, data_class, decision)}."""
    r = seed_r
    lang = list(LANGS)[idx % len(LANGS)]
    cms = list(CMS)[idx % len(CMS)]
    web = Web()
    sm_path = SITEMAP_NAMES[cms]
    robots = CMS[cms] + f"Sitemap: http://site.test{sm_path}\n" if CMS[cms] else f"Sitemap: http://site.test{sm_path}\n"
    web.route("/robots.txt", robots, ctype="text/plain")
    slugs = [word(r, "en", 6) + "-" + word(r, "en", 5) for _ in range(r.randint(2, 6))]
    paths = ["/"] + [f"/blog/{s}" for s in slugs[:2]]
    org = json.dumps({"@context": "https://schema.org", "@type": "Organization", "name": text(r, lang, 2),
                      "url": "http://site.test/", "logo": "http://site.test/logo.png",
                      "sameAs": ["https://example.org/x"]})
    for p in paths:
        title = text(r, "en", 3)[:40].strip().ljust(12, "x")
        web.route(p, _page(r, lang, title, "http://site.test" + p, text(r, lang, 3), org))
    locs = ["http://site.test" + p for p in paths] + [f"http://site.test/blog/{s}" for s in slugs[2:]]
    lms = [f"2025-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}" for _ in locs]
    if len(set(lms)) == 1:
        lms[0] = "2024-01-01"
    urls = "".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in zip(locs, lms))
    web.route(sm_path, f"<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='{NS}'>{urls}</urlset>",
              ctype="application/xml")
    llms = r.random() < 0.6
    if llms:
        web.route("/llms.txt", f"# {text(r, 'en', 2)}\n\n> {text(r, 'en', 6)}\n\n## Docs\n"
                               f"- [Home](http://site.test/): {text(r, 'en', 3)}\n", ctype="text/plain")
    truth = []
    if not llms:
        truth.append(("delia", "LLMS_TXT_ABSENT", "info", "measured", "WATCH"))
    return {"web": web, "paths": paths, "truth": truth, "lang": lang, "cms": cms}
