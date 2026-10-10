"""
First-party server-log ingests (ADR 0017 Wave 2, decisions W2-1 / W2-2): a client uploads its access log in
line-aligned chunks; each chunk is parsed in memory (agents/logs.py) and only its aggregate delta is committed
(record-first, one log line per chunk, idempotent by request id and chunk sequence; the idempotency record keeps the
request's SHA-256 only).

What is retained (AEGIS 1472041 H1): counts, status mixes, PATH TEMPLATES (identifiers replaced by {email}, {uuid},
{id}, {token}), robots-disallowed hit counts, the indices of the public sitemap sample that were crawled, and a
HyperLogLog sketch per family for distinct client IPs. Never retained anywhere — log, ledger, report, error: raw
lines, raw paths, IPs, IP hashes, User-Agent strings. robots.txt (public) and the sitemap sample (public paths from
the tenant's latest audit) are fixed when the ingest is created, so exact paths are compared in memory and dropped.
Retention: nothing identifying is kept, so expiry (SEO_LOG_RETENTION_DAYS) hides the per-ingest report and keeps
totals; the append-only log holds no personal data that would need shredding.

Bounds (AEGIS 1472041 H2): at most SEO_LOG_MAX_OPEN_INGESTS open ingests per tenant, SEO_LOG_MAX_INGESTS ingests per
tenant within a retention period, SEO_LOG_TENANT_BYTES bytes per tenant within a retention period, SEO_LOG_MAX_BYTES
per ingest; per-ingest state is a fixed-size aggregate (templates capped per family, 256 sketch registers), and an
ingest's robots.txt text is dropped from memory when it finishes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import timedelta
from typing import Optional

from agents import bots
from agents import logs as logs_mod
from clock import iso, parse_iso
from envelope import envelope, finding, sha
from errors import Conflict, Forbidden, Invalid, NotFound
from ledger import derived_id
from primitives import Killed
from primitives import robots as robots_mod
from reasons import R

CHUNK_MAX = 90_000
SEARCH_TOKENS = ("Googlebot", "Bingbot")
METHODOLOGY = ("The uploaded access log was parsed line by line ({fmt}); each request was attributed to a bot family "
               "by its User-Agent token (bot list {v}) and the client IP checked with the bot-verification port "
               "(reverse DNS plus forward-confirm against the operator's documented host names) where one is "
               "connected. 'verified' = the port confirmed the IP; 'spoofed' = the port showed the claim false; "
               "'claimed' = User-Agent only. Paths are reported only as templates ({rules}). robots.txt and the "
               "sitemap sample (this tenant's latest audit of the domain) were fixed when the ingest was created; "
               "distinct client IPs are a HyperLogLog estimate.")
LIMITS = ["Only the uploaded file was read: it covers the period and the servers it covers, nothing else.",
          "A User-Agent is a claim; families without DNS verification (and every family while the port is "
          "NOT_CONNECTED) stay 'claimed'. IP-range verification is not connected.",
          "Path templates replace identifier-looking segments; a short or ordinary-looking identifier (a plain "
          "username) cannot be recognised and may remain in a template.",
          "Distinct client IPs are estimated (HyperLogLog, about 6.5% standard error).",
          "robots.txt is the one fetched when the ingest was created; requests in the log may predate it."]


def _refusal(k: Killed) -> Forbidden:
    return Forbidden(R(k.code if k.code == "AGENT_RESTRICTED" or k.code.startswith("KILLED_") else "SERVICE_CLOSED"))


class LogsMixin:
    # ------------------------------------------------------------------ bounds

    def _check_log_caps(self, tid: str, more_bytes: int = 0, new_ingest: bool = False) -> None:
        since = self.now() - timedelta(days=self.settings.log_retention_days)
        mine = [g for g in self.log_ingests.values() if g["tenant_id"] == tid]
        recent = [g for g in mine if parse_iso(g["created_at"]) > since]
        s = self.settings
        if new_ingest and sum(1 for g in mine if g["status"] == "open") >= s.log_max_open_ingests:
            raise Conflict(R("LOG_INGESTS_OPEN_LIMIT"))
        if new_ingest and len(recent) >= s.log_max_ingests:
            raise Conflict(R("LOG_INGESTS_LIMIT"))
        if sum(g["bytes"] for g in recent) + more_bytes > s.log_tenant_bytes:
            raise Conflict(R("LOG_TENANT_QUOTA"))

    def _selene_ok(self) -> None:
        if self.agent_blocked("selene"):
            raise Forbidden(R("AGENT_RESTRICTED"))

    # ------------------------------------------------------------------ create

    def create_log_ingest(self, actor: str, tid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="logs", write=True)
            self._selene_ok()
            rk = self.rk("log_ingest", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.log_ingest_view(tid, prev[1])
            if body["domain"] not in t["domains"]:
                raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
            self._check_log_caps(tid, new_ingest=True)
            sample = self._sitemap_sample(tid, body["domain"])
        guard = self.guard(tid, run="logs")
        robots_text, robots_status = None, "not_fetched"
        try:
            guard(capability="logs", agent="selene")
            if self.ports.fetcher is not None:
                rb = self.ports.fetcher.robots(f"{body['scheme']}://{body['domain']}", guard=guard)
                if rb["fetch"]["state"] == "KILLED":
                    raise Killed(rb["fetch"]["detail"] or "KILLED_GLOBAL")
                robots_status = rb["status_class"]
                robots_text = rb.get("text")
        except Killed as k:
            raise _refusal(k) from None
        with self.lock:
            self._gate()
            self.check_kill(tenant=tid, capability="logs", write=True)
            self._selene_ok()
            prev = self._idem(actor, rk, body)
            if prev:
                return self.log_ingest_view(tid, prev[1])
            self._check_log_caps(tid, new_ingest=True)
            iid = derived_id("lgi", tid, actor, rk)
            data = {"ingest_id": iid, "tenant_id": tid, "domain": body["domain"], "scheme": body["scheme"],
                    "format": body["format"], "key_fp": hashlib.sha256(self.settings.log_hash_key).hexdigest()[:16],
                    "robots_status": robots_status, "robots_text": robots_text, "sitemap_sample": sample}
            self._commit("log_ingest_created", self._req(data, actor, rk, body, iid), actor,
                         evidence=("log_ingest_created", f"ingest:{iid}",
                                   {"ingest_id": iid, "tenant_id": tid, "format": body["format"],
                                    "domain_sha256": sha(body["domain"]), "robots_status": robots_status,
                                    "robots_sha256": None if robots_text is None else sha(robots_text),
                                    "sitemap_sample_sha256": None if sample is None else sha(sample)}, (actor, rk)))
            return self.log_ingest_view(tid, iid)

    def _a_log_ingest_created(self, d, at):
        self.log_ingests[d["ingest_id"]] = {
            "ingest_id": d["ingest_id"], "tenant_id": d["tenant_id"], "domain": d["domain"], "scheme": d["scheme"],
            "format": d["format"], "key_fp": d["key_fp"], "status": "open", "created_at": at, "next_seq": 1,
            "bytes": 0, "last_seen": False, "totals": logs_mod.empty_delta(), "report": None, "report_sha256": None,
            "robots_status": d.get("robots_status"), "robots_text": d.get("robots_text"),
            "sitemap_sample": d.get("sitemap_sample")}

    def _ingest(self, tid: str, iid: str) -> dict:
        g = self.log_ingests.get(iid)
        if g is None or g["tenant_id"] != tid:
            raise NotFound(R("INGEST_NOT_FOUND"))
        return g

    # ------------------------------------------------------------------ chunks

    def add_log_chunk(self, actor: str, tid: str, iid: str, body: dict) -> dict:
        try:
            data = base64.b64decode(body["data_b64"], validate=True)
        except (binascii.Error, ValueError):
            raise Invalid(R("CHUNK_INVALID")) from None
        nbytes = len(data)
        if not data or nbytes > CHUNK_MAX:
            raise Invalid(R("CHUNK_INVALID"))
        if not body["last"] and not data.endswith(b"\n"):
            raise Invalid(R("CHUNK_NOT_LINE_ALIGNED"))
        with self.lock:
            self._gate()
            g = self._ingest(tid, iid)
            self.check_kill(tenant=tid, capability="logs", write=True)
            self._selene_ok()
            rk = self.rk("log_chunk", iid, body)
            if self._idem(actor, rk, body):
                return self.log_ingest_view(tid, iid)
            self._chunk_ok(g, body, nbytes)
            fmt = g["format"]
            cache = self._log_verify_cache.setdefault(iid, {})
            budget = self._log_budget.setdefault(iid, {"left": self.settings.bot_verify_max})
            robots = self._robots_parsed(g)
            sample = {p: i for i, p in enumerate(g["sitemap_sample"] or [])}
        try:
            self.guard(tid, run="logs")(capability="logs", agent="selene")
        except Killed as k:
            raise _refusal(k) from None
        delta = logs_mod.process_chunk(fmt, data, self.settings.log_hash_key, self.ports.bot_verifier.verify, cache,
                                       budget, robots, sample)
        del data
        with self.lock:
            self._gate()
            g = self._ingest(tid, iid)
            self.check_kill(tenant=tid, capability="logs", write=True)
            self._selene_ok()
            self._chunk_ok(g, body, nbytes)                # nothing moved while the lock was released
            payload = {"ingest_id": iid, "seq": body["seq"], "lines": delta["lines"], "delta_sha256": sha(delta)}
            self._commit("log_chunk", self._req({"ingest_id": iid, "seq": body["seq"], "last": body["last"],
                                                 "bytes": nbytes, "delta": delta}, actor, rk, body, iid), actor,
                         evidence=("log_chunk_ingested", f"ingest:{iid}", payload, (actor, rk)))
            return self.log_ingest_view(tid, iid)

    def _robots_parsed(self, g: dict):
        if g.get("robots_text") is None:
            return None
        rb = self._log_robots.get(g["ingest_id"])
        if rb is None:
            rb = self._log_robots[g["ingest_id"]] = robots_mod.parse(g["robots_text"])
        return rb

    def _chunk_ok(self, g: dict, body: dict, nbytes: int) -> None:
        if g["status"] != "open" or g["last_seen"]:
            raise Conflict(R("INGEST_CLOSED"))
        if body["seq"] != g["next_seq"]:
            raise Conflict(R("CHUNK_SEQ"))
        if g["bytes"] + nbytes > self.settings.log_max_bytes:
            raise Conflict(R("LOG_TOO_LARGE"))
        self._check_log_caps(g["tenant_id"], more_bytes=nbytes)

    def _a_log_chunk(self, d, at):
        g = self.log_ingests[d["ingest_id"]]
        logs_mod.merge(g["totals"], d["delta"])
        g["next_seq"] = d["seq"] + 1
        g["bytes"] += d["bytes"]
        g["last_seen"] = bool(d["last"])

    # ------------------------------------------------------------------ finish

    def finish_log_ingest(self, actor: str, tid: str, iid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            g = self._ingest(tid, iid)
            self.check_kill(tenant=tid, capability="logs", write=True)
            self._selene_ok()
            rk = self.rk("log_finish", iid, body)
            if self._idem(actor, rk, body):
                return self.log_ingest_view(tid, iid)
            if g["status"] != "open":
                raise Conflict(R("INGEST_CLOSED"))
            report = self._log_report(iid, g)
            rsha = sha(report)
            self._commit("log_ingest_finished", self._req({"ingest_id": iid, "report": report, "report_sha256": rsha},
                                                          actor, rk, body, iid), actor,
                         evidence=("log_report_recorded", f"ingest:{iid}",
                                   {"ingest_id": iid, "report_sha256": rsha, "outcome": report["outcome"]},
                                   (actor, rk)))
            self._log_verify_cache.pop(iid, None)
            self._log_budget.pop(iid, None)
            return self.log_ingest_view(tid, iid)

    def _a_log_ingest_finished(self, d, at):
        g = self.log_ingests[d["ingest_id"]]
        g.update(status="finished", finished_at=at, report=d["report"], report_sha256=d["report_sha256"],
                 robots_text=None)                         # not needed any more: memory stays bounded
        self._log_robots.pop(d["ingest_id"], None)

    def _sitemap_sample(self, tid: str, domain: str) -> Optional[list]:
        latest = None
        for a in self.audits.values():
            if a["tenant_id"] == tid and a["domain"] == domain and a["status"] == "completed":
                latest = a                                 # log order: the last one completed wins
        if latest is None:
            return None
        for e in latest["report"]["agents"]:
            if e["agent"] == "delia":
                return list(e["facts"].get("sitemap_paths_sample") or [])
        return None

    def _log_report(self, iid: str, g: dict) -> dict:
        totals, sample, fmt = g["totals"], g["sitemap_sample"], g["format"]
        robots_ok = g.get("robots_text") is not None
        findings, fams = [], {}
        for tok, f in sorted(totals["families"].items()):
            fam = bots.by_token(tok)
            top = sorted(f["templates"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            blocked = f["robots_blocked"] if robots_ok else None
            fams[tok] = {"operator": fam["operator"], "purpose": fam["purpose"], "requests": f["requests"],
                         "verified": f["verified"], "spoofed": f["spoofed"], "claimed": f["claimed"],
                         "status": dict(f["status"]), "distinct_client_ips_estimate": logs_mod.hll_estimate(f["hll"]),
                         "path_templates_seen": len(f["templates"]), "templates_over_cap": f["templates_dropped"],
                         "top_path_templates": [{"template": p, "requests": n} for p, n in top],
                         "robots_disallowed_hits": blocked}
            if f["spoofed"]:
                findings.append(finding("SPOOFED_CRAWLER_REQUESTS", "medium", "measured", "TEST", capability="P2",
                                        detail={"token": tok, "requests": f["spoofed"],
                                                "note": "the User-Agent claimed this crawler; DNS showed otherwise"}))
            if blocked:
                findings.append(finding("ROBOTS_DISALLOWED_PATHS_REQUESTED", "low", "measured", "WATCH",
                                        capability="P2", detail={"token": tok, "requests": blocked,
                                                                 "verified": f["verified"], "claimed": f["claimed"],
                                                                 "note": "robots.txt disallows these paths; the "
                                                                         "requests may predate it or be spoofed"}))
            if tok in SEARCH_TOKENS and f["requests"] >= 20:
                err = f["status"].get("5xx", 0)
                if err / f["requests"] > 0.05:
                    findings.append(finding("SEARCH_CRAWLER_SERVER_ERRORS", "high", "measured", "ACT",
                                            capability="P1", detail={"token": tok, "requests": f["requests"],
                                                                     "5xx": err}))
        uncrawled = None
        if sample is not None:
            hit = {i for t in SEARCH_TOKENS for i in (totals["families"].get(t) or {}).get("sample_hits", [])}
            uncrawled = [p for i, p in enumerate(sample) if i not in hit]
            if uncrawled:
                findings.append(finding("IMPORTANT_URLS_NOT_CRAWLED", "medium", "measured", "TEST", capability="P2",
                                        detail={"count": len(uncrawled), "of_sitemap_sample": len(sample),
                                                "examples": uncrawled[:50], "crawlers": list(SEARCH_TOKENS)}))
        if totals["lines"] and not any(totals["families"].get(t) for t in SEARCH_TOKENS):
            findings.append(finding("SEARCH_CRAWLER_ABSENT", "medium", "measured", "WATCH", capability="P2",
                                    detail={"lines": totals["lines"]}))
        if totals["quarantined"]:
            findings.append(finding("LOG_LINES_QUARANTINED", "low", "measured", "WATCH", capability="P5",
                                    detail={"by_reason": dict(totals["quarantined"])}))
        nc = {"bot_ip_range_verification"}
        if not getattr(self.ports.bot_verifier, "connected", False):
            nc.add("bot_dns_verification")
        usable = totals["lines"] - sum(totals["quarantined"].values())
        outcome = "OK" if usable > 0 and robots_ok and sample is not None else \
            ("PARTIAL" if usable > 0 else "INSUFFICIENT_EVIDENCE")
        env = envelope("selene", "log_access", outcome, findings,
                       methodology=METHODOLOGY.format(fmt=fmt, v=bots.VERSION, rules=logs_mod.TEMPLATE_RULES),
                       limitations=LIMITS, not_connected=nc,
                       facts={"lines": totals["lines"], "usable_lines": usable, "non_bot": totals["non_bot"],
                              "no_user_agent": totals["no_user_agent"], "quarantined": dict(totals["quarantined"]),
                              "families": fams, "robots_status": g.get("robots_status"),
                              "uncrawled_important_paths": uncrawled[:50] if uncrawled is not None else None,
                              "sitemap_sample": "latest audit" if sample is not None else
                              "none: run an audit of this domain first", "bot_families_version": bots.VERSION})
        return {"ingest_id": iid, "outcome": outcome, "envelope": env}

    # ------------------------------------------------------------------ views

    def log_ingest_view(self, tid: str, iid: str) -> dict:
        with self.lock:
            g = self._ingest(tid, iid)
            expired = self.now() - parse_iso(g["created_at"]) > timedelta(days=self.settings.log_retention_days)
            t = g["totals"]
            out = {k: g[k] for k in ("ingest_id", "tenant_id", "domain", "scheme", "format", "key_fp", "created_at",
                                     "next_seq", "bytes", "report_sha256", "robots_status")}
            out["status"] = "expired" if expired else g["status"]
            out["totals"] = {"lines": t["lines"], "non_bot": t["non_bot"], "no_user_agent": t["no_user_agent"],
                             "quarantined": dict(t["quarantined"]),
                             "families": {k: {"requests": v["requests"], "verified": v["verified"],
                                              "spoofed": v["spoofed"], "claimed": v["claimed"]}
                                          for k, v in t["families"].items()}}
            out["report"] = None if expired else g["report"]
            out["retention_days"] = self.settings.log_retention_days
            out["as_of"] = iso(self.now())
            return out

    def log_ingests_view(self, tid: str) -> list:
        with self.lock:
            return [self.log_ingest_view(tid, i) for i, g in self.log_ingests.items() if g["tenant_id"] == tid][:1000]
