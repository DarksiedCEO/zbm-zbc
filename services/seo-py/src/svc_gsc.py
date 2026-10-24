"""
Google Search Console in the service (ADR 0017, section "Search Console (first-party truth)"): Andre's tenant ->
property binding, the per-tenant read budget, the record-first read, and Osei's ``search_performance`` section of an
audit. The connector itself is gsc_client.py; the aggregation is agents/search_performance.py.

Rules:
  - NOT_CONNECTED by default: without SEO_GSC_SERVICE_ACCOUNT_FILE the port is NotConnectedSource and the section
    says GSC_NOT_CONNECTED without a single outbound call (the live run proves it with the real binary);
  - a tenant reads ONLY the property Andre bound to it (``POST /tenants/{tid}/gsc-property``, Andre's gate). The
    property must be about one of the tenant's own registered domains (or its ``www.`` twin), and a property (or a
    site overlapping it) belongs to one tenant only (GSC_PROPERTY_TAKEN). The property is never taken from a
    request, a page or Google's site list; a binding whose domain the tenant no longer holds is not read
    (GSC_PROPERTY_UNBOUND). A hub caller reaches only its own tenant (the API's scope, as everywhere);
  - kill switch ``provider:gsc`` (plus global, write, the tenant's and ``capability:audit``, as for every run step),
    checked before the read starts, before every request and on every socket operation (GSC_KILLED);
  - budget: at most SEO_GSC_READS_PER_DAY reads per tenant in any 24 hours (counted from recorded reads, on the
    injected clock; scheduled re-audits included), and at most SEO_GSC_MAX_REQUESTS HTTP requests per read
    (GSC_BUDGET_EXHAUSTED);
  - record-first: ``gsc_read_started`` (tenant, audit, property hash, window) is on the ledger and the log BEFORE any
    request leaves; a read that cannot be recorded is not made. The result is in the audit report, whose SHA-256 is
    recorded with the report (``audit_report_recorded``) and summarised in ``gsc_read_recorded``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from agents import search_performance as sp
from clock import parse_iso
from envelope import envelope, sha
from errors import Conflict, Invalid, Unavailable
from gsc_client import ALLOWED_HOSTS, SCOPE, GscError, property_host
from primitives import Killed
from reasons import R

log = logging.getLogger("seo.gsc")
AGENT, TASK = sp.AGENT, sp.TASK
METHOD = ("Search Analytics (Google Search Console API, read-only scope) for the tenant's bound property: property "
          "totals by date, then page and query rows paged within caps, reduced at once to aggregates by date, page "
          "template and query class. No query text is kept. Page URLs are kept only as path templates: identifiers, "
          "e-mails, numbers and tokens replaced by placeholders, every segment that is not one plain word becomes "
          "{slug}, and a template that still has a literal word but came from fewer than 3 distinct URLs is counted "
          "in {other}")
LIMITS = [
    ("Google returns top rows only and omits anonymised queries; the query-class breakdown therefore has an "
     "'unattributed' remainder, and the page-template breakdown is aggregated by page (not comparable with property "
     "totals)."),
    "Search Analytics data lags 2-3 days; the window ends three Pacific-Time days ago and covers final data only.",
    ("A plain one-word path segment is kept as written when 3 or more URLs share its template, so a site whose "
     "paths name people with single words (/users/jsmith/...) can still show such a word; hyphenated, dotted and "
     "digit-bearing segments never are."),
    ("Clicks and impressions are first-party observations; nothing here is paired with revenue yet (analytics and "
     "CRM are NOT_CONNECTED), so no decision is drawn from them."),
]
BLOCKED = frozenset({"GSC_AUTH_REFUSED", "GSC_PROPERTY_NOT_GRANTED", "GSC_BUDGET_EXHAUSTED"})


def _site(d: str) -> str:
    return d.removeprefix("www.")


def binding_ok(tenant: dict, prop: str | None) -> bool:
    host = property_host(prop or "")
    return host is not None and _site(host) in {_site(d) for d in tenant["domains"]}


class GscMixin:
    # ------------------------------------------------------------------ binding (Andre)

    def set_gsc_property(self, tid: str, body: dict) -> dict:
        prop = body.get("property")
        with self.lock:
            self._gate()
            self.check_kill(tenant=tid, write=True)
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            rk = self.rk("gsc_property", tid, body)
            if self._idem("andre", rk, body):
                return self.tenant_view(tid)
            if prop is not None:
                if not binding_ok(t, prop):
                    raise Invalid(R("GSC_PROPERTY_INVALID"))
                mine = _site(property_host(prop))
                for other, o in self.tenants.items():
                    theirs = o.get("gsc_property")
                    if other != tid and theirs:
                        h = _site(property_host(theirs) or "")
                        if h == mine or h.endswith("." + mine) or mine.endswith("." + h):
                            raise Conflict(R("GSC_PROPERTY_TAKEN"))
            data = {"tenant_id": tid, "property": prop}
            ev = {"tenant_id": tid, "property_sha256": None if prop is None else sha(prop), "bound": prop is not None}
            self._commit("tenant_gsc_property_set", self._req(data, "andre", rk, body, tid), "andre",
                         evidence=("tenant_gsc_property_set", f"tenant:{tid}", ev, ("andre", rk)))
            return self.tenant_view(tid)

    def _a_tenant_gsc_property_set(self, d, at):
        self.tenants[d["tenant_id"]]["gsc_property"] = d["property"]

    # ------------------------------------------------------------------ budget

    def _gsc_reads_used(self, tid: str) -> int:
        since = self.now() - timedelta(days=1)
        return sum(1 for at in self.gsc_reads.get(tid, []) if parse_iso(at) > since)

    def _a_gsc_read_started(self, d, at):
        reads = self.gsc_reads.setdefault(d["tenant_id"], [])
        reads.append(at)
        del reads[:-200]                                   # only the last day matters; bounded memory

    # ------------------------------------------------------------------ status

    def gsc_status(self) -> dict:
        port = self.ports.first_party.get("search_console")
        connected = bool(getattr(port, "connected", False))
        s = self.settings
        return {"state": "connected" if connected else "NOT_CONNECTED",
                "reason": None if connected else "GSC_NOT_CONNECTED",
                "service_account": port.key.client_email if connected else None,
                "scope": SCOPE, "hosts": sorted(ALLOWED_HOSTS), "kill_switch": "provider:gsc",
                "killed": bool(self.switches["provider"].get("gsc") or "gsc" in s.killed_providers),
                "window_days": s.gsc_window_days, "reads_per_tenant_per_day": s.gsc_reads_per_day,
                "max_requests_per_read": s.gsc_max_requests, "page_rows": s.gsc_page_rows,
                "max_pages": s.gsc_max_pages, "max_rows": s.gsc_max_rows, "deadline_seconds": s.gsc_deadline_s,
                "retention_days": s.gsc_retention_days,
                "tenants_bound": sum(1 for t in self.tenants.values() if t.get("gsc_property")),
                "outbound_requests_made": getattr(port, "requests_made", 0) if connected else 0}

    # ------------------------------------------------------------------ the audit section

    def _gsc_section(self, aid: str, tid: str, guard, box: dict) -> dict:
        """Osei's search_performance step. ``box['section']`` receives the aggregates (the report keeps them once,
        outside the envelope); the envelope carries the outcome, the reason and the window totals."""
        port = self.ports.first_party.get("search_console")

        def env(outcome: str, reason: str | None, facts: dict, nc=()) -> dict:
            box["state"] = {"outcome": outcome, "reason": reason}
            return envelope(AGENT, TASK, outcome, [], reason=reason, methodology=METHOD, limitations=LIMITS,
                            not_connected=nc, facts={"requests": 0, **facts})

        if not getattr(port, "connected", False):
            return env("NOT_CONNECTED", "GSC_NOT_CONNECTED", {"state": "NOT_CONNECTED"},
                       nc={"first_party:search_console"})
        with self.lock:
            t = self.tenants[tid]
            prop = t.get("gsc_property")
            if not binding_ok(t, prop):
                return env("NOT_CONNECTED", "GSC_PROPERTY_UNBOUND", {"state": "UNBOUND"},
                           nc={"first_party:search_console"})
            hosts = set()
            for d in t["domains"]:
                hosts |= {_site(d), "www." + _site(d)}
            ent = self.entity_for_tenant(tid)
            brands = sp.brand_tokens(t["domains"], ((ent or {}).get("fields") or {}).get("name", {}).get("value")
                                     if isinstance(ent, dict) else None)
        try:
            guard(provider="gsc")
        except Killed as k:
            return env("KILLED", "GSC_KILLED", {"state": "KILLED", "switch": k.code})
        with self.lock:
            if self._gsc_reads_used(tid) >= self.settings.gsc_reads_per_day:
                return env("BLOCKED", "GSC_BUDGET_EXHAUSTED", {"state": "BUDGET_EXHAUSTED",
                                                               "reads_per_day": self.settings.gsc_reads_per_day})
            start, end = sp.window(self.now(), self.settings.gsc_window_days)
            data = {"tenant_id": tid, "audit_id": aid, "property": prop, "window_start": start, "window_end": end}
            try:
                self._commit("gsc_read_started", data, "seo",
                             evidence=("gsc_read_started", f"audit:{aid}",
                                       {"tenant_id": tid, "audit_id": aid, "property_sha256": sha(prop),
                                        "window_start": start, "window_end": end}, ("gsc", aid)))
            except Unavailable as exc:                     # record-first: no record, no request
                return env("BLOCKED", exc.reason, {"state": "NOT_RECORDED"})
            box["read_started"] = True
        rd = port.open_read(lambda: guard(provider="gsc"))
        try:
            section = sp.read_aggregates(rd, prop, start, end, hosts, brands)
        except GscError as e:
            outcome = "KILLED" if e.code == "GSC_KILLED" else "BLOCKED" if e.code in BLOCKED else "FAILED"
            return env(outcome, e.code, {"state": e.code, "detail": e.detail, "requests": rd.requests,
                                         "window": {"start": start, "end": end}})
        except Killed as k:                                # the read's guard converts these; belt and braces
            return env("KILLED", "GSC_KILLED", {"state": "KILLED", "switch": k.code, "requests": rd.requests})
        except Exception as exc:  # noqa: BLE001 - AEGIS gl-gsc M1: whatever Google sent, the section fails, not the audit
            # a parser gap that let a malformed answer raise something other than GscError: the section is FAILED
            # GSC_PROTOCOL_ERROR and the audit completes. Only the exception's type is logged (its text could quote
            # Google's answer).
            log.error("search_performance read failed: %s", type(exc).__name__)
            return env("FAILED", "GSC_PROTOCOL_ERROR", {"state": "GSC_PROTOCOL_ERROR", "detail": "unexpected_answer",
                                                         "error_type": type(exc).__name__[:60],
                                                         "requests": rd.requests,
                                                         "window": {"start": start, "end": end}})
        box["section"] = section
        return env("OK", None, {"state": "OK", "requests": rd.requests, "window": section["window"],
                                "totals": section["totals"], "section_in": "report.search_performance"})

    # ------------------------------------------------------------------ report and retention

    @staticmethod
    def gsc_report_block(box: dict) -> dict | None:
        st = box.get("state")
        if st is None:
            return None
        return {**st, "read_started": bool(box.get("read_started")), "section": box.get("section")}

    @staticmethod
    def gsc_evidence(aid: str, report: dict) -> list:
        """``gsc_read_recorded`` beside the report record when the audit read Search Console."""
        b = report.get("search_performance")
        if not b or not b.get("read_started"):
            return []
        s = b.get("section")
        return [("gsc_read_recorded", f"audit:{aid}",
                 {"audit_id": aid, "outcome": b["outcome"], "reason": b.get("reason"),
                  "section_sha256": None if s is None else sha(s),
                  "totals": None if s is None else {k: s["totals"][k] for k in ("clicks", "impressions")}},
                 ("gsc_report", aid))]

    def gsc_retained(self, report: dict | None, completed_at: str | None) -> dict | None:
        """Retention (minimisation): past SEO_GSC_RETENTION_DAYS a report's search-performance breakdowns are served
        as window totals only. The record log is append-only and keeps the aggregates it recorded (they identify no
        one; see ADR 0017 decision 18)."""
        if not report or not report.get("search_performance") or not completed_at:
            return report
        b = report["search_performance"]
        if not b.get("section") or self.now() - parse_iso(completed_at) <= timedelta(days=self.settings.gsc_retention_days):
            return report
        return {**report, "search_performance": {**b, "section": sp.totals_only(b["section"])}}
