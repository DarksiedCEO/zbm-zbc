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
"""

from __future__ import annotations

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

    @classmethod
    def default(cls, settings=None) -> "Ports":
        fetcher = None      # stage B wires the fetcher
        return cls(fetcher=fetcher, renderer=NotConnectedRenderer(),
                   engines={n: NotConnectedEngine(n) for n in ANSWER_ENGINES},
                   prompt_volume=NotConnectedSource("prompt_volume"),
                   first_party={n: NotConnectedSource(n) for n in FIRST_PARTY},
                   zero_day=NotConnectedSource("zero_day"), orca_publish=NotConnectedSource("orca_publish"),
                   clientfix=NotConnectedSource("clientfix"))

    def status(self) -> dict:
        def st(p) -> str:
            return "connected" if getattr(p, "connected", False) else NOT_CONNECTED
        return {"fetch": "connected" if self.fetcher is not None else NOT_CONNECTED,
                "render": st(self.renderer),
                "answer_engines": {n: st(p) for n, p in self.engines.items()},
                "prompt_volume": st(self.prompt_volume),
                "first_party": {n: st(p) for n, p in self.first_party.items()},
                "zero_day": st(self.zero_day), "orca_publish": st(self.orca_publish), "clientfix": st(self.clientfix)}

    def not_connected(self) -> list:
        s = self.status()
        out = [k for k in ("fetch", "render", "prompt_volume", "zero_day", "orca_publish", "clientfix")
               if s[k] == NOT_CONNECTED]
        out += [f"answer_engine:{n}" for n, v in s["answer_engines"].items() if v == NOT_CONNECTED]
        out += [f"first_party:{n}" for n, v in s["first_party"].items() if v == NOT_CONNECTED]
        return sorted(out)
