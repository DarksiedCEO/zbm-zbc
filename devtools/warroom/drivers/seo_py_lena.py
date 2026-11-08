"""War room driver: Lena, the real-user page-experience agent (ADR 0050: Chrome UX Report field data inside an
audit), through seo-py's own harness (tests/helpers.py ``Harness`` built by tests/test_lena_service.py
``crux_harness``: the REAL CrUX adapter, ``crux_client.CruxClient``, on ``httpx.MockTransport`` with an injected
clock, keyed by the test-only key of tests/crux_fakes.py, written to a key file in the case's own directory).
Nothing leaves the process: the worker refuses every socket connect before any service code is imported.

Each case gets fresh harnesses in its own temporary directory and a HOSTILE CrUX (``WRCrux``): Google's documented
answers (tests/crux_fakes.py builders) per origin / URL / form factor with distinct values per target (so a value
reported under the wrong target, tenant or twin is visible), Google's 404 for "no data", metrics dropped or
malformed (NaN / null / negative / mistyped percentiles, broken histograms), history series with null points, new
unknown fields and metrics, records keyed for another origin (the www twin, another tenant), storms (400 / 401 /
403 / 429 / 5xx with and without Retry-After, timeouts, refused connections, redirects, HTML, huge bodies), a
per-minute quota on the fake clock, and the key echoed in error bodies and exception messages.

The judge is independent of the service: every query the fake answered is logged with what it served; a reported
p75 must be the value served for that exact key, its rating must be web.dev's for that value (the thresholds are
read from agents/lena.py THRESHOLDS, the documented values), a Core Web Vitals PASS needs LCP, INP and CLS all
served valid and good; an entry without served data carries no rating, percentile or assessment; a history null is
never a number; the key (tests/crux_fakes.TEST_KEY) is searched for in every URL sent, every response, the status
and department views, the client report (Markdown and HTML), every stored byte and every log record; caps are the
settings the harness started with; work is bounded structurally (requests, the fake clock's seconds).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace

WARROOM = Path(__file__).resolve().parents[1]
SEO = None
H = CF = TLS = LENA = CX = CR = CONFIG = httpx = None
SECRETS: dict = {}
_RID = [0]
ACME, BETA = "acme.test", "globex.test"     # tenants acme and globex (the harness has tokens for both)
WWW = "www.acme.test"


def prepare_env(env) -> None:
    for k in list(env):
        if k.startswith("SEO_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
            env.pop(k, None)


def import_harness() -> None:
    global SEO, H, CF, TLS, LENA, CX, CR, CONFIG, httpx
    spec = importlib.util.spec_from_file_location("warroom_driver_seo_py_base", WARROOM / "drivers" / "seo_py.py")
    SEO = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(SEO)
    SEO.import_harness()
    import client_report
    import config
    import crux_client
    import crux_fakes
    import helpers
    import httpx as _httpx
    import test_lena_service
    from agents import lena
    H, CF, TLS, LENA, CX, CR, CONFIG, httpx = helpers, crux_fakes, test_lena_service, lena, crux_client, \
        client_report, config, _httpx
    SECRETS["crux_api_key"] = CF.TEST_KEY
    SECRETS["service_token"] = H.SERVICE_TOKEN
    SECRETS["andre_token"] = H.ANDRE
    SECRETS.update({f"caller_{c}": t for c, t in H.CALLERS.items()})
    SECRETS.update({f"tenant_{t}": v for t, v in H.TENANT_TOKENS.items()})


def rid() -> str:
    _RID[0] += 1
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"warroom-lena-{_RID[0]}"))


# ============================================================================================ the oracle (judge side)

def web_dev(metric: str, p75: float) -> str:
    """web.dev's rating (agents/lena.py THRESHOLDS: the documented values; good at or below, poor above)."""
    t = LENA.THRESHOLDS[metric]
    return "GOOD" if p75 <= t["good"] else "POOR" if p75 > t["poor"] else "NEEDS_IMPROVEMENT"


def profile(target: str) -> dict:
    """Distinct, deterministic p75s per target (each metric somewhere in good / needs improvement / poor), so a value
    reported under the wrong key is visible."""
    h = sum((i + 1) * ord(c) for i, c in enumerate(target))
    return {"largest_contentful_paint": 1200 + h % 4000, "interaction_to_next_paint": 80 + h % 600,
            "cumulative_layout_shift": f"{(h % 40) / 100:.2f}", "first_contentful_paint": 900 + h % 2800,
            "experimental_time_to_first_byte": 300 + h % 2000}


# record-metric mutations: BREAKING ones make the metric unusable (it must never be rated); DROP removes it;
# UNCERTAIN ones have no documented expectation (only: never a wrong number); PRESERVING ones keep valid data
BREAKING = ("p75_nan", "p75_null", "p75_negative", "p75_string", "p75_bool", "p75_huge", "p75_inf_text",
            "density_sum", "density_negative", "bins_gap", "bins_many", "open_middle", "hist_not_list",
            "no_percentiles", "metric_not_dict")
UNCERTAIN = ("percentile_extra", "metric_extra_field")
DROP = ("drop",)
RECORD_MUTATIONS = BREAKING + UNCERTAIN + DROP
# record-level: the whole answer must not be data (a key for another origin) / no expectation / data kept
RECORD_BREAKING = ("key_twin", "key_other_tenant", "key_form_factor", "norm_other_origin", "periods_inverted",
                   "no_period", "metrics_flood", "key_is_key")
# no documented expectation: unknown top-level / record fields (refused or ignored), a period after today
RECORD_UNCERTAIN = ("record_extra_field", "answer_extra_field", "period_future")
RECORD_PRESERVING = ("unknown_metric", "fid_served", "norm_same_origin", "period_old", "metric_named_like_key")
HISTORY_MUTATIONS = ("null_points", "nan_text_points", "short_series", "densities_partial_nan", "periods_unordered",
                     "periods_too_many", "all_null")
HISTORY_BREAKING = ("short_series", "densities_partial_nan", "periods_unordered", "periods_too_many")


def _mutate_metric(name: str, m: dict, how: str) -> object:
    cls = name == "cumulative_layout_shift"
    m = json.loads(json.dumps(m))
    if how == "p75_nan":
        m["percentiles"]["p75"] = "NaN" if cls else float("nan")
    elif how == "p75_null":
        m["percentiles"]["p75"] = None
    elif how == "p75_negative":
        m["percentiles"]["p75"] = "-0.10" if cls else -5
    elif how == "p75_string":
        m["percentiles"]["p75"] = float(m["percentiles"]["p75"]) if cls else str(m["percentiles"]["p75"])
    elif how == "p75_bool":
        m["percentiles"]["p75"] = True
    elif how == "p75_huge":
        m["percentiles"]["p75"] = "1e9" if cls else 10 ** 12
    elif how == "p75_inf_text":
        m["percentiles"]["p75"] = "Infinity"
    elif how == "density_sum":
        m["histogram"][0]["density"] = 0.95
    elif how == "density_negative":
        m["histogram"][1]["density"] = -0.1
    elif how == "bins_gap":
        m["histogram"][1]["start"] = "0.12" if cls else m["histogram"][1]["start"] + 7
    elif how == "bins_many":
        m["histogram"] = [dict(m["histogram"][0]) for _ in range(11)]
    elif how == "open_middle":
        m["histogram"][0].pop("end", None)
    elif how == "hist_not_list":
        m["histogram"] = "many"
    elif how == "no_percentiles":
        m.pop("percentiles")
    elif how == "metric_not_dict":
        return "x"
    elif how == "percentile_extra":
        m["percentiles"]["p90"] = m["percentiles"]["p75"]
    elif how == "metric_extra_field":
        m["fractions"] = {"phone": 0.5}
    return m


class WRCrux:
    """records:queryRecord and records:queryHistoryRecord, per key, logged. ``script``: storm steps consumed first
    (``repeat``: the last one repeats). ``missing``: keys answered with Google's 404 (``(kind, target)``,
    ``(kind, target, ff)`` or ``(method, kind, target, ff)``). ``mut``: ``(method, kind, ff) -> {metric: how}``
    with ``*`` wildcards; ``rec_mut``: ``(method, kind, ff) -> how`` (record level). ``quota``: requests per 60
    fake seconds before 429."""

    def __init__(self):
        self.clock = CF.Clock()
        self.requests: list = []
        self.served: list = []
        self.script: list = []
        self.repeat = False
        self.missing: set = set()
        self.mut: dict = {}
        self.rec_mut: dict = {}
        self.hist_mut: dict = {}
        self.quota = None
        self.times: list = []
        self.on_request = None
        self.today = "2026-10-09"
        self.timeout_s = 10                       # set from the harness's SEO_CRUX_TIMEOUT_SECONDS

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _pop(self):
        if not self.script:
            return "ok"
        if len(self.script) == 1 and self.repeat:
            return self.script[0]
        return self.script.pop(0)

    @staticmethod
    def _sel(d: dict, method: str, kind: str, ff: str):
        for k in ((method, kind, ff), (method, kind, "*"), ("*", kind, ff), ("*", kind, "*"), (method, "*", "*"),
                  ("*", "*", ff), ("*", "*", "*")):
            if k in d:
                return d[k]
        return None

    def handle(self, request):
        self.requests.append({"url": str(request.url), "headers": dict(request.headers)})
        self.times.append(self.clock.t)
        body = json.loads(request.content)
        method = "history" if request.url.path.endswith(":queryHistoryRecord") else "record"
        kind = "origin" if "origin" in body else "url"
        target, ff = body.get(kind), body.get("formFactor")
        log = {"method": method, "kind": kind, "target": target, "ff": ff, "status": None, "metrics": {},
               "valid_key": True, "record_mut": None, "hist_mut": None, "nulls": {}, "periods": None}
        self.served.append(log)
        if self.on_request is not None:
            self.on_request(self, request)
        if self.quota is not None:
            recent = [t for t in self.times if t > self.clock.t - 60]
            if len(recent) > self.quota:
                log["status"] = 429
                return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                           "message": "Quota exceeded"}}, headers={"Retry-After": "30"})
        step = self._pop()
        if step != "ok":
            r = self._storm(step)
            log["status"] = (r.status_code if step != "404_other" else "404_other") if r is not None else None
            if r is not None:
                return r
        key4 = (method, kind, target, ff)
        if key4 in self.missing or (kind, target) in self.missing or (kind, target, ff) in self.missing:
            log["status"] = 404
            return httpx.Response(404, json=CF.NOT_FOUND)
        p = profile(f"{target}|{ff}")
        if method == "record":
            ans = CF.record_answer(kind, target, ff, p75=p)
            mm = self._sel(self.mut, method, kind, ff) or {}
            for name, how in mm.items():
                if name not in ans["record"]["metrics"]:
                    continue
                if how == "drop":
                    ans["record"]["metrics"].pop(name)
                else:
                    ans["record"]["metrics"][name] = _mutate_metric(name, ans["record"]["metrics"][name], how)
            for name in CX.METRICS:
                if name in ans["record"]["metrics"]:
                    log["metrics"][name] = {"p75": p[name], "how": mm.get(name)}
                else:
                    log["metrics"][name] = {"p75": None, "how": "drop"}
            rm = self._sel(self.rec_mut, method, kind, ff)
            log["record_mut"] = rm
            ans = self._record_level(ans, rm, kind, target, ff, log)
        else:
            n = body.get("collectionPeriodCount", 25)
            hm = self._sel(self.hist_mut, method, kind, ff)
            log["hist_mut"] = hm
            series = {}
            for name in CX.METRICS:
                v = p[name]
                s = [v] * n
                if hm in ("null_points", "nan_text_points"):
                    for i in range(0, n, 3):
                        s[i] = None
                elif hm == "all_null":
                    s = [None] * n
                series[name] = s
                log["nulls"][name] = [i for i, x in enumerate(s) if x is None]
                log["metrics"][name] = {"p75": v, "how": hm}
            ans = CF.history_answer(kind, target, ff, series=series, n=n)
            log["periods"] = n
            if hm == "nan_text_points":
                for name in ans["record"]["metrics"]:
                    ans["record"]["metrics"][name]["percentilesTimeseries"]["p75s"] = [
                        "NaN" if x is None else x for x in ans["record"]["metrics"][name]["percentilesTimeseries"][
                            "p75s"]]
            elif hm == "short_series":
                for name in ans["record"]["metrics"]:
                    ans["record"]["metrics"][name]["percentilesTimeseries"]["p75s"].pop()
            elif hm == "densities_partial_nan":
                for name in ans["record"]["metrics"]:
                    ans["record"]["metrics"][name]["histogramTimeseries"][0]["densities"][2] = "NaN"
            elif hm == "periods_unordered":
                cps = ans["record"]["collectionPeriods"]
                cps[0], cps[1] = cps[1], cps[0]
            elif hm == "periods_too_many":
                ans["record"]["collectionPeriods"] = ans["record"]["collectionPeriods"] + \
                    ans["record"]["collectionPeriods"][:3]
        log["status"] = 200
        return httpx.Response(200, content=json.dumps(ans).encode(), headers={"content-type": "application/json"})

    def _record_level(self, ans, how, kind, target, ff, log):
        rec = ans["record"]
        if how in RECORD_BREAKING:
            log["valid_key"] = False
        if how == "key_twin":
            twin = target.replace("://www.", "://") if "://www." in target else target.replace("://", "://www.")
            rec["key"][kind] = twin
        elif how == "key_other_tenant":
            rec["key"][kind] = re.sub(r"://[^/]+", f"://{BETA}" if ACME in target else f"://{ACME}", target)
        elif how == "key_form_factor":
            rec["key"]["formFactor"] = "TABLET" if ff != "TABLET" else "PHONE"
        elif how == "norm_other_origin" and kind == "url":
            other = re.sub(r"://[^/]+", f"://{WWW}" if f"://{ACME}" in target else f"://{ACME}", target)
            rec["key"]["url"] = other
            ans["urlNormalizationDetails"] = {"originalUrl": target, "normalizedUrl": other}
        elif how == "norm_other_origin":
            log["valid_key"] = True
        elif how == "norm_same_origin" and kind == "url":
            other = target.rstrip("/") + "/index"
            rec["key"]["url"] = other
            ans["urlNormalizationDetails"] = {"originalUrl": target, "normalizedUrl": other}
        elif how == "key_is_key":
            rec["key"][kind] = f"https://{CF.TEST_KEY.lower().replace('_', '-')}.test" + ("/" if kind == "url" else "")
        elif how == "periods_inverted":
            cp = rec["collectionPeriod"]
            cp["firstDate"], cp["lastDate"] = cp["lastDate"], cp["firstDate"]
        elif how == "no_period":
            rec.pop("collectionPeriod")
        elif how == "answer_extra_field":
            ans["newTopLevel"] = {"x": 1}
        elif how == "metrics_flood":
            rec["metrics"].update({f"m{i}": {} for i in range(100)})
        elif how == "record_extra_field":
            rec["newField"] = 1
        elif how == "period_future":
            rec["collectionPeriod"]["lastDate"] = {"year": 2026, "month": 12, "day": 1}
        elif how == "period_old":
            rec["collectionPeriod"] = {"firstDate": {"year": 2025, "month": 1, "day": 1},
                                       "lastDate": {"year": 2025, "month": 1, "day": 28}}
        elif how == "unknown_metric":
            rec["metrics"]["round_trip_time"] = {"percentiles": {"p75": 120}}
            rec["metrics"]["navigation_types"] = {"fractions": {"navigate": 0.5}}
        elif how == "fid_served":
            rec["metrics"]["first_input_delay"] = {"histogram": CF.histogram("first_contentful_paint"),
                                                   "percentiles": {"p75": 15}}
        elif how == "metric_named_like_key":
            rec["metrics"][CF.TEST_KEY] = {"percentiles": {"p75": 1}}
        return ans

    def _storm(self, step):
        err = lambda code, st, reason=None, msg="x": {"error": {"code": code, "status": st, "message": msg, **(
            {"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]} if reason
            else {})}}
        if step in ("500", "502", "503", "504", "418"):
            return httpx.Response(int(step), json=err(int(step), "UNAVAILABLE"))
        if step == "429":
            return httpx.Response(429, json=err(429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"))
        if step in ("429ra5", "429ra60"):
            return httpx.Response(429, json=err(429, "RESOURCE_EXHAUSTED"), headers={"Retry-After": step[5:]})
        if step == "400":
            return httpx.Response(400, json=err(400, "INVALID_ARGUMENT"))
        if step == "400key":
            return httpx.Response(400, json=err(400, "INVALID_ARGUMENT", "API_KEY_INVALID", "API key not valid."))
        if step == "400echo":
            return httpx.Response(400, json=err(400, "INVALID_ARGUMENT", "API_KEY_INVALID",
                                                "API key not valid: " + CF.TEST_KEY))
        if step == "403":
            return httpx.Response(403, json=err(403, "PERMISSION_DENIED", "SERVICE_DISABLED"))
        if step == "403echo":
            return httpx.Response(403, json=err(403, "PERMISSION_DENIED", CF.TEST_KEY, "denied for " + CF.TEST_KEY))
        if step == "403quota":
            return httpx.Response(403, json=err(403, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"))
        if step == "401":
            return httpx.Response(401, json=err(401, "UNAUTHENTICATED"))
        if step == "500echo":
            return httpx.Response(500, json=err(500, "INTERNAL", None, "upstream said key=" + CF.TEST_KEY))
        if step == "404_other":                 # a 404 that is not Google's documented "no data" answer
            return httpx.Response(404, json={"error": {"code": 404, "message": "method not found " + CF.TEST_KEY}})
        if step == "timeout":                    # the client's own timeout elapses (the fake clock), then it fires
            self.clock.advance(self.timeout_s)
            raise httpx.ReadTimeout("read timed out")
        if step == "connect":
            raise httpx.ConnectError("refused")
        if step == "connect_echo":
            raise httpx.ConnectError(f"refused for key {CF.TEST_KEY}")
        if step == "redirect":
            return httpx.Response(302, headers={"location": f"https://evil.test/?key={CF.TEST_KEY}"})
        if step == "html":
            return httpx.Response(200, content=b"<html>consent</html>", headers={"content-type": "text/html"})
        if step == "not_json":
            return httpx.Response(200, content=b"{not json")
        if step == "huge":
            return httpx.Response(200, content=b'{"record": {"x": "' + b"a" * (2 * 1024 * 1024) + b'"}}')
        if step == "gzip":
            import gzip
            return httpx.Response(200, content=gzip.compress(b"{}"), headers={"content-encoding": "gzip"})
        if step == "drip":
            return httpx.Response(200, stream=_drip(self.clock))
        raise ValueError(step)


STORM_STEPS = ("ok", "500", "502", "503", "504", "418", "429", "429ra5", "429ra60", "400", "400key", "400echo",
               "403", "403echo", "403quota", "401", "500echo", "404_other", "timeout", "connect", "connect_echo",
               "redirect", "html", "not_json", "huge", "gzip", "drip")
# steps after which a query can never yield data (the judge's own list)
NO_DATA_STEPS = frozenset(s for s in STORM_STEPS if s != "ok")


def _drip(clock):
    class _D(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(200):
                clock.advance(1.0)
                yield b" "
    return _D()


# ============================================================================================ log capture

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        try:
            text = f"{record.name} {record.getMessage()}"
        except Exception:  # noqa: BLE001
            text = f"{record.name} {record.msg!r} {record.args!r}"
        if record.exc_info:
            text += "\n" + "".join(traceback.format_exception(*record.exc_info))
        self.lines.append(text)


# ============================================================================================ case setup

def setup(tmp):
    _RID[0] = 0
    cap = _Capture()
    logging.getLogger().addHandler(cap)
    old = {n: logging.getLogger(n).level for n in ("seo", "httpx", "httpcore")}
    for n in ("seo", "httpx", "httpcore"):
        logging.getLogger(n).setLevel(logging.DEBUG)              # every record anyone could enable
    return SimpleNamespace(tmp=Path(tmp), steps=[], runs=[], hs=[], cap=cap, old=old, audits=[], bodies=[],
                           extra={}, n=0, kill=None, probe=None, keyfile=None)


def teardown(ctx) -> None:
    logging.getLogger().removeHandler(ctx.cap)
    for n, lv in ctx.old.items():
        logging.getLogger(n).setLevel(lv)
    for h in ctx.hs:
        try:
            h.svc.close()
        except Exception:  # noqa: BLE001
            pass


def build(ctx, per_run="40", per_day="2000", env=None, per_tenant=None):
    """A fresh harness on a hostile CrUX: tenants acme (acme.test and its www twin, both registered) and globex
    (globex.test only: Lena asks for its unregistered twin www.globex.test too), own."""
    ctx.n += 1
    fake = WRCrux()
    d = ctx.tmp / f"h{ctx.n}"
    d.mkdir()
    env = {"SEO_AUDIT_MAX_PAGES": "25", **(env or {})}          # the documented maximum: 25 audited paths
    h = TLS.crux_harness(d, fake, calls_per_run=str(per_run), calls_per_day=str(per_day),
                         tenant_per_day=str(per_tenant if per_tenant is not None else per_day),
                         data_dir=str(d / "data"), **{k: str(v) for k, v in env.items()})
    ctx.hs.append(h)
    fake.timeout_s = h.settings.crux_timeout_s
    h.tenant("acme", "own", (ACME, WWW))
    h.tenant("globex", "own", (BETA,))
    run = SimpleNamespace(h=h, fake=fake, dir=d, settings=h.settings)
    ctx.runs.append(run)
    return run


def audit(ctx, run, tid="acme", domain=ACME, paths=("/",), scheme="https"):
    """One audit (Andre approves, as the suite does). What the fake served during it is kept with it."""
    n0, s0, t0 = len(run.fake.requests), len(run.fake.served), run.fake.clock.t
    body = {"request_id": rid(), "domain": domain, "paths": list(paths), "scheme": scheme}
    r = run.h.post(f"/tenants/{tid}/audits", body, andre=True)
    ctx.bodies.append(r.text)
    try:
        view = r.json()
    except ValueError:
        view = None
    rec = {"tid": tid, "domain": domain, "paths": list(paths), "scheme": scheme, "status": r.status_code,
           "view": view, "requests": run.fake.requests[n0:], "served": run.fake.served[s0:],
           "seconds": run.fake.clock.t - t0, "run": run, "day": run.h.clock.now().date().isoformat()}
    ctx.audits.append(rec)
    return r


# ============================================================================================ actions

def crux_audit(ctx, shape=None, missing=None, metric=None, mutation=None, record_mut=None, history_mut=None,
               ff="*", method="record", kind="*", paths="/", script=None, domain=ACME, per_run="40"):
    """One audit with the fake set up as named: ``missing`` (no data: origin / url / both / one form factor / twin),
    a metric mutation, a record- or history-level mutation, a storm script."""
    run = build(ctx, per_run=per_run)
    f = run.fake
    plist = [p for p in paths.split(",") if p]
    origin = f"https://{domain}"
    if missing == "origin":
        f.missing.add(("origin", origin))
    elif missing == "urls":
        for p in plist:
            f.missing.add(("url", origin + p))
    elif missing == "all":
        f.missing.add(("origin", origin))
        for p in plist:
            f.missing.add(("url", origin + p))
    elif missing == "phone_only":
        f.missing.add(("origin", origin, "PHONE"))
    elif missing == "history_only":
        for ffx in CX.FORM_FACTORS:
            f.missing.add(("history", "origin", origin, ffx))
    elif missing == "tablet_everywhere":
        f.missing.add(("origin", origin, "TABLET"))
        for p in plist:
            f.missing.add(("url", origin + p, "TABLET"))
    if metric and mutation:
        names = [m for m in CX.METRICS if m in LENA.CORE] if metric == "*core" else \
            list(CX.METRICS) if metric == "*all" else [metric]
        f.mut[(method, kind, ff)] = {n: mutation for n in names}
    if record_mut:
        f.rec_mut[("record", kind, ff)] = record_mut
    if history_mut:
        f.hist_mut[("history", kind, ff)] = history_mut
    if script:
        f.script = script.split(",")
        f.repeat = True
    return audit(ctx, run, domain=domain, paths=plist)


def twins(ctx, shape):
    """The bare origin and its www twin (both registered to acme) with and without data; each audit reports only
    its own origin's data, never the twin's."""
    run = build(ctx)
    f = run.fake
    bare, www = f"https://{ACME}", f"https://{WWW}"
    if shape == "bare_only":
        f.missing |= {("origin", www), ("url", www + "/")}
    elif shape == "www_only":
        f.missing |= {("origin", bare), ("url", bare + "/")}
    elif shape == "neither":
        f.missing |= {("origin", www), ("url", www + "/"), ("origin", bare), ("url", bare + "/")}
    elif shape == "both":
        pass
    elif shape == "bare_answers_for_www":
        f.rec_mut[("record", "*", "*")] = "key_twin"
    elif shape.startswith("unregistered_twin"):     # globex registers only the bare domain: its twin is asked too
        gb, gw = f"https://{BETA}", f"https://www.{BETA}"
        if shape == "unregistered_twin_only_has_data":
            f.missing |= {("origin", gb), ("url", gb + "/"), ("url", gb + "/pricing")}
        elif shape == "unregistered_twin_has_none":
            f.missing |= {("origin", gw), ("url", gw + "/"), ("url", gw + "/pricing")}
        elif shape == "unregistered_twin_answers_as_bare":
            f.missing |= {("origin", gb), ("url", gb + "/"), ("url", gb + "/pricing")}
            f.rec_mut[("record", "*", "*")] = "key_twin"
        return audit(ctx, run, tid="globex", domain=BETA, paths=["/", "/pricing"])
    audit(ctx, run, domain=ACME)
    return audit(ctx, run, domain=WWW)


def budget(ctx, kind):
    """Per-run and daily caps (0 = OFF), the daily cap spanning tenants, a day passing, a restart."""
    spec = dict(p.split("=") for p in kind.split(","))
    run = build(ctx, per_run=spec.get("per_run", "40"), per_day=spec.get("per_day", "2000"),
                per_tenant=spec.get("per_tenant"))
    plan = spec.get("plan", "acme").split("+")
    r = None
    for step in plan:
        if step == "day":
            run.h.clock.advance(days=1, minutes=1)
            continue
        if step == "restart":
            h2 = run.h.restart(**run.h.crux_env)
            ctx.hs.append(h2)
            run.h = h2
            continue
        tid, dom = ("globex", BETA) if step == "globex" else ("acme", WWW) if step == "www" else ("acme", ACME)
        paths = ["/", "/pricing", "/about"] if spec.get("paths") == "3" else ["/"]
        r = audit(ctx, run, tid=tid, domain=dom, paths=paths)
    return r


def quota(ctx, audits):
    """Google's documented 150 queries per minute per project, enforced by the fake on its clock: back-to-back
    audits at the per-run cap."""
    run = build(ctx, per_run="120", per_day="20000")
    run.fake.quota = 150
    paths = [f"/p{i}" for i in range(19)]
    r = None
    for i in range(int(audits)):
        r = audit(ctx, run, tid="globex" if i % 2 else "acme", domain=BETA if i % 2 else ACME, paths=paths)
    ctx.extra["quota"] = 150
    return r


KILLS = ("provider:crux", "global", "write", "tenant:acme", "capability:audit")


def kill(ctx, switch, when="before"):
    run = build(ctx)
    h = run.h
    kind, _, name = switch.partition(":")
    ctx.kill = {"switch": switch, "when": when}
    if switch == "lifecycle:restricted":
        h.ok(h.post("/agents/lena/lifecycle", {"request_id": rid(), "expected_state": "active", "to": "restricted",
                                               "reason": "QUALITY_REVIEW"}, caller="dashboard", andre=True))
    elif switch == "env:SEO_KILLED_PROVIDERS":
        run2 = build(ctx, env={"SEO_KILLED_PROVIDERS": "crux"})
        ctx.runs.remove(run)
        run = run2
    elif when == "before":
        h.ok(h.switch(switch))
        ctx.extra["interrupt_ok"] = switch != "provider:crux"     # a run-wide switch refuses or ends the audit
    else:
        state = {"n": 0}

        def on(fk, req):
            state["n"] += 1
            if state["n"] == 3:
                ctx.kill["requests_at_kill"] = len(fk.requests)
                if kind in ("capability", "provider"):
                    h.svc.switches[kind][name] = True
                elif kind == "tenant":
                    h.svc.tenants[name]["killed"] = True
                else:
                    h.svc.switches[kind] = True
        run.fake.on_request = on
        ctx.extra["interrupt_ok"] = switch != "provider:crux"     # a run-wide switch interrupts the audit
    return audit(ctx, run, paths=["/", "/pricing"])


def _mask(text: str, x: str) -> str:
    """The id the caller itself sent, in any spelling the probe used, masked (an echo of one's own input is not a
    leak; anything else in the answer must be identical to the answer for a missing id)."""
    return text.replace(x.upper(), "<id>").replace(x, "<id>") if x else text


def cross_tenant(ctx, probe):
    """globex, the hub and other callers reach for acme's audit, its page-experience section and its origin."""
    run = build(ctx)
    h = run.h
    a = audit(ctx, run, paths=["/", "/pricing"])
    aid = a.json().get("audit_id")
    missing = "seo-aud-" + "0" * 40
    n0 = len(run.fake.served)
    calls = {
        "globex_reads_acme_section": lambda x: h.get(f"/tenants/globex/audits/{x}/page-experience"),
        "hub_with_zbm_token": lambda x: h.get(f"/tenants/acme/audits/{x}/page-experience", caller="hub",
                                              tenant="zbm"),
        "hub_with_globex_token": lambda x: h.get(f"/tenants/acme/audits/{x}/page-experience", caller="hub",
                                                 tenant="globex"),
        "finance_reads_section": lambda x: h.get(f"/tenants/acme/audits/{x}/page-experience", caller="finance_31"),
        "globex_audits_acme_domain": lambda x: h.post("/tenants/globex/audits", {"request_id": rid(), "domain": ACME,
                                                                              "paths": ["/"]}, andre=True),
        "globex_audits_acme_www": lambda x: h.post("/tenants/globex/audits", {"request_id": rid(), "domain": WWW,
                                                                           "paths": ["/"]}, andre=True),
        "id_uppercased": lambda x: h.get(f"/tenants/globex/audits/{x.upper()}/page-experience"),
        "path_traversal": lambda x: h.get(f"/tenants/globex/audits/..%2F..%2Facme%2Faudits%2F{x}/page-experience"),
        "globex_audit_then_reads_own": lambda x: h.get("/tenants/globex/audits/" + audit(
            ctx, run, tid="globex", domain=BETA).json()["audit_id"] + "/page-experience"),
    }
    first = calls[probe](aid)
    second = calls[probe](missing) if probe not in ("globex_audits_acme_domain", "globex_audits_acme_www",
                                                    "globex_audit_then_reads_own") else first
    ctx.bodies += [first.text, second.text]
    ctx.probe = {"probe": probe, "status": first.status_code, "missing_status": second.status_code,
                 "same_as_missing": (first.status_code, _mask(first.text, aid or "")) ==
                                    (second.status_code, _mask(second.text, missing)),
                 "text": first.text, "served_after": run.fake.served[n0:], "aid": aid,
                 "foreign": [f"https://{ACME}/pricing", f"https://{ACME}", aid or ""]}
    return first


def scale(ctx, shape):
    """Enterprise scale: up to 25 paths x 3 form factors x 2 APIs per audit at the per-run cap, many audits across
    tenants against the daily cap."""
    n_paths, audits, per_run, per_day = {"25_paths_cap_120": (25, 1, 120, 20000),
                                         "25_paths_cap_40": (25, 1, 40, 20000),
                                         "10_audits_daily_600": (19, 10, 120, 600),
                                         "6_audits_20_paths": (20, 6, 120, 20000)}[shape]
    run = build(ctx, per_run=per_run, per_day=per_day)
    for i in range(0, n_paths, 4):
        run.fake.missing.add(("url", f"https://{ACME}/p{i}"))
        run.fake.missing.add(("url", f"https://{BETA}/p{i}"))
    paths = [f"/p{i}" for i in range(n_paths)]
    r = None
    for i in range(audits):
        tid, dom = ("globex", BETA) if i % 3 == 2 else ("acme", WWW if i % 3 == 1 else ACME)
        r = audit(ctx, run, tid=tid, domain=dom, paths=paths)
    return r


def key_leak(ctx, kind):
    """The key pushed at every exit: error bodies, exception messages, a record keyed with it, a metric named after
    it, the agent raising with it, the parser raising with it."""
    run = build(ctx)
    f = run.fake
    if kind in ("400echo", "403echo", "500echo", "404_other", "connect_echo", "redirect"):
        f.script = [kind]
        f.repeat = True
    elif kind == "metric_named_like_key":
        f.rec_mut[("record", "*", "*")] = "metric_named_like_key"
    elif kind == "record_keyed_with_the_key":
        f.rec_mut[("record", "*", "*")] = "key_is_key"
    elif kind in ("agent_raises_with_key", "parser_raises_with_key"):
        import crux_client
        from agents import lena as lena_mod
        target, attr = (lena_mod, "run") if kind == "agent_raises_with_key" else (crux_client, "parse_record")
        orig = getattr(target, attr)

        def boom(*a, **k):
            raise ZeroDivisionError("quoting " + CF.TEST_KEY)
        setattr(target, attr, boom)
        ctx.extra["restore"] = (target, attr, orig)
    try:
        return audit(ctx, run, paths=["/", "/pricing"])
    finally:
        if "restore" in ctx.extra:
            t, a, o = ctx.extra.pop("restore")
            setattr(t, a, o)


KEY_FILES = ("missing", "empty", "mode_644", "symlink", "directory", "oversize", "not_a_key", "key_plus_spaces",
             "relative_path", "unset", "off_with_bad_file")


def key_file(ctx, kind):
    """The feature on (SEO_CRUX_ENABLED=1) with the key file missing or unusable: start is refused, and no error
    says what the file holds."""
    import os
    d = ctx.tmp / "kf"
    d.mkdir(exist_ok=True)
    p = d / "crux.key"
    env = {"SEO_CRUX_ENABLED": "1", "SEO_CRUX_API_KEY_FILE": str(p), "SEO_CRUX_CALLS_PER_RUN": "10",
           "SEO_CRUX_CALLS_PER_DAY": "10"}
    expect_refused = True
    if kind == "missing":
        pass
    elif kind == "empty":
        CF.write_key_file(p, value="")
    elif kind == "mode_644":
        CF.write_key_file(p, mode=0o644)
    elif kind == "symlink":
        real = CF.write_key_file(d / "real.key")
        os.symlink(real, p)
    elif kind == "directory":
        p.mkdir()
    elif kind == "oversize":
        CF.write_key_file(p, value=CF.TEST_KEY * 2000)
    elif kind == "not_a_key":
        CF.write_key_file(p, value="key: " + CF.TEST_KEY + " !")
    elif kind == "key_plus_spaces":
        CF.write_key_file(p, value=CF.TEST_KEY + "\n\n   \t")
        expect_refused = None                      # no documented expectation (a trailing newline may be allowed)
    elif kind == "relative_path":
        CF.write_key_file(p)
        env["SEO_CRUX_API_KEY_FILE"] = "crux.key"
    elif kind == "unset":
        env.pop("SEO_CRUX_API_KEY_FILE")
    elif kind == "off_with_bad_file":
        CF.write_key_file(p, mode=0o644)
        env["SEO_CRUX_ENABLED"] = "0"
        expect_refused = False
    err, tb, settings = None, "", None
    try:
        settings = CONFIG.load(H.base_env(**env))
    except BaseException as e:  # noqa: BLE001 - what the refusal says is judged
        err = f"{type(e).__name__}: {e}"
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    ctx.extra["key_file"] = {"kind": kind, "refused": err is not None, "expect": expect_refused,
                             "texts": [err or "", tb, repr(settings) if settings is not None else ""],
                             "connected": None}
    if settings is not None:
        from ports import field_data_from_settings
        port = field_data_from_settings(settings)
        ctx.extra["key_file"]["connected"] = bool(port.connected)
        ctx.extra["key_file"]["texts"].append(repr(port))
    return None


def generated_answers(ctx, spec):
    """grammar.crux_answer_plan: a whole hostile CrUX for one or two audits (no-data keys, metric and record and
    history mutations by selector, paths, twins, caps)."""
    g = json.loads(spec)
    run = build(ctx, per_run=g.get("per_run", 40), per_day=g.get("per_day", 2000))
    f = run.fake
    for m in g.get("missing", []):
        f.missing.add(tuple(m))
    for sel, mm in g.get("mut", []):
        f.mut[tuple(sel)] = mm
    for sel, how in g.get("rec_mut", []):
        f.rec_mut[tuple(sel)] = how
    for sel, how in g.get("hist_mut", []):
        f.hist_mut[tuple(sel)] = how
    r = None
    for tid, dom, paths in g["audits"]:
        r = audit(ctx, run, tid=tid, domain=dom, paths=paths)
    return r


def generated_storm(ctx, spec):
    """grammar.crux_storm: storm scripts, caps, a quota, a kill mid-read, repeated audits."""
    g = json.loads(spec)
    run = build(ctx, per_run=g.get("per_run", 40), per_day=g.get("per_day", 2000), env=g.get("env"))
    f = run.fake
    f.script = list(g.get("script") or [])
    f.repeat = bool(g.get("repeat"))
    if g.get("quota"):
        f.quota = int(g["quota"])                 # the fake's own (smaller) quota makes 429s; pacing is judged
        ctx.extra["quota"] = 150                  # against Google's documented 150 per minute
    if g.get("kill_at"):
        ctx.kill = {"switch": "provider:crux", "when": "mid_read", "generated": True}
        state = {"n": 0}

        def on(fk, req):
            state["n"] += 1
            if state["n"] == g["kill_at"]:
                ctx.kill["requests_at_kill"] = len(fk.requests)
                run.h.svc.switches["provider"]["crux"] = True
        f.on_request = on
    r = None
    for tid, dom, paths in g["audits"]:
        r = audit(ctx, run, tid=tid, domain=dom, paths=paths)
    return r


ACTIONS = {"crux_audit": crux_audit, "twins": twins, "budget": budget, "quota": quota, "kill": kill,
           "cross_tenant": cross_tenant, "scale": scale, "key_leak": key_leak, "key_file": key_file,
           "generated_answers": generated_answers, "generated_storm": generated_storm}


# ============================================================================================ observation (the judge)

FIXED_FAST_SLOW = None
FORECAST_WORDS = re.compile(r"forecast|predict|projected|projection|will (?:improve|worsen|be)|expected_p75|"
                            r"next_period|next_week", re.I)
TREND_KEYS = {"periods", "periods_with_data", "method", "direction", "why", "early_median_p75", "late_median_p75",
              "change", "relative_change", "rating_early", "rating_late"}
DIRECTIONS = {"IMPROVING", "WORSENING", "NO_CLEAR_CHANGE", "INSUFFICIENT_HISTORY"}


def _fixed_phrases() -> list:
    """The module's own fixed texts (method, limitations, trend method, report phrases), which may say 'slow' or
    'forecast' to say they do not judge or forecast."""
    import report_page_experience as rpe
    intro = [s[3] for s in CR.SECTIONS if s[0] == "page_experience"]       # the area's fixed description
    return sorted([*intro, LENA.METHOD, LENA.TREND_METHOD, *LENA.LIMITS, *LENA.NOT_REQUESTED.values(), rpe.FIELD_EVIDENCE,
                   "No field data means Google has too few real-user visits to report; it is not a fast or a slow "
                   "result.", "it is not a fast or a slow result",
                   "Trend over the weekly history (descriptive; the periods overlap; no forecast)",
                   "descriptive; the periods overlap; no forecast"], key=len, reverse=True)


def _strip_fixed(text: str) -> str:
    for p in _fixed_phrases():
        text = text.replace(p, " ")
        text = text.replace(json.dumps(p)[1:-1], " ")
    return text


def _keys(o, out=None):
    out = set() if out is None else out
    if isinstance(o, dict):
        for k, v in o.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(o, list):
        for v in o:
            _keys(v, out)
    return out


def _twin(a: dict):
    """The judge's own twin rule (ADR 0017 decision 7): the other form (bare / www.) of the audited domain, https,
    when the tenant has not registered it. acme registers both forms; globex only globex.test."""
    d = a["domain"]
    other = d[4:] if d.startswith("www.") else "www." + d
    registered = {"acme": {ACME, WWW}, "globex": {BETA}}.get(a["tid"], set())
    return None if other in registered else f"https://{other}"


def _last_served(a: dict) -> dict:
    """(method, kind, target, ff) -> what the fake answered last for it during the audit."""
    out = {}
    for s in a["served"]:
        out[(s["method"], s["kind"], s["target"], s["ff"])] = s
    return out


def _metric_usable(s: dict, name: str) -> bool:
    m = s["metrics"].get(name) or {}
    return m.get("p75") is not None and m.get("how") in (None,) and s.get("record_mut") not in RECORD_BREAKING


def stored_blob(run) -> str:
    h = run.h
    parts = []
    data = run.dir / "data"
    if data.is_dir():
        for p in sorted(data.rglob("*")):
            if p.is_file():
                parts.append(p.read_bytes().decode("utf-8", "replace"))
    parts.append(json.dumps(h.ledger.entries(), ensure_ascii=False, default=str))
    try:
        parts.append(json.dumps(h.svc.audit_export(1, 100_000), ensure_ascii=False, default=str))
    except Exception as e:  # noqa: BLE001
        parts.append(f"export failed: {type(e).__name__}")
    try:
        parts.append(b"\n".join(h.svc.log._lines).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts)


def _section(view):
    try:
        return next(e for e in view["report"]["agents"] if e["agent"] == "lena" and e["task"] == "page_experience")
    except Exception:  # noqa: BLE001
        return None


def observe(ctx) -> dict:
    st: dict = {}
    if ctx.extra.get("key_file") is not None:
        kf = ctx.extra["key_file"]
        texts = "\n".join(kf["texts"] + ctx.cap.lines)
        st["key_leaked"] = ["key file refusal"] if CF.TEST_KEY in texts or CF.TEST_KEY[:16] in texts else []
        st["key_file_honoured"] = [] if kf["expect"] is None else (
            [] if kf["refused"] == kf["expect"] and not (kf["expect"] is False and kf["connected"]) else
            [f"{kf['kind']}: refused={kf['refused']} connected={kf['connected']}, wanted refused={kf['expect']}"])
        for k in ("server_errors", "fast_slow_without_data", "pass_with_missing_core", "wrong_value_or_rating",
                  "null_became_number", "cap_violations", "work_over_bound", "forecast", "cross_tenant",
                  "data_not_from_its_key", "retired_metric_reported", "secrets_leaked"):
            st.setdefault(k, [])
        return st
    for run in ctx.runs:                            # the views a person reads, collected for the judge
        for path in ("/status", "/department"):
            try:
                ctx.bodies.append(run.h.get(path).text)
            except Exception:  # noqa: BLE001
                pass
    reports, areas = [], []
    for a in ctx.audits:
        v = a["view"]
        if a["status"] == 201 and isinstance(v, dict) and v.get("status") == "completed":
            try:
                pe = a["run"].h.get(f"/tenants/{a['tid']}/audits/{v['audit_id']}/page-experience")
                ctx.bodies.append(pe.text)
                model = CR.build(v, a["tid"])
                reports += [CR.render_markdown(model), CR.render_html(model)]
                areas += [json.dumps(x, ensure_ascii=False) for x in model.get("sections") or []
                          if x.get("key") == "page_experience"]
            except Exception as e:  # noqa: BLE001 - a report that cannot be built is itself a finding
                reports.append(f"REPORT FAILED {type(e).__name__}")
                st.setdefault("report_failed", []).append(f"{type(e).__name__}: {str(e)[:120]}")
    st.setdefault("report_failed", [])
    stored = [stored_blob(r) for r in ctx.runs]
    logs = "\n".join(ctx.cap.lines)
    urls = [r["url"] for run in ctx.runs for r in run.fake.requests]
    reprs = "\n".join(f"{r.h.settings!r} {r.h.ports.field_data!r}" for r in ctx.runs)
    everything = "\n".join(ctx.bodies + reports + stored + [logs] + urls)
    st["key_leaked"] = sorted({w for w, hay in (("url", "\n".join(urls)), ("response", "\n".join(ctx.bodies)),
                                                 ("report", "\n".join(reports)), ("stored", "\n".join(stored)),
                                                 ("log", logs), ("repr", reprs)) if CF.TEST_KEY in hay})
    st["key_header_missing"] = [] if all(r["headers"].get("x-goog-api-key") == CF.TEST_KEY
                                         for run in ctx.runs for r in run.fake.requests) else ["a request without "
                                                                                                "the key header"]
    st["secrets_leaked"] = sorted(n for n, v in SECRETS.items() if n != "crux_api_key" and v and v in everything)
    st["server_errors"] = [f"{a['tid']}: {a['status']}" for a in ctx.audits if a["status"] >= 500]
    st["audits_not_completed"] = [f"{a['tid']}/{a['domain']}: {a['status']} "
                                  f"{(a['view'] or {}).get('status') if isinstance(a['view'], dict) else ''}"
                                  for a in ctx.audits if a["status"] == 201 and not ctx.extra.get("interrupt_ok")
                                  and (a["view"] or {}).get("status") != "completed"]
    fast_slow, pass_missing, wrong, nulls, forecast, notkey, retired, lost = [], [], [], [], [], [], [], []
    cross, caps, work, stale = [], [], [], []
    for a in ctx.audits:
        v = a["view"]
        if not isinstance(v, dict) or a["status"] != 201:
            continue
        origin = f"{a['scheme']}://{a['domain']}"
        twin = _twin(a)
        mine = [origin] + [o + p for o in [origin] + ([twin] if twin else []) for p in a["paths"]] + \
            ([twin] if twin else [])
        for s in a["served"]:
            if s["target"] not in mine:
                cross.append(f"{a['tid']} audit of {origin} asked CrUX for {s['target']}")
        block = (v.get("report") or {}).get("page_experience") or {}
        sec = block.get("section") if isinstance(block, dict) else None
        served = _last_served(a)
        s_run = a["run"].settings
        reserved = [e["_payload"]["calls_reserved"] for e in a["run"].h.ledger.of_type("crux_read_started")
                    if e["_payload"].get("audit_id") == v.get("audit_id")]
        n_req = len(a["requests"])
        if n_req > s_run.crux_calls_per_run:
            caps.append(f"{n_req} CrUX requests in one audit > SEO_CRUX_CALLS_PER_RUN {s_run.crux_calls_per_run}")
        if reserved and n_req > reserved[0]:
            caps.append(f"{n_req} CrUX requests > the {reserved[0]} reserved")
        if not reserved and n_req:
            caps.append(f"{n_req} CrUX requests with no reservation recorded first")
        bound = s_run.crux_deadline_s + s_run.crux_timeout_s + 2
        if a["seconds"] > bound:
            work.append(f"one audit's CrUX read took {a['seconds']:.1f} fake seconds > {bound}")
        plan_len = len(LENA.plan(origin, [origin + p for p in a["paths"]], twin))
        if n_req > plan_len * 3:
            work.append(f"{n_req} requests > {plan_len} planned queries x 3 attempts")
        if not isinstance(sec, dict):
            continue
        blob = json.dumps(sec, ensure_ascii=False)
        bad_keys = [k for k in _keys(sec) if FORECAST_WORDS.search(k)]
        if bad_keys or FORECAST_WORDS.search(_strip_fixed(blob)):
            forecast.append(f"forecast-like keys or words: {bad_keys[:5]}")
        if '"FID"' in blob or '"metric": "first_input_delay"' in blob:
            retired.append("first_input_delay reported")
        if re.search(r"\bfast\b|\bslow\b", _strip_fixed(blob), re.I):
            fast_slow.append("the section says fast / slow outside its fixed phrases")
        for t in sec.get("targets") or []:
            if t.get("target") not in mine:
                cross.append(f"section target {t.get('target')} is not the audit's")
            for ff, e in (t.get("form_factors") or {}).items():
                for which, method in (("current", "record"), ("history", "history")):
                    ent = e.get(which) or {}
                    sv = served.get((method, t.get("kind"), t.get("target"), ff))
                    tag = f"{t.get('kind')}:{t.get('target')}/{ff}/{which}"
                    if ent.get("state") != "OK":
                        j = json.dumps(ent)
                        if any(k in j for k in ('"rating"', '"p75"', '"core_web_vitals"', '"p75s"', '"trend"',
                                                "GOOD", "POOR", "NEEDS_IMPROVEMENT", "PASSED")):
                            fast_slow.append(f"{tag}: {ent.get('state')} carries a rating or a value")
                        if sv is not None and sv["status"] == 404 and ent.get("state") != "INSUFFICIENT_FIELD_DATA":
                            fast_slow.append(f"{tag}: Google said no data (404) but the entry is {ent.get('state')}")
                        if sv is not None and sv["status"] == 200 and sv["valid_key"] and ent.get("state") == \
                                "INSUFFICIENT_FIELD_DATA":
                            lost.append(f"{tag}: data served, reported as no field data")
                        continue
                    if sv is None or sv["status"] != 200:
                        notkey.append(f"{tag}: OK with nothing served for it "
                                      f"({None if sv is None else sv['status']})")
                        continue
                    if not sv["valid_key"]:
                        notkey.append(f"{tag}: OK from a record keyed {sv['record_mut']}")
                        continue
                    ms = ent.get("metrics") or {}
                    if which == "current":
                        for name, short in CX.METRICS.items():
                            m = ms.get(short) or {}
                            if m.get("state") != "OK":
                                if _metric_usable(sv, name) and m.get("state") != "OK":
                                    lost.append(f"{tag}/{short}: a valid metric was set aside ({m.get('state')})")
                                continue
                            info = sv["metrics"].get(name) or {}
                            if info.get("how") in BREAKING or info.get("how") == "drop" or info.get("p75") is None:
                                wrong.append(f"{tag}/{short}: rated from a {info.get('how')} metric")
                                continue
                            want = float(info["p75"])
                            if not isinstance(m.get("p75"), (int, float)) or abs(m["p75"] - want) > 1e-9:
                                wrong.append(f"{tag}/{short}: p75 {m.get('p75')} but {want} was served")
                            elif m.get("rating") != web_dev(name, want):
                                wrong.append(f"{tag}/{short}: rating {m.get('rating')} for p75 {want}")
                        cwv = (ent.get("core_web_vitals") or {}).get("assessment")
                        usable = {n: _metric_usable(sv, n) or ((sv["metrics"].get(n) or {}).get("how") in UNCERTAIN
                                                               and (ms.get(CX.METRICS[n]) or {}).get("state") == "OK")
                                  for n in LENA.CORE}
                        if cwv == "PASSED":
                            if not all(usable.values()):
                                pass_missing.append(f"{tag}: PASSED with core metric(s) "
                                                    f"{[CX.METRICS[n] for n, ok in usable.items() if not ok]} "
                                                    "missing or malformed")
                            elif any(web_dev(n, float(sv['metrics'][n]['p75'])) != "GOOD" for n in LENA.CORE):
                                pass_missing.append(f"{tag}: PASSED with a core metric not good")
                        if cwv == "FAILED" and not any(
                                (ms.get(CX.METRICS[n]) or {}).get("state") == "OK" and
                                (ms.get(CX.METRICS[n]) or {}).get("rating") != "GOOD" for n in LENA.CORE):
                            wrong.append(f"{tag}: FAILED with no present core metric that is not good")
                        if sv.get("record_mut") == "period_old" and ent.get("stale") is not True:
                            stale.append(f"{tag}: a 2025 collection period is not flagged stale")
                        if sv.get("record_mut") == "period_future":
                            stale.append(f"{tag}: a collection period ending after today was reported as data "
                                         f"(freshness {ent.get('freshness_days')} days)")
                    else:
                        for name, short in CX.METRICS.items():
                            m = ms.get(short) or {}
                            if m.get("state") != "OK":
                                continue
                            info = sv["metrics"].get(name) or {}
                            if sv.get("hist_mut") in HISTORY_BREAKING:
                                wrong.append(f"{tag}/{short}: a {sv.get('hist_mut')} series was used")
                                continue
                            p75s = m.get("p75s") or []
                            for i in sv["nulls"].get(name, []):
                                if i < len(p75s) and p75s[i] is not None:
                                    nulls.append(f"{tag}/{short}: period {i} was null, reported {p75s[i]!r}")
                                    break
                            tr = m.get("trend") or {}
                            if not set(tr) <= TREND_KEYS or tr.get("direction") not in DIRECTIONS:
                                forecast.append(f"{tag}/{short}: trend keys {sorted(set(tr) - TREND_KEYS)} "
                                                f"direction {tr.get('direction')}")
                            have = (len(p75s) - len(sv["nulls"].get(name, []))) if p75s else 0
                            if tr.get("periods_with_data") not in (None, have):
                                nulls.append(f"{tag}/{short}: periods_with_data {tr.get('periods_with_data')} but "
                                             f"{have} periods had data")
    # daily cap across tenants (per service day) and Google's per-minute quota (fake clock)
    st["over_quota_per_minute"] = []
    for run in ctx.runs:
        s = run.settings
        by_day: dict = {}
        by_tenant: dict = {}
        for a in ctx.audits:
            if a["run"] is run:
                by_day[a["day"]] = by_day.get(a["day"], 0) + len(a["requests"])
                k = (a["day"], a["tid"])
                by_tenant[k] = by_tenant.get(k, 0) + len(a["requests"])
        for day, n in by_day.items():
            if n > s.crux_calls_per_day:
                caps.append(f"{n} CrUX requests on {day} > SEO_CRUX_CALLS_PER_DAY {s.crux_calls_per_day}")
        for (day, tid), n in by_tenant.items():
            if n > s.crux_tenant_calls_per_day:
                caps.append(f"{tid}: {n} CrUX requests on {day} > SEO_CRUX_TENANT_CALLS_PER_DAY "
                            f"{s.crux_tenant_calls_per_day}")
        if ctx.extra.get("quota"):
            t = run.fake.times
            worst = max((sum(1 for x in t if y - 60 < x <= y) for y in t), default=0)
            if worst > ctx.extra["quota"]:
                st["over_quota_per_minute"].append(f"{worst} requests within 60 fake seconds > Google's "
                                                   f"{ctx.extra['quota']} per minute")
    for area in areas:                               # Lena's area of the client report (other areas are not hers)
        m = re.search(r"\bfast\b|\bslow\b(?! down)", _strip_fixed(area), re.I)
        if m:
            fast_slow.append(f"the client report's page-experience area says {m.group(0)!r} outside its fixed phrases")
        if FORECAST_WORDS.search(_strip_fixed(area)):
            forecast.append("the client report's page-experience area forecasts")
    st["fast_slow_without_data"] = fast_slow[:20]
    st["pass_with_missing_core"] = pass_missing[:20]
    st["wrong_value_or_rating"] = wrong[:20]
    st["null_became_number"] = nulls[:20]
    st["forecast"] = forecast[:20]
    st["data_not_from_its_key"] = notkey[:20]
    st["retired_metric_reported"] = retired
    st["valid_data_lost"] = lost[:20]
    st["period_honesty"] = stale[:20]
    st["cross_tenant"] = cross[:20]
    st["cap_violations"] = caps[:20]
    st["work_over_bound"] = work[:20]
    st["sections"] = [((_section(a["view"]) or {}).get("outcome"), (_section(a["view"]) or {}).get("reason"))
                      for a in ctx.audits if isinstance(a["view"], dict) and a["view"].get("report")]
    st["requests"] = sum(len(a["requests"]) for a in ctx.audits)
    st["measured"] = any(o in ("OK", "PARTIAL") for o, _r in st["sections"])
    st["kill_violations"] = []
    if ctx.kill is not None:
        _judge_kill(ctx, st)
    if ctx.probe is not None:
        _judge_probe(ctx, st)
    return st


KILL_OUTCOMES = {"provider:crux": ("KILLED",), "lifecycle:restricted": ("RESTRICTED",),
                 "env:SEO_KILLED_PROVIDERS": ("KILLED",)}


def _judge_kill(ctx, st):
    k = ctx.kill
    viol = []
    a = ctx.audits[-1]
    run = a["run"]
    sec = _section(a["view"]) if isinstance(a["view"], dict) else None
    if k["when"] == "before" or k["switch"] in ("lifecycle:restricted", "env:SEO_KILLED_PROVIDERS"):
        if a["requests"]:
            viol.append(f"{k['switch']} before the audit: {len(a['requests'])} CrUX requests")
        if run.h.ledger.of_type("crux_read_started"):
            viol.append(f"{k['switch']} before the audit: a CrUX read was started")
        if sec is not None and k["switch"] in KILL_OUTCOMES and sec.get("outcome") not in KILL_OUTCOMES[k["switch"]]:
            viol.append(f"{k['switch']}: section {sec.get('outcome')}")
    else:
        at = k.get("requests_at_kill")
        if at is not None and len(run.fake.requests) - at > 0:
            viol.append(f"{k['switch']} mid-read: {len(run.fake.requests) - at} more requests after the switch")
        if sec is not None and at is not None and sec.get("outcome") not in ("KILLED", "INTERRUPTED") and \
                k["switch"] == "provider:crux":
            viol.append(f"{k['switch']} mid-read: section {sec.get('outcome')}")
    st["kill_violations"] = viol


def _judge_probe(ctx, st):
    p = ctx.probe
    st["probe_status"] = p["status"]
    if p["probe"] in ("globex_reads_acme_section", "hub_with_zbm_token", "hub_with_globex_token", "id_uppercased",
                      "path_traversal"):
        st["probe_refused"] = p["status"] in (403, 404, 422) and p["same_as_missing"]
    elif p["probe"] == "finance_reads_section":
        st["probe_refused"] = p["status"] in (403, 404)
    elif p["probe"] in ("globex_audits_acme_domain", "globex_audits_acme_www"):
        st["probe_refused"] = p["status"] in (403, 404, 409, 422) and not p["served_after"]
    else:
        st["probe_refused"] = p["status"] == 200
    st["foreign_echoed"] = [f for f in p["foreign"] if f and f in p["text"]]
