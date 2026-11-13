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
    is the client's content: it is stored whole in the tenant's vault (svc_vaulted.py, sink job B) and
    ``audit_completed`` keeps its blob ref, its keyed HMAC (also on the ledger) and machine facts only. A run whose
    process died is marked ``interrupted`` by the ``interrupted-audits`` job, never silently completed;
  - the report keeps OUTCOME apart from FINDINGS per agent, says what was NOT_CONNECTED and why, and carries every
    agent's methodology and limitations. It never states a business effect (no first-party analytics / CRM is
    connected) and never claims superiority over anyone.
"""

from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path
from typing import Optional

from agents import RunContext, bots, brand_mention, delia, entity_check, osei, probes, roman, selene
from agents import secret_guard
from agents.prompt_guard import personal_data
from clock import iso
from envelope import CAPABILITIES, DATA_CLASSES, DECISIONS, EFFECT_CLASSES, envelope, sha, worst_outcome
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ledger import derived_id
from primitives import Killed
from primitives import diff as diff_mod
from primitives.parse import SCHEMA_RULES_VERSION
from reasons import R
import sink_guard
import vault as vault_mod
from svc_entity import unreadable as entity_unreadable
from svc_optext import path_view, text_view, url_flag
from svc_vaulted import PROMPT_SET_FIELD, REPORT_FIELD, SECTION_FIELD, canonical, normalised, owner_view

log = logging.getLogger("seo.audits")
REPORT_VERSION = "seo-audit-report/1"
_PATH_OK = re.compile(r"/[\x21-\x7e]{0,500}")
GLOBAL_LIMITS = [
    "Wave 1 reads public pages only. First-party truth (Bing Webmaster Tools, analytics, CRM; Search Console unless "
    "the search_performance section says it was read) is NOT_CONNECTED, so nothing here is paired with qualified "
    "traffic, conversions or revenue, and no finding claims a business effect (credit is not causation).",
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
    "answer_engines": "an engine without an adapter (OpenAI, Anthropic, Google) or without a key (Perplexity) is "
                      "NOT_CONNECTED; it was asked nothing",
    "prompt_volume": "no prompt-volume source is chosen or built",
    "first_party": "Bing Webmaster, analytics and CRM are not connected; Search Console only when a service-account "
                   "key is configured AND Andre bound the tenant's property (see search_performance); server logs "
                   "only as client uploads",
    "zero_day / orca_publish": "ports only in Wave 1 (founder-pending flag 5)",
    "clientfix": "fix execution is Department 28's, not in Wave 1",
}


def _brand_term_rules(raw: list, terms: list) -> list:
    """ADR 0046 review round 1 L1: one rule per brand term at most, for a term of the set; whitespace folded as the
    terms are; no control characters."""
    out, seen = [], set()
    for r in raw:
        term = " ".join(r["term"].split())
        ctx = [" ".join(w.split()) for w in r.get("context_terms") or []]
        neg = [" ".join(w.split()) for w in r.get("negative_terms") or []]
        if term not in terms or term in seen or any(not w or any(ord(c) < 0x20 for c in w) for w in ctx + neg):
            raise Invalid(R("PROMPT_SET_INVALID"))
        seen.add(term)
        out.append({"term": term, "case_sensitive": bool(r.get("case_sensitive", False)), "context_terms": ctx,
                    "negative_terms": neg, "common_word": r.get("common_word")})
    return sorted(out, key=lambda x: terms.index(x["term"]))


_PERSONAL_CHECKS = frozenset(("e_digits", "f_email", "url_personal"))


def _strings(x) -> list:
    """Every string inside a request body (keys and values; lists and objects walked)."""
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for k, v in x.items() for s in _strings(k) + _strings(v)]
    if isinstance(x, (list, tuple)):
        return [s for v in x for s in _strings(v)]
    return []


def _strict_prompt_set(body: dict) -> None:
    """The prompt set's IDENTIFIER fields (name, brand terms, competitor domains, engines, a rule's term) are written to
    the log in plaintext and must pass the shared core's STRICT check (review c13529af MUST 5; a credential is refused
    ``PROMPT_SECRET``, personal data ``PROMPT_PERSONAL_DATA``). Review c0c09bd1: the FREE TEXT (prompts, a rule's
    context / negative terms) is no longer refused for failing the strict check (it is vaulted; see svc_optext.py):
    only a strong-key prefix, a PEM / PGP block or URL user info in it is refused ``PROMPT_SECRET``."""
    # review 2b26d980 SHOULD 1: a competitor DOMAIN is a host name, judged by the host rule alone (a credential word
    # in a label is a name: ``1password.com``); the strict check is for the other identifier fields
    ids = {k: v for k, v in body.items() if k not in ("request_id", "prompts", "brand_term_rules",
                                                      "competitor_domains")}
    ids["rule_terms"] = [r.get("term") for r in body.get("brand_term_rules") or []]
    for s in _strings(ids):
        name = secret_guard.check(s)
        if name is not None:
            raise Invalid(R("PROMPT_PERSONAL_DATA" if name in _PERSONAL_CHECKS else "PROMPT_SECRET"))
    if isinstance(body.get("name"), str) and globals()["_identifier_refused"](body["name"]):
        raise Invalid(R("IDENTIFIER_WITHHELD"))
    for d in list(body.get("competitor_domains") or []):
        if isinstance(d, str) and globals()["_domain_refused"](d):
            raise Invalid(R("IDENTIFIER_WITHHELD"))
    free = list(body.get("prompts") or []) + [w for r in body.get("brand_term_rules") or []
                                               for k in ("context_terms", "negative_terms") for w in r.get(k) or []]
    if any(globals()["_operator_refused"](t) for t in free):
        raise Invalid(R("PROMPT_SECRET"))


def _operator_refused(text: str) -> bool:
    return secret_guard.operator_refusal(text) is not None


def _identifier_refused(text: str) -> bool:
    return secret_guard.identifier_refusal(text) is not None


def _domain_refused(text: str) -> bool:
    from svc_tenants import domain_refused
    return domain_refused(text)


def path_refused(scheme: str, domain: str, path: str) -> bool:
    """An audit / schedule path (fetched) holding a strong key / PEM / user info, or a value under a credential query
    key: refused (review c0c09bd1). Every other path is accepted and vaulted (svc_optext.py)."""
    return secret_guard.operator_url_refusal(f"{scheme}://{domain}{path}") is not None


def _rules_hashed(rules: list, h) -> list:
    return [{**r, "context_terms": [h(w) for w in r["context_terms"]],
             "negative_terms": [h(w) for w in r["negative_terms"]]} for r in rules]


class AuditsMixin:
    # ------------------------------------------------------------------ prompt sets (versioned)

    def create_prompt_set(self, actor: str, tid: str, body: dict) -> dict:
        if not body.get("brand_term_rules"):           # no rules: the request (and its idempotency key) as before
            body = {k: v for k, v in body.items() if k != "brand_term_rules"}
        terms = [" ".join(t.split()) for t in body["brand_terms"]]
        prompts = [" ".join(p.split()) for p in body["prompts"]]
        if any(not t for t in terms) or any(not p for p in prompts) or len(set(prompts)) != len(prompts) \
                or any(any(ord(c) < 0x20 for c in s) for s in terms + prompts):
            raise Invalid(R("PROMPT_SET_INVALID"))
        if any(personal_data(p) for p in prompts):         # W4-3: prompts sent to an engine carry no personal data
            raise Invalid(R("PROMPT_PERSONAL_DATA"))
        _strict_prompt_set(body)                            # review c0c09bd1: identifiers strict, free text vaulted
        rules = _brand_term_rules(body.get("brand_term_rules") or [], terms)
        # sink job D: the WHOLE content (name, brand terms, rules, competitor domains, prompts, engines) is one blob
        # per version in the operator text vault, encrypted and HMAC-ed OUTSIDE the service lock; the version's
        # fingerprint is the keyed HMAC of its canonical JSON (never a plain SHA-256 over brand terms), and the log
        # keeps the ref, the HMAC and counts
        content = {"name": body["name"], "brand_terms": terms,
                   "competitor_domains": sorted(body["competitor_domains"]), "prompts": prompts,
                   "engines": sorted(set(body["engines"]))}
        if rules:
            content["brand_term_rules"] = rules
        content = normalised(content)
        with self.lock:
            self._gate()
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="prompt_sets", write=True)
            self._optext_key_check()
        sealed = self._doc_seal(tid, PROMPT_SET_FIELD, content, self.optext_vault)
        try:
            with self.lock:
                self._gate()
                self._get(self.tenants, tid, "TENANT_NOT_FOUND")
                self.check_kill(tenant=tid, capability="prompt_sets", write=True)
                self._optext_key_check()

                def hp(t):
                    return self._optext_hmac(tid, "prompt", t)

                def hr(t):
                    return self._optext_hmac(tid, "brand_term", t)
                # review c0c09bd1: the idempotency body and the content hash carry the HMACs of the free text, never it
                hbody = {**body, "prompts": [hp(" ".join(p.split())) for p in body["prompts"]]}
                if body.get("brand_term_rules"):
                    hbody["brand_term_rules"] = [{**r, "context_terms": [hr(" ".join(w.split())) for w in r.get(
                        "context_terms") or []], "negative_terms": [hr(" ".join(w.split())) for w in r.get(
                            "negative_terms") or []]} for r in body["brand_term_rules"]]
                hashed = {"name": body["name"], "brand_terms": terms, "competitor_domains": sorted(body["competitor_domains"]),
                          "prompts": [hp(p) for p in prompts], "engines": sorted(set(body["engines"]))}
                if rules:                                  # absent when none: an existing set's content hash is unchanged
                    hashed["brand_term_rules"] = _rules_hashed(rules, hr)
                rk = self.rk("prompt_set", tid, body)
                prev = self._idem(actor, rk, hbody)
                if prev:
                    return self.prompt_set_view(tid, prev[1])
                psid = derived_id("pst", tid, body["name"])
                cur = self.prompt_sets.get(psid)
                if cur is not None:
                    last = cur["versions"][str(cur["current"])]
                    same = last["sha256"] == sha(hashed) if last.get("plain_sha256") else \
                        self.optext_vault.field_hash_matches(tid, PROMPT_SET_FIELD, canonical(content), last["sha256"])
                    if same:
                        raise Conflict(R("PROMPT_SET_EXISTS"))
                version = 1 if cur is None else cur["current"] + 1
                facts = {"prompts": len(prompts), "brand_terms": len(terms), "brand_term_rules": len(rules),
                         "competitor_domains": len(content["competitor_domains"]), "engines": content["engines"]}
                data = {"prompt_set_id": psid, "tenant_id": tid, "version": version, "content_ref": sealed.ref,
                        "content_hmac": sealed.hmac, "facts": facts}
                self._doc_commit(sealed, content, lambda: self._commit(
                    "prompt_set_version", self._req(data, actor, rk, hbody, psid), actor,
                    evidence=("prompt_set_version", f"prompt_set:{psid}",
                              {"prompt_set_id": psid, "tenant_id": tid, "version": version,
                               "content_hmac": sealed.hmac}, (actor, rk))))
                return self.prompt_set_view(tid, psid)
        finally:
            sealed.discard()

    def _a_prompt_set_version(self, d, at):
        ps = self.prompt_sets.setdefault(d["prompt_set_id"], {"prompt_set_id": d["prompt_set_id"],
                                                              "tenant_id": d["tenant_id"], "versions": {}})
        tid = d["tenant_id"]
        if "content" not in d:
            # sink job D: the content is the version's blob; ``sha256`` (the version's fingerprint everywhere else)
            # is its keyed HMAC. A blob that cannot be read leaves an empty, unusable version (``vault_unavailable``)
            c = self._doc_applied(tid, PROMPT_SET_FIELD, d.get("content_ref"), d.get("content_hmac"),
                                  self.optext_vault)
            ok = isinstance(c, dict) and isinstance(c.get("prompts"), list) and isinstance(c.get("brand_terms"), list)
            facts = d.get("facts") or {}
            v = dict(c) if ok else {"name": None, "brand_terms": [], "competitor_domains": [], "prompts": [],
                                    "engines": list(facts.get("engines") or [])}
            v.update(version=d["version"], sha256=d["content_hmac"], created_at=at)
            if not ok:
                v["vault_unavailable"] = True
            ps["versions"][str(d["version"])] = v
            ps["current"] = d["version"]
            return
        c = d["content"]                                   # a line written before sink job D (per-text stubs)
        cache: dict = {}
        prompts = [self._optext_open(tid, x, cache) for x in c["prompts"]]
        rules = [{**r, "context_terms": [self._optext_open(tid, x, cache) for x in r.get("context_terms") or []],
                  "negative_terms": [self._optext_open(tid, x, cache) for x in r.get("negative_terms") or []]}
                 for r in c.get("brand_term_rules") or []]
        texts = prompts + [w for r in rules for w in r["context_terms"] + r["negative_terms"]]
        v = {**c, "prompts": [p if p is not None else "" for p in prompts], "version": d["version"],
             "sha256": d["sha256"], "created_at": at, "plain_sha256": True}
        if "brand_term_rules" in c:
            v["brand_term_rules"] = [{**r, "context_terms": [w for w in r["context_terms"] if w is not None],
                                      "negative_terms": [w for w in r["negative_terms"] if w is not None]}
                                     for r in rules]
        if any(t is None for t in texts):
            v["vault_unavailable"] = True              # review c0c09bd1: the set's text cannot be read: never used
        ps["versions"][str(d["version"])] = v
        ps["current"] = d["version"]

    def prompt_set_view(self, tid: str, psid: str) -> dict:
        """The owner's view (review c0c09bd1): the free text decrypted for the owning tenant, shown through the
        client-view masker; a masked text is listed in ``masked`` (its index), never shown."""
        with self.lock:
            ps = self.prompt_sets.get(psid)
            if ps is None or ps["tenant_id"] != tid:
                raise NotFound(R("PROMPT_SET_NOT_FOUND"))
            versions = {}
            for k, v in ps["versions"].items():
                shown = [text_view(p) for p in v["prompts"]]
                out_v = {**{f: x for f, x in v.items() if f != "plain_sha256"}, "prompts": [x[0] for x in shown]}
                masked = [i for i, x in enumerate(shown) if x[1]]
                if v.get("brand_term_rules"):
                    out_v["brand_term_rules"] = [{**r, **{f: [text_view(w)[0] for w in r[f]]
                                                          for f in ("context_terms", "negative_terms")}}
                                                 for r in v["brand_term_rules"]]
                    if any(text_view(w)[1] for r in v["brand_term_rules"]
                           for f in ("context_terms", "negative_terms") for w in r[f]):
                        out_v["rules_masked"] = True
                if masked:
                    out_v["masked"] = masked
                versions[k] = out_v
            out = {"prompt_set_id": psid, "tenant_id": tid, "current": ps["current"], "versions": versions}
            cur = ps["versions"][str(ps["current"])]
            common = brand_mention.warnings(cur["brand_terms"], cur.get("brand_term_rules"))
            if common:                                 # round 3: shown on create and on every read, never silent
                out["warnings"] = [{"code": "BRAND_TERM_MAY_BE_COMMON_WORD", "term": t,
                                    "effect": "mentions count only when the answer confirms them (the domain, a "
                                              "client citation or another brand term); add context_terms, "
                                              "negative_terms or case_sensitive, or common_word: false"}
                                   for t in common]
            return out

    # ------------------------------------------------------------------ operator paths (review c0c09bd1)

    def _paths_hbody(self, tid: str, body: dict) -> dict:
        """The request body for idempotency and the log's request hash: each path as its keyed HMAC."""
        self._optext_key_check()
        return {**body, "paths": [self._optext_hmac(tid, "audit_path", p) for p in body["paths"]]}

    def _seal_paths(self, tid: str, scheme: str, domain: str, paths: list) -> list:
        """The paths in the tenant's vault (one record); a stub each, flagged with the strict URL verdict."""
        return self._optext_seal(tid, "audit_path", paths, [url_flag(f"{scheme}://{domain}{p}") for p in paths])

    @staticmethod
    def _report_paths_kept(report: dict, body: dict) -> dict:
        """The report as kept (log, ledger hash, memory): every flagged path (the strict URL verdict fails) and its full
        URL replaced, wherever they appear (keys and values), by the strict lane's kept form (``safe_url``)."""
        scheme, domain = body["scheme"], body["domain"]
        swap = {}
        for p in body["paths"]:
            full = f"{scheme}://{domain}{p}"
            if url_flag(full) is None:
                continue
            kept = secret_guard.safe_url(full)
            path = kept.split(domain, 1)[1] if kept.startswith(f"{scheme}://{domain}") else secret_guard.WITHHELD
            swap[full] = kept
            swap[p] = path or "/"
        if not swap:
            return report
        order = sorted(swap, key=len, reverse=True)

        def fix(x):
            if isinstance(x, str):
                for k in order:
                    if k in x:
                        x = x.replace(k, swap[k])
                return x
            if isinstance(x, dict):
                return {fix(k): fix(v) for k, v in x.items()}
            if isinstance(x, list):
                return [fix(v) for v in x]
            if isinstance(x, tuple):
                return [fix(v) for v in x]
            return x
        return fix(report)

    # ------------------------------------------------------------------ audits

    def request_audit(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        _check_paths(body["paths"], self.settings.audit_max_pages, body.get("scheme", "https"), body["domain"])
        with self.lock:
            self._gate()
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="audit", write=True)
            actor = "andre" if andre else actor
            rk = self.rk("audit", tid, body)
            hbody = self._paths_hbody(tid, body)          # review c0c09bd1: the paths' HMACs, never the paths
            prev = self._idem(actor, rk, hbody)
            if not prev:
                aid = derived_id("aud", tid, actor, rk)
                _authorize(self.tenants[tid], body, andre)
                self._vaulted_key_check()                 # sink job B: nowhere to keep the report: nothing is run
                plan = self._invoice_plan(tid, body, andre)
        if prev:
            return self.audit_view(tid, prev[1])          # the report is read from the vault outside the lock
        verification = self._verify_invoice(plan)         # Finance (31), outside the lock (W3-2)
        with self.lock:
            self._gate()
            self.check_kill(tenant=tid, capability="audit", write=True)
            prev = self._idem(actor, rk, hbody)
            if not prev:
                self._recheck_invoice(plan)
                ps = self._open_audit(actor, tid, rk, body, andre, aid, request=hbody, verification=verification)
        if prev:
            return self.audit_view(tid, prev[1])
        return self._execute_audit(aid, tid, body, ps)

    def _open_audit(self, actor: str, tid: str, rk: str, body: dict, andre: bool, aid: str,
                    request: Optional[dict] = None, schedule: Optional[dict] = None,
                    verification: Optional[dict] = None) -> Optional[dict]:
        """Under the lock: the authorisation rules, then ``audit_requested`` record-first. Returns the prompt set.
        ``verification`` is the invoice check made before (svc_invoices.py), recorded with the request."""
        t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
        _authorize(t, body, andre)
        self._vaulted_key_check()                           # sink job B: no vault key, no audit (nothing written)
        ps = None
        if body.get("prompt_set_id"):
            p = self.prompt_sets.get(body["prompt_set_id"])
            if p is None or p["tenant_id"] != tid:
                raise NotFound(R("PROMPT_SET_NOT_FOUND"))
            ps = {"prompt_set_id": p["prompt_set_id"], **p["versions"][str(p["current"])]}
            if ps.get("vault_unavailable"):
                raise Unavailable(R("VAULT_UNAVAILABLE"))   # review c0c09bd1: its text cannot be read: never used
        if any(a["tenant_id"] == tid and a["audit_id"] in self.running_audits for a in self.audits.values()):
            raise Conflict(R("AUDIT_RUNNING"))
        # review c0c09bd1: the log keeps the host (``domain``) and each path's vault stub (id, HMAC, strict flag)
        paths = self._seal_paths(tid, body["scheme"], body["domain"], list(body["paths"]))
        data = {"audit_id": aid, "tenant_id": tid, "tenant_kind": t["kind"], "domain": body["domain"],
                "scheme": body["scheme"], "paths": paths, "invoice_id": body.get("invoice_id"),
                "prompt_set": None if ps is None else {"prompt_set_id": ps["prompt_set_id"],
                                                       "version": ps["version"], "sha256": ps["sha256"]},
                "andre_approved": bool(andre), "invoice_verification": verification}
        if schedule is not None:
            data.update(schedule_id=schedule["schedule_id"], slot=schedule["slot"])
        ev = [("audit_requested", f"audit:{aid}",
               {"audit_id": aid, "tenant_id": tid, "kind": t["kind"], "domain_sha256": sha(body["domain"]),
                "paths_sha256": sha([x["hmac"] for x in paths]), "invoice_id": body.get("invoice_id"),
                "schedule_id": None if schedule is None else schedule["schedule_id"]}, (actor, rk))]
        if andre and schedule is None:
            ev.append(("audit_approved_by_andre", f"audit:{aid}",
                       {"audit_id": aid, "invoice_id": body.get("invoice_id")}, ("andre", rk)))
        ev += self._invoice_evidence(verification, f"audit:{aid}", {"audit_id": aid}, body.get("invoice_id"), actor,
                                     rk)
        line = self._req(data, actor, rk, request, aid) if request is not None else {**data, "actor": actor}
        try:
            self._commit("audit_requested", line, actor, evidence=ev)
        except BaseException:
            self._optext_orphaned()
            raise
        self._optext_committed()
        self.running_audits.add(aid)
        return ps

    def _execute_audit(self, aid: str, tid: str, body: dict, ps: Optional[dict], full: bool = True) -> dict:
        """The run and its completion record. Nothing heavy under the service lock (sink job B): the report is
        normalised, hashed (keyed) and encrypted OUTSIDE it; under it there is the kill-switch re-check, one vault
        append and the commit. The answer (the owner's view) is read back from the vault outside the lock."""
        try:
            sealed, failure, stop = None, None, None
            try:
                try:
                    report = self._run_audit(aid, tid, body, ps)
                except Exception as exc:  # noqa: BLE001 - a run that crashes is closed now, never left running
                    # War room (WR-F008..F010): whatever a crawled site serves, the run ends in a recorded state. A bug
                    # in an agent closes the audit as interrupted RUN_FAILED (the exception's type is logged, nothing
                    # else) instead of answering 500 and leaving it "running" until the interrupted-audits job.
                    # AEGIS SEO Wave 3 L2: which step failed and where (module:function:line inside this service),
                    # never the exception's message (it could quote crawled content)
                    agent = getattr(exc, "seo_agent", None) or "report"
                    where = where_code(exc)
                    log.error("audit run failed: %s in %s at %s", type(exc).__name__, agent, where)
                    failure = {"agent": agent, "where": where, "error_type": _error_type(exc)}
                else:
                    try:
                        # review c0c09bd1: a flagged path is never kept. Then the report as it reads back from the
                        # vault, its machine facts, its keyed HMAC and its encrypted lines (pending, unwritten)
                        report = normalised(self._report_paths_kept(report, body))
                        facts = report_facts(report)
                        gsc_ev = self.gsc_evidence(aid, report, lambda sec: self._vaulted_hmac(tid, SECTION_FIELD, sec))
                        sealed = self._vaulted_seal(tid, REPORT_FIELD, report)
                    except Exception as exc:  # noqa: BLE001 - the report cannot be stored: closed, nothing raw kept
                        log.error("audit report not stored: %s", type(exc).__name__)
                        failure = _vault_failure(exc)
                    finally:
                        report = None                    # the plaintext report is dropped from memory either way
                with self.lock:
                    self._gate()
                    if failure is None:
                        stop = self.kill_code(tenant=tid, capability="audit", write=True)
                    if failure is None and stop is None:
                        ev = [("audit_report_recorded", f"audit:{aid}",
                               {"audit_id": aid, "report_hmac": sealed.hmac, "outcome": facts["outcome"],
                                "findings": facts["findings"]}, ("report", aid))] + gsc_ev
                        data = {"audit_id": aid, "report_ref": sealed.ref, "report_hmac": sealed.hmac, "facts": facts}
                        try:
                            self._vaulted_commit(sealed, lambda: self._commit("audit_completed", data, "seo",
                                                                              evidence=ev))
                        except vault_mod.VaultError as exc:   # the blob write failed: nothing of it is kept
                            log.error("audit report not stored: %s", type(exc).__name__)
                            failure = _vault_failure(exc)
                    if failure is not None:
                        code = R("RUN_FAILED")
                        self._commit("audit_interrupted", {"audit_id": aid, "reason": code, "failure": failure},
                                     "seo", evidence=("audit_interrupted", f"audit:{aid}",
                                                      {"audit_id": aid, "reason": code, **failure},
                                                      ("interrupted", aid)))
                    elif stop is not None:
                        # AEGIS M3: a switch engaged during the run refuses the completion record. The run is closed
                        # as interrupted (a terminal bookkeeping record, not new work) and the report is discarded.
                        self._commit("audit_interrupted", {"audit_id": aid, "reason": stop}, "seo",
                                     evidence=("audit_interrupted", f"audit:{aid}", {"audit_id": aid, "reason": stop},
                                               ("interrupted", aid)))
            finally:
                if sealed is not None:
                    sealed.discard()                      # killed / failed: the encrypted lines are never written
            return self.audit_view(tid, aid, full=full)
        finally:
            with self.lock:
                self.running_audits.discard(aid)

    def _a_audit_requested(self, d, at):
        cache: dict = {}
        paths = [self._optext_open(d["tenant_id"], x, cache) for x in d["paths"]]
        self.audits[d["audit_id"]] = {**{k: v for k, v in d.items() if k not in ("actor", "request_id",
                                                                                  "request_sha", "_obj", "evidence")},
                                      "paths": [x for x in paths if x is not None],
                                      "status": "running", "requested_at": at, "requested_by": d["actor"],
                                      "completed_at": None, "report": None, "report_sha256": None,
                                      "report_hmac": None, "report_ref": None, "facts": None}
        if any(x is None for x in paths):
            self.audits[d["audit_id"]]["vault_unavailable"] = True
        if d.get("schedule_id"):
            self.schedules[d["schedule_id"]]["slots"][str(d["slot"])] = {"status": "requested",
                                                                         "audit_id": d["audit_id"], "at": at}

    def _a_audit_completed(self, d, at):
        """Sink job B: the record names the report's blob (``report_ref``), its keyed HMAC and the machine facts;
        memory keeps no report. A record written BEFORE sink job B carries the whole report and its plain SHA-256:
        it is kept as it was recorded (served through the same views), and its facts are derived here."""
        a = self.audits[d["audit_id"]]
        if "report" in d:
            a.update(status="completed", completed_at=at, report=d["report"], report_sha256=d["report_sha256"],
                     facts=report_facts(d["report"]))
        else:
            a.update(status="completed", completed_at=at, report_ref=d["report_ref"], report_hmac=d["report_hmac"],
                     facts=d["facts"])

    def _a_audit_interrupted(self, d, at):
        self.audits[d["audit_id"]].update(status="interrupted", completed_at=at,
                                          interrupted_reason=d.get("reason", "PROCESS_LOST"))
        if d.get("failure"):
            self.audits[d["audit_id"]]["failure"] = dict(d["failure"])

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
            s_env = _step(ctx, "selene", "crawlability", lambda: selene.run(ctx))
            if isinstance(s_env, tuple):
                s_env, pages = s_env
            envs.append(s_env)
            envs.append(_step(ctx, "delia", "sitemaps_and_llms_txt", lambda: delia.run(ctx))
                        if ctx.robots is not None else _skipped("delia", "sitemaps_and_llms_txt", s_env))
            envs.append(_step(ctx, "roman", "content_structure", lambda: roman.run(ctx, pages)))
        def entity_step():
            ent = self.entity_for_tenant(tid, raw=True)
            if entity_unreadable(ent):
                # sink job D: a canonical value that cannot be read from the vault is never compared (fail closed)
                return envelope("entity_check", "on_site_entity_consistency", "BLOCKED", [],
                                reason=R("VAULT_UNAVAILABLE"),
                                methodology="not run: the canonical entity record cannot be read from the vault")
            return entity_check.run(ctx, pages, ent)
        envs.append(_step(ctx, "entity_check", "on_site_entity_consistency", entity_step))
        envs.append(_step(ctx, "callum", "citation_sources",
                          lambda: probes.run_callum(self.ports.engines, ps, body["domain"],
                                                    self.settings.probe_samples, ctx.guard, o,
                                                    self.engine_budget(tid, aid, ps))))
        envs.append(_step(ctx, "naomi", "prompt_volume", lambda: probes.run_naomi(self.ports.prompt_volume, ps)))
        # Osei's first-party step: Search Console for the tenant's bound property (svc_gsc.py; NOT_CONNECTED
        # without a service-account key, and then no outbound call is made)
        gsc_box: dict = {}
        envs.append(_step(ctx, "osei", "search_performance", lambda: self._gsc_section(aid, tid, ctx.guard, gsc_box)))
        if envs[-1]["outcome"] in ("KILLED", "RESTRICTED") and "state" not in gsc_box:
            gsc_box["state"] = {"outcome": envs[-1]["outcome"], "reason": envs[-1]["reason"]}
        # Osei's data-hygiene step (AEGIS 1472041 M2): a restricted Osei reports nothing. The parsers' own fail-closed
        # quarantine of malformed input still applies (refusing bad input is not Osei's judgement).
        envs.append(_step(ctx, "osei", "data_hygiene", lambda: envelope(
            "osei", "data_hygiene", "OK", [], facts=o.summary(), limitations=[],
            methodology="every quarantined input with reason, size and SHA-256; every observation with its time and "
                        "refresh tier")))
        return self._report(aid, tid, body, ps, envs, o, started, pages, gsc_box)

    def _report(self, aid, tid, body, ps, envs, o, started, pages, gsc_box=None) -> dict:
        all_findings = [f for e in envs for f in e["findings"]]
        by: dict = {"severity": {}, "decision": {}, "data_class": {}, "capability": {}}
        for f in all_findings:
            for k in by:
                by[k][f[k]] = by[k].get(f[k], 0) + 1
        nc = sorted(set(self.ports.not_connected()) | {x for e in envs for x in e["not_connected"]})
        return {
            "report_version": REPORT_VERSION, "audit_id": aid, "tenant_id": tid, "domain": body["domain"],
            "scheme": body["scheme"], "paths": list(body["paths"]), "started_at": started,
            "finished_at": iso(self.now()), "outcome": overall_outcome(envs),
            "outcome_rule": OUTCOME_RULE,
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
            "legend": LEGEND,
            "data_hygiene": next((e["facts"] for e in envs if e["agent"] == "osei" and e["task"] == "data_hygiene"
                                  and e["outcome"] == "OK"), None),
            "search_performance": self.gsc_report_block(gsc_box or {}),
            "prompt_set": None if ps is None else {"prompt_set_id": ps["prompt_set_id"], "version": ps["version"],
                                                   "sha256": ps["sha256"]},
            "untrusted_content_rule": "every string quoted from a crawled page or an answer engine is data marked "
                                      "untrusted; none of it was acted on",
        }

    def audit_view(self, tid: str, aid: str, full: bool = True, masked: bool = True) -> dict:
        """The owner's view. ``full``: with the report, read from the tenant's vault OUTSIDE the lock (sink job B); a
        report that cannot be read (no key, a lost or changed blob) is ``None`` with ``vault_unavailable: true``.
        ``masked`` (the API): the report through the client-view masker; internal readers (client report, drift) take
        it as recorded."""
        with self.lock:
            a = self.audits.get(aid)
            if a is None or a["tenant_id"] != tid:
                raise NotFound(R("AUDIT_NOT_FOUND"))
            out = {k: v for k, v in a.items() if k not in ("report", "report_ref")}
            # review c0c09bd1: the owner's view of the audit config: each path through the client-view URL masker
            out["paths"] = [path_view(a["scheme"], a["domain"], x) for x in a["paths"]]
            if a["status"] == "running" and aid not in self.running_audits:
                out["status"] = "running_elsewhere_or_interrupted"
            inline, ref, mac, done = a["report"], a.get("report_ref"), a.get("report_hmac"), a.get("completed_at")
        if full:
            report = inline
            if inline is None and ref is not None:
                report = self._vaulted_open(tid, REPORT_FIELD, ref, mac)
                if report is None:
                    out["vault_unavailable"] = True       # never served from anywhere else; dependants refuse
            report = self.gsc_retained(report, done)
            out["report"] = owner_view(report) if masked and report is not None else report
        return out

    def _audit_recorded(self, tid: str, aid: str) -> dict:
        """An internal reader's view (client report, drift): the report as recorded, unmasked. Outside the lock. A
        completed audit whose report cannot be read refuses VAULT_UNAVAILABLE (never a silent ``None``)."""
        view = self.audit_view(tid, aid, masked=False)
        if view["status"] == "completed" and view["report"] is None:
            raise Unavailable(R("VAULT_UNAVAILABLE"))
        return view

    def drift_view(self, tid: str, aid: str, against: str) -> dict:
        """Search-truth drift between two completed audits of the same target (agents/drift.py), oldest first. Both
        reports are read from the tenant's vault outside the lock; one that cannot be read refuses VAULT_UNAVAILABLE."""
        from agents import drift
        with self.lock:
            if self.agent_blocked("osei"):                 # the comparison is Osei's (AEGIS 1472041 M2)
                raise Forbidden(R("AGENT_RESTRICTED"))
            x, y = self.audit_view(tid, aid, full=False), self.audit_view(tid, against, full=False)
            order = {k: i for i, k in enumerate(self.audits)}          # log order: the order they were requested
        if x["status"] != "completed" or y["status"] != "completed":
            raise Conflict(R("AUDIT_NOT_COMPLETED"))
        x, y = self._audit_recorded(tid, aid), self._audit_recorded(tid, against)
        a, b = sorted((x, y), key=lambda r: order[r["audit_id"]])
        try:
            return owner_view(drift.compare(a["report"], b["report"]))
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


# AEGIS gl-gsc M2 (ADR 0017 GSC-7): the Search Console section keeps its own outcome inside its section and is left
# out of the audit's overall outcome. A kill switch on provider:gsc, a spent read budget or a Google outage is about
# Google, not about the audited site, and must not relabel the whole audit KILLED / BLOCKED / FAILED.
OUTSIDE_OVERALL = frozenset({("osei", "search_performance")})
OUTCOME_RULE = ("the worst outcome over every agent's envelope except Osei's search_performance (Search Console), "
                "whose outcome stays inside its own section")


def overall_outcome(envs: list) -> str:
    return worst_outcome(e["outcome"] for e in envs if (e["agent"], e["task"]) not in OUTSIDE_OVERALL)


def _step(ctx, agent: str, task: str, fn):
    """The run guard before each agent (Wave 2, department manager): a restricted or retired agent does not run."""
    try:
        ctx.guard(agent=agent)
    except Killed as k:
        if k.code == "AGENT_RESTRICTED":
            return envelope(agent, task, "RESTRICTED", [], reason="AGENT_RESTRICTED",
                            methodology="not run: the department manager has restricted this agent")
        return envelope(agent, task, "KILLED", [], reason=k.code, methodology="not run: a kill switch is engaged")
    try:
        return fn()
    except Exception as exc:
        if getattr(exc, "seo_agent", None) is None:
            try:
                exc.seo_agent = agent            # L2: the step that failed, for the RUN_FAILED record
            except (AttributeError, TypeError):
                pass
        raise


_SRC = Path(__file__).resolve().parent
_AGENTS = frozenset({"selene", "delia", "roman", "entity_check", "callum", "naomi", "osei"})


WHERE_UNKNOWN = "unknown"


def where_code(exc: BaseException) -> str:
    """Sink job B: ``_where`` as a machine code for the log and the ledger. A frame name such as ``<lambda>`` /
    ``<listcomp>`` / ``<module>`` loses its angle brackets; a value the sink guard would still not take as a code
    (too long, or reading as a credential label) falls back to ``module:line``, then to ``unknown``."""
    raw = _where(exc).replace("<", "").replace(">", "")
    mod, _, rest = raw.partition(":")
    for cand in (raw, f"{mod}:{rest.rpartition(':')[2]}" if rest else raw):
        if sink_guard.classify(cand) == "code":
            return cand
    return WHERE_UNKNOWN


def _error_type(exc: BaseException) -> str:
    name = type(exc).__name__[:60]
    return name if sink_guard.classify(name) == "code" else "Exception"


def _vault_failure(exc: BaseException) -> dict:
    """The report could not be stored in the vault: machine codes only."""
    out = {"agent": "report", "where": "vault", "error_type": _error_type(exc)}
    if isinstance(exc, vault_mod.VaultUnavailable):
        out["code"] = R("VAULT_UNAVAILABLE")
    return out


def report_facts(report: dict) -> dict:
    """The machine facts of a report that the log keeps (sink job B): codes, counts and times. ``_apply``, the
    department scorecards and the non-owner surfaces read these; never a title, a URL, a note or a prompt. Tolerant
    of a missing part (a line written before sink job B is replayed through it)."""
    envs = [e for e in report.get("agents") or [] if isinstance(e, dict)]
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return {"report_version": report.get("report_version"), "outcome": report.get("outcome"),
            "findings": summary.get("findings", sum(len(e.get("findings") or []) for e in envs)),
            "started_at": report.get("started_at"), "finished_at": report.get("finished_at"),
            "agents": [{"agent": e.get("agent"), "task": e.get("task"), "outcome": e.get("outcome"),
                        "findings": len(e.get("findings") or [])} for e in envs]}


def _where(exc: BaseException) -> str:
    """``module:function:line`` of the innermost frame inside this service's source (no message, no values)."""
    best = "unknown"
    for fr in traceback.extract_tb(exc.__traceback__):
        try:
            rel = Path(fr.filename).resolve().relative_to(_SRC)
        except ValueError:
            continue
        mod = ".".join(rel.with_suffix("").parts)
        if re.fullmatch(r"[A-Za-z0-9_.]{1,80}", mod) and re.fullmatch(r"[A-Za-z0-9_<>]{1,80}", fr.name or ""):
            best = f"{mod}:{fr.name}:{fr.lineno}"
    return best


def _authorize(t: dict, body: dict, andre: bool) -> None:
    """Who may run what on whose site: the tenant's own registered domain; a client's run needs a Finance (31) invoice
    id AND Andre; an own-properties run refuses an invoice id. Checked before Finance is asked anything, so nobody
    but Andre can make this service look an invoice up (W3-2)."""
    if body["domain"] not in t["domains"]:
        raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
    if t["kind"] == "client":
        if not body.get("invoice_id"):
            raise Conflict(R("INVOICE_REQUIRED"))
        if not andre:
            raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
    elif body.get("invoice_id"):
        raise Invalid(R("INVOICE_NOT_FOR_OWN_TENANT"))


def _check_paths(paths: list, max_pages: int, scheme: str = "https", domain: str = "site.invalid") -> None:
    if len(paths) > max_pages:
        raise Invalid(R("AUDIT_TOO_LARGE"))
    if not paths or len(set(paths)) != len(paths) or any(
            not _PATH_OK.fullmatch(p) or p.startswith("//") or "#" in p or "\\" in p for p in paths):
        raise Invalid(R("PAGES_INVALID"))
    if any(path_refused(scheme, domain, p) for p in paths):
        raise Invalid(R("PATH_WITHHELD"))         # review c0c09bd1: only a key / PEM / user info / credential query


def _skipped(agent: str, task: str, selene_env: dict) -> dict:
    outcome = "KILLED" if selene_env["outcome"] == "KILLED" else "INSUFFICIENT_EVIDENCE"
    return envelope(agent, task, outcome, [], methodology="not run: robots.txt was not read",
                    reason=selene_env.get("reason") or "robots.txt not read")
