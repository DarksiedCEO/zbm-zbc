"""
First-party server-log ingests (ADR 0017 Wave 2, decision W2-1): a client uploads its access log in line-aligned
chunks; each chunk is parsed in memory (agents/logs.py) and only its aggregate delta is committed (record-first, one
log line per chunk, idempotent by request id and chunk sequence). Raw lines, raw IPs and User-Agent strings are never
stored. ``finish`` turns the totals into Selene's ``log_access`` envelope: per-bot-family crawl volume, status mix,
verified / spoofed / claimed split, robots-disallowed hits (robots.txt fetched live through the guarded fetcher),
and important URLs (the sitemap sample of the tenant's latest audit of that domain) no search crawler requested.

Retention (ADR 0017): client IPs exist only as keyed hashes (SEO_LOG_HASH_KEY_FILE; rotating the key makes earlier
hashes unlinkable), and an ingest older than SEO_LOG_RETENTION_DAYS is served as totals only ("expired").
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
from reasons import R

CHUNK_MAX = 90_000
SEARCH_TOKENS = ("Googlebot", "Bingbot")
METHODOLOGY = ("The uploaded access log was parsed line by line ({fmt}); each request was attributed to a bot family "
               "by its User-Agent token (bot list {v}) and the client IP checked with the bot-verification port "
               "(reverse DNS plus forward-confirm against the operator's documented host names) where one is "
               "connected. 'verified' = the port confirmed the IP; 'spoofed' = the port showed the claim false; "
               "'claimed' = User-Agent only. robots.txt was fetched live and each crawled path evaluated for the "
               "family's token. 'Important' URLs are the sitemap sample from this tenant's latest audit of the domain.")
LIMITS = ["Only the uploaded file was read: it covers the period and the servers it covers, nothing else.",
          "A User-Agent is a claim; families without DNS verification (and every family while the port is "
          "NOT_CONNECTED) stay 'claimed'. IP-range verification is not connected.",
          "Paths are kept without query strings, at most 2000 per family; hashed IPs at most 5000 per family.",
          "robots.txt is today's; requests in the log may predate a robots.txt change."]


class LogsMixin:
    def create_log_ingest(self, actor: str, tid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="logs", write=True)
            rk = self.rk("log_ingest", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.log_ingest_view(tid, prev[1])
            if body["domain"] not in t["domains"]:
                raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
            iid = derived_id("lgi", tid, actor, rk)
            data = {"ingest_id": iid, "tenant_id": tid, "domain": body["domain"], "scheme": body["scheme"],
                    "format": body["format"], "key_fp": hashlib.sha256(self.settings.log_hash_key).hexdigest()[:16]}
            self._commit("log_ingest_created", self._req(data, actor, rk, body, iid), actor,
                         evidence=("log_ingest_created", f"ingest:{iid}",
                                   {"ingest_id": iid, "tenant_id": tid, "format": body["format"],
                                    "domain_sha256": sha(body["domain"])}, (actor, rk)))
            return self.log_ingest_view(tid, iid)

    def _a_log_ingest_created(self, d, at):
        self.log_ingests[d["ingest_id"]] = {
            "ingest_id": d["ingest_id"], "tenant_id": d["tenant_id"], "domain": d["domain"], "scheme": d["scheme"],
            "format": d["format"], "key_fp": d["key_fp"], "status": "open", "created_at": at, "next_seq": 1,
            "bytes": 0, "last_seen": False, "totals": logs_mod.empty_delta(), "report": None, "report_sha256": None}

    def _ingest(self, tid: str, iid: str) -> dict:
        g = self.log_ingests.get(iid)
        if g is None or g["tenant_id"] != tid:
            raise NotFound(R("INGEST_NOT_FOUND"))
        return g

    def add_log_chunk(self, actor: str, tid: str, iid: str, body: dict) -> dict:
        try:
            data = base64.b64decode(body["data_b64"], validate=True)
        except (binascii.Error, ValueError):
            raise Invalid(R("CHUNK_INVALID")) from None
        if not data or len(data) > CHUNK_MAX:
            raise Invalid(R("CHUNK_INVALID"))
        if not body["last"] and not data.endswith(b"\n"):
            raise Invalid(R("CHUNK_NOT_LINE_ALIGNED"))
        with self.lock:
            self._gate()
            g = self._ingest(tid, iid)
            self.check_kill(tenant=tid, capability="logs", write=True)
            rk = self.rk("log_chunk", iid, body)
            if self._idem(actor, rk, body):
                return self.log_ingest_view(tid, iid)
            self._chunk_ok(g, body, data)
            fmt = g["format"]
            cache = self._log_verify_cache.setdefault(iid, {})
            budget = self._log_budget.setdefault(iid, {"left": self.settings.bot_verify_max})
        guard = self.guard(tid, run="logs")
        try:
            guard(capability="logs")
        except Killed as k:
            raise Forbidden(R(k.code if k.code.startswith("KILLED_") else "SERVICE_CLOSED")) from None
        verifier = self.ports.bot_verifier
        delta = logs_mod.process_chunk(fmt, data, self.settings.log_hash_key, verifier.verify, cache, budget)
        with self.lock:
            self._gate()
            g = self._ingest(tid, iid)
            self.check_kill(tenant=tid, capability="logs", write=True)
            self._chunk_ok(g, body, data)                 # nothing moved while the lock was released
            payload = {"ingest_id": iid, "seq": body["seq"], "bytes": len(data), "lines": delta["lines"],
                       "delta_sha256": sha(delta)}
            self._commit("log_chunk", self._req({"ingest_id": iid, "seq": body["seq"], "last": body["last"],
                                                 "bytes": len(data), "delta": delta}, actor, rk, body, iid), actor,
                         evidence=("log_chunk_ingested", f"ingest:{iid}", payload, (actor, rk)))
            return self.log_ingest_view(tid, iid)

    def _chunk_ok(self, g: dict, body: dict, data: bytes) -> None:
        if g["status"] != "open" or g["last_seen"]:
            raise Conflict(R("INGEST_CLOSED"))
        if body["seq"] != g["next_seq"]:
            raise Conflict(R("CHUNK_SEQ"))
        if g["bytes"] + len(data) > self.settings.log_max_bytes:
            raise Conflict(R("LOG_TOO_LARGE"))

    def _a_log_chunk(self, d, at):
        g = self.log_ingests[d["ingest_id"]]
        logs_mod.merge(g["totals"], d["delta"])
        g["next_seq"] = d["seq"] + 1
        g["bytes"] += d["bytes"]
        g["last_seen"] = bool(d["last"])

    def finish_log_ingest(self, actor: str, tid: str, iid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            g = self._ingest(tid, iid)
            self.check_kill(tenant=tid, capability="logs", write=True)
            rk = self.rk("log_finish", iid, body)
            if self._idem(actor, rk, body):
                return self.log_ingest_view(tid, iid)
            if g["status"] != "open":
                raise Conflict(R("INGEST_CLOSED"))
            totals = _copy(g["totals"])
            domain, scheme, fmt = g["domain"], g["scheme"], g["format"]
            sample = self._sitemap_sample(tid, domain)
        guard = self.guard(tid, run="logs")
        robots = None
        try:
            guard(capability="logs")
            if self.ports.fetcher is not None:
                robots = self.ports.fetcher.robots(f"{scheme}://{domain}", guard=guard)
                if robots["fetch"]["state"] == "KILLED":
                    raise Killed(robots["fetch"]["detail"] or "KILLED_GLOBAL")
        except Killed as k:
            raise Forbidden(R(k.code if k.code.startswith("KILLED_") else "SERVICE_CLOSED")) from None
        report = self._log_report(iid, fmt, totals, robots, sample)
        with self.lock:
            self._gate()
            g = self._ingest(tid, iid)
            self.check_kill(tenant=tid, capability="logs", write=True)
            if g["status"] != "open":
                raise Conflict(R("INGEST_CLOSED"))
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
        g.update(status="finished", finished_at=at, report=d["report"], report_sha256=d["report_sha256"])

    def _sitemap_sample(self, tid: str, domain: str) -> Optional[list]:
        latest = None
        for a in self.audits.values():
            if a["tenant_id"] == tid and a["domain"] == domain and a["status"] == "completed":
                if latest is None or (a["completed_at"], a["audit_id"]) > (latest["completed_at"], latest["audit_id"]):
                    latest = a
        if latest is None:
            return None
        for e in latest["report"]["agents"]:
            if e["agent"] == "delia":
                return list(e["facts"].get("sitemap_paths_sample") or [])
        return None

    def _log_report(self, iid: str, fmt: str, totals: dict, robots: Optional[dict], sample: Optional[list]) -> dict:
        findings, fams = [], {}
        parsed = robots.get("parsed") if robots else None
        blocked_total = {}
        for tok, f in sorted(totals["families"].items()):
            fam = bots.by_token(tok)
            blocked = 0
            if parsed is not None:
                blocked = sum(n for p, n in f["paths"].items() if not parsed.allowed(tok, p))
            elif robots and robots.get("status_class") in ("unreachable", "too_large"):
                blocked = None
            blocked_total[tok] = blocked
            top = sorted(f["paths"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            fams[tok] = {"operator": fam["operator"], "purpose": fam["purpose"], "requests": f["requests"],
                         "verified": f["verified"], "spoofed": f["spoofed"], "claimed": f["claimed"],
                         "status": dict(f["status"]), "distinct_client_ips": len(f["ip_hashes"]),
                         "paths_seen": len(f["paths"]), "top_paths": [{"path": p, "requests": n} for p, n in top],
                         "robots_disallowed_hits": blocked}
            if f["spoofed"]:
                findings.append(finding("SPOOFED_CRAWLER_REQUESTS", "medium", "measured", "TEST", capability="P2",
                                        detail={"token": tok, "requests": f["spoofed"],
                                                "note": "the User-Agent claimed this crawler; DNS showed otherwise"}))
            if blocked:
                findings.append(finding("ROBOTS_DISALLOWED_PATHS_REQUESTED", "low", "measured", "WATCH",
                                        capability="P2", detail={"token": tok, "requests": blocked,
                                                                 "verified": f["verified"], "claimed": f["claimed"],
                                                                 "note": "today's robots.txt disallows these paths; "
                                                                         "the requests may predate it or be spoofed"}))
            if tok in SEARCH_TOKENS and f["requests"] >= 20:
                err = f["status"].get("5xx", 0)
                if err / f["requests"] > 0.05:
                    findings.append(finding("SEARCH_CRAWLER_SERVER_ERRORS", "high", "measured", "ACT",
                                            capability="P1", detail={"token": tok, "requests": f["requests"],
                                                                     "5xx": err}))
        crawled = {p for t in SEARCH_TOKENS for p in (totals["families"].get(t, {}).get("paths") or {})}
        uncrawled = None
        if sample is not None:
            uncrawled = [p for p in sample if p not in crawled]
            if uncrawled:
                findings.append(finding("IMPORTANT_URLS_NOT_CRAWLED", "medium", "measured", "TEST", capability="P2",
                                        detail={"count": len(uncrawled), "of_sitemap_sample": len(sample),
                                                "examples": uncrawled[:50],
                                                "crawlers": list(SEARCH_TOKENS)}))
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
        outcome = "OK" if usable > 0 and robots is not None and robots["status_class"] != "unreachable" \
            and sample is not None else ("PARTIAL" if usable > 0 else "INSUFFICIENT_EVIDENCE")
        env = envelope("selene", "log_access", outcome, findings,
                       methodology=METHODOLOGY.format(fmt=fmt, v=bots.VERSION), limitations=LIMITS,
                       not_connected=nc,
                       facts={"lines": totals["lines"], "usable_lines": usable, "non_bot": totals["non_bot"],
                              "no_user_agent": totals["no_user_agent"], "quarantined": dict(totals["quarantined"]),
                              "families": fams, "robots_status": robots["status_class"] if robots else "not_fetched",
                              "uncrawled_important_paths": uncrawled[:50] if uncrawled is not None else None,
                              "sitemap_sample": "latest audit" if sample is not None else
                              "none: run an audit of this domain first", "bot_families_version": bots.VERSION})
        return {"ingest_id": iid, "outcome": outcome, "envelope": env}

    def log_ingest_view(self, tid: str, iid: str) -> dict:
        with self.lock:
            g = self._ingest(tid, iid)
            expired = self.now() - parse_iso(g["created_at"]) > timedelta(days=self.settings.log_retention_days)
            t = g["totals"]
            out = {k: g[k] for k in ("ingest_id", "tenant_id", "domain", "scheme", "format", "key_fp", "created_at",
                                     "next_seq", "bytes", "report_sha256")}
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
            return [self.log_ingest_view(tid, i) for i, g in sorted(self.log_ingests.items(),
                                                                    key=lambda kv: (kv[1]["created_at"], kv[0]))
                    if g["tenant_id"] == tid][:1000]


def _copy(t: dict) -> dict:
    import json
    return json.loads(json.dumps(t))
