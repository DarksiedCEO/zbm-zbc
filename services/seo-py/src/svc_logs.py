"""
First-party server-log ingests (ADR 0017 Wave 2, decisions W2-1 / W2-2): a client uploads its access log in
line-aligned chunks; each chunk is parsed in memory (agents/logs.py) and only its aggregate delta is committed
(record-first, one log line per chunk, idempotent by request id and chunk sequence; the idempotency record keeps the
request's SHA-256 only).

What is retained (AEGIS 1472041 H1): counts, status mixes, PATH TEMPLATES (identifiers replaced by {email}, {uuid},
{id}, {token}), robots-disallowed hit counts, the indices of the public sitemap sample that were crawled, and a
HyperLogLog sketch per family for distinct client IPs. Never retained anywhere — log, ledger, report, error: raw
lines, raw paths, IPs, IP hashes, User-Agent strings. robots.txt and the sitemap sample (from the tenant's latest
audit) are fixed when the ingest is created, so exact paths are compared in memory and dropped. AEGIS Wave-1 M-S3 /
M-S4: the robots.txt TEXT is held in memory only (parsed) and only its SHA-256 is recorded; the sitemap sample is
recorded as path TEMPLATES (shown in the report) and keyed digests (agents/logs.path_digest, matched against a log
path's digest in memory), never as raw paths. After a restart the robots.txt of an open ingest is no longer in
memory: its later bot requests are counted ``robots_unchecked`` and the report is PARTIAL.
Retention: nothing identifying is kept, so expiry (SEO_LOG_RETENTION_DAYS) hides the per-ingest report and keeps
totals; the append-only log holds no personal data that would need shredding.

Sink job D: the log holds machine values only, so what an ingest retains is in the tenant's vault as blobs: the
ingest's inputs (robots.txt and the sitemap sample, ``INGEST_INPUTS_FIELD``), each chunk's aggregate delta (its path
templates are dictionary keys, ``LOG_DELTA_FIELD``) and the finished report (``LOG_REPORT_FIELD``). A log line keeps
the blob ref, the keyed HMAC of the blob's canonical JSON, and counts and codes (a chunk: lines, non-bot, no
User-Agent, quarantine reasons and per-family counts; a report: the outcome and the findings count). Uploaded log
LINES are in neither: a chunk is parsed in memory and dropped. Memory keeps the counts; the full totals (templates,
sketches, sample indices) are rebuilt from the chunk blobs on demand (``_log_full``), outside the lock, and a blob
that cannot be read makes the ingest ``vault_unavailable`` (chunks and the finish refuse VAULT_UNAVAILABLE).

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
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ledger import derived_id
from primitives import Killed
from primitives import robots as robots_mod
from primitives import codes as codes_mod
from reasons import R
import sink_guard
from svc_vaulted import EVIDENCE_FIELD, INGEST_INPUTS_FIELD, LOG_DELTA_FIELD, LOG_REPORT_FIELD, normalised, owner_view

CHUNK_MAX = 90_000
SEARCH_TOKENS = ("Googlebot", "Bingbot")
SERVER_ERROR_MIN_REQUESTS = 20      # SEARCH_CRAWLER_SERVER_ERRORS: at least this many requests, and
SERVER_ERROR_SHARE = 0.05           # more than this share of them answered 5xx
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


def _compact(t: dict) -> dict:
    """What an expired ingest keeps in memory: counts only (no templates, sketches or sample indices)."""
    return {"lines": t["lines"], "non_bot": t["non_bot"], "no_user_agent": t["no_user_agent"],
            "quarantined": dict(t["quarantined"]),
            "families": {k: {x: v[x] for x in ("requests", "verified", "spoofed", "claimed")}
                         for k, v in t["families"].items()}}


class LogsMixin:
    # ------------------------------------------------------------------ expiry (AEGIS 4c0a805 T4)

    def _ingest_expired(self, g: dict) -> bool:
        return self.now() - parse_iso(g["created_at"]) > timedelta(days=self.settings.log_retention_days)

    def _evict_ingest(self, g: dict) -> None:
        """Past retention an ingest keeps only its counts in memory; its report, templates, sketches, robots.txt and
        sitemap sample are dropped (and are never rebuilt at start-up: replay compacts as it goes)."""
        if g.get("evicted"):
            return
        g.update(totals=_compact(g["totals"]), report=None, report_ref=None, robots_text=None, sitemap_sample=None,
                 evicted=True, deltas=[])
        for cache in (self._log_robots, self._log_verify_cache, self._log_budget, self._log_inputs, self._log_full):
            cache.pop(g["ingest_id"], None)

    def _sweep_ingests(self) -> int:
        n = 0
        for g in self.log_ingests.values():
            if not g.get("evicted") and self._ingest_expired(g):
                self._evict_ingest(g)
                n += 1
        return n

    # ------------------------------------------------------------------ bounds

    def _check_log_caps(self, tid: str, more_bytes: int = 0, new_ingest: bool = False) -> None:
        self._sweep_ingests()
        since = self.now() - timedelta(days=self.settings.log_retention_days)
        mine = [g for g in self.log_ingests.values() if g["tenant_id"] == tid]
        recent = [g for g in mine if parse_iso(g["created_at"]) > since]
        s = self.settings
        if new_ingest and sum(1 for g in mine if g["status"] == "open" and not g.get("evicted")) >= \
                s.log_max_open_ingests:
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
            self._vaulted_key_check()                      # sink job D: nowhere to keep what an ingest retains
            latest = self._latest_audit(tid, body["domain"])
        # sink job B: the latest audit's report is read from the tenant's vault, outside the lock; one that cannot be
        # read refuses VAULT_UNAVAILABLE (the ingest is never created against a sample that could not be read)
        sample = self._sitemap_sample(tid, latest)
        sample_ev = self._sample_evidence(tid, latest, sample)
        guard = self.guard(tid, run="logs")
        robots_text, robots_status = None, "not_fetched"      # robots_text: memory only (M-S3)
        robots_truncated = False
        try:
            guard(capability="logs", agent="selene")
            if self.ports.fetcher is not None:
                rb = self.ports.fetcher.robots(f"{body['scheme']}://{body['domain']}", guard=guard)
                if rb["fetch"]["state"] == "KILLED":
                    raise Killed(rb["fetch"]["detail"] or "KILLED_GLOBAL")
                robots_status = rb["status_class"]
                robots_text = rb.get("text")
                robots_truncated = bool(rb.get("truncated"))
        except Killed as k:
            raise _refusal(k) from None
        if sink_guard.classify(robots_status) is None:     # a fetcher's status class is a code, or it is not kept
            robots_status = "unknown"
        # sink job D: robots.txt (crawled text) and the sitemap sample (paths) are the ingest's inputs blob,
        # encrypted outside the lock; the line keeps its ref, its keyed HMAC and two facts
        inputs = normalised({"v": 1, "robots_text": robots_text, "robots_truncated": robots_truncated,
                             "sitemap_sample": sample, "sample_evidence": sample_ev})
        sealed = self._doc_seal(tid, INGEST_INPUTS_FIELD, inputs)
        try:
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
                        "format": body["format"],
                        "key_fp": hashlib.sha256(self.settings.log_hash_key).hexdigest()[:16],
                        "robots_status": robots_status, "inputs_ref": sealed.ref, "inputs_hmac": sealed.hmac,
                        "robots_present": robots_text is not None,
                        "sitemap_sample_count": None if sample is None else len(_sample_templates(sample))}
                self._log_inputs[iid] = inputs
                try:
                    self._doc_commit(sealed, inputs, lambda: self._commit(
                        "log_ingest_created", self._req(data, actor, rk, body, iid), actor,
                        evidence=("log_ingest_created", f"ingest:{iid}",
                                  {"ingest_id": iid, "tenant_id": tid, "format": body["format"],
                                   "domain_sha256": sha(body["domain"]), "robots_status": robots_status,
                                   "inputs_hmac": sealed.hmac}, (actor, rk))))
                except BaseException:
                    self._log_inputs.pop(iid, None)
                    raise
        finally:
            sealed.discard()
        return self.log_ingest_view(tid, iid)

    def _a_log_ingest_created(self, d, at):
        legacy = "inputs_ref" not in d                      # a line written before sink job D carries the inputs
        self.log_ingests[d["ingest_id"]] = {
            "ingest_id": d["ingest_id"], "tenant_id": d["tenant_id"], "domain": d["domain"], "scheme": d["scheme"],
            "format": d["format"], "key_fp": d["key_fp"], "status": "open", "created_at": at, "next_seq": 1,
            "bytes": 0, "last_seen": False, "totals": logs_mod.empty_delta(), "report": None, "report_sha256": None,
            "report_hmac": None, "report_ref": None, "facts": None,
            "robots_status": d.get("robots_status"), "robots_text": d.get("robots_text") if legacy else None,
            "sitemap_sample": d.get("sitemap_sample") if legacy else None,
            "inputs_ref": d.get("inputs_ref"), "inputs_hmac": d.get("inputs_hmac"), "legacy": legacy,
            "robots_present": d.get("robots_text") is not None if legacy else bool(d.get("robots_present")),
            "sitemap_sample_count": (None if d.get("sitemap_sample") is None else len(d["sitemap_sample"]))
            if legacy else d.get("sitemap_sample_count"),
            "deltas": []}

    def _ingest(self, tid: str, iid: str) -> dict:
        g = self.log_ingests.get(iid)
        if g is None or g["tenant_id"] != tid:
            raise NotFound(R("INGEST_NOT_FOUND"))
        return g

    # ------------------------------------------------------------------ the vaulted parts (sink job D)

    def _ingest_inputs(self, tid: str, iid: str) -> dict:
        """OUTSIDE the lock: ``{"robots_text", "sitemap_sample"}`` of an ingest (memory, else its blob). A blob that
        cannot be read refuses VAULT_UNAVAILABLE: a chunk is never judged against inputs that are not the recorded
        ones."""
        with self.lock:
            g = self._ingest(tid, iid)
            if g["legacy"]:
                return {"robots_text": g.get("robots_text"), "sitemap_sample": g.get("sitemap_sample")}
            got = self._log_inputs.get(iid)
            ref, mac = g["inputs_ref"], g["inputs_hmac"]
        if got is None:
            got = self._vaulted_open(tid, INGEST_INPUTS_FIELD, ref, mac)
            if not isinstance(got, dict):
                raise Unavailable(R("VAULT_UNAVAILABLE"))
            with self.lock:
                if iid in self.log_ingests and not self.log_ingests[iid].get("evicted"):
                    self._log_inputs[iid] = got
        return got

    def _ingest_full(self, tid: str, iid: str) -> tuple:
        """OUTSIDE the lock: (the ingest's FULL totals, the ``next_seq`` they were read at). Memory when this process
        holds them for every recorded chunk; else rebuilt from the chunk blobs (each read for the owning tenant, its
        HMAC checked). A chunk blob that cannot be read refuses VAULT_UNAVAILABLE."""
        with self.lock:
            g = self._ingest(tid, iid)
            if g["legacy"]:
                if g.get("vault_unavailable"):
                    raise Unavailable(R("VAULT_UNAVAILABLE"))
                return g["totals"], g["next_seq"]
            refs = list(g["deltas"])
            held = self._log_full.get(iid)
            if held is not None and held[1] == len(refs):
                return held[0], len(refs) + 1
        total = logs_mod.empty_delta()
        for ref, mac in refs:
            delta = self._vaulted_open(tid, LOG_DELTA_FIELD, ref, mac)
            if not isinstance(delta, dict):
                raise Unavailable(R("VAULT_UNAVAILABLE"))
            logs_mod.merge(total, delta)
        with self.lock:
            g = self.log_ingests.get(iid)
            if g is not None and not g.get("evicted") and len(g["deltas"]) == len(refs):
                self._log_full[iid] = (total, len(refs))
        return total, len(refs) + 1

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
            log_hash_key = self.settings.log_hash_key
            self._check_log_key(g, log_hash_key)
            self._chunk_ok(g, body, nbytes)
            self._vaulted_key_check()
            fmt = g["format"]
            cache = self._log_verify_cache.setdefault(iid, {})
            budget = self._log_budget.setdefault(iid, {"left": self.settings.bot_verify_max})
        inputs = self._ingest_inputs(tid, iid)             # outside the lock (a vault read after a restart)
        robots = self._robots_parsed(iid, inputs.get("robots_text"), bool(inputs.get("robots_truncated")))
        robots_expected = inputs.get("robots_text") is not None
        sample = _sample_index(inputs.get("sitemap_sample"), self._codes_coder(tid).key_id)
        try:
            self.guard(tid, run="logs")(capability="logs", agent="selene")
        except Killed as k:
            raise _refusal(k) from None
        # spec 0.1: path templates are coded with the tenant's codes key (the sample's codes were made with it too)
        with codes_mod.coding(self._codes_coder(tid), own=self._codes_own(tid)) as evidence:
            delta = logs_mod.process_chunk(fmt, data, log_hash_key, self.ports.bot_verifier.verify, cache,
                                           budget, robots, sample, robots_expected=robots_expected)
        del data
        # sink job D: the delta (path templates are its keys) is this chunk's blob, encrypted outside the lock; the
        # line keeps its ref, its keyed HMAC and the counts ``_apply`` and the views need
        delta = normalised(delta)
        counts = _compact(delta)
        sealed = self._doc_seal(tid, LOG_DELTA_FIELD, delta)
        # spec 0.1: the chunk's evidence map (code -> raw path / query as observed) is its own blob of the owning
        # tenant, written in the same append as the delta
        try:
            ev_sealed = self._doc_seal(tid, EVIDENCE_FIELD, normalised(evidence.as_blob()))
        except Exception:
            sealed.discard()
            raise
        sealed.pending.lines.extend(ev_sealed.pending.lines)
        ev_sealed.pending.lines = []
        evidence = None
        try:
            with self.lock:
                self._gate()
                g = self._ingest(tid, iid)
                self.check_kill(tenant=tid, capability="logs", write=True)
                self._selene_ok()
                self._check_log_key(g, log_hash_key)           # the key that produced this delta
                self._check_log_key(g, self.settings.log_hash_key)
                self._chunk_ok(g, body, nbytes)                # nothing moved while the lock was released
                held = self._log_full.get(iid)
                payload = {"ingest_id": iid, "seq": body["seq"], "lines": delta["lines"], "delta_hmac": sealed.hmac}
                line = {"ingest_id": iid, "seq": body["seq"], "last": body["last"], "bytes": nbytes,
                        "delta_ref": sealed.ref, "delta_hmac": sealed.hmac, "counts": counts,
                        "evidence_ref": ev_sealed.ref, "evidence_hmac": ev_sealed.hmac}
                self._doc_commit(sealed, delta, lambda: self._commit(
                    "log_chunk", self._req(line, actor, rk, body, iid), actor,
                    evidence=("log_chunk_ingested", f"ingest:{iid}", payload, (actor, rk))))
                n = len(g["deltas"])
                if n == 1:
                    self._log_full[iid] = (logs_mod.merge(logs_mod.empty_delta(), delta), 1)
                elif held is not None and held[1] == n - 1:
                    self._log_full[iid] = (logs_mod.merge(held[0], delta), n)
                else:
                    self._log_full.pop(iid, None)              # rebuilt from the blobs when it is next needed
        finally:
            sealed.discard()
        return self.log_ingest_view(tid, iid)

    def _robots_parsed(self, iid: str, robots_text, truncated: bool = False):
        if robots_text is None:
            return None
        rb = self._log_robots.get(iid)
        if rb is None:
            rb = self._log_robots[iid] = robots_mod.parse(robots_text, truncated=truncated)
        return rb

    def _check_log_key(self, g: dict, key: bytes) -> None:
        # Missing historical binding is not permission to mix a new key into an existing sketch.
        if g.get("key_fp") != hashlib.sha256(key).hexdigest()[:16]:
            raise Conflict(R("LOG_HASH_KEY_MISMATCH"))

    def _chunk_ok(self, g: dict, body: dict, nbytes: int) -> None:
        if g["status"] != "open" or g["last_seen"] or g.get("evicted") or self._ingest_expired(g):
            raise Conflict(R("INGEST_CLOSED"))
        if body["seq"] != g["next_seq"]:
            raise Conflict(R("CHUNK_SEQ"))
        if g["bytes"] + nbytes > self.settings.log_max_bytes:
            raise Conflict(R("LOG_TOO_LARGE"))
        self._check_log_caps(g["tenant_id"], more_bytes=nbytes)

    def _a_log_chunk(self, d, at):
        g = self.log_ingests[d["ingest_id"]]
        if not g.get("evicted") and self._ingest_expired(g):
            self._evict_ingest(g)
        full = None
        if g["legacy"] and "delta_ref" in d and not g.get("evicted"):
            # an ingest opened before sink job D keeps its full totals in memory: a later chunk's blob is read here
            full = self._doc_applied(g["tenant_id"], LOG_DELTA_FIELD, d["delta_ref"], d["delta_hmac"])
            if not isinstance(full, dict):
                g["vault_unavailable"] = True              # the totals are incomplete: the finish refuses
        if "delta" in d and not g.get("evicted"):          # a line written before sink job D carries its delta
            logs_mod.merge(g["totals"], d["delta"])
        elif isinstance(full, dict):
            logs_mod.merge(g["totals"], full)
        else:
            # counts only in memory (the full delta is the chunk's blob, read when the report is built)
            t, c = g["totals"], (d["counts"] if "counts" in d else _compact(d["delta"]))
            for k in ("lines", "non_bot", "no_user_agent"):
                t[k] += c[k]
            for r, n in c["quarantined"].items():
                t["quarantined"][r] = t["quarantined"].get(r, 0) + n
            for tok, f in c["families"].items():
                tf = t["families"].setdefault(tok, {"requests": 0, "verified": 0, "spoofed": 0, "claimed": 0})
                for k in ("requests", "verified", "spoofed", "claimed"):
                    tf[k] += f[k]
        if "delta_ref" in d and not g.get("evicted") and not g["legacy"]:
            g["deltas"].append((d["delta_ref"], d["delta_hmac"]))
        if "evidence_ref" in d and not g.get("evicted"):
            g.setdefault("evidence", []).append((d["evidence_ref"], d["evidence_hmac"]))
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
            if g["status"] != "open" or g.get("evicted") or self._ingest_expired(g):
                raise Conflict(R("INGEST_CLOSED"))
            self._vaulted_key_check()
        # sink job D: the inputs and the full totals come from the tenant's vault (memory when this process holds
        # them), outside the lock; the report is normalised, HMAC-ed (keyed) and encrypted outside it as well
        inputs = self._ingest_inputs(tid, iid)
        totals, covered = self._ingest_full(tid, iid)
        with self.lock:
            g = self._ingest(tid, iid)
            view = {"format": g["format"], "robots_status": g.get("robots_status")}
        report = normalised(self._log_report(iid, view, totals, inputs))
        facts = {"outcome": report["outcome"], "findings": len(report["envelope"].get("findings") or []),
                 "lines": totals["lines"]}
        sealed = self._doc_seal(tid, LOG_REPORT_FIELD, report)
        try:
            with self.lock:
                self._gate()
                g = self._ingest(tid, iid)
                self.check_kill(tenant=tid, capability="logs", write=True)
                self._selene_ok()
                if self._idem(actor, rk, body):
                    return self.log_ingest_view(tid, iid)
                if g["status"] != "open" or g.get("evicted") or self._ingest_expired(g):
                    raise Conflict(R("INGEST_CLOSED"))
                if g["next_seq"] != covered:
                    raise Conflict(R("CHUNK_SEQ"))         # a chunk arrived while the report was being built
                data = {"ingest_id": iid, "report_ref": sealed.ref, "report_hmac": sealed.hmac, "facts": facts}
                self._doc_commit(sealed, report, lambda: self._commit(
                    "log_ingest_finished", self._req(data, actor, rk, body, iid), actor,
                    evidence=("log_report_recorded", f"ingest:{iid}",
                              {"ingest_id": iid, "report_hmac": sealed.hmac, "outcome": facts["outcome"]},
                              (actor, rk))))
                self._log_verify_cache.pop(iid, None)
                self._log_budget.pop(iid, None)
        finally:
            sealed.discard()
        return self.log_ingest_view(tid, iid)

    def _a_log_ingest_finished(self, d, at):
        g = self.log_ingests[d["ingest_id"]]
        if "report" in d:                                  # a line written before sink job D carries the report
            g.update(status="finished", finished_at=at, report=None if g.get("evicted") else d["report"],
                     report_sha256=d["report_sha256"], robots_text=None)
        else:
            g.update(status="finished", finished_at=at, report_ref=None if g.get("evicted") else d["report_ref"],
                     report_hmac=d["report_hmac"], facts=d.get("facts"), robots_text=None)
        for cache in (self._log_robots, self._log_inputs, self._log_full):   # memory stays bounded
            cache.pop(d["ingest_id"], None)
        g["deltas"] = []

    def _latest_audit(self, tid: str, domain: str) -> Optional[str]:
        """Under the lock: the id of the tenant's latest completed audit of ``domain``."""
        latest = None
        for a in self.audits.values():
            if a["tenant_id"] == tid and a["domain"] == domain and a["status"] == "completed":
                latest = a["audit_id"]                     # log order: the last one completed wins
        return latest

    def _sitemap_sample(self, tid: str, latest: Optional[str]) -> Optional[list]:
        """Outside the lock: Delia's sitemap sample in the report of audit ``latest`` (read from the vault)."""
        if latest is None:
            return None
        for e in self._audit_recorded(tid, latest)["report"]["agents"]:
            if e["agent"] == "delia":
                return _sample_of(e["facts"], self._audit_recorded(tid, latest)["report"].get("codes_key_id"))
        return None

    def _log_report(self, iid: str, g: dict, totals: dict, inputs: dict) -> dict:
        sample, fmt = _sample_templates(inputs.get("sitemap_sample")), g["format"]
        robots_ok = inputs.get("robots_text") is not None
        unchecked = totals.get("robots_unchecked", 0)
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
            if tok in SEARCH_TOKENS and f["requests"] >= SERVER_ERROR_MIN_REQUESTS:
                err = f["status"].get("5xx", 0)
                if err / f["requests"] > SERVER_ERROR_SHARE:
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
        if robots_ok and unchecked:
            findings.append(finding("LOG_ROBOTS_CHECK_INCOMPLETE", "info", "measured", "INSUFFICIENT_EVIDENCE",
                                    capability="P2", detail={"requests": unchecked,
                                                             "note": "bot requests not checked against robots.txt "
                                                                     "(time budget, or robots.txt no longer in "
                                                                     "memory after a restart): the disallowed-hit "
                                                                     "counts are a lower bound"}))
        nc = {"bot_ip_range_verification"}
        if not getattr(self.ports.bot_verifier, "connected", False):
            nc.add("bot_dns_verification")
        usable = totals["lines"] - sum(totals["quarantined"].values())
        outcome = "OK" if usable > 0 and robots_ok and sample is not None and not unchecked else \
            ("PARTIAL" if usable > 0 else "INSUFFICIENT_EVIDENCE")
        env = envelope("selene", "log_access", outcome, findings,
                       methodology=METHODOLOGY.format(fmt=fmt, v=bots.VERSION, rules=logs_mod.TEMPLATE_RULES),
                       limitations=LIMITS, not_connected=nc,
                       facts={"lines": totals["lines"], "usable_lines": usable, "non_bot": totals["non_bot"],
                              "no_user_agent": totals["no_user_agent"], "quarantined": dict(totals["quarantined"]),
                              "families": fams, "robots_status": g.get("robots_status"),
                              "robots_unchecked_requests": unchecked if robots_ok else None,
                              "uncrawled_important_paths": uncrawled[:50] if uncrawled is not None else None,
                              "sitemap_sample": "latest audit" if sample is not None else
                              "none: run an audit of this domain first", "bot_families_version": bots.VERSION})
        return {"ingest_id": iid, "outcome": outcome, "envelope": env, "codes_key_id": self._codes_key_id()}

    # ------------------------------------------------------------------ views

    def log_ingest_view(self, tid: str, iid: str, full: bool = True) -> dict:
        """The owner's view. ``full``: with the report, read from the tenant's vault OUTSIDE the lock (sink job D)
        and shown through the client-view masker; a report that cannot be read is ``None`` with
        ``vault_unavailable: true``."""
        with self.lock:
            g = self._ingest(tid, iid)
            expired = self._ingest_expired(g)
            if expired:
                self._evict_ingest(g)
            t = g["totals"]
            out = {k: g[k] for k in ("ingest_id", "tenant_id", "domain", "scheme", "format", "key_fp", "created_at",
                                     "next_seq", "bytes", "report_sha256", "report_hmac", "robots_status")}
            out["status"] = "expired" if expired else g["status"]
            out["totals"] = {"lines": t["lines"], "non_bot": t["non_bot"], "no_user_agent": t["no_user_agent"],
                             "quarantined": dict(t["quarantined"]),
                             "families": {k: {"requests": v["requests"], "verified": v["verified"],
                                              "spoofed": v["spoofed"], "claimed": v["claimed"]}
                                          for k, v in t["families"].items()}}
            inline = None if expired else g["report"]
            ref, mac = (None, None) if expired else (g.get("report_ref"), g.get("report_hmac"))
            out["retention_days"] = self.settings.log_retention_days
            out["as_of"] = iso(self.now())
        report = inline
        if full and inline is None and ref is not None:
            report = self._vaulted_open(tid, LOG_REPORT_FIELD, ref, mac)
            if report is None:
                out["vault_unavailable"] = True            # never served from anywhere else
            else:
                report = owner_view(report)
        out["report"] = report if full else None
        return out

    def _sample_evidence(self, tid: str, latest: Optional[str], sample) -> dict:
        """Outside the lock: {code: cleaned raw value} of the codes in the sitemap sample's stored forms, from the
        latest audit's evidence map (the owner display of the ingest resolves them; spec 0.1)."""
        templates = _sample_templates(sample) or []
        wanted = {m.group(0) for t in templates if isinstance(t, str) for m in codes_mod.CODE_RX.finditer(t)}
        if not wanted or latest is None:
            return {}
        values = self._evidence_values(tid, latest) or {}
        return {c: values[c] for c in sorted(wanted) if c in values}

    def log_ingest_view_resolved(self, tid: str, iid: str) -> dict:
        """D-02h for a log ingest (the API route allows it only to the tenant-authenticated owner caller): the
        report with every code resolved through the ingest's evidence maps (each chunk's, and the sample's from the
        latest audit), cleaned as the client report cleans a value. A map that cannot be read leaves its codes
        ``[not available]`` (``evidence_available: false``), never an error."""
        from svc_audits import _clean_resolved
        out = self.log_ingest_view(tid, iid)
        if out.get("report") is None:
            out["evidence_available"] = False
            return out
        with self.lock:
            g = self._ingest(tid, iid)
            refs = list(g.get("evidence") or [])
        values, ok = {}, True
        try:
            values.update(self._ingest_inputs(tid, iid).get("sample_evidence") or {})
        except Unavailable:
            ok = False
        for ref, mac in refs:
            blob = self._vaulted_open(tid, EVIDENCE_FIELD, ref, mac)
            if not isinstance(blob, dict) or not isinstance(blob.get("values"), dict):
                ok = False
                continue
            values.update({k: _clean_resolved(v) for k, v in blob["values"].items() if isinstance(v, str)})
        out["evidence_available"] = ok
        out["report"] = codes_mod.resolve(out["report"], values)
        return out

    def log_ingests_view(self, tid: str) -> list:
        with self.lock:
            return [self.log_ingest_view(tid, i, full=False)
                    for i, g in self.log_ingests.items() if g["tenant_id"] == tid][:1000]


def _sample_of(delia_facts: dict, key_id: Optional[str]) -> Optional[dict]:
    """The sitemap sample an ingest keeps (B-LG-05): the stored forms to show and the codes of the whole paths to
    match, with the codes key id they were made under; never raw paths. A report without codes (older) has no
    usable sample."""
    templates = delia_facts.get("sitemap_paths_sample")
    cs = delia_facts.get("sitemap_paths_sample_codes")
    if not isinstance(templates, list) or not isinstance(cs, list) or len(cs) != len(templates) or not key_id:
        return None
    return {"templates": list(templates), "codes": list(cs), "codes_key_id": key_id}


def _sample_index(sample, key_id: Optional[str]) -> dict:
    """{path code: index} for process_chunk; a sample coded under another codes key cannot be matched (empty)."""
    if isinstance(sample, dict) and sample.get("codes_key_id") == key_id:
        return {c: i for i, c in enumerate(sample.get("codes") or [])}
    return {}


def _sample_templates(sample) -> Optional[list]:
    if isinstance(sample, dict):
        return list(sample.get("templates") or [])
    return None
