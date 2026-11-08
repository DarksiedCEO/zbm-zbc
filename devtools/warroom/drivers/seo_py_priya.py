"""War room driver: Priya's keyword ledger (ADR 0047, ``POST /tenants/{tid}/keyword-ledger``), through seo-py's own
harness (tests/helpers.py ``Harness`` built by tests/test_gsc.py ``gsc_harness``: the REAL Search Console connector,
``gsc_client.GscClient``, on ``httpx.MockTransport`` with an injected clock and a key generated in the worker by
tests/gsc_fakes.py) and the REAL Bing Webmaster Tools adapter (``bing_webmaster.BingWebmasterClient`` on
tests/bing_fakes.py's MockTransport). Nothing leaves the process: the worker refuses every socket connect before any
service code is imported.

Each case gets fresh harnesses in its own temporary directory, and a HOSTILE Google (``WRGoogle``): it serves the
query rows the case names per property (unicode / RTL / CJK / emoji / huge / near-duplicate / duplicate spellings,
personal data and secrets from the suite's own attack corpus, tests/attack_corpus.py, and the drop tables of
tests/test_query_privacy_r8/r9/r10.py: no secret is hand-written here), with totals that disagree with the rows
(sampling, anonymised remainders), and a storm script per request (429 with Retry-After, 5xx, timeouts, trickled and
endless bodies, malformed / truncated / mistyped JSON, repeated pages, a key echoed in an error body). Bing is a
storm script over tests/bing_fakes.py.

The judge is independent of the service: personal data is what the case planted (the attack corpus and the drop
tables), searched for in every response, the ledger rows' keys, every stored byte (data directory, ledger events,
the append-only log) and the service's log records, read through NFKC, digit folding and the repo's shared lookalike
fold; volume is bounded by what the fake Google and the fake Bing actually served (conservation: the ledger can never
hold more impressions than were served); a source's numbers appear only when that source was read; caps are the
settings the harness was started with; work is bounded structurally (requests per read <= SEO_GSC_MAX_REQUESTS, the
fake clock's seconds <= the read deadline plus one timeout), never by wall-clock time.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import unicodedata
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

WARROOM = Path(__file__).resolve().parents[1]
SEO = None                    # the seo-py base driver: the shared lookalike fold (judge side)
H = GF = BF = TG = PRIYA = SP = AC = R8 = R9 = R10 = MODELS = httpx = None
SECRETS: dict = {}
PROP, PROP_B = "sc-domain:acme.test", "sc-domain:globex.test"
_RID = [0]
_BASELINE: dict = {}


def prepare_env(env) -> None:
    for k in list(env):
        if k.startswith("SEO_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
            env.pop(k, None)


def import_harness() -> None:
    global SEO, H, GF, BF, TG, PRIYA, SP, AC, R8, R9, R10, MODELS, httpx
    spec = importlib.util.spec_from_file_location("warroom_driver_seo_py_base", WARROOM / "drivers" / "seo_py.py")
    SEO = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(SEO)
    SEO.import_harness()
    import attack_corpus
    import bing_fakes
    import gsc_fakes
    import helpers
    import httpx as _httpx
    import models
    import test_gsc
    import test_query_privacy_r8
    import test_query_privacy_r9
    import test_query_privacy_r10
    from agents import priya
    from agents import search_performance
    H, GF, BF, TG, PRIYA, SP, AC = helpers, gsc_fakes, bing_fakes, test_gsc, priya, search_performance, attack_corpus
    R8, R9, R10, MODELS, httpx = test_query_privacy_r8, test_query_privacy_r9, test_query_privacy_r10, models, _httpx
    SECRETS["service_token"] = H.SERVICE_TOKEN
    SECRETS["andre_token"] = H.ANDRE
    SECRETS.update({f"caller_{c}": t for c, t in H.CALLERS.items()})
    SECRETS.update({f"tenant_{t}": v for t, v in H.TENANT_TOKENS.items()})
    SECRETS["bing_api_key"] = BF.KEY_TEXT
    for i, f in enumerate(GF.pem_fragments()):
        if f != "PRIVATE KEY":
            SECRETS[f"gsc_private_key_{i}"] = f
    baseline()


def rid() -> str:
    _RID[0] += 1
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"warroom-priya-{_RID[0]}"))


# ============================================================================================ the oracle (judge side)

def fold(s: str) -> str:
    """NFKC, every decimal digit as ASCII, format characters removed, the shared lookalike fold, case-folded."""
    s = unicodedata.normalize("NFKC", s or "")
    out = []
    for c in s:
        if unicodedata.category(c) in ("Cf", "Mn", "Me"):
            continue
        d = unicodedata.decimal(c, None)
        out.append(str(d) if d is not None else c)
    return SEO._fold("".join(out)).casefold()


def skeleton(s: str) -> str:
    return re.sub(r"[^a-z0-9@]", "", fold(s))


def my_norm(s: str) -> str:
    return " ".join(fold(s).split())


# ============================================================================================ hostile Google

API_STEPS = ("ok", "429", "429ra5", "429ra60", "500", "502", "503", "504", "400", "403quota", "403perm", "404",
             "401", "timeout", "connect", "drip", "endless", "not_json", "truncated", "html", "nan", "dup_key",
             "wrong_agg", "rows_not_list", "keys_arity", "negative", "huge_int", "clicks_gt_impr", "extra_fields",
             "partial", "repeat_page", "empty_obj", "key_echo", "huge_body", "gzip", "bom", "date_out_of_window")
TOKEN_STEPS = ("ok", "tok500", "tok400", "tok_not_json", "tok_echo", "tok_short", "tok_timeout", "tok_429")
# steps after which the read can never yield query rows (the judge's own list)
FATAL_STEPS = frozenset(s for s in API_STEPS if s not in ("ok", "partial", "extra_fields", "empty_obj"))


class WRGoogle:
    """oauth2.googleapis.com/token and www.googleapis.com/webmasters/v3/{sites, .../searchAnalytics/query}, per
    property. ``script`` / ``token_script``: one step per request (the last repeats while ``repeat`` is set)."""

    def __init__(self, props: dict, dates=(10, 150)):
        self.clock = GF.Clock()
        self.props = {p: [tuple(r) for r in rows] for p, rows in props.items()}
        self.sites = [{"siteUrl": p, "permissionLevel": "siteFullUser"} for p in props]
        self.dates_clicks = tuple(dates)
        self.requests: list = []
        self.tokens: list = []
        self.script: list = []
        self.token_script: list = []
        self.repeat = False
        self.served: list = []                # (prop, key, clicks, impressions) of every query row served
        self.on_request = None
        self.secret_rows = False              # serve the live access token / Bing key / service token as queries
        self._last_page = None
        self._rows_cache: dict = {}
        self.timeout_s = 10                   # set from the harness's SEO_GSC_TIMEOUT_SECONDS

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _pop(self, lst):
        if not lst:
            return "ok"
        if len(lst) == 1 and self.repeat:
            return lst[0]
        return lst.pop(0)

    def handle(self, request):
        body = request.read()
        self.requests.append({"method": request.method, "host": request.url.host,
                              "path": request.url.raw_path.decode(), "body": body})
        if self.on_request is not None:
            self.on_request(self, request)
        if request.url.host == "oauth2.googleapis.com":
            return self._token(self._pop(self.token_script))
        step = self._pop(self.script)
        if step != "ok":
            r = self._storm(step, request, body)
            if r is not None:
                return r
        auth = request.headers.get("authorization", "")
        if not self.tokens or auth != f"Bearer {self.tokens[-1]}":
            return httpx.Response(401, json={"error": {"code": 401, "message": "unauthorized",
                                                       "errors": [{"reason": "authError"}]}})
        path = request.url.raw_path.decode()
        if request.method == "GET" and path == "/webmasters/v3/sites":
            return httpx.Response(200, json={"siteEntry": self.sites})
        if request.method == "POST" and path.endswith("/searchAnalytics/query"):
            prop = unquote(path.split("/sites/", 1)[1].rsplit("/searchAnalytics", 1)[0])
            if prop not in self.props:
                return httpx.Response(403, json={"error": {"code": 403, "message": "no",
                                                           "errors": [{"reason": "insufficientPermissions"}]}})
            out = self.query(prop, json.loads(body), step)
            if step == "dup_key":                 # a duplicated top-level key: the first "rows" is empty
                raw = json.dumps(out)
                return httpx.Response(200, content=('{"rows": [], ' + raw[1:]).encode())
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})

    def _token(self, step):
        if step == "tok500":
            return httpx.Response(500, json={"error": "internal"})
        if step == "tok_429":
            return httpx.Response(429, json={"error": "rate"}, headers={"Retry-After": "7"})
        if step == "tok400":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
        if step == "tok_not_json":
            return httpx.Response(200, content=b"<html>token</html>", headers={"content-type": "text/html"})
        if step == "tok_echo":                    # an error body that carries key material
            return httpx.Response(400, json={"error": "invalid_grant",
                                             "error_description": "bad key " + GF.pem_fragments()[0]})
        if step == "tok_timeout":                 # the client's own timeout elapses (the fake clock), then it fires
            self.clock.advance(self.timeout_s)
            raise httpx.ReadTimeout("token timeout")
        tok = f"fake-access-token-{len(self.tokens) + 1:04d}-" + "t" * 24
        self.tokens.append(tok)
        return httpx.Response(200, json={"access_token": tok, "expires_in": 60 if step == "tok_short" else 3600,
                                         "token_type": "Bearer"})

    def _storm(self, step, request, body):
        err = {"error": {"code": 0, "message": "x", "errors": [{"reason": "backendError"}]}}
        if step in ("429", "429ra5", "429ra60"):
            hdr = {"Retry-After": step[5:]} if step.startswith("429ra") else {}
            return httpx.Response(429, json={"error": {"code": 429, "message": "rate", "errors": [
                {"reason": "rateLimitExceeded"}]}}, headers=hdr)
        if step in ("500", "502", "503", "504", "400", "404"):
            return httpx.Response(int(step), json=err)
        if step == "403quota":
            return httpx.Response(403, json={"error": {"code": 403, "message": "quota", "errors": [
                {"reason": "quotaExceeded"}]}})
        if step == "403perm":
            return httpx.Response(403, json={"error": {"code": 403, "message": "no", "errors": [
                {"reason": "insufficientPermissions"}]}})
        if step == "401":
            return httpx.Response(401, json={"error": {"code": 401, "message": "expired",
                                                       "errors": [{"reason": "authError"}]}})
        if step == "timeout":
            self.clock.advance(self.timeout_s)
            raise httpx.ReadTimeout("read timeout")
        if step == "connect":
            raise httpx.ConnectError("connect refused")
        if step == "drip":
            return httpx.Response(200, stream=GF.Drip(b'{"rows": []}' * 50, self.clock, 2.0))
        if step == "endless":
            return httpx.Response(200, stream=GF.Endless())
        if step == "huge_body":
            return httpx.Response(200, content=b'{"rows": [' + b'{"keys":["x"],"clicks":0,"impressions":1},' *
                                  250_000 + b'{"keys":["y"],"clicks":0,"impressions":1}]}')
        if step == "not_json":
            return httpx.Response(200, content=b"not json at all {")
        if step == "truncated":
            return httpx.Response(200, content=b'{"rows": [{"keys": ["rug cleaner"], "clicks": 1, "impr')
        if step == "html":
            return httpx.Response(200, content=b"<html><body>Sign in</body></html>",
                                  headers={"content-type": "text/html"})
        if step == "gzip":
            import gzip
            return httpx.Response(200, content=gzip.compress(b'{"rows": []}'), headers={"content-encoding": "gzip"})
        if step == "bom":
            return httpx.Response(200, content=b"\xef\xbb\xbf{\"rows\": []}")
        if step == "key_echo":                    # Google's error body echoing the bearer token and key material
            tok = self.tokens[-1] if self.tokens else "none"
            return httpx.Response(500, json={"error": {"code": 500, "message": f"token {tok} key "
                                                       f"{GF.pem_fragments()[1]}", "errors": [{"reason": tok}]}})
        return None                               # a row-level mutation: applied in query()

    def query(self, prop: str, q: dict, step: str = "ok") -> dict:
        dims, start, end = q["dimensions"], q["startDate"], q["endDate"]
        from datetime import date, timedelta
        if dims == ["date"]:
            rows = []
            d = date.fromisoformat(start)
            while d <= date.fromisoformat(end):
                c, i = self.dates_clicks
                rows.append({"keys": [d.isoformat()], "clicks": c, "impressions": i, "ctr": c / i if i else 0,
                             "position": 7.5})
                d += timedelta(days=1)
            if step == "date_out_of_window":
                rows.append({"keys": ["1999-01-01"], "clicks": 0, "impressions": 1})
            page = rows
        else:
            src = list(self.props.get(prop, []))
            if self.secret_rows:
                src += [(s, 0, 25, 3.0) for s in (self.tokens[-1] if self.tokens else "", BF.KEY_TEXT,
                                                  H.SERVICE_TOKEN, GF.pem_fragments()[2]) if s]
            ck = (prop, len(src), self.secret_rows and len(self.tokens))
            if self._rows_cache.get("k") != ck:
                self._rows_cache = {"k": ck, "rows": [
                    {"keys": [k], "clicks": c, "impressions": i, "ctr": (c / i if i else 0), "position": p}
                    for k, c, i, p in src]}
            rows = self._rows_cache["rows"]
            sr, lim = q.get("startRow", 0), q.get("rowLimit", 1000)
            page = rows[sr:sr + lim]
            if step == "repeat_page" and self._last_page:
                page = self._last_page
            if step == "partial":
                page = page[:len(page) // 2]
            self._last_page = page
        page = [dict(r) for r in page]
        if page and step in ("nan", "negative", "huge_int", "clicks_gt_impr", "keys_arity", "extra_fields"):
            r = page[0]
            if step == "nan":
                r["position"] = float("nan")
            elif step == "negative":
                r["impressions"] = -1
            elif step == "huge_int":
                r["impressions"] = 10 ** 30
            elif step == "clicks_gt_impr":
                r["clicks"] = r["impressions"] + 5
            elif step == "keys_arity":
                r["keys"] = r["keys"] + ["extra"]
            elif step == "extra_fields":
                r["unknownField"] = {"x": 1}
        out = {"rows": page, "responseAggregationType": q["aggregationType"]}
        if step == "wrong_agg":
            out["responseAggregationType"] = "byPage"
        if step == "rows_not_list":
            out["rows"] = {"0": page}
        if step == "empty_obj":
            out = {}
        if dims == ["query"]:                   # every row handed out (an upper bound on what can be counted)
            for r in page:
                self.served.append((prop, r["keys"][0], r["clicks"], r["impressions"]))
        return out

    def api_requests(self) -> list:
        return [r for r in self.requests if r["host"] == "www.googleapis.com"]


BING_STEPS = ("ok", "429", "500", "502", "503", "504", "400key", "400echo", "400shape", "401", "403", "not_json",
              "huge", "timeout", "connect", "bad_date", "negative", "clicks_gt_impr", "rows_not_list", "nan",
              "many_fields", "html")


def bing_script(steps: list, clock) -> list:
    """Bing storm steps as bing_fakes.FakeBing script items (callables where the transport must fail)."""
    out = []
    for s in steps:
        if s == "ok":
            out.append(None)
        elif s in ("429", "500", "502", "503", "504", "401", "403"):
            out.append((int(s), {}))
        elif s == "400key":
            out.append((400, {"ErrorCode": 3, "Message": "InvalidApiKey"}))
        elif s == "400echo":                       # an error message that repeats the key from the URL
            out.append((400, {"ErrorCode": 14, "Message": f"NotAuthorized apikey={BF.KEY_TEXT}"}))
        elif s == "400shape":
            out.append((400, {"ErrorCode": "x", "Message": None, "Url": f"?apikey={BF.KEY_TEXT}"}))
        elif s == "not_json":
            out.append((200, b"<d>not json"))
        elif s == "html":
            out.append((200, b"<html>Bing</html>", {"content-type": "text/html"}))
        elif s == "huge":
            out.append((200, b'{"d": [' + b'{"Query": "q", "Clicks": 0},' * 400_000 + b'{}]}'))
        elif s == "timeout":
            out.append(lambda req, c=clock: _raise(httpx.ReadTimeout("t"), c, 10))   # the client's 10 s timeout
        elif s == "connect":
            out.append(lambda req: _raise(httpx.ConnectError("refused")))
        elif s == "bad_date":
            out.append((200, {"d": [{**BF.row("rug cleaner", 1, 9), "Date": "/Date(abc)/"}]}))
        elif s == "negative":
            out.append((200, {"d": [BF.row("rug cleaner", -1, 9)]}))
        elif s == "clicks_gt_impr":
            out.append((200, {"d": [BF.row("rug cleaner", 50, 9)]}))
        elif s == "rows_not_list":
            out.append((200, {"d": {"0": BF.row("rug cleaner", 1, 9)}}))
        elif s == "nan":
            out.append((200, b'{"d": [{"Query": "rug cleaner", "Clicks": NaN, "Impressions": 9, '
                             b'"Date": "/Date(1791183600000-0700)/"}]}'))
        elif s == "many_fields":
            out.append((200, {"d": [{**BF.row("rug cleaner", 1, 9), **{f"F{i}": i for i in range(80)}}]}))
        else:
            raise ValueError(s)
    return out


def _raise(exc, clock=None, seconds=0.0):
    if clock is not None:
        clock.sleep(seconds)
    raise exc


class WRBing(object):
    """tests/bing_fakes.FakeBing with storm steps; an ``ok`` step answers as the fake normally does."""

    def __init__(self, rows=None, steps=None):
        self.fake = BF.FakeBing()
        if rows is not None:
            self.fake.rows = [BF.row(q, c, i, w) for q, c, i, w in rows]
        self.served: list = []
        self.steps = list(steps or [])
        script = bing_script(self.steps, self.fake.clock)
        inner = self.fake.handle

        def handle(request):
            item = script.pop(0) if script else None
            if item is None:
                self.fake.script = []
                r = inner(request)
                if r.status_code == 200:
                    self.served += [(x["Query"], x["Clicks"], x["Impressions"]) for x in self.fake.rows]
                return r
            self.fake.script = [item]
            return inner(request)
        self.handle = handle

    def client(self):
        return BF.bw.BingWebmasterClient(BF.key(), transport=httpx.MockTransport(self.handle),
                                         monotonic=self.fake.clock.mono, sleep=self.fake.clock.sleep)


# ============================================================================================ log capture

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.service: list = []
        self.third: list = []

    def emit(self, record):
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a message that cannot be formatted is kept raw
            text = f"{record.msg!r} {record.args!r}"
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        (self.service if record.name.startswith("seo") or record.name == "root" else self.third).append(text)


# ============================================================================================ case setup

def setup(tmp):
    _RID[0] = 0
    cap = _Capture()
    logging.getLogger().addHandler(cap)
    old = {n: logging.getLogger(n).level for n in ("seo", "httpx", "httpcore")}
    logging.getLogger("seo").setLevel(logging.DEBUG)          # the service's own records, all levels
    logging.getLogger("httpx").setLevel(logging.INFO)         # third-party records an INFO deployment would write
    return SimpleNamespace(tmp=Path(tmp), steps=[], runs=[], hs=[], cap=cap, old_levels=old, pii=[], pii_gaps=[],
                           pii_bases=[], extra={}, n=0, bodies=[], probe=None, second=None, kill=None)


def teardown(ctx) -> None:
    logging.getLogger().removeHandler(ctx.cap)
    for n, lv in ctx.old_levels.items():
        logging.getLogger(n).setLevel(lv)
    for h in ctx.hs:
        try:
            h.svc.close()
        except Exception:  # noqa: BLE001 - a harness already closed is fine
            pass


DEFAULT_QUERIES = None        # tests/test_priya_service.py QUERIES (the suite's own run), set from the module


def pinned_pii() -> list:
    """The personal data the suite's own query tables carry (tests/test_priya.py SITE, test_priya_service.py QUERIES:
    the e-mail rows those tests assert are dropped)."""
    if "pii" not in _BASELINE:
        import test_priya as tp
        import test_priya_service as tps
        _BASELINE["pii"] = sorted({r[0] for r in list(tp.SITE) + list(tps.QUERIES) if "@" in r[0]})
    return _BASELINE["pii"]


def _default_rows():
    import test_priya_service as tps
    return [[q, c, i, p] for q, c, i, _ctr, p in tps.QUERIES]


def build(ctx, props=None, env=None, bing=None, bing_steps=None, dates=(10, 150), globex=False, data=True,
          dedupe=True):
    """A fresh harness on a hostile Google: tenant acme (own, acme.test) bound by Andre to sc-domain:acme.test;
    ``globex``: a second own tenant (globex.test), bound to its own property when it has rows."""
    ctx.n += 1
    props = props if props is not None else {PROP: _default_rows()}
    if dedupe:                                    # Google never repeats a key (a repeat is GSC_PROTOCOL_ERROR)
        props = {p: list({r[0]: r for r in reversed(rows) if r[0]}.values())[::-1] for p, rows in props.items()}
    for rows in props.values():                   # the suite's own personal-data rows, however they are spelled
        for r in rows:
            for base in pinned_pii():
                if skeleton(base) in skeleton(r[0]) and hasattr(ctx, "pii"):
                    ctx.pii.append(r[0])
                    ctx.pii_bases.append(base)
    fake = WRGoogle(props, dates)
    d = ctx.tmp / f"h{ctx.n}"
    d.mkdir()
    env = {k: str(v) for k, v in (env or {}).items()}
    h = TG.gsc_harness(d, fake, data_dir=str(d / "data") if data else None, **env)
    ctx.hs.append(h)
    fake.timeout_s = h.settings.gsc_timeout_s
    h.tenant("acme", "own", ("acme.test",))
    h.ok(TG.bind(h, "acme", PROP))
    if globex:
        h.tenant("globex", "own", ("globex.test",))
        if PROP_B in props:
            h.ok(TG.bind(h, "globex", PROP_B))
    wb = None
    if bing is not None or bing_steps:
        wb = WRBing(bing, bing_steps)
        h.ports.first_party["bing_webmaster"] = wb.client()
    run = SimpleNamespace(h=h, fake=fake, bing=wb, env=env, settings=h.settings, posts=[], dir=d)
    ctx.runs.append(run)
    return run


def post(ctx, run, body=None, tid="acme", **kw):
    """One keyword-ledger request; what each source served and how many requests it took is kept per request."""
    b = {"request_id": rid(), **(body or {})}
    n0, s0, t0 = len(run.fake.requests), len(run.fake.served), run.fake.clock.t
    q0 = sum(1 for x in run.fake.requests if x["path"].endswith("/searchAnalytics/query") and b'"query"' in x["body"])
    bn0 = len(run.bing.fake.requests) if run.bing else 0
    bs0 = len(run.bing.served) if run.bing else 0
    bt0 = run.bing.fake.clock.t if run.bing else 0.0
    r = run.h.post(f"/tenants/{tid}/keyword-ledger", b, **kw)
    try:
        view = r.json()
    except ValueError:
        view = None
    rec = {"tid": tid, "body": b, "status": r.status_code, "view": view, "text": r.text,
           "gsc_requests": len(run.fake.requests) - n0, "served": run.fake.served[s0:],
           "query_reads": sum(1 for x in run.fake.requests if x["path"].endswith("/searchAnalytics/query")
                              and b'"query"' in x["body"]) - q0,
           "gsc_seconds": run.fake.clock.t - t0,
           "bing_requests": (len(run.bing.fake.requests) - bn0) if run.bing else 0,
           "bing_served": run.bing.served[bs0:] if run.bing else [], "bing_seconds":
           (run.bing.fake.clock.t - bt0) if run.bing else 0.0}
    run.posts.append(rec)
    ctx.bodies.append(r.text)
    return r


# ============================================================================================ personal data / secrets

def pii_corpus(name: str) -> list:
    """The in-repo generators and drop tables (nothing hand-written here): attack_corpus.cases() / gaps() and the
    round 8 / 9 / 10 drop tables. Returns (text, base value or None)."""
    if name == "attack_corpus":
        return [(t, AC.BASES[b][1]) for b, _n, t in AC.cases()]
    if name == "attack_corpus_gaps":                     # the disclosed gaps (none since review round 11)
        return [(t, AC.BASES[b][1]) for b, _n, t in AC.gaps()]
    if name == "attack_corpus_composed":                 # two obfuscations composed (review round 11)
        return [(t, AC.BASES[b][1]) for b, _n, t in AC.composed_cases()] if hasattr(AC, "composed_cases") else []
    if name == "round10_drop":
        return [(t, None) for t in R10.ROUND10_DROP]
    if name == "round9_drop":
        return [(t, None) for t in R9.ROUND9_DROP]
    if name == "round8_attacks":
        return [(t, None) for t in R8.ROUND8_ATTACKS]
    if name == "round8_smuggles":
        return [(t, None) for t in R8.ROUND8_SMUGGLES]
    raise ValueError(name)


WRAPS = ("alone", "prefix", "suffix", "both", "emoji", "rtl", "cjk", "upper", "tab", "dup_spelling")


def wrap(text: str, how: str, ctxq: str) -> list:
    if how == "alone":
        return [text]
    if how == "prefix":
        return [f"{ctxq} {text}"]
    if how == "suffix":
        return [f"{text} {ctxq}"]
    if how == "both":
        return [f"{ctxq} {text} {ctxq}"]
    if how == "emoji":
        return [f"\U0001f525 {text} \U0001f9f9"]
    if how == "rtl":
        return [f"‏{text}‏ مرحبا"]
    if how == "cjk":
        return [f"東京 {text}"]
    if how == "upper":
        return [text.upper()]
    if how == "tab":
        return [f"{ctxq}\t{text}"]
    if how == "dup_spelling":
        return [text, text.upper() if text.upper() != text else text + " "]
    raise ValueError(how)


def disclosed_gaps() -> set:
    """The spellings the suite itself pins as disclosed residuals (attack_corpus.gaps(); the composed residual class
    of tests/test_query_privacy_r11.py ``_pinned_composed``): judged as SHOULD, never as a new MUST failure."""
    if "gaps" not in _BASELINE:
        out = {t for t, _b in pii_corpus("attack_corpus_gaps")}
        try:
            import test_query_privacy_r11 as r11
            out |= {t for _b, n, t in AC.composed_cases() if r11._pinned_composed(n)}
        except (ImportError, AttributeError):
            pass
        _BASELINE["gaps"] = out
    return _BASELINE["gaps"]


def plant_pii(ctx, entries: list, how: str, ctxq: str) -> list:
    rows = []
    gap_texts = disclosed_gaps()
    for i, (t, base) in enumerate(entries):
        for w in wrap(t, how, ctxq):
            if t in gap_texts:
                ctx.pii_gaps.append(w)
            else:
                ctx.pii.append(w)
                if base is not None:
                    ctx.pii_bases.append(base)
            rows.append([w, 0, 25 + i % 7, 4.0])
    return rows


# ============================================================================================ actions

def ledger_run(ctx, query=None, placement="with_site", sources="gsc", runs=1):
    """One (or ``runs``) ledger runs with ``query`` planted among the suite's own query rows."""
    rows = _default_rows()
    q = query
    if q is not None:
        if placement == "alone":
            rows = [[q, 3, 60, 4.0]]
        elif placement == "duplicate_spelling":
            rows += [[q, 3, 60, 4.0], [q.upper() if q.upper() != q else q + " ", 1, 20, 5.0]]
        elif placement == "near_duplicate":
            rows += [[q, 3, 60, 4.0], [q + "s", 1, 20, 5.0], [" ".join(reversed(q.split())), 1, 22, 5.0]]
        elif placement == "emoji":
            rows += [[f"{q} \U0001f9f9✨", 3, 60, 4.0]]
        elif placement == "rtl":
            rows += [[f"‫{q}‬ שלום", 3, 60, 4.0]]
        elif placement == "cjk":
            rows += [[f"{q} 清洁 クリーニング", 3, 60, 4.0]]
        elif placement == "huge":
            rows += [[((q + " ") * 60)[:4000], 3, 60, 4.0], [q * max(1, 4000 // max(1, len(q))), 1, 30, 4.0]]
        else:
            rows += [[q, 3, 60, 4.0]]
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"} if sources == "gsc,bing" else {}
    run = build(ctx, {PROP: rows}, bing=[(r[0], r[1], r[2], 0) for r in rows[:8]] if "bing" in sources else None)
    r = None
    for _ in range(int(runs)):
        r = post(ctx, run, body)
    return r


def pii_queries(ctx, corpus, wrap_as="alone", sources="gsc"):
    """Every case of one in-repo personal-data / secret corpus, served as query rows (wrapped as named), next to the
    suite's own clean queries; Bing serves the same texts when it is a source."""
    entries = pii_corpus(corpus)
    import test_priya as tp
    ctxq = tp.SITE[0][0] if isinstance(tp.SITE[0], tuple) else "rug cleaner"
    rows = _default_rows()
    pii_rows = plant_pii(ctx, entries, wrap_as, ctxq)
    run = build(ctx, {PROP: rows + pii_rows},
                bing=[(r[0], r[1], r[2], k % 3) for k, r in enumerate(pii_rows[:300])] if "bing" in sources else None)
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"} if "bing" in sources else {}
    return post(ctx, run, body)


def secret_queries(ctx, kind):
    """The harness's own live secrets served as query text (the access token Google just issued, the Bing key, the
    service token, a line of the service account's private key) or echoed in an error body."""
    run = build(ctx, bing=[("rug cleaner", 1, 30, 0)], bing_steps=["400echo"] if kind == "bing_error_echo" else None)
    ctx.extra["secret_kind"] = kind
    if kind == "as_queries":
        run.fake.secret_rows = True
        ctx.extra["secret_rows"] = True
    elif kind == "google_error_echo":
        run.fake.script = ["ok", "ok", "key_echo"]
        run.fake.repeat = True
    elif kind == "token_error_echo":
        run.fake.token_script = ["tok_echo"]
        run.fake.repeat = True
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"}
    return post(ctx, run, body)


def sampling(ctx, shape):
    """Search Console totals that disagree with its rows: anonymised remainders, rows above the totals, zero totals,
    row caps (truncation), a property with only anonymised demand."""
    rows = _default_rows()
    env, dates = {}, (10, 150)
    if shape == "anonymised_remainder":
        dates = (50, 5000)
    elif shape == "rows_exceed_totals":
        dates = (0, 1)
    elif shape == "zero_totals_zero_rows":
        rows, dates = [], (0, 0)
    elif shape == "only_anonymised":
        rows, dates = [], (40, 900)
    elif shape == "row_cap_truncation":
        rows = [[f"term {i} rug cleaner", 1, 30, 5.0] for i in range(260)]
        env = {"SEO_GSC_PAGE_ROWS": "100", "SEO_GSC_MAX_PAGES": "2"}
    elif shape == "max_rows_truncation":
        rows = [[f"term {i} sofa cleaner", 1, 25, 6.0] for i in range(1600)]
        env = {"SEO_GSC_PAGE_ROWS": "400", "SEO_GSC_MAX_ROWS": "1000"}
    elif shape == "zero_impression_rows":
        rows = rows + [[f"zero {i} cleaner", 0, 0, 0.0] for i in range(5)]
    elif shape == "exact_page_boundary":
        rows = [[f"term {i} carpet cleaner", 1, 30, 5.0] for i in range(200)]
        env = {"SEO_GSC_PAGE_ROWS": "100"}
    else:
        raise ValueError(shape)
    run = build(ctx, {PROP: rows}, env=env, dates=dates)
    return post(ctx, run)


def gsc_storm(ctx, script, sources="gsc", token=None):
    """A storm over the Search Console requests of one run (one step per request; the last step repeats)."""
    run = build(ctx, bing=[("rug cleaner", 1, 30, 0)] if "bing" in sources else None)
    run.fake.script = script.split(",")
    run.fake.repeat = True
    if token:
        run.fake.token_script = token.split(",")
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"} if "bing" in sources else {}
    return post(ctx, run, body)


def bing_storm(ctx, script):
    """A storm over Bing's GetQueryStats (Search Console answers normally): Bing's failure is PARTIAL, never zero."""
    run = build(ctx, bing=[("rug cleaner", 4, 60, 0), ("rug cleaner", 3, 40, 1)], bing_steps=script.split(","))
    return post(ctx, run, {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"})


def budget(ctx, kind):
    """The keyword ledger's own daily read cap (SEO_GSC_LEDGER_READS_PER_DAY), kept apart from audits'
    (SEO_GSC_READS_PER_DAY), and the per-read request cap (SEO_GSC_MAX_REQUESTS)."""
    spec = dict(p.split("=") for p in kind.split(","))
    env = {}
    if "ledger_cap" in spec:
        env["SEO_GSC_LEDGER_READS_PER_DAY"] = spec["ledger_cap"]
    if "audit_cap" in spec:
        env["SEO_GSC_READS_PER_DAY"] = spec["audit_cap"]
    if "max_requests" in spec:
        env.update(SEO_GSC_MAX_REQUESTS=spec["max_requests"], SEO_GSC_MAX_PAGES="1", SEO_GSC_PAGE_ROWS="1000",
                   SEO_GSC_MAX_ROWS="1000")
    run = build(ctx, env=env)
    if "storm" in spec:
        run.fake.script = spec["storm"].split("+")
        run.fake.repeat = True
    order = spec.get("order", "ledger")
    audits = int(spec.get("audits", 0))
    ctx.extra["audits"] = []
    if order == "audits_first":
        for _ in range(audits):
            ctx.extra["audits"].append(_audit(ctx, run))
    r = None
    for _ in range(int(spec.get("runs", 1))):
        r = post(ctx, run)
    if order != "audits_first":
        for _ in range(audits):
            ctx.extra["audits"].append(_audit(ctx, run))
    return r


def _audit(ctx, run):
    r = run.h.post("/tenants/acme/audits", {"request_id": rid(), "domain": "acme.test", "paths": ["/"]}, andre=True)
    ctx.bodies.append(r.text)
    try:
        e = next(x for x in r.json()["report"]["agents"] if x["task"] == "search_performance")
        return e.get("reason")
    except Exception:  # noqa: BLE001 - reported as what it is
        return f"http_{r.status_code}"


def concurrent(ctx, second):
    """A second request arrives while the first run's first Search Console query request is in flight."""
    props = {PROP: _default_rows(), PROP_B: [["globex widget repair", 5, 90, 3.0], ["widget repair near me", 1, 12,
                                                                                    5.0]]}
    run = build(ctx, props, globex=True)
    h = run.h
    got = {}

    def call(tid, body):
        from errors import SeoError
        b = MODELS.KeywordLedgerCreate.model_validate(body).model_dump(mode="json")
        try:
            out = h.svc.run_keyword_ledger("seo_agent", tid, b, andre=False)
            return {"status": 201, "body": out}
        except SeoError as e:
            return {"status": {"Conflict": 409, "Forbidden": 403, "NotFound": 404, "Invalid": 422}.get(
                type(e).__name__, 400), "body": {"detail": e.reason}}

    first = {"request_id": rid()}

    def hook(fk, request):
        if "r" in got or not request.url.raw_path.decode().endswith("/searchAnalytics/query"):
            return
        got["r"] = "pending"
        if second == "same_tenant_new_request":
            got["r"] = call("acme", {"request_id": rid()})
        elif second == "same_request_replayed":
            got["r"] = call("acme", dict(first))
        elif second == "other_tenant_runs":
            got["r"] = call("globex", {"request_id": rid()})
        elif second == "same_tenant_with_bing":
            got["r"] = call("acme", {"request_id": rid(), "sources": ["gsc", "bing"], "bing_site_domain": "acme.test"})
        got["running"] = len(h.svc.running_keyword_runs)
    run.fake.on_request = hook
    ctx.extra["first_body"] = first
    b = dict(first)
    b.pop("request_id")
    n0 = len(run.fake.requests)
    r = h.post("/tenants/acme/keyword-ledger", first)
    ctx.bodies.append(r.text)
    run.posts.append({"tid": "acme", "body": first, "status": r.status_code, "view": r.json(), "text": r.text,
                      "gsc_requests": len(run.fake.requests) - n0, "served": list(run.fake.served),
                      "gsc_seconds": run.fake.clock.t - 1000.0, "bing_requests": 0, "bing_served": [],
                      "bing_seconds": 0.0})
    s = got.get("r")
    ctx.second = {"kind": second, "status": None if not isinstance(s, dict) else s["status"],
                  "body": None if not isinstance(s, dict) else s["body"], "running": got.get("running")}
    if isinstance(s, dict):
        ctx.bodies.append(json.dumps(s["body"], ensure_ascii=False, default=str))
    return r


def cross_tenant(ctx, probe):
    """globex (another tenant) and the hub try to reach acme's keyword-ledger runs, property or request ids."""
    props = {PROP: _default_rows(), PROP_B: [["globex widget repair", 5, 90, 3.0]]}
    run = build(ctx, props, globex=probe != "unbound_other_tenant_runs")
    h = run.h
    if probe == "unbound_other_tenant_runs":
        h.tenant("globex", "own", ("globex.test",))
    first = {"request_id": rid()}
    a = h.post("/tenants/acme/keyword-ledger", first)
    run.posts.append({"tid": "acme", "body": first, "status": a.status_code, "view": a.json(), "text": a.text,
                      "gsc_requests": len(run.fake.requests), "served": list(run.fake.served),
                      "gsc_seconds": run.fake.clock.t - 1000.0, "bing_requests": 0, "bing_served": [],
                      "bing_seconds": 0.0})
    ctx.bodies.append(a.text)
    acme = a.json()
    n0, s0 = len(run.fake.requests), len(run.fake.served)
    hub_g = {"caller": "hub", "tenant": "globex"}
    calls = {
        "hub_runs_for_acme": lambda: h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}, caller="hub",
                                            tenant="acme"),
        "hub_lists_acme_with_globex_token": lambda: h.get("/tenants/acme/keyword-ledger-runs", **hub_g),
        "hub_lists_acme": lambda: h.get("/tenants/acme/keyword-ledger-runs", caller="hub", tenant="acme"),
        "globex_lists_its_runs": lambda: h.get("/tenants/globex/keyword-ledger-runs"),
        "compliance_lists_globex": lambda: h.get("/tenants/globex/keyword-ledger-runs", caller="compliance_38"),
        "globex_runs_own_property": lambda: h.post("/tenants/globex/keyword-ledger", {"request_id": rid()}),
        "unbound_other_tenant_runs": lambda: h.post("/tenants/globex/keyword-ledger", {"request_id": rid()}),
        "globex_names_acme_property": lambda: h.post("/tenants/globex/keyword-ledger", {
            "request_id": rid(), "property": PROP}),
        "globex_bing_on_acme_domain": lambda: h.post("/tenants/globex/keyword-ledger", {
            "request_id": rid(), "sources": ["gsc", "bing"], "bing_site_domain": "acme.test"}),
        "globex_reuses_acme_request_id": lambda: h.post("/tenants/globex/keyword-ledger", dict(first)),
        "path_traversal": lambda: h.post("/tenants/globex%2F..%2Facme/keyword-ledger", {"request_id": rid()}),
        "tenant_case_variant": lambda: h.post("/tenants/ACME/keyword-ledger", {"request_id": rid()}),
        "svc_view_under_other_tenant": None,
    }
    if probe == "svc_view_under_other_tenant":
        from errors import SeoError
        try:
            v = h.svc.keyword_run_view("globex", acme["run_id"])
            status, text = 200, json.dumps(v, default=str)
        except SeoError as e:
            status, text = 404 if type(e).__name__ == "NotFound" else 400, e.reason
    else:
        r = calls[probe]()
        status, text = r.status_code, r.text
    ctx.bodies.append(text)
    gl = h.get("/tenants/globex/keyword-ledger-runs")
    ctx.bodies.append(gl.text)
    paths = [x["path"] for x in run.fake.requests[n0:] if x["path"].endswith("/query")]
    acme_texts = [q for q, *_ in _default_rows() if len(q) >= 6]
    ctx.probe = {"probe": probe, "status": status, "text": text, "globex_list": gl.text,
                 "acme_property_reads_after": [p for p in paths if "acme.test" in unquote(p)],
                 "served_after": run.fake.served[s0:],
                 "foreign": [acme.get("run_id") or "", acme.get("ledger_sha256") or "", PROP] + acme_texts}
    return None


def idempotency(ctx, kind):
    run = build(ctx, data=True)
    body = {"request_id": rid()}
    a = post(ctx, run, body)
    n1 = len(run.fake.requests)
    h = run.h
    if kind == "same_request_twice":
        b = h.post("/tenants/acme/keyword-ledger", body)
    elif kind == "same_request_five_times":
        for _ in range(4):
            b = h.post("/tenants/acme/keyword-ledger", body)
    elif kind == "same_id_other_body":
        b = h.post("/tenants/acme/keyword-ledger", {**body, "sources": ["gsc", "bing"],
                                                    "bing_site_domain": "acme.test"})
    elif kind == "same_request_after_restart":
        h2 = h.restart()
        ctx.hs.append(h2)
        run.h = h2
        b = h2.post("/tenants/acme/keyword-ledger", body)
    elif kind == "same_request_other_caller":
        b = h.post("/tenants/acme/keyword-ledger", body, caller="dashboard")
    else:
        raise ValueError(kind)
    ctx.bodies.append(b.text)
    ctx.second = {"kind": kind, "status": b.status_code, "body": b.json(), "first": a.json(),
                  "requests_added": len(run.fake.requests) - n1,
                  "started": len(run.h.ledger.of_type("keyword_ledger_started"))}
    return b


KILLS = {"capability:keywords": "KILLED_CAPABILITY", "tenant:acme": "KILLED_TENANT", "global": "KILLED_GLOBAL",
         "write": "KILLED_WRITE", "provider:gsc": "GSC_KILLED", "provider:bing": "BING_KILLED"}


def kill(ctx, switch, when="before"):
    """A kill switch engaged before the run, or in memory while the run's first query request is in flight; or
    Priya restricted by the department manager."""
    run = build(ctx, bing=[("rug cleaner", 4, 60, 0)])
    h = run.h
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"}
    ctx.kill = {"switch": switch, "when": when}
    if switch == "lifecycle:restricted":
        h.ok(h.post("/agents/priya/lifecycle", {"request_id": rid(), "to": "restricted", "expected_state": "active",
                                                "reason": "FOUNDER_DECISION"}, caller="dashboard", andre=True))
        return post(ctx, run, body)
    kind, _, name = switch.partition(":")
    if when == "before":
        h.ok(h.switch(switch))
    else:
        state = {"done": False}

        def on(fk, req):
            if not state["done"] and req.url.raw_path.decode().endswith("/searchAnalytics/query"):
                state["done"] = True
                ctx.kill["requests_at_kill"] = len(fk.requests)
                if kind in ("capability", "provider"):
                    h.svc.switches[kind][name] = True     # as an engaged switch is held in memory
                elif kind == "tenant":
                    h.svc.tenants[name]["killed"] = True
                else:
                    h.svc.switches[kind] = True
        run.fake.on_request = on
    return post(ctx, run, body)


def scale(ctx, shape):
    """Enterprise-scale properties within the caps: many thousand query rows over many pages, past the row cap,
    with Bing measuring thousands more."""
    n, env, nb = {"5k_one_page": (5000, {}, 0), "10k_pages_of_2000": (10000, {"SEO_GSC_PAGE_ROWS": "2000"}, 0),
                  "25k_rows_cap_8k": (25000, {"SEO_GSC_MAX_ROWS": "8000", "SEO_GSC_PAGE_ROWS": "4000"}, 0),
                  "6k_with_bing_3k": (6000, {}, 3000)}[shape]
    heads = ("rug cleaner", "carpet cleaner", "sofa cleaner", "tile cleaner", "drape cleaner")
    rows = [[f"{heads[i % 5]} {('near me', 'cost', 'how to', 'best', 'cheap')[i // 5 % 5]} v{i}", i % 4,
             10 + (i * 7) % 400, 1.0 + (i % 30)] for i in range(n)]
    bing = [(rows[i][0], 1, 30, i % 4) for i in range(nb)] if nb else None
    run = build(ctx, {PROP: rows}, env=env, bing=bing)
    ctx.extra["scale"] = n
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"} if nb else {}
    return post(ctx, run, body)


def generated_queries(ctx, spec):
    """grammar.priya_query_set: a whole hostile property (rows, totals, PII references, caps, sources)."""
    g = json.loads(spec)
    rows = [list(r) for r in g["rows"]]
    if g.get("pii"):
        import test_priya as tp
        ctxq = tp.SITE[0][0]
        for corpus, idx, how in g["pii"]:
            entries = pii_corpus(corpus)
            if entries:
                rows += plant_pii(ctx, [entries[idx % len(entries)]], how, ctxq)
    run = build(ctx, {PROP: rows}, env=g.get("env"), dates=tuple(g.get("dates") or (10, 150)),
                bing=[tuple(b) for b in g["bing"]] if g.get("bing") is not None else None, dedupe=False)
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"} if g.get("bing") is not None else {}
    r = None
    for _ in range(int(g.get("runs", 1))):
        r = post(ctx, run, body)
    return r


def generated_storm(ctx, spec):
    """grammar.priya_storm: storm scripts over Search Console's token and API requests and Bing's, caps, runs."""
    g = json.loads(spec)
    rows = _default_rows()[: max(1, int(g.get("n_rows", 8)))]
    run = build(ctx, {PROP: rows}, env=g.get("env"),
                bing=[(r[0], r[1], r[2], 0) for r in rows] if g.get("bing") else None,
                bing_steps=g.get("bing_script") if g.get("bing") else None)
    run.fake.script = list(g.get("script") or [])
    run.fake.token_script = list(g.get("token_script") or [])
    run.fake.repeat = bool(g.get("repeat"))
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"} if g.get("bing") else {}
    r = None
    for _ in range(int(g.get("runs", 1))):
        r = post(ctx, run, body)
    return r


ACTIONS = {"ledger_run": ledger_run, "pii_queries": pii_queries, "secret_queries": secret_queries,
           "sampling": sampling, "gsc_storm": gsc_storm, "bing_storm": bing_storm, "budget": budget,
           "concurrent": concurrent, "cross_tenant": cross_tenant, "idempotency": idempotency, "kill": kill,
           "scale": scale, "generated_queries": generated_queries, "generated_storm": generated_storm}


# ============================================================================================ observation (the judge)

def stored_blob(h, d: Path) -> str:
    """Every byte the service keeps: its data directory, the ledger's events (payloads and summaries) and the
    append-only log's export."""
    parts = []
    data = d / "data"
    if data.is_dir():
        for p in sorted(data.rglob("*")):
            if p.is_file():
                parts.append(p.read_bytes().decode("utf-8", "replace"))
    parts.append(json.dumps([e.get("_payload") for e in h.ledger.events], ensure_ascii=False, default=str))
    parts.append(json.dumps([e.get("summary") for e in h.ledger.events], ensure_ascii=False, default=str))
    try:
        parts.append(json.dumps(h.svc.audit_export(1, 100_000), ensure_ascii=False, default=str))
    except Exception as e:  # noqa: BLE001
        parts.append(f"export failed: {type(e).__name__}")
    return "\n".join(parts)


def baseline() -> str:
    """What a run with no query rows at all stores (constant strings: reason codes, method text, field names): a
    query text that is a substring of it cannot be told apart from it."""
    if "blob" not in _BASELINE:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="warroom-base-") as tmp:
            c = SimpleNamespace(tmp=Path(tmp), n=0, runs=[], hs=[], bodies=[], steps=[])
            run = build(c, {PROP: []}, bing=[])
            post(c, run, {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"})
            _BASELINE["blob"] = stored_blob(run.h, run.dir) + "\n".join(c.bodies)
            for h in c.hs:
                h.svc.close()
    return _BASELINE["blob"]


def _ledgers(ctx):
    """(run, post, ledger) for every request whose answer carries a ledger."""
    for run in ctx.runs:
        for p in run.posts:
            v = p["view"]
            if isinstance(v, dict) and isinstance(v.get("ledger"), dict):
                yield run, p, v["ledger"]


def _row_texts(led) -> list:
    out = []
    for r in led.get("rows") or []:
        q = r.get("query")
        out.append(q.get("text") if isinstance(q, dict) else q)
    for f in ((led.get("frames") or {}).get("kept") or []):
        out.append((f.get("frame") or {}).get("text") if isinstance(f.get("frame"), dict) else f.get("frame"))
    for it in ((led.get("handoff") or {}).get("items") or []):
        q = it.get("query")
        out.append(q.get("text") if isinstance(q, dict) else q)
    return [t for t in out if isinstance(t, str)]


def _secret_values(ctx) -> dict:
    vals = dict(SECRETS)
    for run in ctx.runs:
        for i, t in enumerate(run.fake.tokens):
            vals[f"gsc_access_token_{i}"] = t
    return vals


def observe(ctx) -> dict:
    st: dict = {}
    responses = list(ctx.bodies)
    stored = [stored_blob(r.h, r.dir) for r in ctx.runs]
    service_logs = "\n".join(ctx.cap.service)
    third_logs = "\n".join(ctx.cap.third)
    st["server_errors"] = [f"{p['tid']}: {p['status']}" for r in ctx.runs for p in r.posts if p["status"] >= 500] + \
        [f"{s['action']}: {s['status']}" for s in ctx.steps if (s.get("status") or 0) >= 500]
    # ---- secrets: never in a response, a stored record or the service's own log
    hay = "\n".join(responses + stored + [service_logs])
    hay_f = fold(unquote(hay))
    sv = _secret_values(ctx)
    st["secrets_leaked"] = sorted(n for n, v in sv.items() if v and (v in hay or fold(v) in hay_f))
    st["secrets_in_third_party_logs"] = sorted(n for n, v in sv.items() if v and v in unquote(third_logs))
    # ---- personal data planted by the case: never in a ledger row, key, frame, hand-off, response, record or log
    leaks, gap_leaks = [], []
    texts = [t for _r, _p, led in _ledgers(ctx) for t in _row_texts(led)]
    norms = [my_norm(t) for t in texts]
    skels = [skeleton(t) for t in texts]
    everything = "\n".join(responses + stored + [service_logs])
    for lst, out in ((ctx.pii, leaks), (ctx.pii_gaps, gap_leaks)):
        for q in dict.fromkeys(lst):
            nq = my_norm(q)
            key = PRIYA.row_key(PRIYA.normalize(q))
            if nq and any(nq == n or (len(nq) >= 6 and nq in n) for n in norms):
                out.append(f"row text {q!r}")
            elif key in everything:
                out.append(f"row key of {q!r}")
            elif len(q) >= 6 and (q in "\n".join(stored) or q in service_logs):
                out.append(f"stored or logged {q!r}")
    for base in dict.fromkeys(ctx.pii_bases):
        sk = skeleton(base)
        for cand in (sk, sk[::-1]):
            if len(cand) >= 7 and any(cand in s for s in skels):
                leaks.append(f"payload {base[:4]}...({len(base)}) in a row")
                break
    st["pii_leaked"] = leaks
    st["pii_known_gap_leaked"] = gap_leaks
    # ---- query text never persisted (stored records and the service's log); a ledger is handed out once
    base = baseline()
    persisted = []
    blob = "\n".join(stored) + "\n" + service_logs
    for run in ctx.runs:
        served = {k for _p, k, _c, _i in run.fake.served}
        if run.bing:
            served |= {q for q, _c, _i in run.bing.served}
        for q in sorted(served):
            nq = " ".join((q or "").split())
            if len(nq) < 5 or nq in base:
                continue
            forms = {q, nq, json.dumps(q)[1:-1], json.dumps(q, ensure_ascii=False)[1:-1]}
            if any(f and len(f) >= 5 and f in blob for f in forms):
                persisted.append(q[:80])
    st["query_text_persisted"] = persisted[:20]
    # ---- volume: never invented; UNKNOWN never 0; status honest; caps; bounded work
    invented, zero, dishonest, caps, work, ok_inc = [], [], [], [], [], []
    for run, p, led in _ledgers(ctx):
        s = run.settings
        srcs = led.get("sources") or {}
        g_ok = (srcs.get("search_console") or {}).get("state") == "OK"
        b_ok = (srcs.get("bing_webmaster") or {}).get("state") == "OK"
        g_served = sum(i for _prop, _k, _c, i in p["served"])
        b_served = sum(i for _q, _c, i in p["bing_served"])
        rows = led.get("rows") or []
        g_sum = b_sum = 0
        for r in rows:
            m = r.get("metrics") or {}
            gm, bm = m.get("search_console"), m.get("bing_webmaster")
            if r.get("market_volume") is not None or r.get("difficulty") is not None:
                invented.append(f"{r.get('key')}: market_volume / difficulty {r.get('market_volume')!r}")
            if r.get("origin") == "derived" and (gm is not None or bm is not None):
                invented.append(f"{r.get('key')}: a derived variant carries a measurement")
            if r.get("volume_status") == "UNKNOWN_NOT_MEASURED" and (gm is not None or bm is not None):
                invented.append(f"{r.get('key')}: UNKNOWN_NOT_MEASURED with metrics")
            if gm is not None:
                if not g_ok:
                    invented.append(f"{r.get('key')}: Search Console numbers while it was not read")
                g_sum += gm.get("impressions") or 0
                if (gm.get("impressions") or 0) > g_served:
                    invented.append(f"{r.get('key')}: {gm.get('impressions')} impressions > {g_served} served")
            if bm is not None:
                if not b_ok:
                    invented.append(f"{r.get('key')}: Bing numbers while it was not read")
                b_sum += bm.get("impressions") or 0
            op = r.get("opportunity") or {}
            if op.get("status") != "COMPUTED" and op.get("click_gap_modeled") == 0:
                ok_inc.append(f"{r.get('key')}: opportunity {op.get('status')} shows click_gap_modeled 0")
            if r.get("origin") == "derived" and any(op.get(k) is not None for k in ("ctr_gap", "click_gap_modeled",
                                                                                      "expected_ctr")):
                zero.append(f"{r.get('key')}: a derived variant shows an opportunity number")
        pii_i = ((led.get("counts") or {}).get("pii_suspect_dropped") or {}).get("impressions") or 0
        if g_sum + b_sum + pii_i > g_served + b_served:
            invented.append(f"{g_sum} + {b_sum} + {pii_i} (pii) impressions > {g_served} + {b_served} served")
        if (led.get("difficulty") or {}).get("computed") or (led.get("market_volume") or {}).get("measured"):
            invented.append("the ledger claims a difficulty or a market volume")
        requested = [k for k, v in srcs.items() if k != "google_ads_keyword_planner"
                     and (v or {}).get("state") != "NOT_REQUESTED"]
        read = [k for k in requested if (srcs.get(k) or {}).get("state") == "OK"]
        for k in requested:
            v = srcs.get(k) or {}
            if v.get("state") != "OK":
                shown = [f for f in ("totals", "query_rows_sum", "unattributed", "rows_read", "pages_read")
                         if f in v]
                if shown:
                    zero.append(f"{k} {v.get('state')}: shows {shown} for an unread source")
        status = led.get("status")
        observed = any(r.get("origin") == "observed" for r in rows)
        want = "INCONCLUSIVE" if not read else ("EMPTY_MEASURED" if not observed else
                                                 "OK" if len(read) == len(requested) else "PARTIAL")
        if status != want:
            dishonest.append(f"status {status} but sources {[(k, (srcs.get(k) or {}).get('state')) for k in requested]}"
                             f" and observed rows={observed}: expected {want}")
        env_out = ((p["view"] or {}).get("envelope") or {}).get("outcome")
        if env_out == "OK" and status not in ("OK", "EMPTY_MEASURED"):
            dishonest.append(f"envelope OK with ledger status {status}")
        if status == "INCONCLUSIVE" and rows:
            dishonest.append(f"INCONCLUSIVE with {len(rows)} rows")
        gv = srcs.get("search_console") or {}
        if (gv.get("rows_read") or 0) > s.gsc_max_rows:
            caps.append(f"rows_read {gv.get('rows_read')} > SEO_GSC_MAX_ROWS {s.gsc_max_rows}")
        if (gv.get("pages_read") or 0) > s.gsc_max_pages:
            caps.append(f"pages_read {gv.get('pages_read')} > SEO_GSC_MAX_PAGES {s.gsc_max_pages}")
        if len(rows) > PRIYA.ROWS_CAP:
            caps.append(f"{len(rows)} ledger rows > ROWS_CAP {PRIYA.ROWS_CAP}")
        if sum(1 for r in rows if r.get("origin") == "derived") > PRIYA.VARIANTS_CAP:
            caps.append("derived variants past VARIANTS_CAP")
    for run in ctx.runs:
        s = run.settings
        for p in run.posts:
            if p["gsc_requests"] > s.gsc_max_requests:
                caps.append(f"{p['gsc_requests']} Search Console requests in one run > SEO_GSC_MAX_REQUESTS "
                            f"{s.gsc_max_requests}")
            bmax = 6
            if p["bing_requests"] > bmax:
                caps.append(f"{p['bing_requests']} Bing requests in one run > its max_requests {bmax}")
            gbound = s.gsc_deadline_s + s.gsc_timeout_s + 2
            if p["gsc_seconds"] > gbound:
                work.append(f"Search Console read took {p['gsc_seconds']:.1f} fake seconds > {gbound}")
            if p["bing_seconds"] > 30 + 10 + 2:
                work.append(f"Bing read took {p['bing_seconds']:.1f} fake seconds > 42")
        ev = run.h.ledger.of_type("keyword_ledger_started")
        per: dict = {}
        for e in ev:
            pl = e["_payload"]
            if pl.get("gsc_read"):
                per[pl.get("tenant_id")] = per.get(pl.get("tenant_id"), 0) + 1
        for t, n in per.items():
            if n > s.gsc_ledger_reads_per_day:
                caps.append(f"{t}: {n} keyword-ledger reads in one day > SEO_GSC_LEDGER_READS_PER_DAY "
                            f"{s.gsc_ledger_reads_per_day}")
        reads = sum(1 for p in run.posts if p.get("query_reads"))     # what reached Google, not what was recorded
        if reads > s.gsc_ledger_reads_per_day:
            caps.append(f"{reads} ledger runs asked Google for query rows > SEO_GSC_LEDGER_READS_PER_DAY "
                        f"{s.gsc_ledger_reads_per_day}")
        try:
            sc = run.h.ok(run.h.get("/department"))["agents"]["priya"]["scorecard"]["keyword_runs"]
            ctx.bodies.append(json.dumps(sc))
            for k in ("not_connected_rate", "failure_rate"):
                if sc.get("runs") == 0 and sc.get(k) is not None:
                    zero.append(f"scorecard {k} = {sc.get(k)} over zero runs")
        except Exception as e:  # noqa: BLE001 - a department view that cannot be read is itself a finding
            zero.append(f"department view unreadable: {type(e).__name__}")
    st["invented_volume"] = invented[:20]
    st["unknown_shown_as_zero"] = zero[:20]
    st["opportunity_inconclusive_shows_zero"] = ok_inc[:5]
    st["status_dishonest"] = dishonest[:20]
    st["cap_violations"] = caps[:20]
    st["work_over_bound"] = work[:20]
    st["ledgers"] = sum(1 for _ in _ledgers(ctx))
    # the case measured something (a case whose read failed by accident would test nothing)
    st["measured"] = any(((led.get("sources") or {}).get("search_console") or {}).get("state") == "OK"
                         for _r, _p, led in _ledgers(ctx))
    st["statuses"] = [led.get("status") for _r, _p, led in _ledgers(ctx)]
    st["http"] = [p["status"] for r in ctx.runs for p in r.posts]
    st["third_party_log_records"] = len(ctx.cap.third)
    # ---- source failure never fails the run: every request answered 201 with a run record
    st["runs_not_answered"] = [p["status"] for r in ctx.runs for p in r.posts
                               if p["status"] != 201 and not ctx.extra.get("refusal_expected")]
    if ctx.kill is not None:
        _judge_kill(ctx, st)
    if ctx.second is not None:
        _judge_second(ctx, st)
    if ctx.probe is not None:
        _judge_probe(ctx, st)
    if ctx.extra.get("audits") is not None:
        st["audit_reasons"] = ctx.extra["audits"]
        run = ctx.runs[-1]
        cap_a = run.settings.gsc_reads_per_day
        # audits get their own SEO_GSC_READS_PER_DAY whatever the ledger spent (ADR 0047: never starved)
        st["audit_starved_by_ledger"] = [f"audit {i + 1} of {len(ctx.extra['audits'])}: {r}"
                                         for i, r in enumerate(ctx.extra["audits"])
                                         if i < cap_a and r == "GSC_BUDGET_EXHAUSTED"]
    if ctx.extra.get("secret_kind"):
        st["secret_rows_dropped"] = [] if not any(
            v in t for _r, _p, led in _ledgers(ctx) for t in _row_texts(led) for v in _secret_values(ctx).values()
            if v) else ["a live secret served as a query is a ledger row"]
    return st


def _judge_kill(ctx, st):
    k = ctx.kill
    sw, when = k["switch"], k["when"]
    run = ctx.runs[-1]
    p = run.posts[-1]
    v = p["view"] if isinstance(p["view"], dict) else {}
    led = v.get("ledger") if isinstance(v.get("ledger"), dict) else None
    viol = []
    recorded = run.h.ledger.of_type("keyword_ledger_recorded")
    if sw == "lifecycle:restricted":
        if p["status"] != 403 or p["gsc_requests"] or p["bing_requests"]:
            viol.append(f"restricted Priya: http {p['status']}, {p['gsc_requests']} Google requests")
    elif sw.startswith("provider:"):
        src = "search_console" if sw.endswith("gsc") else "bing_webmaster"
        state = ((led or {}).get("sources") or {}).get(src, {}).get("state")
        if when == "before":
            n = p["gsc_requests"] if src == "search_console" else p["bing_requests"]
            if n:
                viol.append(f"{sw} engaged before the run: {n} requests to it")
            if state != KILLS[sw]:
                viol.append(f"{sw}: source state {state}")
        else:
            if led is not None and state not in (KILLS[sw], "OK"):
                viol.append(f"{sw} mid-run: source state {state}")
            if led is not None and state == KILLS[sw] and any(
                    (r.get("metrics") or {}).get(src) for r in led.get("rows") or []):
                viol.append(f"{sw} mid-run: killed source's numbers kept")
    else:
        if when == "before":
            if p["status"] != 403 or p["gsc_requests"] or p["bing_requests"]:
                viol.append(f"{sw} before: http {p['status']}, {p['gsc_requests']} Google / {p['bing_requests']} "
                            f"Bing requests")
        else:
            if v.get("status") != "interrupted" or led is not None or recorded:
                viol.append(f"{sw} mid-run: status {v.get('status')}, ledger {'kept' if led else 'none'}, "
                            f"{len(recorded)} recorded")
            if v.get("interrupted_reason") not in (KILLS[sw], "KILLED_GLOBAL", "KILLED_WRITE", None):
                viol.append(f"{sw} mid-run: reason {v.get('interrupted_reason')}")
            at = k.get("requests_at_kill")
            if at is not None and len(run.fake.requests) - at > 1:
                viol.append(f"{sw} mid-run: {len(run.fake.requests) - at} more Google requests after the switch")
    st["kill_violations"] = viol
    ctx.extra["refusal_expected"] = True
    st["runs_not_answered"] = []


def _judge_second(ctx, st):
    s = ctx.second
    run = ctx.runs[-1]
    st["second_status"] = s["status"]
    if s["kind"] in ("same_tenant_new_request", "same_tenant_with_bing"):
        st["second_ok"] = s["status"] == 409 and (s["body"] or {}).get("detail") == "KEYWORD_RUN_RUNNING"
    elif s["kind"] == "same_request_replayed":
        b = s["body"] or {}
        st["second_ok"] = s["status"] in (200, 201) and b.get("ledger") is None and \
            b.get("run_id") == (run.posts[0]["view"] or {}).get("run_id")
    elif s["kind"] == "other_tenant_runs":
        b = s["body"] or {}
        led = b.get("ledger") or {}
        texts = _row_texts(led) if isinstance(led, dict) else []
        acme = {q for q, *_ in _default_rows()}
        mine = _row_texts((run.posts[0]["view"] or {}).get("ledger") or {})
        st["second_ok"] = s["status"] == 201 and not (set(texts) & acme) and "globex widget repair" not in mine
        if not texts:
            st["second_ok"] = st["second_ok"] and (led.get("status") in ("OK", "EMPTY_MEASURED"))
    if "requests_added" in s:
        st["replay_requests_added"] = s["requests_added"]
        st["runs_started"] = s["started"]
        if s["kind"] == "same_id_other_body":
            st["idempotent_ok"] = s["status"] == 409 and (s["body"] or {}).get("detail") == "REQUEST_ID_REUSED"
        else:
            b, f = s["body"] or {}, s["first"] or {}
            st["idempotent_ok"] = s["status"] in (200, 201) and b.get("ledger") is None and \
                b.get("run_id") == f.get("run_id") and b.get("ledger_sha256") == f.get("ledger_sha256")
    reads = sum(1 for e in run.h.ledger.of_type("keyword_ledger_started")
                if e["_payload"].get("gsc_read") and e["_payload"].get("tenant_id") == "acme")
    st["acme_reads"] = reads


def _judge_probe(ctx, st):
    p = ctx.probe
    st["probe_status"] = p["status"]
    leaked = [f for f in p["foreign"] if f and (f in p["text"] or f in p["globex_list"])]
    st["foreign_echoed"] = leaked
    st["acme_property_read_for_other"] = p["acme_property_reads_after"] if p["probe"] not in (
        "hub_runs_for_acme",) else []
    if p["probe"] in ("hub_runs_for_acme", "hub_lists_acme_with_globex_token", "hub_lists_acme",
                      "globex_names_acme_property", "globex_bing_on_acme_domain", "path_traversal",
                      "tenant_case_variant", "svc_view_under_other_tenant"):
        st["probe_refused"] = p["status"] in (403, 404, 409, 422)
    else:
        st["probe_refused"] = True
    try:
        st["globex_sees_no_acme_run"] = all(r.get("tenant_id") == "globex" for r in json.loads(p["globex_list"]))
    except ValueError:
        st["globex_sees_no_acme_run"] = False
    ctx.extra["refusal_expected"] = True
    st["runs_not_answered"] = []
