"""
Idris's competitor visibility (ADR 0048): the tenant's competitor set, the observations Idris records from Callum's
citation runs, emerging-competitor dismissals, and competitor-visibility runs.

  POST /seo/v1/tenants/{tid}/competitor-sets                          dashboard / seo_agent: a new version
  GET  /seo/v1/tenants/{tid}/competitor-set                           + hub (own tenant), compliance_38
  POST /seo/v1/tenants/{tid}/competitor-set/dismissals                dashboard / seo_agent (owner), hub (the tenant)
  POST /seo/v1/tenants/{tid}/competitor-visibility-runs               dashboard / seo_agent (a fresh run of a client
                                                                      tenant: Andre, as for a citation run)
  GET  /seo/v1/tenants/{tid}/competitor-visibility-runs[/{vid}]       + hub (own tenant), compliance_38

Idris never asks an engine. While a citation run's answers are in memory (svc_citations.py), Idris's observer scores
them and the service records ``competitor_observations_recorded`` right after ``citation_run_recorded`` (only when
the tenant has a competitor set and Idris is not restricted; text is never stored). A competitor-visibility run then

  REUSES (no engine call) the most recent citation run of the SAME tenant that: completed (never an interrupted one);
  has Idris's observations; used the same domain, the same prompt set id, version and sha256, and the competitor set
  version now in force (same sha256: competitor names are matched while the text exists, so a new set needs new
  answers); covered every requested engine (a request may narrow, never widen; the observations are narrowed to the
  requested engines); and completed within ``max_age_minutes`` (default and maximum 60 = Callum's near-real-time
  refresh interval; 0 = always fresh);

  otherwise asks for a FRESH citation run with the request's own request_id and every one of Callum's rules (domain,
  prompt set, Andre + Finance + Compliance for a client tenant, one running run per tenant, budgets, kill switches),
  so one collection serves both agents. A crash between the citation run and the visibility record is recovered by
  retrying: the retry finds the just-completed run and reuses it.

A run whose citation run was interrupted, or has no observations, is recorded with no numbers (an honest outcome,
never zeros). Tenant isolation: another tenant's objects answer the same 404 as missing ones.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from agents import domain_match, idris
from clock import iso, parse_iso
from envelope import envelope, sha
from errors import Conflict, Forbidden, Invalid, NotFound
from ledger import derived_id
from reasons import R

log = logging.getLogger("seo.competitors")
LOOKBACK = timedelta(days=30)                   # earlier observations considered for emerging-competitor recurrence


class CompetitorsMixin:
    # ------------------------------------------------------------------ the competitor set (versioned)

    def create_competitor_set(self, actor: str, tid: str, body: dict) -> dict:
        content = self._competitor_content(body)
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, capability="prompt_sets", write=True)
            rk = self.rk("competitor_set", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.competitor_set_view(tid)
            self._not_the_client(t, tid, content)
            cur = self.competitor_sets.get(tid)
            csha = sha(content)
            if cur is not None and cur["versions"][str(cur["current"])]["sha256"] == csha:
                raise Conflict(R("COMPETITOR_SET_EXISTS"))
            version = 1 if cur is None else cur["current"] + 1
            data = {"tenant_id": tid, "version": version, "content": content, "sha256": csha}
            self._commit("competitor_set_version", self._req(data, actor, rk, body, tid), actor,
                         evidence=("competitor_set_version", f"competitor_set:{tid}",
                                   {"tenant_id": tid, "version": version, "sha256": csha,
                                    "competitors": len(content["competitors"])}, (actor, rk)))
            return self.competitor_set_view(tid)

    @staticmethod
    def _competitor_content(body: dict) -> dict:
        if len(body["competitors"]) > idris.MAX_TRACKED:
            raise Invalid(R("COMPETITOR_LIMIT"))
        out, names, terms_seen = [], set(), set()
        for c in body["competitors"]:
            name = " ".join(c["name"].split())
            terms = [" ".join(x.split()) for x in (c.get("terms") or [name])]
            if not name or any(not x or any(ord(ch) < 0x20 for ch in x) for x in [name] + terms) \
                    or idris.compact(name) in names or len(set(terms)) != len(terms) \
                    or any(idris.compact(x) in terms_seen or len(idris.compact(x)) < 2 for x in terms):
                raise Invalid(R("COMPETITOR_SET_INVALID"))
            names.add(idris.compact(name))
            terms_seen |= {idris.compact(x) for x in terms}
            # review round 1 (HIGH): a term containing another competitor's term ("Jungle Scout" / "Scout") would let
            # one span count for two brands: refused
            if any(idris.overlapping(x, y) for o in out for y in o["terms"] for x in terms):
                raise Invalid(R("COMPETITOR_TERMS_OVERLAP"))
            rules = []
            for r in c.get("term_rules") or []:
                term = " ".join(r["term"].split())
                ctx = [" ".join(w.split()) for w in r.get("context_terms") or []]
                neg = [" ".join(w.split()) for w in r.get("negative_terms") or []]
                if term not in terms or term in {x["term"] for x in rules} \
                        or any(not w or any(ord(ch) < 0x20 for ch in w) for w in ctx + neg):
                    raise Invalid(R("COMPETITOR_SET_INVALID"))
                rules.append({"term": term, "case_sensitive": bool(r.get("case_sensitive", False)),
                              "context_terms": ctx, "negative_terms": neg, "common_word": r.get("common_word")})
            domains = sorted({d.lower().rstrip(".") for d in c.get("domains") or []})
            if any(domain_match._ascii(d) is None or domain_match.is_public_suffix(d) for d in domains):
                raise Invalid(R("COMPETITOR_SET_INVALID"))
            out.append({"competitor_id": idris.competitor_id(name), "name": name, "terms": terms,
                        "term_rules": sorted(rules, key=lambda x: terms.index(x["term"])), "domains": domains})
        exclude = sorted({" ".join(x.split()) for x in body.get("exclude_terms") or [] if x.strip()})
        return {"competitors": out, "exclude_terms": exclude, "emerging_detection": bool(body["emerging_detection"])}

    def _not_the_client(self, t: dict, tid: str, content: dict) -> None:
        """Under the lock: no tracked competitor may be the client — its domain (or a lookalike of it, or a parent of
        it) or its brand terms (or a lookalike of them) as configured in the tenant's prompt sets."""
        client_terms = sorted({x for p in self.prompt_sets.values() if p["tenant_id"] == tid
                               for x in p["versions"][str(p["current"])]["brand_terms"]})
        if not client_terms:                      # review round 1: nothing to tell the client from a competitor by
            raise Conflict(R("CLIENT_BRAND_TERMS_REQUIRED"))
        labels = {idris.compact(r.split(".")[0]) for r in (idris.registrable(td) for td in t["domains"]) if r}
        for c in content["competitors"]:
            for d in c["domains"]:
                for td in t["domains"]:
                    if domain_match.match(d, td) is not None or domain_match.on_site(td, d):
                        raise Invalid(R("COMPETITOR_IS_CLIENT"))
            for x in c["terms"] + [c["name"]]:
                if any(idris.client_like(x, client_terms, lb) for lb in labels or {None}):
                    raise Invalid(R("COMPETITOR_IS_CLIENT"))
                if any(idris.overlapping(x, ct) for ct in client_terms):
                    raise Invalid(R("COMPETITOR_TERMS_OVERLAP"))

    def _a_competitor_set_version(self, d, at):
        cs = self.competitor_sets.setdefault(d["tenant_id"], {"tenant_id": d["tenant_id"], "versions": {}})
        cs["versions"][str(d["version"])] = {**d["content"], "version": d["version"], "sha256": d["sha256"],
                                             "created_at": at}
        cs["current"] = d["version"]

    def current_competitor_set(self, tid: str) -> Optional[dict]:
        cs = self.competitor_sets.get(tid)
        return None if cs is None else cs["versions"][str(cs["current"])]

    def competitor_set_view(self, tid: str) -> dict:
        with self.lock:
            cs = self.competitor_sets.get(tid)
            if cs is None:
                raise NotFound(R("COMPETITOR_SET_NOT_FOUND"))
            cur = cs["versions"][str(cs["current"])]
            out = {"tenant_id": tid, "current": cs["current"], "versions": {k: dict(v) for k, v in
                                                                            cs["versions"].items()},
                   "limits": {"tracked_max": idris.MAX_TRACKED, "emerging_folded_max": idris.MAX_EMERGING_FOLDED},
                   "dismissals": [{"key": k, **v} for k, v in sorted(self.emerging_dismissals.get(tid, {}).items())],
                   "confirmations": [{"key": k, **v} for k, v in
                                     sorted(self.emerging_confirmations.get(tid, {}).items())]}
            warns = idris.set_warnings(cur)
            if warns:
                out["warnings"] = warns
            return out

    # ------------------------------------------------------------------ observations (from a citation run)

    def idris_observer(self, tid: str, domain: str, ps: dict, cid: str) -> Optional[idris.Observer]:
        """Under the lock: Idris's observer for a citation run, or None (no competitor set, or Idris restricted)."""
        try:
            cs = self.current_competitor_set(tid)
            if cs is None or self.agent_blocked("idris"):
                return None
            return idris.Observer(cs, ps, domain, cid, ner=self._idris_ner())
        except Exception as exc:  # noqa: BLE001 - review round 1: Idris can never change Callum's run
            log.error("idris observer not built: %s", type(exc).__name__)
            return None

    def _idris_ner(self):
        """The optional NER port (agents/ner.py), loaded once; None when SEO_IDRIS_NER is off. A port the tests
        install (``self.ports.idris_ner``) wins."""
        port = getattr(self.ports, "idris_ner", None)
        if port is not None:
            return port
        if not getattr(self.settings, "idris_ner", False):
            return None
        return self.settings.idris_ner_port          # loaded and run once at start (config.load): never lazily

    def record_observations(self, tid: str, cid: str, observer: Optional[idris.Observer]) -> None:
        """Under the lock, right after ``citation_run_recorded``: Idris's observations (or its failure). Never raises:
        Callum's response and outcome are the same whatever happens here (review round 1); a run whose observations
        were not recorded is simply not reusable."""
        try:
            self._record_observations(tid, cid, observer)
        except Exception as exc:  # noqa: BLE001
            log.error("idris observations not recorded: %s", type(exc).__name__)

    def _record_observations(self, tid: str, cid: str, observer: Optional[idris.Observer]) -> None:
        if observer is None or (observer.result is None and observer.error is None):
            return
        data = {"citation_run_id": cid, "tenant_id": tid}
        if observer.error is not None:
            from svc_audits import _where
            where = _where(observer.error)
            log.error("idris observation failed: %s at %s", type(observer.error).__name__, where)
            data["failed"] = {"agent": "idris", "where": where, "error_type": type(observer.error).__name__[:60]}
            osha = sha(data["failed"])
        else:
            data["observations"] = observer.result
            osha = sha(observer.result)
        data["observations_sha256"] = osha
        obs = observer.result or {}
        self._commit("competitor_observations_recorded", data, "seo",
                     evidence=("competitor_observations_recorded", f"citation_run:{cid}",
                               {"citation_run_id": cid, "observations_sha256": osha, "failed": "failed" in data,
                                "scopes": len(obs.get("tallies") or []),
                                "candidates": len(obs.get("candidates") or [])}, ("idris_obs", cid)))

    def _a_competitor_observations_recorded(self, d, at):
        self.competitor_observations[d["citation_run_id"]] = {
            "tenant_id": d["tenant_id"], "recorded_at": at, "observations": d.get("observations"),
            "failed": d.get("failed"), "observations_sha256": d["observations_sha256"]}

    # ------------------------------------------------------------------ dismissals

    def dismiss_candidate(self, actor: str, tid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, write=True)
            rk = self.rk("dismiss", tid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self._dismissal_view(tid, body["key"])
            key = body["key"]
            if not idris.candidate_key_valid(key) or not self._seen(tid, key):
                raise NotFound(R("CANDIDATE_NOT_FOUND"))
            if key in self.emerging_dismissals.get(tid, {}):
                raise Conflict(R("CANDIDATE_ALREADY_DISMISSED"))
            data = {"tenant_id": tid, "key": key, "reason": body["reason"], "by": actor,
                    "name": self._candidate_name(tid, key)}
            self._commit("emerging_competitor_dismissed", self._req(data, actor, rk, body, key), actor,
                         evidence=("emerging_competitor_dismissed", f"competitor_set:{tid}",
                                   {"tenant_id": tid, "key_sha256": sha(key), "reason": body["reason"], "by": actor},
                                   (actor, rk)))
            return self._dismissal_view(tid, key)

    def undismiss_candidate(self, actor: str, tid: str, body: dict) -> dict:
        """The owner (dashboard only) withdraws a dismissal."""
        with self.lock:
            self._gate()
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, write=True)
            rk = self.rk("undismiss", tid, body)
            if self._idem(actor, rk, body):
                return self._dismissal_view(tid, body["key"])
            if body["key"] not in self.emerging_dismissals.get(tid, {}):
                raise NotFound(R("CANDIDATE_NOT_FOUND"))
            data = {"tenant_id": tid, "key": body["key"], "by": actor}
            self._commit("emerging_competitor_undismissed", self._req(data, actor, rk, body, body["key"]), actor,
                         evidence=("emerging_competitor_undismissed", f"competitor_set:{tid}",
                                   {"tenant_id": tid, "key_sha256": sha(body["key"]), "by": actor}, (actor, rk)))
            return self._dismissal_view(tid, body["key"])

    def _a_emerging_competitor_undismissed(self, d, at):
        self.emerging_dismissals.get(d["tenant_id"], {}).pop(d["key"], None)

    def confirm_candidate(self, actor: str, tid: str, body: dict) -> dict:
        """The owner (dashboard, seo_agent) or the tenant (hub) confirms an emerging candidate: it is promoted into the
        shares (review round 2, spec clarification: auto-listing stays presence-only until confirmed)."""
        with self.lock:
            self._gate()
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.check_kill(tenant=tid, write=True)
            rk = self.rk("confirm", tid, body)
            if self._idem(actor, rk, body):
                return self._confirmation_view(tid, body["key"])
            key = body["key"]
            if not idris.candidate_key_valid(key) or not self._seen(tid, key):
                raise NotFound(R("CANDIDATE_NOT_FOUND"))
            if idris.DismissalIndex(self.emerging_dismissals.get(tid, {})).covers(key, self._candidate_name(tid, key)):
                raise Conflict(R("CANDIDATE_ALREADY_DISMISSED"))
            if key in self.emerging_confirmations.get(tid, {}):
                raise Conflict(R("CANDIDATE_ALREADY_CONFIRMED"))
            data = {"tenant_id": tid, "key": key, "by": actor, "name": self._candidate_name(tid, key)}
            self._commit("emerging_competitor_confirmed", self._req(data, actor, rk, body, key), actor,
                         evidence=("emerging_competitor_confirmed", f"competitor_set:{tid}",
                                   {"tenant_id": tid, "key_sha256": sha(key), "by": actor}, (actor, rk)))
            return self._confirmation_view(tid, key)

    def _a_emerging_competitor_confirmed(self, d, at):
        self.emerging_confirmations.setdefault(d["tenant_id"], {})[d["key"]] = {"by": d["by"], "at": at,
                                                                               "name": d.get("name")}

    def _confirmation_view(self, tid: str, key: str) -> dict:
        return {"tenant_id": tid, "key": key,
                "confirmed": dict(self.emerging_confirmations.get(tid, {}).get(key) or {})}

    def _candidate_name(self, tid: str, key: str) -> Optional[str]:
        """The most frequent display form of a candidate key in the tenant's observations."""
        counts: dict = {}
        for o in self.competitor_observations.values():
            if o["tenant_id"] != tid:
                continue
            for c in (o["observations"] or {}).get("candidates") or []:
                if c["key"] == key:
                    for d, n in c["display"].items():
                        counts[d] = counts.get(d, 0) + n
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0][:80] if counts else None

    def _seen(self, tid: str, key: str) -> bool:
        for o in self.competitor_observations.values():
            if o["tenant_id"] == tid and any(c["key"] == key for c in (o["observations"] or {}).get("candidates")
                                             or []):
                return True
        return False

    def _dismissal_view(self, tid: str, key: str) -> dict:
        return {"tenant_id": tid, "key": key, "dismissed": dict(self.emerging_dismissals.get(tid, {}).get(key) or {})}

    def _a_emerging_competitor_dismissed(self, d, at):
        self.emerging_dismissals.setdefault(d["tenant_id"], {})[d["key"]] = {"reason": d["reason"], "by": d["by"],
                                                                            "at": at, "name": d.get("name")}
        self.emerging_confirmations.get(d["tenant_id"], {}).pop(d["key"], None)   # a dismissal ends a confirmation

    # ------------------------------------------------------------------ competitor-visibility runs

    def request_visibility_run(self, actor: str, tid: str, body: dict, andre: bool) -> dict:
        # review round 1: the visibility record names the real caller; Andre's approval (when presented) is passed on
        # to a fresh citation run, which records it as Callum's rules say, and noted as ``andre_presented``
        eff = actor
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            rk = self.rk("competitor_visibility", tid, body)
            prev = self._idem(eff, rk, body)
            if prev:
                return self.visibility_run_view(tid, prev[1])
            self._visibility_rules(t, tid, body)
            cid = self._reusable(tid, body)
        source = "reused" if cid is not None else "fresh"
        if cid is None:
            cbody = {"request_id": body["request_id"], "domain": body["domain"],
                     "prompt_set_id": body["prompt_set_id"]}
            if body.get("engines"):
                cbody["engines"] = body["engines"]
            cid = self.request_citation_run(actor, tid, cbody, andre)["citation_run_id"]   # Callum's rules apply
        classifier = self._idris_classify(tid, cid, derived_id("vis", tid, eff, rk), body)    # optional, outside lock
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            prev = self._idem(eff, rk, body)
            if prev:
                return self.visibility_run_view(tid, prev[1])
            self._visibility_rules(t, tid, body)
            return self._record_visibility(eff, rk, body, tid, cid, source, andre, classifier)

    def _idris_classify(self, tid: str, cid: str, vid: str, body: dict) -> dict:
        """The optional classifier (ADR 0048 round 2): OFF unless SEO_IDRIS_CLASSIFIER_ENGINE names an engine; one
        question per visibility run through the adapter, inside a recorded reservation (``visibility_run:<vid>``),
        the engine's budgets binding; its answer can only REMOVE candidates; any failure removes nothing."""
        eng = getattr(self.settings, "idris_classifier_engine", None)
        if not eng:
            return {"state": "OFF"}
        from agents.prompt_guard import personal_data
        from answer_adapter import EngineError
        from primitives import Killed
        with self.lock:
            o = self.competitor_observations.get(cid)
            p = self.prompt_sets.get(body["prompt_set_id"])
            ps = None if p is None else p["versions"][str(p["current"])]
            obs = None if o is None or o["observations"] is None else o["observations"]
        if obs is None:
            return {"state": "NOT_RUN", "engine": eng, "reason": "NO_OBSERVATIONS"}
        cands = sorted(obs.get("candidates") or [], key=lambda c: (-c["answers"], c["key"]))
        cands = cands[:idris.CLASSIFIER_MAX_NAMES]
        if not cands:
            return {"state": "NOT_RUN", "engine": eng, "reason": "NO_CANDIDATES"}
        port = self.ports.engines.get(eng)
        if port is None or not getattr(port, "connected", False):
            return {"state": "NOT_CONNECTED", "engine": eng}
        names = []
        for i, c in enumerate(cands):
            dom = sorted(c["domain_hints"], key=lambda h: (-c["domain_hints"][h], h))[:1]
            snip = next(((e.get("excerpt") or {}).get("text") for e in c.get("evidence") or [] if e.get("excerpt")),
                        None)
            names.append((i, idris._best(c["display"]) or (dom[0] if dom else c["key"]), dom[0] if dom else None,
                          snip))
        prompt = idris.classifier_prompt(names)
        if personal_data(prompt) is not None:
            return {"state": "NOT_RUN", "engine": eng, "reason": "PERSONAL_DATA"}
        guard = self.guard(tid)
        run = meter = None
        answers, stop = 0, None
        try:
            guard(capability="ai_probe", provider=eng, agent="idris")
            if getattr(port, "metered", False):
                run = self.engine_budget(tid, vid, ps, subject="visibility_run").open(eng, getattr(port, "model", None))
                meter = run.reserve(0, int(getattr(port, "attempts", 1) or 1))
            a = port.ask(prompt, guard=guard, meter=meter) if getattr(port, "accepts_guard", False) else port.ask(prompt)
            if getattr(a, "status", None) != "ANSWER":
                stop = "ENGINE_UNAVAILABLE"
                return {"state": "FAILED", "engine": eng, "reason": "NO_ANSWER"}
            answers = 1
            got = idris.parse_classifier(a.text, {i for i, *_r in names})
            if got is None:
                return {"state": "FAILED", "engine": eng, "reason": "REPLY_NOT_UNDERSTOOD",
                        "model": getattr(a, "model", None)}
            labels = {cands[i]["key"]: c for i, c in got.items()}
            return {"state": "OK", "engine": eng, "model": getattr(a, "model", None),
                    "model_version": getattr(a, "model_version", None), "asked": len(names),
                    "labels": dict(sorted(labels.items())),
                    "not_brand_keys": sorted(k for k, c in labels.items() if c != "brand"),
                    "filtered": sum(1 for c in labels.values() if c != "brand"),
                    "note": "the classifier can only remove candidates; it never adds or promotes one"}
        except Killed as k:
            stop = "ENGINE_KILLED"
            return {"state": "KILLED", "engine": eng, "reason": k.code}
        except EngineError as e:
            stop = e.code
            return {"state": "BLOCKED" if e.code == "ENGINE_BUDGET_EXHAUSTED" else "FAILED", "engine": eng,
                    "reason": e.code, "reason_detail": e.detail}
        except Exception as exc:  # noqa: BLE001 - fail closed: nothing filtered
            stop = "ENGINE_UNAVAILABLE"
            return {"state": "FAILED", "engine": eng, "reason": "ADAPTER_FAILED", "error_type": type(exc).__name__}
        finally:
            if run is not None:
                run.settle(answers, stop)

    def _visibility_rules(self, t: dict, tid: str, body: dict) -> None:
        self.check_kill(tenant=tid, capability="ai_probe", write=True)
        if self.agent_blocked("idris"):
            raise Forbidden(R("AGENT_RESTRICTED"))
        if body["domain"] not in t["domains"]:
            raise Forbidden(R("DOMAIN_NOT_AUTHORIZED"))
        p = self.prompt_sets.get(body["prompt_set_id"])
        if p is None or p["tenant_id"] != tid:
            raise NotFound(R("PROMPT_SET_NOT_FOUND"))
        if self.current_competitor_set(tid) is None:
            raise NotFound(R("COMPETITOR_SET_NOT_FOUND"))
        if body.get("engines") and not set(body["engines"]) & set(p["versions"][str(p["current"])]["engines"]):
            raise Invalid(R("INVALID"), field="engines")

    def _wanted_engines(self, body: dict) -> set:
        p = self.prompt_sets[body["prompt_set_id"]]
        engines = set(p["versions"][str(p["current"])]["engines"])
        return engines & set(body["engines"]) if body.get("engines") else engines

    def _reusable(self, tid: str, body: dict) -> Optional[str]:
        """The reuse rules (module docstring): the citation run to reuse, or None."""
        if body.get("max_age_minutes", 60) <= 0:
            return None
        p = self.prompt_sets[body["prompt_set_id"]]
        cur = p["versions"][str(p["current"])]
        cs = self.current_competitor_set(tid)
        wanted = self._wanted_engines(body)
        floor = self.now() - timedelta(minutes=body.get("max_age_minutes", 60))
        best = None
        for cid, r in self.citation_runs.items():
            o = self.competitor_observations.get(cid)
            if r["tenant_id"] != tid or r["status"] != "completed" or o is None or o["observations"] is None:
                continue
            ref = r.get("prompt_set") or {}
            if r["domain"] != body["domain"] or ref.get("prompt_set_id") != body["prompt_set_id"] \
                    or ref.get("version") != cur["version"] or ref.get("sha256") != cur["sha256"]:
                continue
            if o["observations"]["competitor_set"].get("sha256") != cs["sha256"] or not wanted <= set(r["engines"]):
                continue
            done = parse_iso(o["observations"]["observed_at"])     # when the answers were observed (round 1)
            if done < floor:
                continue
            if best is None or (done, cid) > best[0]:
                best = ((done, cid), cid)
        return None if best is None else best[1]

    def _record_visibility(self, actor: str, rk: str, body: dict, tid: str, cid: str, source: str,
                           andre: bool = False, classifier: Optional[dict] = None) -> dict:
        """Under the lock: compute Idris's envelope from the citation run's observations and record it."""
        vid = derived_id("vis", tid, actor, rk)
        r = self.citation_runs[cid]
        o = self.competitor_observations.get(cid)
        now = iso(self.now())
        if r["status"] != "completed" or o is None or o["observations"] is None:
            why = r.get("interrupted_reason") if r["status"] != "completed" else \
                ("IDRIS_OBSERVATION_FAILED" if o is not None and o.get("failed") else "NO_OBSERVATIONS")
            outcome = "KILLED" if str(why).startswith("KILLED") else "FAILED" if r["status"] != "completed" \
                else "INSUFFICIENT_EVIDENCE"
            env = envelope(idris.AGENT, idris.TASK, outcome, [], methodology="Idris reads Callum's citation-run "
                           "answers; this run's answers could not be read, so no share of voice is reported.",
                           limitations=idris.LIMITS, reason=why,
                           facts={"visibility_run_id": vid, "citation_run_id": cid, "source": source,
                                  "rules": idris.VERSION})
        else:
            obs = idris.narrow(o["observations"], self._wanted_engines(body))
            at = parse_iso(obs["observed_at"])
            history = [x["observations"] for c2, x in sorted(self.competitor_observations.items())
                       if c2 != cid and x["tenant_id"] == tid and x["observations"] is not None
                       and x["observations"]["domain"] == obs["domain"]
                       and at - LOOKBACK <= parse_iso(x["observations"]["observed_at"]) <= at]
            env = idris.compute(obs, history, self.emerging_dismissals.get(tid, {}), self._previous(tid, obs, cid),
                                {"outcome": (r["result"] or {}).get("outcome")}, vid, now, source,
                                self.emerging_confirmations.get(tid, {}), classifier)
        rsha = sha(env)
        facts = env.get("facts") or {}
        data = {"visibility_run_id": vid, "tenant_id": tid, "domain": body["domain"],
                "prompt_set_id": body["prompt_set_id"], "citation_run_id": cid, "source": source,
                "andre_presented": bool(andre),
                "result": env, "result_sha256": rsha}
        self._commit("competitor_visibility_recorded", self._req(data, actor, rk, body, vid), actor,
                     evidence=("competitor_visibility_recorded", f"visibility_run:{vid}",
                               {"visibility_run_id": vid, "tenant_id": tid, "citation_run_id": cid, "source": source,
                                "result_sha256": rsha, "outcome": env["outcome"],
                                "emerging": len((facts.get("emerging") or {}).get("items") or []),
                                "opportunities": len((facts.get("opportunities") or {}).get("items") or [])},
                               (actor, rk)))
        return self.visibility_run_view(tid, vid)

    def _previous(self, tid: str, obs: dict, cid: str) -> Optional[dict]:
        """The previous comparable visibility result: same tenant, domain and prompt set version, an earlier
        citation run's answers."""
        best = None
        at = obs["observed_at"]
        for v in self.visibility_runs.values():
            f = (v["result"] or {}).get("facts") or {}
            if v["tenant_id"] != tid or v["citation_run_id"] == cid or "benchmark" not in f:
                continue
            if f.get("domain") != obs["domain"] or f.get("prompt_set") != obs["prompt_set"] \
                    or not f.get("observed_at") or f["observed_at"] >= at:
                continue
            key = (f["observed_at"], v["recorded_at"], v["visibility_run_id"])
            if best is None or key > best[0]:
                best = (key, f)
        return None if best is None else best[1]

    def _a_competitor_visibility_recorded(self, d, at):
        self.visibility_runs[d["visibility_run_id"]] = {
            **{k: v for k, v in d.items() if k not in ("actor", "request_id", "request_sha", "_obj", "evidence")},
            "requested_by": d["actor"], "recorded_at": at}

    def visibility_run_view(self, tid: str, vid: str, full: bool = True) -> dict:
        with self.lock:
            v = self.visibility_runs.get(vid)
            if v is None or v["tenant_id"] != tid:
                raise NotFound(R("VISIBILITY_RUN_NOT_FOUND"))
            out = {k: x for k, x in v.items() if k != "result"}
            if full:
                out["result"] = v["result"]
            else:
                out["outcome"] = v["result"]["outcome"]
            return out

    def visibility_runs_view(self, tid: str) -> list:
        with self.lock:
            return [self.visibility_run_view(tid, v["visibility_run_id"], full=False)
                    for v in sorted(self.visibility_runs.values(), key=lambda v: (v["recorded_at"],
                                                                                  v["visibility_run_id"]))
                    if v["tenant_id"] == tid][:1000]

    def visibility_scorecard(self) -> dict:
        """Idris's runs, from recorded runs only (the department manager's scorecard)."""
        out = {"runs": 0, "reused": 0, "fresh": 0, "outcomes": {}}
        for v in self.visibility_runs.values():
            out["runs"] += 1
            out[v["source"]] += 1
            o = v["result"]["outcome"]
            out["outcomes"][o] = out["outcomes"].get(o, 0) + 1
        out["outcomes"] = dict(sorted(out["outcomes"].items()))
        return out
