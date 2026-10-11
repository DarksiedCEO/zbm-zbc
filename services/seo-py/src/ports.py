"""
Ports: every external dependency of Search & Answer Intelligence behind one small interface (ADR 0017 decision 12).

A port that is not connected says so: it answers ``NOT_CONNECTED`` and never returns invented data. The only port
built for real in Wave 1 is the web fetcher (primitives/fetch.py: SSRF-safe, robots-honouring). Everything else is a
named NOT_CONNECTED port, and config.NOT_BUILT refuses to start if one is "selected" by environment.

  render                 headless rendering (separate from fetch, explicit states)
  answer_engines         openai, anthropic, google, perplexity (flag 1: Perplexity included)
  prompt_volume          Naomi's prompt-volume source
  first_party            Search Console, Bing Webmaster, server logs, analytics, CRM
  zero_day, orca_publish flag 5: ports only
  clientfix              Department 28 fix execution (not in Wave 1)
  finance                Finance (31) invoice verification (Wave 3, W3-2: finance_client.py; NOT_CONNECTED unless
                         SEO_FINANCE_URL / _TOKEN / _CALLER_TOKEN are set). A business gate, not an observation port:
                         it is listed in /status, never in an audit report's ``not_connected``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

NOT_CONNECTED = "NOT_CONNECTED"
ANSWER_ENGINES = ("openai", "anthropic", "google", "perplexity")
FIRST_PARTY = ("search_console", "bing_webmaster", "server_logs", "analytics", "crm")


@dataclass
class ProviderAnswer:
    """One sampled answer from an answer engine. ``status``: ANSWER, REFUSAL, ERROR or NOT_CONNECTED."""

    status: str
    text: str = ""
    citations: list = field(default_factory=list)
    model: Optional[str] = None
    model_version: Optional[str] = None


class NotConnectedEngine:
    """An answer-engine port with no adapter: every ask is NOT_CONNECTED (no credentials, no invented answer)."""

    connected = False

    def __init__(self, name: str):
        self.name = name

    def ask(self, prompt: str, model: Optional[str] = None) -> ProviderAnswer:
        return ProviderAnswer(NOT_CONNECTED)


class NotConnectedRenderer:
    connected = False

    def render(self, url: str, html: str):
        return {"state": "RENDER_NOT_CONNECTED"}


class NotConnectedSource:
    connected = False

    def __init__(self, name: str):
        self.name = name

    def read(self, *a, **k):
        return {"state": NOT_CONNECTED, "source": self.name}


class NotConnectedBotVerifier:
    """No resolver configured: every claimed bot stays "claimed" (the User-Agent alone is never trusted)."""

    connected = False

    def verify(self, ip: str, token: str) -> str:
        return NOT_CONNECTED


class DnsBotVerifier:
    """Reverse DNS, then forward-confirm (the host's own A/AAAA records must contain the IP), against the operator's
    documented host-name suffixes (agents/bots.VERIFY_DNS_SUFFIXES). Answers: ``verified``, ``failed`` (the claim is
    false: a spoofed crawler), ``unverifiable`` (no DNS method for this family, or an address that is not globally
    routable — a proxy or load-balancer address; no lookup is made) or ``error`` (DNS did not answer in time, the
    shared pool is busy, or the global lookup cap is used up; the hit stays claimed).

    AEGIS 1472041 M1: lookups run on ONE shared, bounded worker pool (POOL_WORKERS threads for the whole process); a
    lookup is only submitted when a worker slot is free (never queued behind a hung resolver), so a timed-out lookup
    can occupy at most its own slot; and the process makes at most ``max_lookups`` lookups in its lifetime."""

    connected = True
    POOL_WORKERS = 4
    _pool = None
    _pool_lock = threading.Lock()
    _slots = threading.BoundedSemaphore(POOL_WORKERS)      # shared: in flight across every instance <= workers

    def __init__(self, rdns=None, forward=None, timeout_s: float = 2.0, max_lookups: int = 100_000):
        import socket
        self._rdns = rdns or (lambda ip: socket.gethostbyaddr(ip)[0])
        self._fwd = forward or (lambda host: [i[4][0] for i in socket.getaddrinfo(host, None)])
        self.timeout_s = timeout_s
        self.max_lookups = max_lookups
        self.lookups = 0
        self._count = threading.Lock()

    @classmethod
    def _executor(cls):
        import concurrent.futures as cf
        with cls._pool_lock:
            if cls._pool is None:
                cls._pool = cf.ThreadPoolExecutor(max_workers=cls.POOL_WORKERS, thread_name_prefix="seo-dns")
            return cls._pool

    def _call(self, fn, arg):
        with self._count:
            if self.lookups >= self.max_lookups:
                raise TimeoutError("global lookup cap reached")
            self.lookups += 1
        if not self._slots.acquire(blocking=False):
            raise TimeoutError("every DNS worker is busy")
        try:
            fut = self._executor().submit(fn, arg)
        except BaseException:
            self._slots.release()
            raise
        fut.add_done_callback(lambda _f: self._slots.release())     # the slot frees when the lookup ends
        return fut.result(timeout=self.timeout_s)

    def verify(self, ip: str, token: str) -> str:
        from agents.bots import VERIFY_DNS_SUFFIXES
        import ipaddress
        import socket
        suffixes = VERIFY_DNS_SUFFIXES.get(token)
        if not suffixes:
            return "unverifiable"
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return "unverifiable"
        if not addr.is_global:
            return "unverifiable"
        try:
            host = str(self._call(self._rdns, ip) or "").lower().rstrip(".")
        except socket.herror:                              # no PTR record: the operator's crawlers all have one
            return "failed"
        except Exception:                                  # timeout, busy, cap, resolver error: no claim either way
            return "error"
        if not any(host == s or host.endswith("." + s) for s in suffixes):
            return "failed"
        try:
            addrs = self._call(self._fwd, host) or []
            ok = any(ipaddress.ip_address(a.split("%")[0]) == addr for a in addrs)
        except Exception:
            return "error"
        return "verified" if ok else "failed"


@dataclass
class Ports:
    fetcher: object = None
    renderer: object = None
    engines: dict = field(default_factory=dict)
    prompt_volume: object = None
    first_party: dict = field(default_factory=dict)
    zero_day: object = None
    orca_publish: object = None
    clientfix: object = None
    bot_verifier: object = None
    finance: object = None

    @classmethod
    def default(cls, settings=None) -> "Ports":
        from finance_client import FinanceClient, NotConnectedFinance
        fetcher = None
        finance = NotConnectedFinance()
        if settings is not None:
            from primitives.fetch import Fetcher
            fetcher = Fetcher.from_settings(settings)
            if settings.finance_url:
                finance = FinanceClient(settings.finance_url, settings.finance_token, settings.finance_caller_token,
                                        timeout_s=settings.finance_timeout_s)
        return cls(fetcher=fetcher, renderer=NotConnectedRenderer(),
                   engines={n: NotConnectedEngine(n) for n in ANSWER_ENGINES},
                   prompt_volume=NotConnectedSource("prompt_volume"),
                   first_party={n: NotConnectedSource(n) for n in FIRST_PARTY},
                   zero_day=NotConnectedSource("zero_day"), orca_publish=NotConnectedSource("orca_publish"),
                   clientfix=NotConnectedSource("clientfix"),
                   bot_verifier=DnsBotVerifier() if settings is not None and settings.bot_verify_dns
                   else NotConnectedBotVerifier(), finance=finance)

    def status(self) -> dict:
        def st(p) -> str:
            return "connected" if getattr(p, "connected", False) else NOT_CONNECTED
        return {"fetch": "connected" if self.fetcher is not None else NOT_CONNECTED,
                "render": st(self.renderer),
                "answer_engines": {n: st(p) for n, p in self.engines.items()},
                "prompt_volume": st(self.prompt_volume),
                "first_party": {n: st(p) for n, p in self.first_party.items()},
                "zero_day": st(self.zero_day), "orca_publish": st(self.orca_publish), "clientfix": st(self.clientfix),
                "bot_dns_verification": st(self.bot_verifier), "bot_ip_range_verification": NOT_CONNECTED,
                "finance": st(self.finance)}

    def not_connected(self) -> list:
        s = self.status()
        out = [k for k in ("fetch", "render", "prompt_volume", "zero_day", "orca_publish", "clientfix",
                           "bot_dns_verification", "bot_ip_range_verification")
               if s[k] == NOT_CONNECTED]
        out += [f"answer_engine:{n}" for n, v in s["answer_engines"].items() if v == NOT_CONNECTED]
        out += [f"first_party:{n}" for n, v in s["first_party"].items() if v == NOT_CONNECTED]
        return sorted(out)
