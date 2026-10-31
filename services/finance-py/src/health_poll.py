"""
Service health poll for the founder-alerts job (AEGIS round 2 N-3; ADR 0025 section 8).

Why. Railway's deploy healthcheck is ``/livez`` (AEGIS wave A F-1), so Railway activates a deployment whose service
is held, in reconcile mode or degraded: ``/livez`` answers 200 whenever the process serves. ``/health`` stays the
truth (503 with a fixed code while a service cannot write; ``"maintenance"`` in reconcile mode; ``warnings`` such as
``rotation_expired``, ``clock_skew`` or ``ledger_anchor_unkeyed``). Something must read it and tell Andre. That is
the ``ops-alerts`` cron job (fix wave C, ADR 0027, ``founder_alerts.FounderAlerts.run``), which runs every 5 minutes;
this module is the self-contained piece it calls.

Integration point (wired when wave C's branch is merged; nothing here sends anything)::

    import health_poll
    targets = health_poll.targets_from_env(os.environ)          # OPS_HEALTH_URLS
    for category, source, ref in health_poll.track(st, health_poll.poll(targets), now):
        conds.append(Condition.make(category, source, ref))      # SEVERITY["service_unhealthy"] = "high"

``st`` is the founder-alerts state dict (this module keeps its own ``"health"`` key in it). A condition exists once a
service has been non-ok for ``DOWN_MIN`` (10) minutes without a break, and clears when one poll sees it ok.

What counts as non-ok: no answer, an HTTP status other than 200, a ``status`` other than ``"ok"``, ``write_ok`` false,
or any entry in ``warnings``. Only fixed codes leave this module (``unreachable``, ``http_503``, ``maintenance``,
``degraded``, ``write_refused``, ``warning:rotation_expired`` ...): a value that is not a short lowercase code is
replaced by ``bad_status``/``bad_warning``, so no id or free text from a body can reach an email.

``OPS_HEALTH_URLS``: ``name=url`` pairs separated by commas, e.g.
``finance=http://finance.railway.internal:8080,legal=http://legal.railway.internal:8080,compliance=...,ledger=...``.
Each URL is the service root (``/health`` is appended); http or https, no credentials, no query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx

DOWN_MIN = 10
CATEGORY = "service_unhealthy"
TIMEOUT_S = 5
ENV = "OPS_HEALTH_URLS"

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
        if status != "ok" and status is not None:
            return Reading(name, False, _code(status, "bad_status"))
        return Reading(name, False, f"http_{r.status_code}")
    if body.get("write_ok") is False:
        return Reading(name, False, "write_refused")
    warnings = body.get("warnings")
    if warnings:
        first = warnings[0] if isinstance(warnings, list) else None
        return Reading(name, False, "warning:" + _code(first, "bad_warning"))
    return Reading(name, True, "ok")


def poll(targets: dict[str, str], transport: Optional[httpx.BaseTransport] = None) -> list[Reading]:
    return [probe(name, url, transport) for name, url in sorted(targets.items())]


def _parse(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if isinstance(s, str) else None
    except ValueError:
        return None


def track(state: dict, readings: Iterable[Reading], now: datetime, down_min: int = DOWN_MIN) -> list[tuple[str, str, str]]:
    """Non-ok for ``down_min`` minutes without a break -> ``(CATEGORY, service name, code)``. Keeps the first non-ok
    time per service in ``state["health"]``; an ok reading (or a service no longer polled) clears it."""
    st = state.get("health")
    if not isinstance(st, dict):
        st = state["health"] = {}
    seen = set()
    out = []
    for r in readings:
        seen.add(r.name)
        if r.ok:
            st.pop(r.name, None)
            continue
        since = _parse(st.get(r.name))
        if since is None:
            st[r.name] = now.isoformat()
            since = now
        if now - since >= timedelta(minutes=down_min):
            out.append((CATEGORY, r.name, r.code))
    for name in [n for n in st if n not in seen]:
        st.pop(name, None)
    return out
