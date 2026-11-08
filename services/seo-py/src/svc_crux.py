"""
Lena in the service (ADR 0050): the record-first CrUX read inside an audit, its budgets, its status, and the
tenant-scoped page-experience view. The provider is crux_client.py; the shaping is agents/lena.py.

Rules:
  - OFF by default: without SEO_CRUX_ENABLED=1 and a key file the port is NotConnectedFieldData and Lena's section
    says CRUX_NOT_CONNECTED with zero calls;
  - Lena reads only the audit's own target: the origin ``<scheme>://<domain>`` of a domain registered to the audit's
    tenant (checked by the audit before anything runs) and the audit's own paths on it. Nothing from a crawled page,
    a request body beyond the audit's, or Google's answer chooses what is asked;
  - kill switch ``provider:crux`` (plus global, write, the tenant's and ``capability:audit``, as for every run step),
    checked before the read starts, before every request and on every socket operation (CRUX_KILLED);
  - budgets, all 0 (OFF) by default: at most SEO_CRUX_CALLS_PER_RUN HTTP calls per audit (retries included), at
    most SEO_CRUX_CALLS_PER_DAY calls in any 24 hours across all tenants (Google's quota is per project) and at most
    SEO_CRUX_TENANT_CALLS_PER_DAY per tenant in any 24 hours; the client paces itself to at most 100 queries a
    minute (crux_client token bucket) under Google's 150. A run
    reserves its allowance record-first; a completed run is charged what it actually sent, an interrupted one its
    whole reservation (CRUX_BUDGET_EXHAUSTED when nothing is left);
  - record-first: ``crux_read_started`` (tenant, audit, origin hash, calls reserved) is on the ledger and the log
    BEFORE any request leaves; a read that cannot be recorded is not made. The section is in the audit report (whose
    SHA-256 is recorded) and summarised in ``crux_read_recorded``;
  - the API key is never in a URL, a log line, an error, the status or the report.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import crux_client as cx
from agents import lena
from clock import iso, parse_iso
from envelope import envelope, sha
from errors import NotFound, Unavailable
from primitives import Killed
from reasons import R

log = logging.getLogger("seo.crux")
AGENT, TASK = lena.AGENT, lena.TASK


class CruxMixin:
    # ------------------------------------------------------------------ budget

    def _crux_calls_used(self, tid: str | None = None) -> int:
        """Calls charged in the last 24 hours (all tenants, or ``tid``'s): a completed audit's actual calls, otherwise
        the reservation."""
        since = self.now() - timedelta(days=1)
        used = 0
        for r in self._crux_reads():
            if parse_iso(r["at"]) <= since or (tid is not None and r.get("tenant_id") != tid):
                continue
            a = self.audits.get(r["audit_id"]) or {}
            block = ((a.get("report") or {}).get("page_experience") or {}) if a.get("status") == "completed" else {}
            sec = block.get("section") if isinstance(block, dict) else None
            used += sec["calls"] if isinstance(sec, dict) and isinstance(sec.get("calls"), int) else r["reserved"]
        return used

    def _crux_reads(self) -> list:
        return self.crux_reads

    def _a_crux_read_started(self, d, at):
        reads = self._crux_reads()
        reads.append({"at": at, "audit_id": d["audit_id"], "tenant_id": d["tenant_id"], "reserved": d["calls_reserved"]})
        # only the last day matters; a reservation is at least one call, so a day holds at most the daily cap's
        # (20,000) worth: bounded memory without ever dropping a reservation still inside the window
        del reads[:-20_000]

    # ------------------------------------------------------------------ status

    def crux_status(self) -> dict:
        port = getattr(self.ports, "field_data", None)
        connected = bool(getattr(port, "connected", False))
        s = self.settings
        with self.lock:
            used = self._crux_calls_used()
        return {"agent": AGENT, "task": TASK, "state": "connected" if connected else "NOT_CONNECTED",
                "reason": None if connected else "CRUX_NOT_CONNECTED",
                "provider": getattr(port, "provider", None) if connected else None,
                "enabled": bool(getattr(s, "crux_enabled", False)), "hosts": sorted(cx.ALLOWED_HOSTS),
                "key_in": f"header {cx.KEY_HEADER} (never the URL)", "kill_switch": "provider:crux",
                "killed": bool(self.switches["provider"].get("crux") or "crux" in s.killed_providers),
                "calls_per_run": s.crux_calls_per_run, "calls_per_day": s.crux_calls_per_day,
                "tenant_calls_per_day": s.crux_tenant_calls_per_day,
                "pacing": {"queries_per_minute_max": cx.PACE_MAX_PER_MINUTE, "burst": cx.PACE_BURST,
                           "refill_per_minute": cx.PACE_REFILL_PER_MINUTE},
                "calls_used_last_24h": used, "deadline_seconds": s.crux_deadline_s,
                "timeout_seconds": s.crux_timeout_s, "history_periods": s.crux_history_periods,
                "thresholds_version": lena.THRESHOLDS_VERSION,
                "outbound_requests_made": getattr(port, "requests_made", 0) if connected else 0}

    # ------------------------------------------------------------------ the audit section

    def _crux_section(self, aid: str, tid: str, guard, box: dict, body: dict) -> dict:
        """Lena's step. ``box['section']`` receives the section (the report keeps it once, outside the envelope)."""
        port = getattr(self.ports, "field_data", None)

        def env(outcome: str, reason: str | None, facts: dict, nc=()) -> dict:
            box["state"] = {"outcome": outcome, "reason": reason}
            return envelope(AGENT, TASK, outcome, [], reason=reason, methodology=lena.METHOD,
                            limitations=lena.LIMITS, not_connected=nc, facts={"calls": 0, **facts})

        if not getattr(port, "connected", False):
            return env("NOT_CONNECTED", "CRUX_NOT_CONNECTED", {"state": "NOT_CONNECTED"},
                       nc={"field_data:crux"})
        origin = f"{body['scheme']}://{body['domain']}"
        with self.lock:
            registered = set(self.tenants[tid]["domains"])
        # review M1: the other form (bare / www.) of the audited domain, when the tenant has not registered it (a
        # domain and its www. twin belong to one tenant, ADR 0017 decision 7, so the twin is the tenant's to ask about)
        other = lena.twin_of(body["domain"])         # None unless a registrable domain or its www. form (PSL)
        twin = None if other is None or other in registered else f"https://{other}"
        if not cx.ORIGIN.fullmatch(origin) or (twin is not None and not cx.ORIGIN.fullmatch(twin)):
            return env("FAILED", "CRUX_PROTOCOL_ERROR", {"state": "ORIGIN_INVALID"})
        try:
            guard(provider="crux")
        except Killed as k:
            return env("KILLED", "CRUX_KILLED", {"state": "KILLED", "switch": k.code})
        s = self.settings
        with self.lock:
            left = s.crux_calls_per_day - self._crux_calls_used()
            tenant_left = s.crux_tenant_calls_per_day - self._crux_calls_used(tid)
            reserve = max(0, min(s.crux_calls_per_run, left, tenant_left))
            if reserve < 1:
                return env("BLOCKED", "CRUX_BUDGET_EXHAUSTED",
                           {"state": "BUDGET_EXHAUSTED", "calls_per_run": s.crux_calls_per_run,
                            "calls_per_day": s.crux_calls_per_day, "calls_left_today": max(0, left),
                            "tenant_calls_per_day": s.crux_tenant_calls_per_day,
                            "tenant_calls_left_today": max(0, tenant_left)})
            data = {"tenant_id": tid, "audit_id": aid, "origin": origin, "calls_reserved": reserve}
            try:
                self._commit("crux_read_started", data, "seo",
                             evidence=("crux_read_started", f"audit:{aid}",
                                       {"tenant_id": tid, "audit_id": aid, "origin_sha256": sha(origin),
                                        "calls_reserved": reserve}, ("crux", aid)))
            except Unavailable as exc:                     # record-first: no record, no request
                return env("BLOCKED", exc.reason, {"state": "NOT_RECORDED"})
            box["read_started"] = True
            today, now = self.now().date().isoformat(), iso(self.now())
        rd = port.open_read(lambda: guard(provider="crux"), max_calls=reserve)
        try:
            section = lena.run(rd, origin, list(body["paths"]), s.crux_history_periods, today, now, twin=twin)
        except Exception as exc:  # noqa: BLE001 - whatever Google sent, the section fails, not the audit
            log.error("page_experience read failed: %s", type(exc).__name__)
            return env("FAILED", "CRUX_PROTOCOL_ERROR", {"state": "CRUX_PROTOCOL_ERROR", "detail": "unexpected_answer",
                                                          "error_type": type(exc).__name__[:60],
                                                          "calls": getattr(rd, "requests", 0)})
        box["section"] = section
        outcome, reason = lena.outcome(section)
        return env(outcome, reason, {"state": outcome, "calls": section["calls"], "counts": section["counts"],
                                     "stopped": section["stopped"], "section_in": "report.page_experience"})

    # ------------------------------------------------------------------ report and evidence

    @staticmethod
    def crux_report_block(box: dict) -> dict | None:
        st = box.get("state")
        if st is None:
            return None
        return {**st, "read_started": bool(box.get("read_started")), "section": box.get("section")}

    @staticmethod
    def crux_evidence(aid: str, report: dict) -> list:
        b = report.get("page_experience")
        if not b or not b.get("read_started"):
            return []
        s = b.get("section")
        return [("crux_read_recorded", f"audit:{aid}",
                 {"audit_id": aid, "outcome": b["outcome"], "reason": b.get("reason"),
                  "section_sha256": None if s is None else sha(s),
                  "calls": None if s is None else s["calls"],
                  "counts": None if s is None else s["counts"]}, ("crux_report", aid))]

    def page_experience_view(self, tid: str, aid: str) -> dict:
        """Lena's section of one completed audit, tenant-scoped (another tenant's audit is the same 404)."""
        a = self.audit_view(tid, aid)
        if a["status"] != "completed":
            from errors import Conflict
            raise Conflict(R("AUDIT_NOT_COMPLETED"))
        rep = a.get("report") or {}
        block = rep.get("page_experience")
        env = next((e for e in rep.get("agents") or [] if e.get("agent") == AGENT and e.get("task") == TASK), None)
        if env is None:
            raise NotFound(R("PAGE_EXPERIENCE_NOT_IN_AUDIT"))
        return {"audit_id": aid, "tenant_id": tid, "agent": AGENT, "task": TASK, "outcome": env["outcome"],
                "reason": env.get("reason"), "page_experience": block, "methodology": env["methodology"],
                "limitations": env["limitations"]}
