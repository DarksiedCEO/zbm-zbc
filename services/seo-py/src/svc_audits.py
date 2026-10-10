"""
The audit product and prompt sets (ADR 0017 decision 13). Cash path: a paid audit (Wave 1, on the client's own site)
-> the managed tier -> fixes through Department 28 clientfix (not in this wave). Payments are Finance (31)'s, through
Stripe: this service never charges, quotes or calls Stripe; it takes a Finance invoice id as an INPUT.

Rules:
  - an audit names one tenant and one of that tenant's registered domains (Andre registers them); any other domain
    is refused DOMAIN_NOT_AUTHORIZED, so this service never audits a site nobody here is authorised for;
  - a ``client`` tenant's audit needs a Finance (31) invoice id AND Andre's approval token; an ``own`` tenant's
    (ZBM's own properties) needs neither and refuses an invoice id;
  - one audit at a time per tenant; kill switches checked at the start and before every outbound step;
  - record-first: ``audit_requested`` (and Andre's approval) on the ledger and the log before any fetch; the report
    is committed as ``audit_completed`` with its SHA-256 on the ledger. A run whose process died is marked
    ``interrupted`` by the ``interrupted-audits`` job, never silently completed;
  - the report keeps OUTCOME apart from FINDINGS per agent, says what was NOT_CONNECTED and why, and carries every
    agent's methodology and limitations. It never states a business effect (no first-party analytics / CRM is
    connected) and never claims superiority over anyone.
"""

from __future__ import annotations

import re
from typing import Optional

from agents import RunContext, bots, delia, entity_check, osei, probes, roman, selene
from clock import iso
from envelope import CAPABILITIES, DATA_CLASSES, DECISIONS, EFFECT_CLASSES, envelope, sha, worst_outcome
from errors import Conflict, Forbidden, Invalid, NotFound
from ledger import derived_id
from primitives import diff as diff_mod
from primitives.parse import SCHEMA_RULES_VERSION
from reasons import R

REPORT_VERSION = "seo-audit-report/1"
_PATH_OK = re.compile(r"/[\x21-\x7e]{0,500}")
GLOBAL_LIMITS = [
    "Wave 1 reads public pages only. First-party truth (Search Console, Bing Webmaster Tools, server logs, analytics, "
    "CRM) is NOT_CONNECTED, so nothing here is paired with qualified traffic, conversions or revenue, and no finding "
    "claims a business effect (credit is not causation).",
    "Observations are from one network location at one time (search-truth drift: a later run may differ because the "
    "site changed, because a measurement failed, or because an engine changed).",
    "No single visibility score is produced; per-engine readiness is an uncalibrated heuristic checklist.",
]
LEGEND = {"data_classes": list(DATA_CLASSES), "effect_classes": list(EFFECT_CLASSES), "decisions": list(DECISIONS),
          "capabilities": CAPABILITIES,
          "note": "data_class says how a finding's data was obtained; effect_class is null for every Wave-1 finding "
                  "(no effect is claimed)"}
NOT_CONNECTED_WHY = {
    "render": "no headless renderer is wired (raw HTML only)",
    "answer_engines": "no answer-engine adapter is built; no engine was asked anything",
    "prompt_volume": "no prompt-volume source is chosen or built",
    "first_party": "Search Console, Bing Webmaster, logs, analytics and CRM are not connected",
    "zero_day / orca_publish": "ports only in Wave 1 (founder-pending flag 5)",
    "clientfix": "fix execution is Department 28's, not in Wave 1",
}


class AuditsMixin:
    # ------------------------------------------------------------------ prompt sets (versioned)

    def create_prompt_set(self, actor: str, tid: str, body: dict) -> dict:
        terms = [" ".join(t.split()) for t in body["brand_terms"]]
        prompts = [" ".join(p.split()) for p in body["prompts"]]
        if any(not t for t in terms) or any(not p for p in prompts) or len(set(prompts)) != len(prompts) \
                or any(any(ord(c) < 0x20 for c in s) for s in terms + prompts):
            raise Invalid(R("PROMPT_SET_INVALID"))
        content = {"name": body["name"], "brand_terms": terms, "competitor_domains": sorted(body["competitor_domains"]),
                   "prompts": prompts, "engines": sorted(set(body["engines"]))}
        with self.lock:
            self._gate()
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="prompt_sets", write=True)
            rk = self.rk("prompt_set", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.prompt_set_view(tid, prev[1])
            psid = derived_id("pst", tid, body["name"])
            cur = self.prompt_sets.get(psid)
            csha = sha(content)
            if cur is not None and cur["versions"][str(cur["current"])]["sha256"] == csha:
                raise Conflict(R("PROMPT_SET_EXISTS"))
            version = 1 if cur is None else cur["current"] + 1
            data = {"prompt_set_id": psid, "tenant_id": tid, "version": version, "content": content, "sha256": csha}
            self._commit("prompt_set_version", self._req(data, actor, rk, body, psid), actor,
                         evidence=("prompt_set_version", f"prompt_set:{psid}",
                                   {"prompt_set_id": psid, "tenant_id": tid, "version": version, "sha256": csha},
                                   (actor, rk)))
            return self.prompt_set_view(tid, psid)

    def _a_prompt_set_version(self, d, at):
        ps = self.prompt_sets.setdefault(d["prompt_set_id"], {"prompt_set_id": d["prompt_set_id"],
                                                              "tenant_id": d["tenant_id"], "versions": {}})
        ps["versions"][str(d["version"])] = {**d["content"], "version": d["version"], "sha256": d["sha256"],
                                             "created_at": at}
        ps["current"] = d["version"]

    def prompt_set_view(self, tid: str, psid: str) -> dict:
        with self.lock:
            ps = self.prompt_sets.get(psid)
            if ps is None or ps["tenant_id"] != tid:
                raise NotFound(R("PROMPT_SET_NOT_FOUND"))
            return {"prompt_set_id": psid, "tenant_id": tid, "current": ps["current"],
                    "versions": {k: {**v, "prompts": list(v["prompts"])} for k, v in ps["versions"].items()}}

    # ------------------------------------------------------------------ audits

    def request_audit(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        paths = body["paths"]
        if len(paths) > self.settings.audit_max_pages:
            raise Invalid(R("AUDIT_TOO_LARGE"))
        if not paths or len(set(paths)) != len(paths) or any(
                not _PATH_OK.fullmatch(p) or p.startswith("//") or "#" in p or "\\" in p for p in paths):
            raise Invalid(R("PAGES_INVALID"))
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="audit", write=True)
            actor = "andre" if andre else actor
            rk = self.rk("audit", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.audit_view(tid, prev[1])
            if body["domain"] not in t["domains"]:
                raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
            if t["kind"] == "client":
                if not body.get("invoice_id"):
                    raise Conflict(R("INVOICE_REQUIRED"))
                if not andre:
                    raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
            elif body.get("invoice_id"):
                raise Invalid(R("INVOICE_NOT_FOR_OWN_TENANT"))
            ps = None
            if body.get("prompt_set_id"):
                p = self.prompt_sets.get(body["prompt_set_id"])
                if p is None or p["tenant_id"] != tid:
                    raise NotFound(R("PROMPT_SET_NOT_FOUND"))
                ps = {"prompt_set_id": p["prompt_set_id"], **p["versions"][str(p["current"])]}
            if any(a["tenant_id"] == tid and a["audit_id"] in self.running_audits for a in self.audits.values()):
                raise Conflict(R("AUDIT_RUNNING"))
            aid = derived_id("aud", tid, actor, rk)
            data = {"audit_id": aid, "tenant_id": tid, "tenant_kind": t["kind"], "domain": body["domain"],
                    "scheme": body["scheme"], "paths": list(paths), "invoice_id": body.get("invoice_id"),
                    "prompt_set": None if ps is None else {"prompt_set_id": ps["prompt_set_id"],
                                                           "version": ps["version"], "sha256": ps["sha256"]},
                    "andre_approved": bool(andre)}
            ev = [("audit_requested", f"audit:{aid}",
                   {"audit_id": aid, "tenant_id": tid, "kind": t["kind"], "domain_sha256": sha(body["domain"]),
                    "paths_sha256": sha(paths), "invoice_id": body.get("invoice_id")}, (actor, rk))]
            if andre:
                ev.append(("audit_approved_by_andre", f"audit:{aid}",
                           {"audit_id": aid, "invoice_id": body.get("invoice_id")}, ("andre", rk)))
            self._commit("audit_requested", self._req(data, actor, rk, body, aid), actor, evidence=ev)
            self.running_audits.add(aid)
        try:
            report = self._run_audit(aid, tid, body, ps)
            with self.lock:
                self._gate()
                stop = self.kill_code(tenant=tid, capability="audit", write=True)
                if stop is not None:
                    # AEGIS M3: a switch engaged during the run refuses the completion record. The run is closed as
                    # interrupted (a terminal bookkeeping record, not new work) and the report is discarded.
                    self._commit("audit_interrupted", {"audit_id": aid, "reason": stop}, "seo",
                                 evidence=("audit_interrupted", f"audit:{aid}", {"audit_id": aid, "reason": stop},
                                           ("interrupted", aid)))
                    return self.audit_view(tid, aid)
                rsha = sha(report)
                self._commit("audit_completed", {"audit_id": aid, "report": report, "report_sha256": rsha},
                             "seo", evidence=("audit_report_recorded", f"audit:{aid}",
                                              {"audit_id": aid, "report_sha256": rsha, "outcome": report["outcome"],
                                               "findings": report["summary"]["findings"]}, ("report", aid)))
                return self.audit_view(tid, aid)
        finally:
            with self.lock:
                self.running_audits.discard(aid)

    def _a_audit_requested(self, d, at):
        self.audits[d["audit_id"]] = {**{k: v for k, v in d.items() if k not in ("actor", "request_id",
                                                                                  "request_sha", "_obj", "evidence")},
                                      "status": "running", "requested_at": at, "requested_by": d["actor"],
                                      "completed_at": None, "report": None, "report_sha256": None}

    def _a_audit_completed(self, d, at):
        self.audits[d["audit_id"]].update(status="completed", completed_at=at, report=d["report"],
                                          report_sha256=d["report_sha256"])

    def _a_audit_interrupted(self, d, at):
        self.audits[d["audit_id"]].update(status="interrupted", completed_at=at,
                                          interrupted_reason=d.get("reason", "PROCESS_LOST"))

    def _run_audit(self, aid: str, tid: str, body: dict, ps: Optional[dict]) -> dict:
        """The run, outside the service lock: Selene, Delia, Roman, the entity check, Callum, Naomi."""
        o = osei.Osei(self.clock)
        started = iso(self.now())
        ctx = RunContext(fetcher=self.ports.fetcher, renderer=self.ports.renderer, guard=self.guard(tid),
                         clock=self.clock, osei=o, domain=body["domain"], scheme=body["scheme"],
                         paths=list(body["paths"]))
        envs, pages = [], {}
        if self.ports.fetcher is None:
            envs.append(envelope("selene", "crawlability", "NOT_CONNECTED", [], methodology="no fetcher is wired",
                                 not_connected={"fetch"}, reason="the web fetcher port is not connected"))
        else:
            s_env, pages = selene.run(ctx)
            envs.append(s_env)
            envs.append(delia.run(ctx) if ctx.robots is not None else _skipped("delia", "sitemaps_and_llms_txt",
                                                                              s_env))
            envs.append(roman.run(ctx, pages))
        envs.append(entity_check.run(ctx, pages, self.entity_for_tenant(tid)))
        envs.append(probes.run_callum(self.ports.engines, ps, body["domain"], self.settings.probe_samples,
                                      ctx.guard, o))
        envs.append(probes.run_naomi(self.ports.prompt_volume, ps))
        return self._report(aid, tid, body, ps, envs, o, started, pages)

    def _report(self, aid, tid, body, ps, envs, o, started, pages) -> dict:
        all_findings = [f for e in envs for f in e["findings"]]
        by: dict = {"severity": {}, "decision": {}, "data_class": {}, "capability": {}}
        for f in all_findings:
            for k in by:
                by[k][f[k]] = by[k].get(f[k], 0) + 1
        nc = sorted(set(self.ports.not_connected()) | {x for e in envs for x in e["not_connected"]})
        return {
            "report_version": REPORT_VERSION, "audit_id": aid, "tenant_id": tid, "domain": body["domain"],
            "scheme": body["scheme"], "paths": list(body["paths"]), "started_at": started,
            "finished_at": iso(self.now()), "outcome": worst_outcome(e["outcome"] for e in envs),
            "agents": envs,
            "summary": {"findings": len(all_findings), **by, "agent_outcomes": {e["agent"]: e["outcome"] for e in envs}},
            "pages": {p: {"fetch": v["fetch"].summary(), "render_state": v["render"]["state"],
                          "access_diff": (v.get("access_diff") or {}).get("state"),
                          "fingerprint": diff_mod.fingerprint(v["extract"]) if v.get("extract") else None}
                      for p, v in pages.items()},
            "versions": {"bot_families": bots.VERSION, "schema_rules": SCHEMA_RULES_VERSION,
                         "report": REPORT_VERSION},
            "not_connected": nc, "not_connected_why": dict(NOT_CONNECTED_WHY),
            "limitations": GLOBAL_LIMITS + sorted({lim for e in envs for lim in e["limitations"]}),
            "legend": LEGEND, "data_hygiene": o.summary(),
            "prompt_set": None if ps is None else {"prompt_set_id": ps["prompt_set_id"], "version": ps["version"],
                                                   "sha256": ps["sha256"]},
            "untrusted_content_rule": "every string quoted from a crawled page or an answer engine is data marked "
                                      "untrusted; none of it was acted on",
        }

    def audit_view(self, tid: str, aid: str, full: bool = True) -> dict:
        with self.lock:
            a = self.audits.get(aid)
            if a is None or a["tenant_id"] != tid:
                raise NotFound(R("AUDIT_NOT_FOUND"))
            out = {k: v for k, v in a.items() if k != "report"}
            if a["status"] == "running" and aid not in self.running_audits:
                out["status"] = "running_elsewhere_or_interrupted"
            if full:
                out["report"] = a["report"]
            return out

    def drift_view(self, tid: str, aid: str, against: str) -> dict:
        """Search-truth drift between two completed audits of the same target (agents/drift.py), oldest first."""
        from agents import drift
        with self.lock:
            x, y = self.audit_view(tid, aid), self.audit_view(tid, against)
            order = {k: i for i, k in enumerate(self.audits)}          # log order: the order they were requested
        if x["status"] != "completed" or y["status"] != "completed":
            raise Conflict(R("AUDIT_NOT_COMPLETED"))
        a, b = sorted((x, y), key=lambda r: order[r["audit_id"]])
        try:
            return drift.compare(a["report"], b["report"])
        except drift.NotComparable:
            raise Conflict(R("DRIFT_NOT_COMPARABLE")) from None

    def audits_view(self, tid: str) -> list:
        with self.lock:
            return [self.audit_view(tid, a["audit_id"], full=False)
                    for a in sorted(self.audits.values(), key=lambda a: (a["requested_at"], a["audit_id"]))
                    if a["tenant_id"] == tid][:1000]

    def mark_interrupted_audits(self) -> dict:
        """Audits left ``running`` by a process that is gone are recorded ``interrupted`` (never completed)."""
        n = 0
        with self.lock:
            stale = [a["audit_id"] for a in self.audits.values()
                     if a["status"] == "running" and a["audit_id"] not in self.running_audits]
            for aid in stale:
                self._gate()
                self._commit("audit_interrupted", {"audit_id": aid}, "scheduler",
                             evidence=("audit_interrupted", f"audit:{aid}", {"audit_id": aid}, ("interrupted", aid)))
                n += 1
        return {"interrupted": n}


def _skipped(agent: str, task: str, selene_env: dict) -> dict:
    outcome = "KILLED" if selene_env["outcome"] == "KILLED" else "INSUFFICIENT_EVIDENCE"
    return envelope(agent, task, outcome, [], methodology="not run: robots.txt was not read",
                    reason=selene_env.get("reason") or "robots.txt not read")
