"""A fake Chrome UX Report API for Lena's tests (ADR 0050): records:queryRecord and records:queryHistoryRecord behind
httpx.MockTransport, answers built in Google's documented shape (developer.chrome.com/docs/crux/api and /history-api),
Google's documented 404 for "no data", and an injected clock. No network, no real key."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import httpx
from gsc_fakes import Clock

# a test-only key, built from parts at run time (never a real key, never shaped like one)
TEST_KEY = "fixture_crux_" + "ab" * 12

NOT_FOUND = {"error": {"code": 404, "message": "chrome ux report data not found", "status": "NOT_FOUND"}}
BOUNDS = {"largest_contentful_paint": (2500, 4000), "interaction_to_next_paint": (200, 500),
          "cumulative_layout_shift": ("0.10", "0.25"), "first_contentful_paint": (1800, 3000),
          "experimental_time_to_first_byte": (800, 1800)}
P75 = {"largest_contentful_paint": 2100, "interaction_to_next_paint": 180, "cumulative_layout_shift": "0.05",
       "first_contentful_paint": 1500, "experimental_time_to_first_byte": 600}
PERIOD = ((2026, 9, 10), (2026, 10, 7))            # the harness clock is 2026-10-09: two days behind


def _d(t):
    return {"year": t[0], "month": t[1], "day": t[2]}


def histogram(name: str, dens=(0.8, 0.15, 0.05)) -> list:
    lo, hi = BOUNDS[name]
    zero = "0.00" if name == "cumulative_layout_shift" else 0
    return [{"start": zero, "end": lo, "density": dens[0]}, {"start": lo, "end": hi, "density": dens[1]},
            {"start": hi, "density": dens[2]}]


def record_answer(kind: str, target: str, ff: str, p75: dict | None = None, drop=(), metrics: dict | None = None,
                  period=PERIOD, norm: dict | None = None) -> dict:
    vals = {**P75, **(p75 or {})}
    ms = {n: {"histogram": histogram(n), "percentiles": {"p75": v}} for n, v in vals.items() if n not in drop}
    ms.update(metrics or {})
    out = {"record": {"key": {"formFactor": ff, kind: target}, "metrics": ms,
                      "collectionPeriod": {"firstDate": _d(period[0]), "lastDate": _d(period[1])}}}
    if norm is not None:
        out["urlNormalizationDetails"] = norm
    return out


def periods(n: int = 25, last=date(2026, 10, 4)) -> list:
    out = []
    for i in range(n):
        end = last - timedelta(weeks=n - 1 - i)
        start = end - timedelta(days=27)
        out.append({"firstDate": _d((start.year, start.month, start.day)),
                    "lastDate": _d((end.year, end.month, end.day))})
    return out


def history_answer(kind: str, target: str, ff: str, series: dict | None = None, n: int = 25, drop=(),
                   metrics: dict | None = None) -> dict:
    ms = {}
    for name, v in P75.items():
        if name in drop:
            continue
        s = (series or {}).get(name) or [v] * n
        lo, hi = BOUNDS[name]
        zero = "0.00" if name == "cumulative_layout_shift" else 0
        dens = [[0.8 if x is not None else "NaN" for x in s], [0.15 if x is not None else "NaN" for x in s],
                [0.05 if x is not None else "NaN" for x in s]]
        ms[name] = {"histogramTimeseries": [{"start": zero, "end": lo, "densities": dens[0]},
                                           {"start": lo, "end": hi, "densities": dens[1]},
                                           {"start": hi, "densities": dens[2]}],
                    "percentilesTimeseries": {"p75s": list(s)}}
    ms.update(metrics or {})
    return {"record": {"key": {"formFactor": ff, kind: target}, "metrics": ms, "collectionPeriods": periods(n)}}


class FakeCrux:
    """Answers every query with data unless told otherwise: ``missing`` holds (method, kind, target, ff) keys (or
    (kind, target) / (kind, target, ff)) answered with Google's 404; ``answers`` maps a key to (status, payload[,
    headers]); ``script`` is consumed first, whatever the key; ``on_request(fake, request)`` runs before answering."""

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.requests: list = []
        self.bodies: list = []
        self.missing: set = set()
        self.answers: dict = {}
        self.script: list = []
        self.on_request = None
        self.raise_on: list = []                    # exceptions to raise (consumed), e.g. httpx.ReadTimeout

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def keys(self) -> list:
        return [(b["_method"], b["_kind"], b[b["_kind"]], b["formFactor"]) for b in self.bodies]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        method = "history" if request.url.path.endswith(":queryHistoryRecord") else "record"
        kind = "origin" if "origin" in body else "url"
        self.bodies.append({**body, "_method": method, "_kind": kind})
        if self.on_request is not None:
            self.on_request(self, request)
        if self.raise_on:
            raise self.raise_on.pop(0)
        key = (method, kind, body[kind], body["formFactor"])
        if self.script:
            item = self.script.pop(0)
        elif key in self.answers:
            item = self.answers[key]
        elif key in self.missing or (kind, body[kind]) in self.missing or (kind, body[kind], body["formFactor"]) \
                in self.missing:
            item = (404, NOT_FOUND)
        elif method == "record":
            item = (200, record_answer(kind, body[kind], body["formFactor"]))
        else:
            item = (200, history_answer(kind, body[kind], body["formFactor"], n=body.get("collectionPeriodCount", 25)))
        status, payload = item[0], item[1]
        headers = item[2] if len(item) > 2 else {}
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return httpx.Response(status, content=content, headers={"content-type": "application/json", **headers})


def write_key_file(path, value: str = TEST_KEY, mode: int = 0o600) -> str:
    path.write_text(value)
    os.chmod(path, mode)
    return str(path)
