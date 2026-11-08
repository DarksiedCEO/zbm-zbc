"""
Priya's keyword ledger in the service (ADR 0047): ``POST /seo/v1/tenants/{tid}/keyword-ledger``.

Rules:
  - callers: ``dashboard`` and ``seo_agent`` for an own tenant; a client tenant needs Andre (the dashboard plus his
    approval token), a bound Finance (31) client and Compliance (38)'s current activation of it (ADR 0024, as for the
    client report; asked outside the lock, no override). The hub cannot run it: a run spends a Search Console read;
  - Search Console: only the property Andre bound to the tenant (svc_gsc.binding_ok); NOT_CONNECTED without a key;
    the run counts as one read against SEO_GSC_READS_PER_DAY (shared with audits and scheduled re-audits);
  - Bing Webmaster Tools: only for one of the tenant's own registered domains (``bing_site_domain``); the port is
    NOT_CONNECTED in production (bing_webmaster.py: fixtures only);
  - kill switches: global, write, the tenant's, ``capability:keywords``, ``provider:gsc`` / ``provider:bing``, and
    the department manager's lifecycle (a restricted or retired Priya does not run);
  - record-first: ``keyword_ledger_started`` (tenant, run, sources, property hash, window) is on the ledger and the
    log BEFORE any request leaves; a run that cannot be recorded makes no request. The ledger itself is handed out
    once in the answer and NOT stored anywhere (it carries query text; ADR 0017 GSC-6 keeps query text out of the
    log): ``keyword_ledger_recorded`` keeps its SHA-256, the status and the counts. A replay of the same request
    answers the recorded summary with ``ledger: null``;
  - a source that fails never fails the run: its state and reason are in the ledger and the status says what was
    measured (OK, PARTIAL, EMPTY_MEASURED, INCONCLUSIVE). Nothing unread is ever reported as zero.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import bing_webmaster as bw
from agents import priya
from agents import search_performance as sp
from clock import iso
from envelope import envelope, sha
from errors import Conflict, Forbidden, Invalid, NotFound
from gsc_client import GscError
from ledger import derived_id
from primitives import Killed
from reasons import R
from svc_gsc import BLOCKED as GSC_BLOCKED
from svc_gsc import binding_ok

log = logging.getLogger("seo.keywords")
METHOD = ("Search Console query rows (and Bing Webmaster Tools GetQueryStats when wired) for the tenant's own "
          "property, PII-suspect queries dropped, normalised and merged per query with each source's numbers kept "
          "apart; tiered ESTABLISHED / EMERGING by measured impressions; EMERGING variants derived deterministically "
          "from the site's own long-tail frames; no model, no paid data, no market volume, no difficulty score")


def _outcome_of(code: str) -> str:
    if code in ("GSC_KILLED", "BING_KILLED"):
        return "KILLED"
    if code in GSC_BLOCKED or code in bw.BLOCKED or code == "GSC_BUDGET_EXHAUSTED":
        return "BLOCKED"
    if code in ("GSC_NOT_CONNECTED", "GSC_PROPERTY_UNBOUND", "BING_NOT_CONNECTED"):
        return "NOT_CONNECTED"
    return "FAILED"


class KeywordsMixin:
    # ------------------------------------------------------------------ the run

    def run_keyword_ledger(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        sources = list(body["sources"])
        if len(set(sources)) != len(sources):
            raise Invalid(R("KEYWORD_SOURCES_INVALID"))
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="keywords", write=True)
            actor = "andre" if andre else actor
            rk = self.rk("keyword_ledger", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.keyword_run_view(tid, prev[1], replay=True)
            client = self._kw_authorize(t, body, andre)
        if client is not None:
            self._verify_compliance(client)                # ADR 0024: outside the lock, never overridden
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="keywords", write=True)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.keyword_run_view(tid, prev[1], replay=True)
            if self._kw_authorize(t, body, andre) != client:
                raise Conflict(R("TENANT_FINANCE_CLIENT_CHANGED"))
            if any(r["tenant_id"] == tid and r["run_id"] in self.running_keyword_runs
                   for r in self.keyword_runs.values()):
                raise Conflict(R("KEYWORD_RUN_RUNNING"))
            run_id = derived_id("kwl", tid, actor, rk)
            plan = self._kw_plan(tid, t, sources, body)
            data = {"run_id": run_id, "tenant_id": tid, "tenant_kind": t["kind"], "sources": sources,
                    "gsc_read": plan["gsc"]["read"], "property": plan["gsc"].get("property"),
                    "window_start": plan["gsc"].get("start"), "window_end": plan["gsc"].get("end"),
                    "bing_site": plan["bing"].get("site"), "andre_approved": bool(andre)}
            ev = {"run_id": run_id, "tenant_id": tid, "sources": sources, "gsc_read": plan["gsc"]["read"],
                  "property_sha256": None if not plan["gsc"].get("property") else sha(plan["gsc"]["property"]),
                  "window_start": plan["gsc"].get("start"), "window_end": plan["gsc"].get("end"),
                  "bing_site_sha256": None if not plan["bing"].get("site") else sha(plan["bing"]["site"])}
            evidence = [("keyword_ledger_started", f"keywords:{run_id}", ev, (actor, rk))]
            if andre:
                evidence.append(("keyword_ledger_approved_by_andre", f"keywords:{run_id}", {"run_id": run_id},
                                 ("andre", rk)))
            self._commit("keyword_ledger_started", self._req(data, actor, rk, body, run_id), actor, evidence=evidence)
            self.running_keyword_runs.add(run_id)
        try:
            return self._kw_execute(tid, run_id, plan)
        finally:
            with self.lock:
                self.running_keyword_runs.discard(run_id)

    def _kw_authorize(self, t: dict, body: dict, andre: bool) -> Optional[str]:
        """Who may run Priya on whose data. Returns the Finance client to ask Compliance about (client tenants)."""
        if self.agent_blocked(priya.AGENT):
            raise Forbidden(R("AGENT_RESTRICTED"))
        dom = body.get("bing_site_domain")
        if dom is not None and dom not in t["domains"]:
            raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
        if "bing" in body["sources"] and dom is None:
            raise Invalid(R("KEYWORD_SOURCES_INVALID"))
        if t["kind"] != "client":
            return None
        if not andre:
            raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
        client = t.get("finance_client_id")
        if not client:
            raise Conflict(R("TENANT_FINANCE_CLIENT_UNBOUND"))
        return client

    def _kw_plan(self, tid: str, t: dict, sources: list, body: dict) -> dict:
        """Under the lock: what each requested source will do. Only a planned Search Console read is counted against
        the tenant's read budget (when ``keyword_ledger_started`` is applied)."""
        plan = {"gsc": {"read": False, "state": "NOT_REQUESTED"}, "bing": {"state": "NOT_REQUESTED"}}
        if "gsc" in sources:
            g = plan["gsc"]
            port = self.ports.first_party.get("search_console")
            prop = t.get("gsc_property")
            if not getattr(port, "connected", False):
                g["state"] = "GSC_NOT_CONNECTED"
            elif not binding_ok(t, prop):
                g["state"] = "GSC_PROPERTY_UNBOUND"
            elif self.kill_code(tenant=tid, provider="gsc", write=True):
                g["state"] = "GSC_KILLED"
            elif self._gsc_reads_used(tid) >= self.settings.gsc_reads_per_day:
                g["state"] = "GSC_BUDGET_EXHAUSTED"
            else:
                start, end = sp.window(self.now(), self.settings.gsc_window_days)
                g.update(read=True, state="PLANNED", property=prop, start=start, end=end)
        ent = self.entity_for_tenant(tid)
        plan["brands"] = sp.brand_tokens(t["domains"], ((ent or {}).get("fields") or {}).get("name", {})
                                         .get("value") if isinstance(ent, dict) else None)
        if "bing" in sources:
            b = plan["bing"]
            port = self.ports.first_party.get("bing_webmaster")
            if not getattr(port, "connected", False):
                b["state"] = "BING_NOT_CONNECTED"
            elif self.kill_code(tenant=tid, provider="bing", write=True):
                b["state"] = "BING_KILLED"
            else:
                b.update(state="PLANNED", site=bw.site_url_for(body["bing_site_domain"]))
        return plan

    def _kw_execute(self, tid: str, run_id: str, plan: dict) -> dict:
        guard = self.guard(tid, run="keywords")
        try:
            guard(agent=priya.AGENT)
            gsc = self._kw_gsc(plan["gsc"], guard)
            bing = self._kw_bing(plan["bing"], guard)
            ledger = priya.build(tid, run_id, iso(self.now()), plan["brands"], gsc, bing)
        except Killed as k:
            return self._kw_interrupt(tid, run_id, R(k.code) if k.code in _KILL_CODES else R("KILLED_GLOBAL"))
        except Exception as exc:  # noqa: BLE001 - a crash closes the run, never leaves it running or answers 500
            log.error("keyword ledger run failed: %s", type(exc).__name__)
            return self._kw_interrupt(tid, run_id, R("RUN_FAILED"), error_type=type(exc).__name__[:60])
        env = self._kw_envelope(ledger, gsc, bing)
        with self.lock:
            self._gate()
            stop = self.kill_code(tenant=tid, capability="keywords", write=True)
            if stop is not None:
                return self._kw_interrupt(tid, run_id, R(stop))
            lsha = sha(ledger)
            rec = {"run_id": run_id, "status": ledger["status"], "outcome": env["outcome"], "ledger_sha256": lsha,
                   "counts": _count_view(ledger["counts"]),
                   "sources": {k: v["state"] for k, v in ledger["sources"].items()}}
            self._commit("keyword_ledger_recorded", rec, "seo",
                         evidence=("keyword_ledger_recorded", f"keywords:{run_id}", dict(rec), ("recorded", run_id)))
            view = self.keyword_run_view(tid, run_id)
        return {**view, "envelope": env, "ledger": ledger, "ledger_retained": False}

    def _kw_interrupt(self, tid: str, run_id: str, reason: str, error_type: Optional[str] = None) -> dict:
        data = {"run_id": run_id, "reason": reason}
        if error_type:
            data["error_type"] = error_type
        with self.lock:
            self._gate()
            self._commit("keyword_ledger_interrupted", data, "seo",
                         evidence=("keyword_ledger_interrupted", f"keywords:{run_id}", dict(data),
                                   ("interrupted", run_id)))
            return {**self.keyword_run_view(tid, run_id), "ledger": None, "ledger_retained": False}

    # ------------------------------------------------------------------ the sources (outside the lock)

    def _kw_gsc(self, g: dict, guard) -> priya.SourceResult:
        if not g["read"]:
            st = g["state"]
            return priya.SourceResult("search_console", st, "NOT_REQUESTED" if st == "NOT_REQUESTED"
                                      else _outcome_of(st))
        port = self.ports.first_party.get("search_console")
        prop, start, end = g["property"], g["start"], g["end"]
        meta = {"property_sha256": sha(prop), "window": {"start": start, "end": end,
                                                        "timezone": "America/Los_Angeles", "data_state": "final",
                                                        "search_type": "web"}}
        rd = port.open_read(lambda: guard(provider="gsc"))
        try:
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            rd.check_property(prop)
            tc = ti = 0
            for _keys, c, i, _ctr, _pos in rd.query(prop, start, end, ("date",), "byProperty", days, 0):
                tc += c
                ti += i
            rows: list = []
            qstats: dict = {}
            for page in rd.pages(prop, start, end, ("query",), "byProperty", qstats):
                rows += [(k[0], c, i, ctr, pos) for k, c, i, ctr, pos in page]
        except GscError as e:
            return priya.SourceResult("search_console", e.code, _outcome_of(e.code),
                                      meta={**meta, "detail": e.detail, "requests": rd.requests})
        except Killed:
            return priya.SourceResult("search_console", "GSC_KILLED", "KILLED", meta={**meta,
                                                                                    "requests": rd.requests})
        except Exception as exc:  # noqa: BLE001 - a parser gap fails the source, never the run (AEGIS gl-gsc M1)
            log.error("keyword ledger gsc read failed: %s", type(exc).__name__)
            return priya.SourceResult("search_console", "GSC_PROTOCOL_ERROR", "FAILED",
                                      meta={**meta, "detail": "unexpected_answer", "requests": rd.requests})
        qc, qi = sum(r[1] for r in rows), sum(r[2] for r in rows)
        meta.update(requests=rd.requests, rows_read=qstats.get("rows_read", 0), pages_read=qstats.get("pages_read", 0),
                    truncated=bool(qstats.get("truncated")), totals={"clicks": tc, "impressions": ti},
                    query_rows_sum={"clicks": qc, "impressions": qi},
                    unattributed={"clicks": max(0, tc - qc), "impressions": max(0, ti - qi),
                                  "rows_exceed_totals": qc > tc or qi > ti,
                                  "why": "anonymised queries Google does not return, and rows past the caps"})
        return priya.SourceResult("search_console", "OK", "OK", rows, meta)

    def _kw_bing(self, b: dict, guard) -> priya.SourceResult:
        if b["state"] != "PLANNED":
            st = b["state"]
            return priya.SourceResult("bing_webmaster", st, "NOT_REQUESTED" if st == "NOT_REQUESTED"
                                      else _outcome_of(st))
        port = self.ports.first_party.get("bing_webmaster")
        meta = {"site_sha256": sha(b["site"])}
        try:
            rows, stats = port.query_stats(b["site"], lambda: guard(provider="bing"))
        except bw.BingError as e:
            return priya.SourceResult("bing_webmaster", e.code, _outcome_of(e.code), meta={**meta, "detail": e.detail})
        except Killed:
            return priya.SourceResult("bing_webmaster", "BING_KILLED", "KILLED", meta=meta)
        except Exception as exc:  # noqa: BLE001 - as for Search Console
            log.error("keyword ledger bing read failed: %s", type(exc).__name__)
            return priya.SourceResult("bing_webmaster", "BING_PROTOCOL_ERROR", "FAILED",
                                      meta={**meta, "detail": "unexpected_answer"})
        dates = sorted({r[1] for r in rows})
        meta.update(requests=stats.get("requests", 0),
                    date_range=None if not dates else {"start": dates[0], "end": dates[-1]})
        return priya.SourceResult("bing_webmaster", "OK", "OK", rows, meta)

    @staticmethod
    def _kw_envelope(ledger: dict, gsc: priya.SourceResult, bing: priya.SourceResult) -> dict:
        requested = [s for s in (gsc, bing) if s.state != "NOT_REQUESTED"]
        outs = [s.outcome for s in requested]
        st = ledger["status"]
        if st in ("OK", "EMPTY_MEASURED"):
            outcome = "OK"
        elif st == "PARTIAL":
            outcome = "PARTIAL"
        elif "KILLED" in outs:
            outcome = "KILLED"
        elif set(outs) == {"NOT_CONNECTED"}:
            outcome = "NOT_CONNECTED"
        else:
            outcome = next((o for o in ("FAILED", "BLOCKED", "NOT_CONNECTED") if o in outs), "INSUFFICIENT_EVIDENCE")
        reason = None if outcome == "OK" else next((s.state for s in requested if not s.ok), None)
        nc = {f"first_party:{s.name}" for s in requested if s.outcome == "NOT_CONNECTED"}
        nc.add("keyword_planner:google_ads")
        return envelope(priya.AGENT, priya.TASK, outcome, [], reason=reason, methodology=METHOD,
                        limitations=priya.LIMITS, not_connected=nc,
                        facts={"status": st, "counts": _count_view(ledger["counts"]),
                               "sources": {k: v["state"] for k, v in ledger["sources"].items()}})

    # ------------------------------------------------------------------ replay and views

    def _a_keyword_ledger_started(self, d, at):
        self.keyword_runs[d["run_id"]] = {
            "run_id": d["run_id"], "tenant_id": d["tenant_id"], "sources": list(d["sources"]),
            "gsc_read": bool(d["gsc_read"]), "window": None if not d.get("window_start") else
            {"start": d["window_start"], "end": d["window_end"]}, "andre_approved": bool(d.get("andre_approved")),
            "requested_by": d["actor"], "requested_at": at, "status": "running", "completed_at": None,
            "ledger_status": None, "outcome": None, "ledger_sha256": None, "counts": None, "source_states": None}
        if d["gsc_read"]:                                  # one Search Console read against the tenant's budget
            reads = self.gsc_reads.setdefault(d["tenant_id"], [])
            reads.append(at)
            del reads[:-200]

    def _a_keyword_ledger_recorded(self, d, at):
        self.keyword_runs[d["run_id"]].update(status="completed", completed_at=at, ledger_status=d["status"],
                                              outcome=d["outcome"], ledger_sha256=d["ledger_sha256"],
                                              counts=dict(d["counts"]), source_states=dict(d["sources"]))

    def _a_keyword_ledger_interrupted(self, d, at):
        self.keyword_runs[d["run_id"]].update(status="interrupted", completed_at=at, interrupted_reason=d["reason"],
                                              error_type=d.get("error_type"))

    def keyword_run_view(self, tid: str, run_id: str, replay: bool = False) -> dict:
        with self.lock:
            r = self.keyword_runs.get(run_id)
            if r is None or r["tenant_id"] != tid:
                raise NotFound(R("KEYWORD_RUN_NOT_FOUND"))
            out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                   for k, v in r.items()}
            if r["status"] == "running" and run_id not in self.running_keyword_runs:
                out["status"] = "running_elsewhere_or_interrupted"
            out["agent_card"] = dict(priya.CARD)
            if replay:
                out.update(replay=True, ledger=None, ledger_retained=False,
                           note="the ledger is handed out once and never stored; run a new request for a new ledger")
            return out

    def keyword_runs_view(self, tid: str) -> list:
        with self.lock:
            runs = sorted((r for r in self.keyword_runs.values() if r["tenant_id"] == tid),
                          key=lambda r: (r["requested_at"], r["run_id"]))
            return [self.keyword_run_view(tid, r["run_id"]) for r in runs][:1000]

    def keyword_scorecard(self) -> dict:
        """Priya's main card: her runs and their outcomes (recorded runs only; a rate over zero runs is null)."""
        runs, outcomes, statuses = 0, {}, {}
        for r in self.keyword_runs.values():
            if r["status"] == "completed":
                runs += 1
                outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
                statuses[r["ledger_status"]] = statuses.get(r["ledger_status"], 0) + 1
            elif r["status"] == "interrupted":
                runs += 1
                outcomes["INTERRUPTED"] = outcomes.get("INTERRUPTED", 0) + 1
        return {"runs": runs, "outcomes": dict(sorted(outcomes.items())),
                "ledger_statuses": dict(sorted(statuses.items())),
                "not_connected_rate": None if runs == 0 else round(outcomes.get("NOT_CONNECTED", 0) / runs, 4),
                "failure_rate": None if runs == 0 else round((outcomes.get("FAILED", 0)
                                                               + outcomes.get("INTERRUPTED", 0)) / runs, 4)}


_KILL_CODES = frozenset({"KILLED_GLOBAL", "KILLED_TENANT", "KILLED_CAPABILITY", "KILLED_PROVIDER", "KILLED_WRITE",
                         "AGENT_RESTRICTED", "SERVICE_CLOSED"})


def _count_view(c: dict) -> dict:
    """Counts as recorded: numbers only (the PII bucket's sums included; no text)."""
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in c.items()}
