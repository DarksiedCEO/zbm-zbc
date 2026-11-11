"""Sink job D: the universal canary test.

EVERY route of the FastAPI app is discovered from ``app.routes``; every string field a caller can send to it (path
parameters, query parameters, the headers the service reads, and the JSON body's string fields at any depth, from the
request model's schema) gets a unique canary value, built at run time, in six families (prose with spaces,
e-mail-shaped, key-prefix-shaped, a 16-digit Luhn number, SSN-shaped, a short no-space token) and three disguises
(raw, base64, percent-encoded), in an otherwise valid call made as the owning caller. The flows that persist run with
canaries in the operator's text AND in what engines and crawls return (answers, citation titles and hints, cited
URLs, page titles, canonical and redirect URLs, robots.txt, sitemap paths, uploaded log lines, Search Console rows).

Then everything a non-owner could read is scanned for every canary in every form (raw, lower / upper case, hex,
base64 and base64url at three alignments, percent-encoded, JSON-escaped, digits only): every file under the data
directory (the log, pending and discarded lines, both vault files, quarantine files, markers, a real log file written
by a logging handler), the ledger payloads, the logs captured at DEBUG for all loggers, every error body, the
responses to another tenant and to the non-owner callers, /health and /status.

Expected: 0 hits, EXCEPT the documented known limit: a short no-space token (no whitespace; not key-, e-mail- or
9+-digit-shaped) accepted into an IDENTIFIER field is a machine value by definition, so the log names it. The test
asserts exactly which (route, field) pairs accept it (``KNOWN_LIMIT``) and that nothing else does.

A route the test cannot build a valid call for must be in ``EXEMPT`` with its reason; a new route with neither a
builder nor an exemption fails. Failures print route names, field names, family names and counts: never a value."""

from __future__ import annotations

import base64
import collections
import json
import logging
import os
import random
import string
import urllib.parse

import pytest

import api
import config as config_mod
import helpers
import log_sink
import sink_guard as sg
import test_sink_audits as sa
from fixture_server import home_html
from gsc_fakes import FakeGoogle, write_key_file
from ports import gsc_from_settings
from helpers import base_env, invoice_id, rid
from ports import Ports, ProviderAnswer

DOMAIN = sa.DOMAIN
# review 2b26d980 NIT: four more families (an e-mail address written without "@", digits formatted with
# separators, a 32-hex key, a JSON number); each is planted raw (and syntax-fitted), not in the three disguises
EXTRA_FAMILIES = ("atless", "sepdigits", "hex", "number")
FAMILIES = ("prose", "email", "key", "luhn", "ssn", "token") + EXTRA_FAMILIES
# a family whose value is a machine value by definition wherever no route rule refuses it: a short token (the
# known limit) and 32 hex characters (the guard's ``hash`` class; the identifier fields refuse it themselves).
# Review 98b76cde MUST 1: the ``atless`` value (``first.last.at.domain.com``) is a HOST NAME with an ``at`` label,
# a machine value as at 2b26d980: the guard takes it, the identifier fields still refuse it as an e-mail form, and
# a DOMAIN field takes it (``HOST_LIMIT``: exactly where it may be found afterwards)
MACHINE_FAMILIES = ("token", "hex", "atless")
DISGUISES = ("raw", "base64", "percent")
HEADERS_READ = ("Authorization", api.CALLER_HEADER, api.TENANT_HEADER, api.FOUNDER_HEADER)
P = "/seo/v1"

# A route the test cannot build a valid call for: ``"METHOD path": "why"``. Empty: every route is covered.
EXEMPT: dict = {}

# The known limit (ADR 0051, sink job D): the (route, field) pairs where a short no-space token is accepted as an
# IDENTIFIER and therefore appears in the log / the ledger / a non-owner view as a machine value. Nothing else may.
KNOWN_LIMIT = frozenset((
    "POST /seo/v1/tenants body:tenant_id",
    "POST /seo/v1/tenants/{tid}/domains body:domains[]",
    "POST /seo/v1/tenants/{tid}/finance-client body:finance_client_id",
))


# Review 98b76cde MUST 1: the one (route, field) pair where the ``atless`` family (a host name with an ``at`` label)
# is accepted and therefore logged as a machine value: a tenant's DOMAIN. Nowhere else.
HOST_LIMIT = frozenset(("POST /seo/v1/tenants/{tid}/domains body:domains[]",))


# ================================================================================================ canaries

class Canaries:
    """Every planted value with where it was planted. ``forms`` are searched; hits report names only."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.values: dict = {}                             # underlying value -> (route, field, family, disguise)
        self.planted = 0

    def _tag(self, n: int = 12, alphabet: str = string.ascii_lowercase) -> str:
        return "".join(self.rng.choice(alphabet) for _ in range(n))

    def _luhn(self) -> str:
        digits = [self.rng.randrange(1, 10)] + [self.rng.randrange(10) for _ in range(14)]
        total = 0
        for i, d in enumerate(reversed(digits)):
            d = d * 2 if i % 2 == 0 else d
            total += d - 9 if d > 9 else d
        return "".join(map(str, digits)) + str((10 - total % 10) % 10)

    def make(self, family: str) -> str:
        if family == "prose":
            return f"canary {self._tag(7)} walked {self._tag(9)} home"
        if family == "email":
            return f"{self._tag(9)}" + "@" + f"{self._tag(8)}.example"
        if family == "key":
            return "sk" + "-" + self._tag(40, string.ascii_letters + string.digits)
        if family == "luhn":
            return self._luhn()
        if family == "ssn":
            return f"{self.rng.randrange(100, 665):03d}-{self.rng.randrange(10, 99):02d}-{self.rng.randrange(1000, 9999)}"
        if family == "token":
            return "zq" + self._tag(12)
        if family == "atless":
            return f"{self._tag(6)}.{self._tag(7)}.at.{self._tag(8)}.com"
        if family == "sepdigits":
            return f"{self.rng.randrange(200, 999)}.{self.rng.randrange(200, 999)}.{self.rng.randrange(1000, 9999)}"
        if family == "hex":
            return "a" + self._tag(30, "0123456789abcdef") + "7"
        if family == "number":
            return str(self.rng.randrange(10 ** 14, 10 ** 15))
        raise AssertionError(family)

    def new(self, route: str, field: str, family: str, disguise: str = "raw", as_json: bool = False):
        """A fresh canary of ``family`` registered for (route, field); returns the string to plant (``as_json``: a
        ``number`` canary as a JSON number, an int)."""
        value = self.make(family)
        while value in self.values:
            value = self.make(family)
        self.values[value] = (route, field, family, disguise)
        self.planted += 1
        if as_json and family == "number" and disguise == "raw":
            return int(value)
        if disguise == "base64":
            return base64.b64encode(value.encode()).decode()
        if disguise == "percent":
            return urllib.parse.quote(value, safe="")
        return value


def forms(v: str) -> set:
    """Every form a canary is searched in (each at least 8 characters)."""
    out = set()
    for x in {v, v.lower(), v.upper()}:
        b = x.encode("utf-8")
        out |= {x, b.hex(), b.hex().upper(), urllib.parse.quote(x, safe=""), urllib.parse.quote_plus(x),
                json.dumps(x)[1:-1]}
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            whole = enc(b).decode()
            out |= {whole, whole.rstrip("=")}
            for pad in (1, 2):
                e = enc(b"\x00" * pad + b).decode().rstrip("=")
                out.add(e[4:len(e) - 4])
    digits = "".join(c for c in v if c.isdigit())
    if len(digits) >= 9:
        out.add(digits)
    return {f for f in out if len(f) >= 8}


def scan(blobs: dict, canaries: Canaries) -> list:
    """[(sink name, route, field, family, disguise)] for every canary found in any form in any blob. One pass per
    blob: every needle is indexed by its first 8 characters."""
    index: dict = {}
    for value, meta in canaries.values.items():
        for f in forms(value):
            index.setdefault(f[:8], []).append((f, meta))
    hits = set()
    for sink, blob in blobs.items():
        for i in range(len(blob) - 7):
            cands = index.get(blob[i:i + 8])
            if cands:
                for f, meta in cands:
                    if blob.startswith(f, i):
                        hits.add((sink,) + meta)
    return sorted(hits)


# ================================================================================================ discovery

def discover(app) -> dict:
    """``{"METHOD path": {"path_params", "query_params", "model"}}`` for every route of the app."""
    from fastapi.routing import APIRoute
    out = {}
    for route in app.routes:
        assert isinstance(route, APIRoute), type(route).__name__       # no mounted app, no websocket, no static
        model = None
        names = {"path": set(), "query": set(), "header": set(), "body": set(), "cookie": set()}
        stack = [route.dependant]
        while stack:
            dep = stack.pop()
            stack.extend(dep.dependencies)
            for kind in names:
                names[kind].update(p.name for p in getattr(dep, kind + "_params"))
            call = dep.call
            if getattr(call, "__qualname__", "").endswith("body.<locals>.parse"):
                (model,) = [c.cell_contents for c in call.__closure__ if isinstance(c.cell_contents, type)]
        assert not names["cookie"], route.path
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out[f"{method} {route.path}"] = {
                "path_params": sorted(names["path"]), "query_params": sorted(names["query"]),
                "header_params": sorted(names["header"]), "body_any": bool(names["body"]), "model": model}
    return out


def schema_string_paths(model) -> set:
    """The string fields of a request model at any depth, as normalised paths (``fields[].value``)."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    out = set()

    def walk(node, path):
        if "$ref" in node:
            node = defs[node["$ref"].rsplit("/", 1)[1]]
        for alt in node.get("anyOf", []) + node.get("oneOf", []) + node.get("allOf", []):
            walk(alt, path)
        t = node.get("type")
        if t == "string" or "const" in node and isinstance(node["const"], str) or \
                ("enum" in node and all(isinstance(x, str) for x in node["enum"])):
            out.add(path)
        elif t == "array":
            walk(node.get("items", {}), path + "[]")
        elif t == "object" or "properties" in node:
            for k, sub in node.get("properties", {}).items():
                walk(sub, f"{path}.{k}" if path else k)
    walk(schema, "")
    return out


def instance_string_paths(body, path=()) -> list:
    """[(instance path, normalised path)] of every string leaf of a baseline body."""
    out = []
    if isinstance(body, str):
        norm = ""
        for p in path:
            norm = norm + "[]" if isinstance(p, int) else (f"{norm}.{p}" if norm else p)
        out.append((path, norm))
    elif isinstance(body, dict):
        for k, v in body.items():
            out += instance_string_paths(v, path + (k,))
    elif isinstance(body, list):
        for i, v in enumerate(body):
            out += instance_string_paths(v, path + (i,))
    return out


def with_value(body, path: tuple, value):
    """A deep copy of ``body`` with ``value`` at ``path``."""
    body = json.loads(json.dumps(body))
    cur = body
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    return body


# ================================================================================================ the world

class Engine:
    """A connected, unmetered answer engine whose every answer carries canaries (engine-originated text)."""

    connected, metered, accepts_guard, name, attempts = True, False, True, "openai", 1

    def __init__(self, canaries: Canaries):
        c = canaries
        q = urllib.parse.quote
        self.answers = []
        for fam in FAMILIES:
            text, title, hint, upath, uquery = (c.new("engine", f"answer_{k}", fam) for k in
                                                ("text", "title", "hint", "url_path", "url_query"))
            url1 = f"https://competitor.test/a/{q(upath, safe='')}/view"
            url2 = f"https://competitor.test/p?ref={q(uquery, safe='')}"
            a = ProviderAnswer("ANSWER", f"Z Best Media of Long Beach says {text} today.", [url1, url2,
                               "http://site.test/"], "m", "1", cite_from_text=False, provider="openai",
                               asked_at="2026-10-09T18:00:00Z")
            a.citation_titles = {url1: f"Guide {title}"}
            a.citation_hints = {url2: f"note {hint}"}
            self.answers.append(a)
        self.asked = 0

    def ask(self, prompt, *a, **k):
        self.asked += 1
        return self.answers[(self.asked - 1) % len(self.answers)]


class World:
    """One durable service with a crawlable fixture site, an answer engine, a Search Console fake, a client tenant
    (``acme``) holding canaries everywhere an operator can type, and another tenant (``globex``)."""

    def __init__(self, tmp_path, srv, caplog, seed: int, probe_guard: bool = True):
        self.c = Canaries(seed)
        self.probe_guard = probe_guard
        self.srv, self.caplog = srv, caplog
        self.errors: list = []                             # every response body with a status of 400 or more
        self.unwritable: list = []                         # a 500, or a write the guard refused (SINK_REFUSED)
        self.calls = 0
        tmp_path.mkdir(parents=True, exist_ok=True)
        self._site()
        keyfile = write_key_file(tmp_path / "sa.json")
        self.fake = FakeGoogle(prop=self._gsc_property_value())
        settings = config_mod.load(base_env(SEO_GSC_SERVICE_ACCOUNT_FILE=keyfile))
        ports = Ports.default(settings)
        ports.fetcher = srv.site_fetcher()
        ports.engines["openai"] = Engine(self.c)
        fc = self.fake.clock
        ports.first_party["search_console"] = gsc_from_settings(settings, transport=self.fake.transport(),
                                                                monotonic=fc.mono, wall=fc.wall, sleep=fc.sleep)
        self.fake.pages = [(f"http://site.test/blog/{urllib.parse.quote(self.c.new('gsc', 'page_url', f), safe='')}",
                            30, 300, 4.0) for f in FAMILIES]
        self.fake.queries = [(self.c.new("gsc", "query", f), 20, 100, 2.0) for f in FAMILIES]
        self.h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"), ports=ports,
                                 SEO_GSC_SERVICE_ACCOUNT_FILE=keyfile, SEO_PROBE_SAMPLES="3",
                                 SEO_LOG_MAX_OPEN_INGESTS="20", SEO_LOG_MAX_INGESTS="1000", SEO_GSC_READS_PER_DAY="48")
        # a real handler writing into the data directory: what a deployment's log file would hold
        self.handler = logging.FileHandler(os.path.join(self.h.settings.data_dir, "process.log"), encoding="utf-8")
        self.handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self.handler)
        self.ids: dict = {}

    def close(self) -> None:
        logging.getLogger().removeHandler(self.handler)
        self.handler.close()

    # ------------------------------------------------------------------ crawl-originated text

    def _gsc_property_value(self) -> str:
        self.gsc_prop = "http://site.test/" + urllib.parse.quote(self.c.new("flow", "gsc_property", "prose"), safe="")
        return self.gsc_prop

    def _site(self) -> None:
        c, srv, q = self.c, self.srv, urllib.parse.quote
        robots = "User-agent: *\nAllow: /\nSitemap: http://site.test/sitemap.xml\n"
        urls = ["http://site.test/", "http://site.test/about"]
        for fam in FAMILIES:
            robots += f"# {c.new('crawl', 'robots_comment', fam)}\nDisallow: /private/{q(c.new('crawl', 'robots_path', fam), safe='')}\n"
            urls.append(f"http://site.test/s/{q(c.new('crawl', 'sitemap_url', fam), safe='')}")
        srv.text(DOMAIN, "/robots.txt", robots)
        srv.text(DOMAIN, "/sitemap.xml", "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps."
                 "org/schemas/sitemap/0.9'>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>",
                 ctype="application/xml")
        srv.text(DOMAIN, "/llms.txt", "# Z Best Media\n\n> " + c.new("crawl", "llms_txt", "prose") + "\n")
        titles = " ".join(c.new("crawl", "page_title", f) for f in FAMILIES)
        canon = f"http://site.test/order/{q(c.new('crawl', 'canonical_url', 'luhn'), safe='')}"
        srv.html(DOMAIN, "/", home_html(title=f"Home {titles}", canonical=canon,
                                         body="<p>" + " ".join(c.new("crawl", "page_text", f) for f in FAMILIES) + "</p>"))
        srv.html(DOMAIN, "/about", home_html(title="About " + c.new("crawl", "page_title", "prose"),
                                              canonical="http://site.test/about"))
        target = "/landing?contact=" + q(c.new("crawl", "redirect_url", "email"), safe="") + "&id=" + \
            c.new("crawl", "redirect_url", "luhn")
        srv.redirect(DOMAIN, "/moved", f"http://site.test{target}")
        srv.html(DOMAIN, "/landing", home_html(title="Landing"))

    # ------------------------------------------------------------------ calls

    def call(self, method: str, path: str, body=None, caller="dashboard", andre=False, tenant=None, query=None,
             headers=None):
        """One request. Its error body (status 400 or more) is kept for the scan. Every request is also logged the
        way a server's access log and a careless handler would log it: the logging sink must withhold all of it."""
        hd = self.h.headers(caller, andre, tenant)
        if headers:
            for k, v in headers.items():
                if v is None:
                    hd.pop(k, None)
                else:
                    hd[k] = v
        wire = P + path if not path.startswith("/health") else path
        kw = {"headers": hd, "params": query} if query else {"headers": hd}
        if method == "GET":
            r = self.h.client.get(wire, **kw)
        else:
            r = self.h.client.post(wire, json=body if body is not None else {}, **kw)
        self.calls += 1
        if r.status_code >= 400:
            self.errors.append(r.text)
            if "SINK_REFUSED" in r.text or r.status_code == 500:
                self.unwritable.append((r.status_code, method, path.split("/")[1:2]))
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", method,
                                                 wire + ("?" + urllib.parse.urlencode(query) if query else ""),
                                                 "1.1", r.status_code)
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', method, "http://testserver" + wire, r.status_code)
        logging.getLogger("seo.canary").debug("request body %s headers %s", json.dumps(body), json.dumps(hd))
        try:
            raise ValueError(json.dumps({"path": wire, "body": body, "query": query, "headers": hd}))
        except ValueError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application\n")
        return r

    def ok(self, r, code=200):
        assert r.status_code == code, (r.status_code, r.json().get("detail") if r.headers.get(
            "content-type", "").startswith("application/json") else None)
        return r.json()

    # ------------------------------------------------------------------ the flows that persist

    def flows(self) -> None:
        c, h, ids = self.c, self.h, self.ids
        post, get = self._post, self._get
        self.ok(post("/tenants", {"request_id": rid(), "tenant_id": "acme", "kind": "client"}, andre=True), 201)
        self.ok(post("/tenants", {"request_id": rid(), "tenant_id": "globex", "kind": "client"}, andre=True), 201)
        self.ok(post("/tenants/acme/domains", {"request_id": rid(), "domains": [DOMAIN]}, andre=True))
        self.ok(post("/tenants/globex/domains", {"request_id": rid(), "domains": ["globex.test"]}, andre=True))
        self.ok(post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                     andre=True))
        fields = [{"field": f, "value": (f"(562) 555-01{self.c.rng.randrange(10, 99)}" if f == "telephone" else
                                         c.new("flow", f"entity.{f}.value", fam)),
                   "source": "founder_statement", "provenance": c.new("flow", f"entity.{f}.provenance", fam),
                   "authority": "first_party"}
                  for f, fam in zip(("name", "street_address", "locality", "region", "telephone"),
                                    ("prose", "email", "luhn", "ssn", "prose"))]
        ent = self.ok(post("/tenants/acme/entity", {"request_id": rid(), "fields": fields}, andre=True), 201)
        ids["eid"] = ent["entity_id"]
        self.ok(post(f"/entities/{ids['eid']}/fields", {
            "request_id": rid(), "field": "locality", "value": c.new("flow", "entity_field.value", "prose"),
            "source": "founder_statement", "provenance": c.new("flow", "entity_field.provenance", "email"),
            "authority": "first_party", "expected_version": 1}, andre=True))
        brand = "Z Best Media"
        ps = self.ok(post("/tenants/acme/prompt-sets", {
            "request_id": rid(), "name": "core", "brand_terms": [brand, "Acme " + c.new("flow", "brand_term", "prose")],
            "brand_term_rules": [{"term": brand, "case_sensitive": False,
                                  "context_terms": [c.new("flow", "context_term", f) for f in ("prose", "email")],
                                  "negative_terms": [c.new("flow", "negative_term", f) for f in ("prose", "ssn")]}],
            "competitor_domains": ["competitor.test"],
            "prompts": ["who recovers marketplace revenue"] + [f"tell me about {c.new('flow', 'prompt', f)} please"
                                                               for f in ("prose", "token")],
            "engines": ["openai"]}), 201)
        ids["psid"] = ps["prompt_set_id"]
        self.ok(post("/tenants/acme/gsc-property", {"request_id": rid(), "property": self.gsc_prop}, andre=True))
        paths = ["/", "/about", "/moved", "/s/" + urllib.parse.quote(c.new("flow", "audit_path", "prose"), safe="")]
        a = self.ok(post("/tenants/acme/audits", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                                   "paths": paths, "invoice_id": invoice_id(1),
                                                   "prompt_set_id": ids["psid"]}, andre=True), 201)
        assert a["status"] == "completed", a["status"]
        ids["aid"] = a["audit_id"]
        self.ok(post(f"/tenants/acme/audits/{ids['aid']}/client-report", {"request_id": rid(), "format": "html"},
                     andre=True))
        run = self.ok(post("/tenants/acme/citation-runs", {"request_id": rid(), "domain": DOMAIN,
                                                            "prompt_set_id": ids["psid"], "engines": ["openai"]},
                           andre=True), 201)
        ids["cid"] = run["citation_run_id"]
        s = self.ok(post("/tenants/acme/schedules", {
            "request_id": rid(), "domain": DOMAIN, "scheme": "http", "every_days": 7, "invoice_id": invoice_id(2),
            "paths": ["/", "/about", "/t/" + urllib.parse.quote(c.new("flow", "schedule_path", "prose"), safe="")],
            "prompt_set_id": ids["psid"]}, andre=True), 201)
        ids["sid"] = s["schedule_id"]
        t = self.ok(post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
        assert t["ran"] == 1, t
        h.clock.advance(days=7)
        self.srv.html(DOMAIN, "/", home_html(title="Home " + c.new("crawl", "page_title_later", "prose"),
                                              canonical=None))
        t = self.ok(post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
        assert t["ran"] == 1 and t["drift_recorded"] == 1, t
        ids["aid2"] = [x["audit_id"] for x in self.ok(get("/tenants/acme/audits")) if x.get("schedule_id")][-1]
        g = self.ok(post("/tenants/acme/log-ingests", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                                        "format": "combined"}), 201)
        ids["iid"] = g["ingest_id"]
        self.ok(post(f"/tenants/acme/log-ingests/{ids['iid']}/chunks",
                     {"request_id": rid(), "seq": 1, "last": False, "data_b64": self.log_lines("flow")}))
        fin = self.ok(post(f"/tenants/acme/log-ingests/{ids['iid']}/finish", {"request_id": rid()}))
        assert fin["status"] == "finished" and fin["report"] is not None
        for job in ("integrity", "interrupted-audits"):
            self.ok(post(f"/jobs/{job}/run", {"request_id": rid()}, caller="scheduler"))
        # the guard is the backstop: a record carrying a canary is refused whatever tried to write it
        for fam in FAMILIES:
            if fam in MACHINE_FAMILIES or not self.probe_guard:    # (a short token / 32 hex IS a machine value)
                continue
            with pytest.raises(Exception) as caught:
                h.svc._commit("job_ran", {"job": c.new("flow", "direct_commit", fam)}, "scheduler")
            assert getattr(caught.value, "reason", None) == "SINK_REFUSED", type(caught.value).__name__
        # a restart replays every line and re-reads the vault
        self.h = h = h.restart()
        assert h.svc.integrity["ok"]

    def log_lines(self, route: str) -> str:
        """Access-log lines (combined format) whose path, query, referrer and User-Agent carry canaries."""
        c, q, lines = self.c, urllib.parse.quote, []
        for i, fam in enumerate(FAMILIES):
            path = f"/u/{q(c.new(route, 'log_line.path', fam), safe='')}?who={q(c.new(route, 'log_line.query', fam), safe='')}"
            ref = "http://ref.test/" + q(c.new(route, "log_line.referrer", fam), safe="")
            ua = f"Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html) {c.new(route, 'log_line.user_agent', fam)}"
            lines.append(f'66.249.66.{i + 1} - {c.new(route, "log_line.user", "token")} [09/Oct/2026:17:0{i}:00 +0000] '
                         f'"GET {path} HTTP/1.1" 200 512 "{ref}" "{ua}"')
        lines.append("this is not a log line " + c.new(route, "log_line.malformed", "prose"))
        return base64.b64encode(("\n".join(lines) + "\n").encode()).decode()

    def _post(self, path, body=None, caller="dashboard", andre=False, tenant=None):
        return self.call("POST", path, body, caller=caller, andre=andre, tenant=tenant)

    def _get(self, path, caller="dashboard", tenant=None, query=None):
        return self.call("GET", path, caller=caller, tenant=tenant, query=query)

    # ------------------------------------------------------------------ a valid call for every route

    def fresh_tenant(self) -> str:
        """A client tenant with no entity record yet."""
        for tid, t in self.h.svc.tenants.items():
            if tid.startswith("fresh") and not any(e["tenant_id"] == tid for e in self.h.svc.entities.values()):
                return tid
        tid = "fresh" + "".join(self.c.rng.choice(string.ascii_lowercase) for _ in range(10))
        self.ok(self._post("/tenants", {"request_id": rid(), "tenant_id": tid, "kind": "client"}, andre=True), 201)
        return tid

    def open_ingest(self, chunks: int) -> tuple:
        """(ingest id, next seq) of an open ingest of acme holding at least ``chunks`` chunks."""
        svc = self.h.svc
        for g in svc.log_ingests.values():
            if g["tenant_id"] == "acme" and g["status"] == "open" and not g["last_seen"] and g["next_seq"] > chunks:
                return g["ingest_id"], g["next_seq"]
        g = self.ok(self._post("/tenants/acme/log-ingests", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                                              "format": "combined"}), 201)
        seq = 1
        for _ in range(chunks):
            self.ok(self._post(f"/tenants/acme/log-ingests/{g['ingest_id']}/chunks",
                               {"request_id": rid(), "seq": seq, "last": False, "data_b64": self.plain_chunk()}))
            seq += 1
        return g["ingest_id"], seq

    @staticmethod
    def plain_chunk() -> str:
        line = ('66.249.66.1 - - [09/Oct/2026:17:00:00 +0000] "GET /about HTTP/1.1" 200 512 "-" '
                '"Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"\n')
        return base64.b64encode(line.encode()).decode()

    def spec(self, key: str) -> dict:
        """An otherwise valid call for route ``key`` as its owning caller: ``{"params", "query", "body", "caller",
        "andre", "tenant", "expect"}``. Built fresh for each request (ids, sequence numbers and states move)."""
        ids, svc = self.ids, self.h.svc
        r = {"request_id": rid()}
        A = {"tid": "acme"}

        def s(params=None, body=None, caller="dashboard", andre=False, query=None, expect=200):
            return {"params": params or {}, "query": query or {}, "body": body, "caller": caller, "andre": andre,
                    "tenant": None, "expect": expect}
        if key == "GET /health":
            return s(caller=None)
        if key in (f"GET {P}/status", f"GET {P}/pricing", f"GET {P}/tenants", f"GET {P}/kill-switches",
                   f"GET {P}/department", f"GET {P}/audit/integrity"):
            return s()
        if key == f"GET {P}/audit/export":
            return s(query={"since": "1", "limit": "5"})
        if key == f"GET {P}/audit/evidence":
            return s(query={"limit": "5", "offset": "0", "event_type": "tenant_created"})
        if key == f"POST {P}/tenants":
            tid = "plain" + "".join(self.c.rng.choice(string.ascii_lowercase) for _ in range(10))
            return s(body={**r, "tenant_id": tid, "kind": "client"}, andre=True, expect=201)
        if key == f"GET {P}/tenants/{{tid}}":
            return s(A)
        if key == f"POST {P}/tenants/{{tid}}/domains":
            return s(A, {**r, "domains": [DOMAIN, "second.acme.test"]}, andre=True)
        if key == f"POST {P}/tenants/{{tid}}/finance-client":
            return s(A, {**r, "finance_client_id": "acme-party-1"}, andre=True)
        if key == f"POST {P}/tenants/{{tid}}/gsc-property":
            return s(A, {**r, "property": self.gsc_prop}, andre=True)
        if key == f"POST {P}/kill-switches":
            on = bool(svc.switches["provider"].get("perplexity"))
            return s(body={**r, "switch": "provider:perplexity", "engaged": not on}, andre=True)
        if key == f"GET {P}/tenants/{{tid}}/entity":
            return s(A)
        if key == f"POST {P}/tenants/{{tid}}/entity":
            return s({"tid": self.fresh_tenant()}, {**r, "fields": [
                {"field": "name", "value": "Fresh Co", "source": "founder_statement", "provenance": "stated",
                 "authority": "first_party"}]}, andre=True, expect=201)
        if key == f"POST {P}/entities/{{eid}}/fields":
            return s({"eid": ids["eid"]}, {**r, "field": "region", "value": "CA", "source": "founder_statement",
                                           "provenance": "stated", "authority": "first_party",
                                           "expected_version": svc.entities[ids["eid"]]["version"]}, andre=True)
        if key == f"POST {P}/tenants/{{tid}}/prompt-sets":
            name = "set" + "".join(self.c.rng.choice(string.ascii_lowercase) for _ in range(10))
            return s(A, {**r, "name": name, "brand_terms": ["Z Best Media"],
                         "brand_term_rules": [{"term": "Z Best Media", "context_terms": ["revenue"],
                                               "negative_terms": ["recipe"]}],
                         "competitor_domains": ["competitor.test"], "prompts": ["who recovers marketplace revenue"],
                         "engines": ["openai"]}, expect=201)
        if key == f"GET {P}/tenants/{{tid}}/prompt-sets/{{psid}}":
            return s({**A, "psid": ids["psid"]})
        if key == f"POST {P}/tenants/{{tid}}/audits":
            return s(A, {**r, "domain": DOMAIN, "scheme": "http", "paths": ["/about"],
                         "invoice_id": invoice_id(1000 + self.calls), "prompt_set_id": ids["psid"]}, andre=True,
                     expect=201)
        if key == f"GET {P}/tenants/{{tid}}/audits":
            return s(A)
        if key == f"GET {P}/tenants/{{tid}}/audits/{{aid}}/drift":
            return s({**A, "aid": ids["aid2"]}, query={"against": self.ids["aid_first"]})
        if key == f"POST {P}/tenants/{{tid}}/audits/{{aid}}/client-report":
            return s({**A, "aid": ids["aid"]}, {**r, "format": "markdown"}, andre=True)
        if key in (f"GET {P}/tenants/{{tid}}/audits/{{aid}}/client-reports", f"GET {P}/tenants/{{tid}}/audits/{{aid}}"):
            return s({**A, "aid": ids["aid"]})
        if key == f"POST {P}/tenants/{{tid}}/citation-runs":
            return s(A, {**r, "domain": DOMAIN, "prompt_set_id": ids["psid"], "engines": ["openai"]}, andre=True,
                     expect=201)
        if key == f"GET {P}/tenants/{{tid}}/citation-runs":
            return s(A)
        if key in (f"GET {P}/tenants/{{tid}}/citation-runs/{{cid}}",
                   f"GET {P}/tenants/{{tid}}/citation-runs/{{cid}}/mnc-priority"):
            return s({**A, "cid": ids["cid"]})
        if key == f"POST {P}/tenants/{{tid}}/log-ingests":
            return s(A, {**r, "domain": DOMAIN, "scheme": "http", "format": "combined"}, expect=201)
        if key == f"POST {P}/tenants/{{tid}}/log-ingests/{{iid}}/chunks":
            iid, seq = self.open_ingest(0)
            return s({**A, "iid": iid}, {**r, "seq": seq, "data_b64": self.plain_chunk(), "last": False})
        if key == f"POST {P}/tenants/{{tid}}/log-ingests/{{iid}}/finish":
            iid, _seq = self.open_ingest(1)
            return s({**A, "iid": iid}, dict(r))
        if key == f"GET {P}/tenants/{{tid}}/log-ingests":
            return s(A)
        if key == f"GET {P}/tenants/{{tid}}/log-ingests/{{iid}}":
            return s({**A, "iid": ids["iid"]})
        if key == f"POST {P}/tenants/{{tid}}/schedules":
            return s(A, {**r, "domain": DOMAIN, "scheme": "http", "paths": ["/about"], "every_days": 30,
                         "invoice_id": invoice_id(5000 + self.calls), "prompt_set_id": ids["psid"]}, andre=True,
                     expect=201)
        if key == f"POST {P}/tenants/{{tid}}/schedules/{{sid}}/status":
            cur = svc.schedules[ids["sid"]]["status"]
            return s({**A, "sid": ids["sid"]}, {**r, "status": "paused" if cur == "active" else "active"}, andre=True)
        if key == f"GET {P}/tenants/{{tid}}/schedules":
            return s(A)
        if key == f"GET {P}/tenants/{{tid}}/schedules/{{sid}}":
            return s({**A, "sid": ids["sid"]})
        if key == f"POST {P}/agents/{{agent}}/lifecycle":
            cur = svc.agent_state("naomi")
            return s({"agent": "naomi"}, {**r, "to": "watch" if cur == "active" else "active",
                                          "expected_state": cur, "reason": "QUALITY_REVIEW"}, andre=True)
        if key == f"POST {P}/jobs/{{name}}/run":
            return s({"name": "interrupted-audits"}, dict(r), caller="scheduler")
        raise KeyError(key)

    def send(self, key: str, spec: dict, headers=None):
        method, template = key.split(" ", 1)
        path = template.removeprefix(P)
        for name, value in spec["params"].items():
            path = path.replace("{" + name + "}", urllib.parse.quote(value, safe=""))
        return self.call(method, path, spec["body"], caller=spec["caller"], andre=spec["andre"],
                         tenant=spec["tenant"], query=spec["query"] or None, headers=headers)


# fields whose syntax a canary must be wrapped in to be accepted at all: normalised field -> wrapper
def _path(v):
    return "/c/" + urllib.parse.quote(v, safe="")


FITTED = {
    "paths[]": _path,
    "property": lambda v: "http://site.test/" + urllib.parse.quote(v, safe=""),
    "domains[]": lambda v: v.lower() + ".canary.test",
    "competitor_domains[]": lambda v: v.lower() + ".competitor.test",
    "domain": lambda v: v.lower() + ".site.test",
    "switch": lambda v: "tenant:" + v.lower(),
    "reason": lambda v: v.upper(),
    "data_b64": lambda v: base64.b64encode(
        (f'66.249.66.9 - - [09/Oct/2026:17:00:00 +0000] "GET /x/{urllib.parse.quote(v, safe="")} HTTP/1.1" 200 5 '
         f'"-" "Mozilla/5.0 (compatible; Googlebot/2.1) {v}"\n').encode()).decode(),
}


def plant_everything(w: World, routes: dict, families=FAMILIES, disguises=DISGUISES, parts=("body", "path", "query",
                                                                                           "headers")) -> dict:
    """Plant a canary in every string field of every route. Returns the coverage: per route, the fields planted."""
    covered: dict = {}
    order = sorted(routes, key=lambda k: (any(x in k for x in ("/domains", "/finance-client", "/gsc-property",
                                                                "/kill-switches")), k))
    for key in order:
        info = routes[key]
        if key in EXEMPT:
            continue
        fields = covered.setdefault(key, [])
        base = w.spec(key)
        if "body" in parts and info["model"] is not None:
            declared = schema_string_paths(info["model"])
            leaves = instance_string_paths(base["body"])
            present = {norm for _p, norm in leaves}
            extra = sorted(declared - present)             # optional fields the valid call does not carry
            assert all("." not in x and "[]" not in x for x in extra), (key, extra)
            targets = leaves + [((x,), x) for x in extra]
            for path, norm in targets:
                fields.append("body:" + norm)
                wrap = FITTED.get(norm)
                for fam in families:
                    for dis in (disguises if fam not in EXTRA_FAMILIES else ("raw",)):
                        spec = w.spec(key)
                        spec["body"] = with_value(spec["body"], path, w.c.new(key, "body:" + norm, fam, dis,
                                                                              as_json=True))
                        w.send(key, spec)
                    if wrap is not None:
                        spec = w.spec(key)
                        spec["body"] = with_value(spec["body"], path, wrap(w.c.new(key, "body:" + norm, fam, "fitted")))
                        w.send(key, spec)
        if "path" in parts:
            for name in info["path_params"]:
                fields.append("path:" + name)
                for fam in families:
                    for dis in (disguises if fam not in EXTRA_FAMILIES else ("raw",)):
                        spec = w.spec(key)
                        spec["params"][name] = w.c.new(key, "path:" + name, fam, dis)
                        w.send(key, spec)
        if "query" in parts:
            for name in info["query_params"]:
                fields.append("query:" + name)
                for fam in families:
                    for dis in (disguises if fam not in EXTRA_FAMILIES else ("raw",)):
                        spec = w.spec(key)
                        spec["query"][name] = w.c.new(key, "query:" + name, fam, dis)
                        w.send(key, spec)
        if "headers" in parts:
            for name in HEADERS_READ:
                fields.append("header:" + name)
                for fam in families:
                    for dis in (disguises if fam not in EXTRA_FAMILIES else ("raw",)):
                        value = w.c.new(key, "header:" + name, fam, dis)
                        spec = w.spec(key)
                        if name == api.TENANT_HEADER:
                            spec["caller"] = "hub"
                        w.send(key, spec, headers={name: ("Bearer " + value) if name == "Authorization" else value})
    return covered


EXPORT_PAGE = 50                         # small pages: the paging itself is exercised


def non_owner_reads(w: World) -> dict:
    """What other tenants and non-owner callers can read: every GET as another tenant's hub (its own tenant and
    attempts on acme's objects), the Finance and scheduler views, the operator-wide aggregates, /health, /status."""
    ids, out = w.ids, {}
    get = w.h.client.get

    def take(path, caller, tenant=None, **params):
        r = get((P + path) if path != "/health" else path, headers=w.h.headers(caller, tenant=tenant) if caller else {},
                params=params or None)
        label = "/".join("ID" if (x.startswith("seo-") or x in ("acme", "globex")) else x
                         for x in path.strip("/").split("/"))
        name = f"non_owner:{caller or 'anyone'}:{label}"
        out[name] = out.get(name, "") + r.text + "\n"
        return r
    for tid in ("globex", "acme"):                          # globex's hub: its own tenant, then acme's objects
        for path in (f"/tenants/{tid}", f"/tenants/{tid}/entity", f"/tenants/{tid}/audits",
                     f"/tenants/{tid}/audits/{ids['aid']}", f"/tenants/{tid}/citation-runs",
                     f"/tenants/{tid}/citation-runs/{ids['cid']}",
                     f"/tenants/{tid}/citation-runs/{ids['cid']}/mnc-priority", f"/tenants/{tid}/log-ingests",
                     f"/tenants/{tid}/log-ingests/{ids['iid']}", f"/tenants/{tid}/schedules",
                     f"/tenants/{tid}/schedules/{ids['sid']}", f"/tenants/{tid}/prompt-sets/{ids['psid']}"):
            r = take(path, "hub", tenant="globex")
            assert r.status_code in ((200, 403, 404) if tid == "globex" else (403, 404)), (path.split("/")[3:4],
                                                                                          r.status_code)
    take("/tenants/globex/audits/" + ids["aid"], "dashboard")              # acme's audit under globex's path
    take("/tenants/acme/audits", "finance_31")
    take(f"/tenants/acme/audits/{ids['aid']}", "finance_31")
    take("/pricing", "finance_31")
    take("/health", None)
    for path in ("/status", "/department", "/kill-switches", "/audit/integrity"):
        assert take(path, "dashboard").status_code == 200, path
    # review 2b26d980 NIT: paged with the routes' REAL keys (``events`` / ``evidence``), to the last line: the scan
    # reads the whole export and the whole evidence view, not their first page
    since, exported = 1, 0
    while True:
        r = take("/audit/export", "compliance_38", since=since, limit=EXPORT_PAGE)
        assert r.status_code == 200, r.status_code
        page = r.json()
        rows = page["events"]
        exported += len(rows)
        if len(rows) < EXPORT_PAGE:
            break
        since = rows[-1]["seq"] + 1
    assert exported == page["log_length"] == len(w.h.svc.log) and exported > EXPORT_PAGE, (exported, len(w.h.svc.log))
    offset, listed = 0, 0
    while True:
        r = take("/audit/evidence", "compliance_38", limit=EXPORT_PAGE, offset=offset)
        assert r.status_code == 200, r.status_code
        page = r.json()
        rows = page["evidence"]
        listed += len(rows)
        if len(rows) < EXPORT_PAGE:
            break
        offset += EXPORT_PAGE
    assert listed == page["total"] > EXPORT_PAGE, (listed, page["total"])
    return out


def blobs_of(w: World) -> dict:
    """Every sink a non-owner or an operator of the machine can read, by name."""
    w.handler.flush()
    out = {"ledger": json.dumps(w.h.ledger.events, ensure_ascii=False, default=str),
           "captured_logs": w.caplog.text + "\n".join(r.getMessage() for r in w.caplog.records),
           "error_bodies": "\n".join(w.errors), **non_owner_reads(w)}
    for root, _d, files in os.walk(w.h.settings.data_dir):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
            name = "file:" + "".join(ch if ch.isalpha() or ch in "._" else "N" for ch in f)[:40]
            text = raw
            if f.endswith(".jsonl") and "vault" not in f:
                text += "".join(json.dumps(json.loads(line), ensure_ascii=False) for line in raw.splitlines()
                                if line.startswith("{"))
            out[name] = out.get(name, "") + text
    return out


def scenario(tmp_path, srv, caplog, seed: int, quick: bool = False, probe_guard: bool = True) -> dict:
    """The whole test body: the world, the flows, the planting, the scan. Returns names and counts only.
    ``probe_guard=False`` (mutation tests only) skips the direct commits that expect the guard's refusal."""
    caplog.set_level(logging.DEBUG)
    w = World(tmp_path, srv, caplog, seed, probe_guard)
    try:
        routes = discover(w.h.client.app.app.app)
        w.flows()
        first = [x["audit_id"] for x in w.ok(w._get("/tenants/acme/audits")) if x.get("schedule_id")]
        w.ids["aid_first"] = first[0]
        baseline = {}
        for key in sorted(routes):
            if key in EXEMPT:
                continue
            spec = w.spec(key)
            r = w.send(key, spec)
            baseline[key] = (r.status_code, spec["expect"])
        if quick:
            covered = plant_everything(w, {k: v for k, v in routes.items() if v["model"] or v["query_params"]},
                                       families=("prose", "token"), disguises=("raw",), parts=("body", "query"))
        else:
            covered = plant_everything(w, routes)
        hits = scan(blobs_of(w), w.c)
        return {"routes": routes, "baseline": baseline, "covered": covered, "hits": hits, "planted": w.c.planted,
                "unwritable": w.unwritable,
                "calls": w.calls, "files": sorted(k for k in blobs_of(w) if k.startswith("file:"))}
    finally:
        w.close()


def summarise(hits: list) -> dict:
    """Hits as {(sink, route, field, family, disguise): 1} folded to names and counts."""
    out: dict = {}
    for sink, route, field, family, disguise in hits:
        out.setdefault((route, field, family), set()).add(sink)
    return {k: sorted(v) for k, v in out.items()}


def stray_of(found: dict) -> dict:
    """The hits that are not the known limit (a token family value in a ``KNOWN_LIMIT`` pair; review 98b76cde: a
    host name with an ``at`` label in a ``HOST_LIMIT`` pair)."""
    return {k: v for k, v in found.items()
            if not ((k[2] == "token" and f"{k[0]} {k[1]}" in KNOWN_LIMIT)
                    or (k[2] == "atless" and f"{k[0]} {k[1]}" in HOST_LIMIT))}


# ================================================================================================ the tests

@pytest.mark.local_http
def test_every_route_and_every_string_field_with_canaries_leaves_nothing_but_the_known_limit(tmp_path, srv, caplog):
    res = scenario(tmp_path, srv, caplog, seed=20261110)
    assert log_sink.installed()
    routes, covered = res["routes"], res["covered"]
    # every route has a decision: a builder (its valid call answers 2xx) or an explicit exemption
    assert set(EXEMPT) <= set(routes), sorted(set(EXEMPT) - set(routes))
    bad = {k: v for k, v in res["baseline"].items() if v[0] != v[1]}
    assert bad == {}, f"the valid call of {len(bad)} route(s) was not accepted: {bad}"
    assert set(covered) | set(EXEMPT) == set(routes) and len(routes) >= 40
    # every declared string field of every request model was planted, and every path / query parameter and header
    for key, info in routes.items():
        if key in EXEMPT:
            continue
        want = {"path:" + p for p in info["path_params"]} | {"query:" + p for p in info["query_params"]} | \
            {"header:" + n for n in HEADERS_READ}
        if info["model"] is not None:
            want |= {"body:" + p for p in schema_string_paths(info["model"])}
        assert want <= set(covered[key]), (key, sorted(want - set(covered[key])))
        assert info["header_params"] in ([], ["authorization"]), key
    # no planted call ended in a 500, and none was accepted by its route only to be refused at the sink (a value a
    # route takes is either vaulted or a machine value: the guard is a backstop, never the validator)
    assert res["unwritable"] == [], (len(res["unwritable"]), res["unwritable"][:5])
    found = summarise(res["hits"])
    stray = stray_of(found)
    assert not stray, f"{len(stray)} canary leak(s): " + "; ".join(f"{k} in {v}" for k, v in sorted(stray.items()))
    accepted = {f"{route} {field}" for route, field, _family in found}
    assert accepted == set(KNOWN_LIMIT), (sorted(accepted - set(KNOWN_LIMIT)), sorted(set(KNOWN_LIMIT) - accepted))
    # review 98b76cde: the host name with an ``at`` label is found exactly in the DOMAIN pair, a short token in
    # the three pairs, and no other family anywhere
    by_family = collections.defaultdict(set)
    for route, field, family in found:
        by_family[family].add(f"{route} {field}")
    assert dict(by_family) == {"token": set(KNOWN_LIMIT), "atless": set(HOST_LIMIT)}, {
        k: sorted(v) for k, v in by_family.items()}
    print("canary coverage: routes=%d covered=%d exempt=%d fields=%d planted=%d calls=%d known_limit_pairs=%d files=%d"
          % (len(routes), len(covered), len(EXEMPT), sum(len(v) for v in covered.values()), res["planted"],
             res["calls"], len(accepted), len(res["files"])))
    assert res["planted"] > 5000 and any(f.startswith("file:process") for f in res["files"])
    assert len([f for f in res["files"] if "vault" in f]) == 2


def test_the_headers_the_service_reads_are_the_ones_planted():
    """A header read anywhere in api.py that is not planted fails here."""
    import inspect
    import re
    src = inspect.getsource(api)
    names = set(re.findall(r"request\.headers\.get\(([A-Z_]+)\)", src))
    assert names == {"CALLER_HEADER", "TENANT_HEADER", "FOUNDER_HEADER"}, sorted(names)
    assert "request.headers[" not in src and "request.cookies" not in src and "request.query_params" not in src
    assert len(re.findall(r"Header\(", src)) == 1                       # Authorization only


def test_the_scan_finds_every_form_of_a_canary():
    c = Canaries(7)
    for fam in FAMILIES:
        v = c.new("r", "f", fam)
        b = v.encode()
        for i, text in enumerate((v, v.upper(), v.lower(), b.hex(), base64.b64encode(b).decode(),
                                  base64.urlsafe_b64encode(b"ab" + b + b"cd").decode(),
                                  base64.b64encode(b"a" + b + b"cd").decode(), urllib.parse.quote(v, safe=""),
                                  json.dumps({"k": v}))):
            assert scan({"s": "xx " + text + " yy"}, c) != [], (fam, i)
    assert scan({"s": "nothing planted here " * 50}, c) == []


# ------------------------------------------------------------------------------------------------ mutations

def _guard_off(mp):
    mp.setattr(sg, "violations", lambda kind, data, stored=False: [])


def _inline(kind: str):
    """The record of ``kind`` carries its vaulted document again (as before the kind was migrated)."""
    def patch(mp):
        from service import SeoService
        real = SeoService._commit

        def commit(self, k, data, actor, evidence=None):
            if k == kind:
                docs = list(self._doc_handoff.values())
                data = {**data, "inline": docs[-1] if docs else None}
            return real(self, k, data, actor, evidence=evidence)
        mp.setattr(SeoService, "_commit", commit)
    return patch


def _no_log_filter(mp):
    mp.setitem(log_sink._STATE, "enabled", False)


def _pydantic_echo(mp):
    mp.setattr(api, "_sanitize", lambda errors: {"detail": json.loads(json.dumps(errors, default=str))})


MIGRATED = ("entity_created", "entity_field_set", "prompt_set_version", "log_ingest_created", "log_chunk",
            "log_ingest_finished")
MUTATIONS = {
    "sink_guard_disabled": _guard_off,
    "logging_filter_removed": _no_log_filter,
    "pydantic_input_echo_restored": _pydantic_echo,
}


@pytest.mark.local_http
def test_the_quick_scenario_holds_unmutated(tmp_path, srv, caplog):
    res = scenario(tmp_path, srv, caplog, seed=5, quick=True)
    stray = stray_of(summarise(res["hits"]))
    assert not stray, f"{len(stray)} canary leak(s): " + "; ".join(f"{k} in {v}" for k, v in sorted(stray.items()))


def _mutated(name, patch, tmp_path, srv, caplog, monkeypatch, **kw):
    """The quick scenario under a mutation: ("failed", None) when a flow broke, else ("ran", the stray hits)."""
    monkeypatch.setattr(sg, "OBSERVER", None)               # a mutated service's records are not the suite's
    sg._CACHE.clear()
    sg._KEYS.clear()
    patch(monkeypatch)
    try:
        try:
            res = scenario(tmp_path, srv, caplog, seed=5, quick=True, **kw)
        except (AssertionError, pytest.fail.Exception):
            return "failed", None
        return "ran", stray_of(summarise(res["hits"]))
    finally:
        monkeypatch.undo()
        sg._CACHE.clear()
        sg._KEYS.clear()


@pytest.mark.local_http
@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutation_bites(name, tmp_path, srv, caplog, monkeypatch):
    """Each mutation makes the canary scenario fail: a flow breaks, or canaries are found outside the known limit."""
    how, stray = _mutated(name, MUTATIONS[name], tmp_path, srv, caplog, monkeypatch)
    assert how == "failed" or stray, name


@pytest.mark.local_http
def test_with_the_guard_off_and_nothing_inlined_the_scan_is_clean(tmp_path, srv, caplog, monkeypatch):
    """The control of the re-inlining mutations: every kind is migrated, so even WITHOUT the guard (and without the
    direct commits that probe it) the flows write no canary. What the mutations below find is what they re-inlined."""
    how, stray = _mutated("guard_off", _guard_off, tmp_path, srv, caplog, monkeypatch, probe_guard=False)
    assert how == "ran" and not stray, (how, sorted(stray or {}))


@pytest.mark.local_http
@pytest.mark.parametrize("kind", MIGRATED)
def test_re_inlining_a_migrated_kind_is_refused_by_the_guard_and_found_by_the_scan(kind, tmp_path, srv, caplog,
                                                                                    monkeypatch):
    # with the guard: the record is refused, so the flow that writes it fails
    how, _stray = _mutated(kind, _inline(kind), tmp_path / "guarded", srv, caplog, monkeypatch)
    assert how == "failed", kind

    def both(mp):
        _inline(kind)(mp)
        _guard_off(mp)
    # without the guard: the flows run, and the scan finds the canaries in the log
    how, stray = _mutated(kind, both, tmp_path / "unguarded", srv, caplog, monkeypatch, probe_guard=False)
    assert how == "ran" and any("file:seo_log.jsonl" in sinks for sinks in stray.values()), (kind, how)
