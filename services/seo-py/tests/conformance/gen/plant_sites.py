"""
A-18: a site whose every input surface carries planted values (gen/plants.py shapes). ``build(r, n)`` returns the
web, the audit paths, log lines and the plants as (agent, surface, value, coded) where ``coded`` says the value is on
a surface the stored report references by code (the D-06 positive control: the owner display must resolve it).
"""

from __future__ import annotations

import json
import random

from cert_web import Web
from gen import plants as P

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
# surfaces a stored report always names (by code): the owner display must resolve every one of them (D-06)
ALWAYS_REFERENCED = frozenset({"canonical_other_host", "canonical_other_path",
                               "robots_sitemap_host", "root_name", "namespace", "audit_path"})


def _slug(v: str) -> str:
    """A path-safe spelling of a planted value (what a site would put in a URL)."""
    return "".join(c if c.isalnum() or c in "-._~@" else "-" for c in v).strip("-") or "x"


def _label(v: str) -> str:
    s = "".join(c for c in v.lower() if c.isalnum())[:40]
    return s or "x"


def build(r: random.Random, n: int) -> dict:
    web = Web()
    pl = []

    def plant(agent, surface, coded, shape=None):
        v = P.shape_value(shape or r.choice(P.SHAPES), r)
        pl.append((agent, surface, v, surface in ALWAYS_REFERENCED))
        return v

    # robots.txt: comment, rule patterns, Sitemap line (same host path; other host label)
    rules, comments, sms = [], [], []
    for _ in range(n):
        comments.append("# " + plant("selene", "robots_comment", False))
        rules.append("Disallow: /" + _slug(plant("selene", "robots_rule", True)))
    sm_own = "/sm-" + _slug(plant("delia", "robots_sitemap_path", True)) + ".xml"
    other = _label(plant("delia", "robots_sitemap_host", True, "given_name")) + ".test"
    sms = [f"Sitemap: http://site.test{sm_own}", f"Sitemap: http://{other}/s.xml",
           "Sitemap: http://site.test/sitemap.xml"]
    web.route("/robots.txt", "\n".join(comments + ["User-agent: *"] + rules + ["Allow: /"] + sms) + "\n",
              ctype="text/plain")
    # pages
    paths = ["/"]
    for i in range(3):
        paths.append("/" + _slug(plant("selene", "audit_path", True, r.choice(("given_name", "surname",
                                                                                "first_last_slug")))))
    for p in paths:
        title = plant("roman", "title", False)
        hs = "".join(f"<h{k}>{plant('roman', 'h' + str(k), False)}</h{k}>" for k in range(1, 7))
        desc = plant("roman", "meta_description", False)
        lang = plant("roman", "html_lang", True, "given_name")
        canon_host = _label(plant("selene", "canonical_other_host", True, r.choice(("given_name", "surname"))))
        canon_path = _slug(plant("selene", "canonical_other_path", True))
        typ = plant("roman", "jsonld_type", True, "given_name")
        org = {"@context": "https://schema.org", "@type": "Organization",
               "name": plant("entity_check", "org_name", True), "telephone": plant("entity_check", "telephone",
                                                                                  True, "phone_nanp"),
               "address": {"@type": "PostalAddress", "streetAddress": plant("entity_check", "street", True)},
               "sameAs": ["https://" + _label(plant("entity_check", "sameas", False)) + ".test/"],
               "description": plant("roman", "jsonld_string", False)}
        ld = json.dumps([org, {"@context": "https://schema.org", "@type": typ}])
        xr = _label(plant("selene", "x_robots_bot", False, "given_name")) + ": noindex"
        meta_name = _label(plant("selene", "meta_robots_name", False, "given_name"))
        phone_text = plant("entity_check", "homepage_phone_text", False, "phone_e164")
        body = (f"<!doctype html><html lang='{lang}'><head><title>{title}</title>"
                f"<meta name='description' content='{desc}'><meta name='{meta_name}' content='noindex'>"
                f"<link rel='canonical' href='http://{canon_host}.test/{canon_path}'>"
                f"<script type='application/ld+json'>{ld}</script></head><body>{hs}<p>{phone_text}</p>"
                + "<p>word </p>" * 120 + "</body></html>")
        web.route(p, body, x_robots_tag=xr)
    # a redirect whose Location carries a host label, a path and a query
    rhost = _label(plant("selene", "redirect_host", True, "given_name"))
    web.route("/go", "", status=302,
              location=f"http://{rhost}.test/" + _slug(plant("selene", "redirect_path", True)) + "?q=" +
              _slug(plant("selene", "redirect_query", False)))
    paths.append("/go")
    # a non-HTML page whose Content-Type names a planted media type (rv-wave1v2 #7 c05: a media type off the closed
    # list is stored coded, never because it starts text/ or application/)
    for top in ("application", "text"):
        web.route("/doc-" + top, "x", ctype=f"{top}/x-" + _label(plant("selene", "content_type", False, "given_name")))
        paths.append("/doc-" + top)
    # sitemaps: locs (own host paths, other host labels, queries), lastmod, index child, namespace, root
    locs, lms = [], []
    for _ in range(n):
        locs.append("http://site.test/" + _slug(plant("delia", "sitemap_loc_path", True)) + "?" +
                    _slug(plant("delia", "sitemap_loc_query", False)))
        locs.append("http://" + _label(plant("delia", "sitemap_loc_host", True, "surname")) + ".test/x")
        lms.append(plant("delia", "sitemap_lastmod", True, r.choice(("dob_date", "given_name"))))
    urls = "".join(f"<url><loc>{u}</loc><lastmod>{lms[i // 2]}</lastmod></url>" for i, u in enumerate(locs))
    web.route(sm_own, f"<?xml version='1.0'?><sitemapindex xmlns='{NS}'><sitemap><loc>http://site.test/c-"
                      f"{_slug(plant('delia', 'index_child', True))}.xml</loc></sitemap></sitemapindex>",
              ctype="application/xml")
    kid = [x for x in pl if x[1] == "index_child"][-1][2]
    ns = "http://" + _label(plant("delia", "namespace", True, "given_name")) + ".test/ns"
    web.route(f"/c-{_slug(kid)}.xml", f"<?xml version='1.0'?><urlset xmlns='{ns}'>{urls}</urlset>",
              ctype="application/xml")
    root = _label(plant("delia", "root_name", True, "given_name"))
    web.route("/sitemap.xml", f"<?xml version='1.0'?><{root}><x/></{root}>", ctype="application/xml")
    # llms.txt text and links
    web.route("/llms.txt", f"# {plant('delia', 'llms_text', False)}\n\n## {plant('delia', 'llms_text', False)}\n"
                           f"- [a](http://{_label(plant('delia', 'llms_link', False, 'given_name'))}.test/)\n",
              ctype="text/plain")
    # log lines: path, query, User-Agent, referer, IP
    lines = []
    for _ in range(n):
        ip = plant("logs", "log_ip", False, "ipv4")
        path = "/" + _slug(plant("logs", "log_path", True)) + "?" + _slug(plant("logs", "log_query", False))
        ua = "Googlebot/2.1 " + plant("logs", "log_user_agent", False, "pronounceable")
        ref = "http://" + _label(plant("logs", "log_referer", False, "given_name")) + ".test/"
        lines.append(f'{ip} - - [10/Oct/2026:13:55:36 -0700] "GET {path} HTTP/1.1" 200 1 "{ref}" "{ua}"')
    return {"web": web, "paths": paths, "lines": lines, "plants": pl}
