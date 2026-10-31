"""
Service health poll for the founder-alerts job (AEGIS round 2 N-3; ADR 0025 section 8).

Why. Railway's deploy healthcheck is ``/livez`` (AEGIS wave A F-1), so Railway activates a deployment whose service
is held, in reconcile mode or degraded: ``/livez`` answers 200 whenever the process serves. ``/health`` stays the
truth (503 with a fixed code while a service cannot write, in reconcile mode (``"maintenance"``), after an unpromoted
rotation (``rotation_expired_unpromoted``) or while the ledger's clock is behind its log (``ledger_clock_behind_log``);
200 ``warnings`` such as ``ledger_anchor_unkeyed``). Something must read it and tell Andre. That is
the ``ops-alerts`` cron job (fix wave C, ADR 0027, ``founder_alerts.FounderAlerts.run``), which runs every 5 minutes;
this module is the self-contained piece it calls.

Integration point (wired when wave C's branch is merged; nothing here sends anything). The whole call site, inside
``FounderAlerts.run`` after ``conds`` is built::

    conds += [Condition.make(*c) for c in health_poll.run(st, os.environ, now)]

with ``SEVERITY`` / ``WHAT_TO_DO`` entries for ``service_unhealthy`` (high), ``service_flapping`` (high) and
``health_poll_unconfigured`` (high). ``st`` is the founder-alerts state dict (this module keeps its own ``"health"``
and ``"health_hist"`` keys in it). Conditions (AEGIS round 3 N3-8):

* ``service_unhealthy``: non-ok for ``DOWN_MIN`` (10) minutes without a break;
* ``service_flapping``: non-ok in at least 3 of the last 6 polls, or in more than 30% of at least ``FLAP_MIN_POLLS``
  polls over ``FLAP_WINDOW_MIN`` (30) minutes (AEGIS round 4 N4-2: a service down at every other poll);
* ``health_poll_unconfigured``: ``OPS_HEALTH_URLS`` empty or malformed (``ops-alerts``), or one of ``REQUIRED``
  missing from it (the service's name); also logged as a warning on every run.

What counts as non-ok: no answer, an HTTP status other than 200, a ``status`` other than ``"ok"``, ``write_ok`` false,
or any entry in ``warnings``. The code is the sorted SET of the body's reason codes (``reasons`` /
``degraded_reasons``, text after a ``:`` dropped), so a new or worse reason on a service that is already alerting is a
new condition (AEGIS round 4 N4-3: wave C keys a condition by source, category and ref). Only fixed codes leave this module (``unreachable``, ``http_503``, ``maintenance``,
``degraded``, ``write_refused``, ``warning:ledger_anchor_unkeyed`` ...): a value that is not a short lowercase code is
replaced by ``bad_status``/``bad_warning``, so no id or free text from a body can reach an email.

``OPS_HEALTH_URLS``: ``name=url`` pairs separated by commas, e.g.
``finance=http://finance.railway.internal:8080,legal=http://legal.railway.internal:8080,compliance=...,ledger=...``.
Each URL is the service root (``/health`` is appended); http or https, no credentials, no query.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx

DOWN_MIN = 10
FLAP_WINDOW_MIN = 30
FLAP_MIN_POLLS = 4
FLAP_LAST_N, FLAP_LAST_K = 6, 3          # AEGIS round 4 N4-2: non-ok in at least 3 of the last 6 polls ...
FLAP_FRACTION = 0.3                      # ... or in more than 30% of at least FLAP_MIN_POLLS polls over 30 minutes
CATEGORY = "service_unhealthy"
FLAPPING = "service_flapping"
UNCONFIGURED = "health_poll_unconfigured"
REQUIRED = ("compliance", "finance", "ledger", "legal")
TIMEOUT_S = 5
ENV = "OPS_HEALTH_URLS"

log = logging.getLogger("health_poll")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,47}")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class Reading:
    name: str
    ok: bool
    code: str                    # "ok" or a fixed code (never free text)


def _code(value, fallback: str) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def targets_from_env(env) -> dict[str, str]:
    """``{name: base_url}`` from ``OPS_HEALTH_URLS``; raises ValueError on a malformed entry (a deploy-time error)."""
    raw = (env.get(ENV) or "").strip()
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        name, sep, url = part.strip().partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not _NAME.fullmatch(name) or name in out:
            raise ValueError(f"{ENV}: each entry is name=url with a unique lowercase name")
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError(f"{ENV}: the URL of {name} must be http(s)://host[:port][/path] without credentials or "
                             "a query")
        out[name] = url.rstrip("/")
    return out


def probe(name: str, base_url: str, transport: Optional[httpx.BaseTransport] = None) -> Reading:
    """One unauthenticated GET <base_url>/health: no redirects, no proxy from the environment, 5 seconds."""
    try:
        with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S), transport=transport, follow_redirects=False,
                          trust_env=False) as c:
            r = c.get(base_url.rstrip("/") + "/health")
    except httpx.HTTPError:
        return Reading(name, False, "unreachable")
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return Reading(name, False, f"http_{r.status_code}" if r.status_code != 200 else "bad_body")
    status = body.get("status")
    if r.status_code != 200 or status != "ok":
        raw = body.get("reasons") or body.get("degraded_reasons")
        if isinstance(raw, list) and raw:
            codes = sorted({_code(x.split(":", 1)[0].strip() if isinstance(x, str) else None, "bad_reason")
                            for x in raw[:16]})
            return Reading(name, False, ",".join(codes))
        if status != "ok" and status is not None:
            return Reading(name, False, _code(status, "bad_status"))
        return Reading(name, False, f"http_{r.status_code}")
    if body.get("write_ok") is False:
        return Reading(name, False, "write_refused")
    warnings = body.get("warnings")
    if warnings:
        codes = [_code(w, "bad_warning") for w in (warnings if isinstance(warnings, list) else [None])][:8]
        return Reading(name, False, "warning:" + ",".join(sorted(set(codes))))   # every code (round 3 N3-8)
    return Reading(name, True, "ok")


def poll(targets: dict[str, str], transport: Optional[httpx.BaseTransport] = None) -> list[Reading]:
    return [probe(name, url, transport) for name, url in sorted(targets.items())]


def _parse(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if isinstance(s, str) else None
    except ValueError:
        return None


def track(state: dict, readings: Iterable[Reading], now: datetime, down_min: int = DOWN_MIN) -> list[tuple[str, str, str]]:
    """Non-ok for ``down_min`` minutes without a break -> ``(CATEGORY, name, code)``; non-ok in more than half of the
    polls of the last ``FLAP_WINDOW_MIN`` minutes (at least ``FLAP_MIN_POLLS``) -> ``(FLAPPING, name, code)``. Keeps
    the first non-ok time per service in ``state["health"]`` and the recent polls in ``state["health_hist"]``; a
    service no longer polled is forgotten."""
    st = state.get("health")
    if not isinstance(st, dict):
        st = state["health"] = {}
    hist = state.get("health_hist")
    if not isinstance(hist, dict):
        hist = state["health_hist"] = {}
    window = timedelta(minutes=FLAP_WINDOW_MIN)
    seen = set()
    out = []
    for r in readings:
        seen.add(r.name)
        polls = [p for p in hist.get(r.name, []) if isinstance(p, list) and len(p) == 2
                 and (t := _parse(p[0])) is not None and now - t < 2 * window][-(FLAP_LAST_N * 2):]
        polls.append([now.isoformat(), bool(r.ok)])
        hist[r.name] = polls
        if r.ok:
            st.pop(r.name, None)
            continue
        since = _parse(st.get(r.name))
        if since is None:
            st[r.name] = now.isoformat()
            since = now
        if now - since >= timedelta(minutes=down_min):
            out.append((CATEGORY, r.name, r.code))
        elif _flapping(polls, now, window):
            out.append((FLAPPING, r.name, r.code))
    for d in (st, hist):
        for name in [n for n in d if n not in seen]:
            d.pop(name, None)
    return out


def _flapping(polls: list, now: datetime, window: timedelta) -> bool:
    last = polls[-FLAP_LAST_N:]
    if sum(1 for p in last if not p[1]) >= FLAP_LAST_K:
        return True
    recent = [p for p in polls if now - _parse(p[0]) < window]
    bad = sum(1 for p in recent if not p[1])
    return len(recent) >= FLAP_MIN_POLLS and bad > FLAP_FRACTION * len(recent)


def run(state: dict, env, now: datetime, transport: Optional[httpx.BaseTransport] = None) -> list[tuple[str, str, str]]:
    """The whole call site for the ops-alerts job: read ``OPS_HEALTH_URLS``, poll, track. Never raises for a bad
    configuration: an empty, malformed or incomplete list is a logged warning and a ``health_poll_unconfigured``
    condition (round 3 N3-8)."""
    try:
        targets = targets_from_env(env)
    except ValueError:
        log.warning("health poll: %s is malformed; no service is polled", ENV)
        return [(UNCONFIGURED, "ops-alerts", "malformed")]
    if not targets:
        log.warning("health poll: %s is empty; no service is polled", ENV)
        return [(UNCONFIGURED, "ops-alerts", "no_targets")]
    out = [(UNCONFIGURED, name, "missing") for name in REQUIRED if name not in targets]
    if out:
        log.warning("health poll: %s lacks %s", ENV, ", ".join(n for _, n, _ in out))
    return out + track(state, poll(targets, transport), now)
