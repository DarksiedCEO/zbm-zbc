"""
Callum's citation-source runs (ADR 0046): the near-real-time refresh tier of "where and how do AI engines cite the
client", separate from the audit (which keeps Callum's per-prompt summary, agents/probes.py, unchanged).

  POST /seo/v1/tenants/{tid}/citation-runs                       dashboard / seo_agent (a client tenant: Andre)
  GET  /seo/v1/tenants/{tid}/citation-runs                       + hub (its own tenant only), compliance_38
  GET  /seo/v1/tenants/{tid}/citation-runs/{cid}                 the rows (dashboard drill-down) and the metrics
  GET  /seo/v1/tenants/{tid}/citation-runs/{cid}/mnc-priority    the callum.mnc_priority/2 list (Roman, Adaeze)

Rules (the audit product's, applied here):
  - one of the tenant's registered domains only (DOMAIN_NOT_AUTHORIZED otherwise) and one of its prompt sets (404);
  - a ``client`` tenant's run needs Andre's approval token, a bound Finance client and Compliance (38)'s current
    ALLOW (ADR 0024; asked outside the lock, never overridden). No invoice is asked for: founder-pending default
    (ADR 0046) — the managed tier pays for monitoring; per-run billing is a founder decision;
  - one running citation run per tenant; the department manager's run guard (a restricted Callum is refused 403
    AGENT_RESTRICTED); kill switches: global, write, the tenant, ``capability:ai_probe``, ``capability:audit`` and
    each engine's ``provider:<engine>`` — checked at the request, at every reservation, before every request and
    every socket operation, and again before the result is recorded (engaged mid-run: the run is recorded
    ``interrupted`` with the switch's code, the result discarded);
  - record-first: ``citation_run_requested`` on the ledger and the log before any engine is asked; every metered
    request inside a recorded reservation (svc_engines.py, subject ``citation_run:<cid>``); the result committed as
    ``citation_run_recorded`` with its SHA-256 on the ledger. A run lost to a dead process is marked ``interrupted``
    by the ``interrupted-audits`` job; a run that raises is closed ``interrupted`` RUN_FAILED at once.
  - tenant isolation: another tenant's run answers the same 404 as a missing one.
Ledger payloads carry ids, codes, counts and hashes only. ADR 0051 (job 2, Option B): each row's full evidence (cited
URL, excerpt, title, hint) is encrypted into the per-tenant citation vault (vault.py) under the run's tenant; the
result committed to the append-only log keeps only the vault reference, keyed per-field hashes (HMAC-SHA256 under a
sub-key of the vault key, job 3), host-only URLs, codes, counts and the strict filter's verdict flags (and is still
swept by secret_guard.guard_record before the commit). The owning tenant's GET decrypts from the vault: a field with a
HIGH-PRECISION client-view finding (secret_guard.client_view_findings / client_view_url, job 3) is masked with
``alert`` (a URL only in its offending parts), every other field is shown in full. No vault key with a data directory: the run fails closed (RUN_FAILED, VAULT_UNAVAILABLE) before any
engine is asked, and nothing is written. Any exception while splitting, sweeping or committing closes the run
RUN_FAILED (never left running); nothing raw is written on any path.
"""

from __future__ import annotations

import logging
from typing import Optional

import vault as vault_mod
from agents import callum
from envelope import sha
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ledger import derived_id
from primitives import Killed
from reasons import R

log = logging.getLogger("seo.citations")


class CitationsMixin:
    def request_citation_run(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            actor = "andre" if andre else actor
            rk = self.rk("citation_run", tid, body)
            prev = self._idem(actor, rk, body)
            if not prev:
                client = self._citation_rules(t, tid, body, andre)
        if prev:                                     # review 601e328a SHOULD 1: the replay's view outside the lock
            return self.citation_run_view(tid, prev[1])
        cmp_ = self._verify_compliance(client) if client is not None else None     # ADR 0024, outside the lock
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            prev = self._idem(actor, rk, body)
            if not prev:
                if self._citation_rules(t, tid, body, andre) != client:
                    raise Conflict(R("TENANT_FINANCE_CLIENT_CHANGED"))
                cid, ps = self._begin_citation_run(t, tid, actor, rk, body, andre, cmp_)
        if prev:                                     # review 601e328a SHOULD 1: the replay's view outside the lock
            return self.citation_run_view(tid, prev[1])
        return self._execute_citation_run(cid, tid, body["domain"], ps)

    def _begin_citation_run(self, t: dict, tid: str, actor: str, rk: str, body: dict, andre: bool, cmp_) -> tuple:
        """Under the lock: refuse a second running run, record the request, mark it running. Returns (cid, ps)."""
        if any(r["tenant_id"] == tid and r["citation_run_id"] in self.running_citation_runs
               for r in self.citation_runs.values()):
            raise Conflict(R("CITATION_RUN_RUNNING"))
        p = self.prompt_sets[body["prompt_set_id"]]
        ps = {"prompt_set_id": p["prompt_set_id"], **p["versions"][str(p["current"])]}
        wanted = body.get("engines")
        ps["engines"] = sorted(set(ps["engines"]) & set(wanted)) if wanted else list(ps["engines"])
        if not ps["engines"]:                        # narrowing to nothing would report "not connected": refuse
            raise Invalid(R("INVALID"), field="engines")
        cid = derived_id("cit", tid, actor, rk)
        ref = {"prompt_set_id": ps["prompt_set_id"], "version": ps["version"], "sha256": ps["sha256"]}
        data = {"citation_run_id": cid, "tenant_id": tid, "tenant_kind": t["kind"], "domain": body["domain"],
                "prompt_set": ref, "engines": ps["engines"], "andre_approved": bool(andre),
                "compliance": None if cmp_ is None else {"ruling_id": cmp_.get("ruling_id"),
                                                         "verdict": cmp_.get("verdict")}}
        ev = [("citation_run_requested", f"citation_run:{cid}",
               {"citation_run_id": cid, "tenant_id": tid, "kind": t["kind"], "domain_sha256": sha(body["domain"]),
                "prompt_set_id": ref["prompt_set_id"], "prompt_set_version": ref["version"],
                "prompt_set_sha256": ref["sha256"], "engines": ps["engines"], "andre_approved": bool(andre)},
               (actor, rk))]
        self._commit("citation_run_requested", self._req(data, actor, rk, body, cid), actor, evidence=ev)
        self.running_citation_runs.add(cid)
        return cid, ps

    def _citation_rules(self, t: dict, tid: str, body: dict, andre: bool) -> Optional[str]:
        """Under the lock: the authorisation rules. Returns the Finance client a client tenant is bound to."""
        self.check_kill(tenant=tid, capability="ai_probe", write=True)
        self.check_kill(capability="audit")
        if self.agent_blocked("callum"):
            raise Forbidden(R("AGENT_RESTRICTED"))
        if body["domain"] not in t["domains"]:
            raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
        p = self.prompt_sets.get(body["prompt_set_id"])
        if p is None or p["tenant_id"] != tid:
            raise NotFound(R("PROMPT_SET_NOT_FOUND"))
        if p["versions"][str(p["current"])].get("vault_unavailable"):
            raise Unavailable(R("VAULT_UNAVAILABLE"))       # review c0c09bd1: its text cannot be read: never sent
        if t["kind"] != "client":
            return None
        if not andre:
            raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
        client = t.get("finance_client_id")
        if not client:
            raise Conflict(R("TENANT_FINANCE_CLIENT_UNBOUND"))
        return client

    def _execute_citation_run(self, cid: str, tid: str, domain: str, ps: dict) -> dict:
        from svc_audits import _where
        try:
            if not self.vault.key_configured:
                # ADR 0051: fail closed before any engine is asked (no spend, nothing kept): there is nowhere to keep
                # the client's evidence, and it is never kept in plaintext instead
                return self._interrupt_citation_run(tid, cid, R("RUN_FAILED"), _vault_failure())
            guard = self.guard(tid)
            sink: list = []
            try:
                guard(agent="callum")
                resolver = callum.Resolver(self.ports.fetcher, guard, self.settings.callum_resolve_urls,
                                           self.settings.callum_resolve_max)
                env = callum.run(self.ports.engines, ps, domain, self.settings.probe_samples, guard,
                                 self.engine_budget(tid, cid, ps, subject="citation_run"), resolver, cid, self.clock,
                                 sink=sink)
            except Killed as k:
                return self._interrupt_citation_run(tid, cid, k.code)
            except Exception as exc:  # noqa: BLE001 - a run that crashes is closed now, never left running
                where = _where(exc)
                log.error("citation run failed: %s at %s", type(exc).__name__, where)
                return self._interrupt_citation_run(tid, cid, R("RUN_FAILED"),
                                                    {"agent": "callum", "where": where,
                                                     "error_type": type(exc).__name__[:60]})
            # review adacb2ca SHOULD 1: the split (encryption into a pending batch, keyed hashes) and the sweep run
            # OUTSIDE the service lock; under it only the kill check, ONE vault append (one fsync) and the commit
            pending = vault_mod.VaultBatch()
            stopped = False
            try:
                try:
                    logged = self._prepare_citation_result(tid, env, sink, pending)
                    failure = logged if not isinstance(logged, tuple) else None
                    if failure is None:
                        with self.lock:
                            self._gate()
                            stop = self.kill_code(tenant=tid, capability="ai_probe", write=True) or self.kill_code(
                                capability="audit")
                            if stop is not None:
                                stopped = True
                                pending.discard()        # killed: nothing is written
                            else:
                                failure = self._record_citation_result(tid, cid, logged[0], pending)
                except Exception as exc:  # noqa: BLE001 - split / sweep / commit failed: closed now, never left running
                    where = _where(exc)
                    log.error("citation run failed: %s at %s", type(exc).__name__, where)
                    failure = _vault_failure() if isinstance(exc, vault_mod.VaultUnavailable) else {
                        "agent": "callum", "where": where, "error_type": type(exc).__name__[:60]}
            finally:
                sink.clear()                         # the plaintext evidence is dropped from memory either way
                pending.discard()
                env = logged = None
            if stopped:
                return self._interrupt_citation_run(tid, cid, stop)
            if failure is not None:
                return self._interrupt_citation_run(tid, cid, R("RUN_FAILED"), failure)
            return self.citation_run_view(tid, cid)
        finally:
            with self.lock:
                self.running_citation_runs.discard(cid)

    def _prepare_citation_result(self, tid: str, env, sink: list, pending) -> object:
        """Outside the lock. ADR 0051: split (each row's full evidence encrypted for ``tid`` into ``pending``, written
        later in one append; the envelope keeps references, keyed hashes, host-only URLs and verdict flags), then the
        fail-closed sweep of the whole envelope (ADR 0050 v2 step 4: every key and value; a field that is not clearly
        safe is withheld whole). Returns ``(logged envelope,)``, or the failure (the run is then closed RUN_FAILED).
        Anything that raises is caught by the caller and closes the run the same way."""
        with self.vault.collecting(pending):
            env = callum.split_evidence(env, sink, lambda record: self.vault.put(tid, record),
                                        field_hash=lambda field, value: self.vault.field_hash(tid, field, value))
        env = callum.secret_guard.guard_record(env)
        if not isinstance(env, dict) or not isinstance(env.get("facts", {}), dict):
            # the sweep itself failed (it returns "[withheld]"): never stored
            return {"agent": "callum", "where": "secret_guard", "error_type": "GuardFailed"}
        return (env,)

    def _record_citation_result(self, tid: str, cid: str, env, pending) -> Optional[dict]:
        """Under the lock: the pending vault records in one append (one fsync), then the commit. Returns None when
        committed. Anything that raises is caught by the caller and closes the run RUN_FAILED."""
        written = len(pending)
        self.vault.write_batch(pending)
        rsha = sha(env)
        facts = env.get("facts") or {}
        totals = (facts.get("metrics") or {}).get("totals") or {}
        try:
            self._commit("citation_run_recorded", {"citation_run_id": cid, "result": env, "result_sha256": rsha},
                         "seo", evidence=("citation_run_recorded", f"citation_run:{cid}",
                                          {"citation_run_id": cid, "result_sha256": rsha,
                                           "outcome": env["outcome"], "rows": len(facts.get("rows") or []),
                                           "mentioned_not_cited": totals.get("mnc"),
                                           "strength_answers": totals.get("strength")}, ("result", cid)))
        except BaseException:
            # review 601e328a SHOULD 2: the records are in the vault but no recorded run refers to them (ciphertext
            # under the tenant's key, never served: nothing holds their refs). Counted in health; no purge job yet
            # (ADR 0051)
            self.vault.note_orphans(written)
            raise
        return None

    def _interrupt_citation_run(self, tid: str, cid: str, reason: str, failure: Optional[dict] = None) -> dict:
        with self.lock:
            self._gate()
            data = {"citation_run_id": cid, "reason": reason}
            if failure:
                data["failure"] = failure
            self._commit("citation_run_interrupted", data, "seo",
                         evidence=("citation_run_interrupted", f"citation_run:{cid}",
                                   {"citation_run_id": cid, "reason": reason, **(failure or {})},
                                   ("interrupted", cid)))
            return self.citation_run_view(tid, cid)

    def mark_interrupted_citation_runs(self) -> int:
        n = 0
        with self.lock:
            stale = [r["citation_run_id"] for r in self.citation_runs.values()
                     if r["status"] == "running" and r["citation_run_id"] not in self.running_citation_runs]
            for cid in stale:
                self._gate()
                self._commit("citation_run_interrupted", {"citation_run_id": cid, "reason": "PROCESS_LOST"},
                             "scheduler", evidence=("citation_run_interrupted", f"citation_run:{cid}",
                                                    {"citation_run_id": cid, "reason": "PROCESS_LOST"},
                                                    ("interrupted", cid)))
                n += 1
        return n

    # ------------------------------------------------------------------ replay

    def _a_citation_run_requested(self, d, at):
        self.citation_runs[d["citation_run_id"]] = {
            **{k: v for k, v in d.items() if k not in ("actor", "request_id", "request_sha", "_obj", "evidence")},
            "status": "running", "requested_at": at, "requested_by": d["actor"], "completed_at": None,
            "result": None, "result_sha256": None}

    def _a_citation_run_recorded(self, d, at):
        self.citation_runs[d["citation_run_id"]].update(status="completed", completed_at=at, result=d["result"],
                                                       result_sha256=d["result_sha256"])

    def _a_citation_run_interrupted(self, d, at):
        r = self.citation_runs[d["citation_run_id"]]
        r.update(status="interrupted", completed_at=at, interrupted_reason=d.get("reason", "PROCESS_LOST"))
        if d.get("failure"):
            r["failure"] = dict(d["failure"])

    # ------------------------------------------------------------------ views

    def _owns(self, r: Optional[dict], tid: str) -> bool:
        """Tenant isolation: a run is answered only on its own tenant's path (another tenant's run is the same 404 as
        a missing one)."""
        return r is not None and r["tenant_id"] == tid

    def citation_run_view(self, tid: str, cid: str, full: bool = True) -> dict:
        """The owner's view (ADR 0051): the logged result with each row's evidence decrypted from the vault for
        ``tid``; a field with a client-view finding (job 3) is masked with ``alert``, every other field shown in full."""
        with self.lock:
            # review adacb2ca SHOULD 1: a snapshot under the lock (the recorded result is never changed once
            # recorded); every row is decrypted and masked outside it
            r = self.citation_runs.get(cid)
            if not self._owns(r, tid):
                raise NotFound(R("CITATION_RUN_NOT_FOUND"))
            out = {k: v for k, v in r.items() if k != "result"}
            if r["status"] == "running" and cid not in self.running_citation_runs:
                out["status"] = "running_elsewhere_or_interrupted"
            result = r["result"]
        if full:
            out["result"] = callum.client_view(result, lambda ref: self._vault_record(tid, ref)) \
                if result is not None else None
        else:
            out["outcome"] = (result or {}).get("outcome")
        return out

    def _vault_record(self, tid: str, ref) -> tuple:
        """(record, None) for the owning tenant, else (None, reason): never raises, never says why beyond the code."""
        vid = vault_mod.parse_ref(ref)
        if vid is None:
            return None, R("VAULT_RECORD_NOT_FOUND")
        try:
            return self.vault.get(tid, vid), None
        except vault_mod.VaultUnavailable:
            return None, R("VAULT_UNAVAILABLE")
        except vault_mod.VaultError:
            return None, R("VAULT_RECORD_NOT_FOUND")

    def citation_runs_view(self, tid: str) -> list:
        with self.lock:
            return [self.citation_run_view(tid, r["citation_run_id"], full=False)
                    for r in sorted(self.citation_runs.values(), key=lambda r: (r["requested_at"],
                                                                                r["citation_run_id"]))
                    if r["tenant_id"] == tid][:1000]

    def mnc_priority_view(self, tid: str, cid: str) -> dict:
        v = self.citation_run_view(tid, cid)
        if v["status"] != "completed":
            raise Conflict(R("CITATION_RUN_NOT_COMPLETED"))
        mp = ((v["result"] or {}).get("facts") or {}).get("mnc_priority")
        if mp is None:                                   # NOT_CONNECTED / KILLED envelopes carry no list
            return {"contract": callum.CONTRACT_MNC, "producer": callum.AGENT, "run_id": cid, "items": [],
                    "outcome": v["result"]["outcome"], "note": "nothing was observed: no list"}
        return {**mp, "outcome": v["result"]["outcome"]}

    def citation_scorecard(self) -> dict:
        """Callum's citation runs, from recorded runs only (the department manager's scorecard)."""
        out = {"runs": 0, "completed": 0, "interrupted": 0, "outcomes": {}}
        for r in self.citation_runs.values():
            out["runs"] += 1
            if r["status"] == "completed":
                out["completed"] += 1
                o = r["result"]["outcome"]
                out["outcomes"][o] = out["outcomes"].get(o, 0) + 1
            elif r["status"] == "interrupted":
                out["interrupted"] += 1
        out["outcomes"] = dict(sorted(out["outcomes"].items()))
        return out


def _vault_failure() -> dict:
    return {"agent": "callum", "where": "vault", "error_type": "VaultUnavailable", "code": R("VAULT_UNAVAILABLE")}
