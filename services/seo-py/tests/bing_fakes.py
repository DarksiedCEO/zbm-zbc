"""A fake Bing Webmaster Tools JSON endpoint behind httpx.MockTransport (ADR 0047): GetQueryStats only, answering in
the shape of Microsoft's documented sample. The API key is derived at run time (never a literal). No network."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
from helpers import derived

import bing_webmaster as bw

KEY_TEXT = derived("bing webmaster api key")[:32]
# 2026-10-05T07:00:00Z and the following weeks, written as Bing writes dates
WEEK0_MS = 1_791_183_600_000
WEEK_MS = 7 * 86_400_000


def key() -> bw.ApiKey:
    return bw.ApiKey(KEY_TEXT)


def row(q: str, clicks: int, impressions: int, week: int = 0, pos: float | None = 7.0) -> dict:
    r = {"__type": "QueryStats:#Microsoft.Bing.Webmaster.Api", "Clicks": clicks, "Impressions": impressions,
         "Date": f"/Date({WEEK0_MS + week * WEEK_MS}-0700)/", "Query": q}
    if pos is not None:
        r["AvgClickPosition"] = pos
        r["AvgImpressionPosition"] = pos
    return r


class Clock:
    def __init__(self):
        self.t = 100.0
        self.sleeps: list = []

    def mono(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class FakeBing:
    def __init__(self, sites=("https://acme.test/",)):
        self.sites = set(sites)
        self.rows = [row("rug cleaner", 4, 60, 0), row("rug cleaner", 3, 40, 1), row("how to clean a rug", 1, 9, 0)]
        self.script: list = []
        self.requests: list = []
        self.clock = Clock()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.requests.append({"host": u.hostname, "path": u.path, "query": q, "method": request.method})
        if self.script:
            item = self.script.pop(0)
            if callable(item):
                return item(request)
            status, payload, *rest = item
            headers = rest[0] if rest else {}
            if isinstance(payload, (bytes, httpx.SyncByteStream)):
                return httpx.Response(status, content=payload if isinstance(payload, bytes) else None,
                                      stream=payload if isinstance(payload, httpx.SyncByteStream) else None,
                                      headers=headers)
            return httpx.Response(status, json=payload, headers=headers)
        if q.get("apikey") != KEY_TEXT:
            return httpx.Response(400, json={"ErrorCode": 3, "Message": "InvalidApiKey"})
        if u.path != "/webmaster/api.svc/json/GetQueryStats":
            return httpx.Response(404, content=b"")
        if q.get("siteUrl") not in self.sites:
            return httpx.Response(400, json={"ErrorCode": 14, "Message": "NotAuthorized"})
        return httpx.Response(200, json={"d": self.rows})

    def client(self, **kw) -> bw.BingWebmasterClient:
        return bw.BingWebmasterClient(key(), transport=self.transport(), monotonic=self.clock.mono,
                                      sleep=self.clock.sleep, **kw)
