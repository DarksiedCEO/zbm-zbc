"""
The department manager (spec; ADR 0017 Wave 2, decision W2-4): work queues per agent, agent scorecards, and the agent
lifecycle active -> watch -> retrain -> restricted -> retired.

Scorecards are computed from what is recorded, never estimated: runs and outcomes from every completed audit report
and finished log ingest, findings produced, the NOT_CONNECTED and failure rates over those runs, and — from the
recorded drift reports of scheduled re-audits — findings CONFIRMED (the same finding, same severity and decision, in
the next run) and OVERTURNED (a finding that vanished for a reason that says it was never real: MEASUREMENT_ERROR or
SAMPLING_NOISE). A rate over zero runs is null, not zero.

Lifecycle: the dashboard moves an agent between active, watch and retrain; ANY move into or out of ``restricted``,
and into ``retired`` (terminal), is Andre's alone. Every move is recorded with its reason code and the SHA-256 of the
agent's scorecard at that moment. A restricted or retired agent is refused by the run guard: in an audit it reports
outcome RESTRICTED (it does not run; no findings), and its other work (Selene's log reports) is refused 403
AGENT_RESTRICTED. Restricting is a safety brake: it is allowed while the write switch is engaged.
"""

from __future__ import annotations

from envelope import sha
from errors import Conflict, Forbidden, Invalid, Unavailable
from reasons import R

AGENTS = ("selene", "delia", "roman", "entity_check", "callum", "naomi", "osei",
          # the Marcus index pipeline v1 (marcus-pipeline-spec/1): role ids; names pending (Q-MP-1)
          "cc_release_watcher", "cc_wat_fetcher", "cc_link_extractor", "cc_index_builder", "cc_graph_importer")
# The agent registry (ADR 0046, owner rule: every agent has a human name, ONE job, and an entry with its assignment,
# inputs and outputs). Shared plumbing (primitives/, answer_adapter.py and the engine adapters, the ports) is not an
# agent and is not listed. Shown on GET /seo/v1/department.
REGISTRY = {
    "selene": {"assignment": "crawlability: can search and AI crawlers reach and read the site",
               "inputs": ["the tenant's registered domain and paths", "robots.txt", "uploaded access logs"],
               "outputs": ["crawlability envelope (audit)", "log-ingest report"]},
    "delia": {"assignment": "sitemaps and llms.txt: discovery files present and valid",
              "inputs": ["robots.txt sitemap lines", "sitemap.xml", "llms.txt"],
              "outputs": ["sitemaps_and_llms_txt envelope (audit)"]},
    "roman": {"assignment": "content structure and machine readability (heuristic per-engine checklists)",
              "inputs": ["fetched pages", "callum.mnc_priority/2 (contract defined; Roman does not read it yet)"],
              "outputs": ["content_structure envelope (audit)"]},
    "entity_check": {"assignment": "the site's structured data / NAP against the canonical entity record",
                     "inputs": ["fetched pages", "the canonical entity record"],
                     "outputs": ["on_site_entity_consistency envelope (audit)"]},
    "callum": {"assignment": "citation sources: classify where and how AI answer engines cite the client",
               "inputs": ["a tenant prompt set (prompts, brand terms, competitor domains, engines)",
                          "the tenant's registered domain", "answers from the multi-engine adapter (ADR 0046)",
                          "the SSRF-safe fetcher, to resolve cited URLs (off unless SEO_CALLUM_RESOLVE_URLS=1)"],
               "outputs": ["citation_sources envelope (audit: per-prompt summary classes)",
                           "citation_source_rows (citation runs: STRENGTH_URL / CANONICAL_TO_CLIENT / OPPORTUNITY_URL / "
                           "LOOKALIKE_URL / MENTIONED_NOT_CITED / INCONCLUSIVE rows with evidence, metrics with "
                           "confidence)",
                           "callum.mnc_priority/2 (the Mentioned-Not-Cited list for Roman and Adaeze)"]},
    "naomi": {"assignment": "prompt volume with confidence bands",
              "inputs": ["a prompt-volume source (NOT_CONNECTED)"], "outputs": ["prompt_volume envelope (audit)"]},
    "osei": {"assignment": "data hygiene, first-party truth and search-truth drift",
             "inputs": ["every run's quarantines and observations", "Search Console (when bound)",
                        "two completed audits"],
             "outputs": ["data_hygiene and search_performance envelopes", "drift reports"]},
    # marcus-pipeline-spec/1 B-MP-04 / 15 / 27 / 36 / 43 (CB-1): the one job of each service is the spec's section 0
    # sentence; the role id stands until the owner names the service (Q-MP-1, ``name_pending``)
    "cc_release_watcher": {"assignment": "notice a new Common Crawl crawl or web-graph release and record it",
                           "inputs": ["the cc_releases port (v1: a fixture release list shaped as CC's published "
                                      "crawl list, GP-1)"],
                           "outputs": ["cc_release_seen records {crawl id, kind, published date}"]},
    "cc_wat_fetcher": {"assignment": "deliver the WAT files of one crawl (or one targeted subset) to the Link "
                                     "Extractor, verified",
                       "inputs": ["one recorded crawl release", "its wat.paths list or a targeted path list (path B)",
                                  "the cc_wat port (v1: the fixture directory SEO_MARCUS_FIXTURE_DIR, tests only)"],
                       "outputs": ["verified WAT record streams for the Link Extractor",
                                   "wat_file_done records {path hash, records, bytes}"]},
    "cc_link_extractor": {"assignment": "turn WAT records into normalised, personal-data-scrubbed link rows",
                          "inputs": ["WAT records (GP-2) delivered by the WAT Fetcher"],
                          "outputs": ["link rows {src, dst, anchor, path, rel}"]},
    "cc_index_builder": {"assignment": "build one snapshot: the link slices for the registered target domains, "
                                       "sharded, encrypted, manifested",
                         "inputs": ["link rows from the Link Extractor",
                                    "the registered domains of every tenant at build time",
                                    "the previous snapshot's slices (crawled_sources)"],
                         "outputs": ["marcus.index_snapshot/1 (manifest) and its encrypted shards"]},
    "cc_graph_importer": {"assignment": "import the host / domain graph rank files of one release",
                          "inputs": ["one recorded host_graph or domain_graph release (rank files, GP-3)",
                                     "the current snapshot's slices (the hosts and domains to keep)"],
                          "outputs": ["marcus.graph_ranks/1 (encrypted at rest)"]},
}
# Q-MP-1: the role id stands until the owner names the service; the naming state is shown BESIDE the registry block
# on GET /seo/v1/department (the registry block itself stays assignment / inputs / outputs, CB-1).
NAMES = {"cc_release_watcher": {"name_pending": True, "proposed_name": "Ruth"},
         "cc_wat_fetcher": {"name_pending": True, "proposed_name": "Felix"},
         "cc_link_extractor": {"name_pending": True, "proposed_name": "Esther"},
         "cc_index_builder": {"name_pending": True, "proposed_name": "Ivan"},
         "cc_graph_importer": {"name_pending": True, "proposed_name": "Gideon"}}
# Named in the spec, NOT built (no code, no registry entry until they are): Adaeze (content gap) is a consumer of
# callum.mnc_priority/2; Marcus, Priya, Theo, Zara, Idris, Vesper, Julian, Sable, Farrah (ADR 0017); the Marcus
# pipeline's v2 services (marcus-pipeline-spec/1 section 6: Link Verifier, Spam Scorer, RDAP Enricher).
NOT_BUILT_AGENTS = ("adaeze", "marcus", "priya", "theo", "zara", "idris", "vesper", "julian", "sable", "farrah",
                    "link_verifier", "spam_scorer", "rdap_enricher")
LINK_INDEX_AGENTS = AGENTS[7:]
STATES = ("active", "watch", "retrain", "restricted", "retired")
MOVES = {"active": ("watch", "restricted"), "watch": ("active", "retrain", "restricted"),
         "retrain": ("active", "watch", "restricted"), "restricted": ("active", "retrain", "retired"),
         "retired": ()}
ANDRE_ONLY = ("restricted", "retired")
REASONS = ("FAILURE_RATE", "OVERTURN_RATE", "NOT_CONNECTED_RATE", "QUALITY_REVIEW", "FOUNDER_DECISION",
           "RETRAINED", "RECOVERED")
BLOCKED_STATES = ("restricted", "retired")


class ManagerMixin:
    def agent_state(self, agent: str) -> str:
        return self.agent_states.get(agent, {}).get("state", "active")

    def agent_blocked(self, agent: str) -> bool:
        return self.agent_state(agent) in BLOCKED_STATES

    # ------------------------------------------------------------------ lifecycle

    def move_agent(self, agent: str, body: dict, andre: bool) -> dict:
        if agent not in AGENTS:
            raise Invalid(R("AGENT_UNKNOWN"))
        to = body["to"]
        if body["reason"] not in REASONS:
            raise Invalid(R("INVALID"), field="reason")
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            self._gate()
            actor = "andre" if andre else "dashboard"
            rk = self.rk("agent", agent, body)
            if self._idem(actor, rk, body):                 # AEGIS L1: a replay answers before any state check
                return self._agent_view(agent)
            cur = self.agent_state(agent)
            if body["expected_state"] != cur:
                raise Conflict(R("AGENT_STATE_STALE"))
            if to not in MOVES[cur]:
                raise Conflict(R("AGENT_MOVE_NOT_ALLOWED"))
            if (to in ANDRE_ONLY or cur == "restricted") and not andre:
                raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
            if to != "restricted":
                self.check_kill(write=True)
            card = self._scorecard(agent)
            data = {"agent": agent, "from": cur, "to": to, "reason": body["reason"], "scorecard_sha256": sha(card)}
            self._commit("agent_moved", self._req(data, actor, rk, body, agent), actor,
                         evidence=("agent_lifecycle", f"agent:{agent}", data, (actor, rk)))
        return self._agent_view(agent)

    def _agent_view(self, agent: str) -> dict:
        return self.department_view()["agents"][agent]

    def _a_agent_moved(self, d, at):
        st = self.agent_states.setdefault(d["agent"], {"state": "active", "history": []})
        st["state"] = d["to"]
        st["history"].append({"from": d["from"], "to": d["to"], "reason": d["reason"], "by": d["actor"], "at": at,
                              "scorecard_sha256": d["scorecard_sha256"]})

    # ------------------------------------------------------------------ scorecards and queues

    def _scorecard(self, agent: str) -> dict:
        runs, outcomes, produced = 0, {}, 0
        sp_runs, sp_outcomes = 0, {}
        for a in self.audits.values():
            if a["status"] != "completed":
                continue
            for e in a["facts"]["agents"]:                 # sink job B: the machine facts kept in the log
                if e["agent"] == agent and e.get("task") == "search_performance":
                    # Osei's first-party Search Console step is scored on its own (sub-card below), so the main card
                    # keeps meaning what it always did: Osei's data-hygiene runs
                    sp_runs += 1
                    sp_outcomes[e["outcome"]] = sp_outcomes.get(e["outcome"], 0) + 1
                    continue
                if e["agent"] == agent:
                    runs += 1
                    outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
                    produced += e["findings"]
        if agent == "selene":
            for g in self.log_ingests.values():
                if g["status"] == "finished":
                    # sink job D: the machine facts the log keeps (a line written before it carries the report)
                    if g.get("facts"):
                        outcome, n = g["facts"]["outcome"], g["facts"]["findings"]
                    elif g.get("report"):
                        outcome, n = g["report"]["envelope"]["outcome"], len(g["report"]["envelope"]["findings"])
                    else:
                        continue
                    runs += 1
                    outcomes[outcome] = outcomes.get(outcome, 0) + 1
                    produced += n
        if agent in LINK_INDEX_AGENTS:                 # the Marcus pipeline: its recorded jobs (svc_link_index.py)
            li = self.link_index_scorecard(agent)
            runs += li["runs"]
            for k, v in li["outcomes"].items():
                outcomes[k] = outcomes.get(k, 0) + v
        confirmed = overturned = drifts = 0
        for s in self.schedules.values():
            for d in s["drifts"]:
                drifts += 1
                confirmed += d["persisted_by_agent"].get(agent, 0)
                overturned += d["overturned_by_agent"].get(agent, 0)

        def rate(n):
            return None if runs == 0 else round(n / runs, 4)
        judged = confirmed + overturned
        return {"agent": agent, "runs": runs, "outcomes": dict(sorted(outcomes.items())),
                "findings_produced": produced, "findings_confirmed": confirmed, "findings_overturned": overturned,
                "overturn_rate": None if judged == 0 else round(overturned / judged, 4),
                "not_connected_rate": rate(outcomes.get("NOT_CONNECTED", 0)),
                "failure_rate": rate(outcomes.get("FAILED", 0)),
                "restricted_runs": outcomes.get("RESTRICTED", 0),
                "drift_reports_recorded": drifts if agent == "osei" else None,
                "search_performance": {"runs": sp_runs, "outcomes": dict(sorted(sp_outcomes.items())),
                                       "not_connected_rate": None if sp_runs == 0 else
                                       round(sp_outcomes.get("NOT_CONNECTED", 0) / sp_runs, 4)}
                if agent == "osei" else None,
                "engine_runs": self.engine_scorecard() if agent == "callum" else None,
                "citation_runs": self.citation_scorecard() if agent == "callum" else None,
                "basis": "completed audit reports, finished log ingests, recorded drift reports and (Callum) "
                         "recorded answer-engine reservations and settlements"}

    def _queues(self, agent: str) -> dict:
        running = len(self.running_audits)
        due = 0
        for s in self.schedules.values():
            if s["status"] == "active" and str(self._slot(s)) not in s["slots"]:
                due += 1
        q = {"audits_running": running if agent != "osei" else 0, "schedule_slots_due": due}
        if agent == "selene":
            q["log_ingests_open"] = sum(1 for g in self.log_ingests.values() if g["status"] == "open")
        return q

    def department_view(self) -> dict:
        with self.lock:
            return {"agents": {a: {"state": self.agent_state(a),
                                   "history": [dict(h) for h in self.agent_states.get(a, {}).get("history", [])],
                                   "queue": self._queues(a), "scorecard": self._scorecard(a),
                                   "registry": dict(REGISTRY[a]), **dict(NAMES.get(a, {}))} for a in AGENTS},
                    "not_built_agents": list(NOT_BUILT_AGENTS),
                    "lifecycle": {"states": list(STATES), "moves": {k: list(v) for k, v in MOVES.items()},
                                  "andre_only": "any move into or out of restricted, and into retired"}}
