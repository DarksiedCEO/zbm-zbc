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

AGENTS = ("selene", "delia", "roman", "entity_check", "callum", "naomi", "osei")
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
            cur = self.agent_state(agent)
            if body["expected_state"] != cur:
                raise Conflict(R("AGENT_STATE_STALE"))
            if to not in MOVES[cur]:
                raise Conflict(R("AGENT_MOVE_NOT_ALLOWED"))
            if (to in ANDRE_ONLY or cur == "restricted") and not andre:
                raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
            if to != "restricted":
                self.check_kill(write=True)
            actor = "andre" if andre else "dashboard"
            rk = self.rk("agent", agent, body)
            if self._idem(actor, rk, body):
                return self.department_view()["agents"][agent]
            card = self._scorecard(agent)
            data = {"agent": agent, "from": cur, "to": to, "reason": body["reason"], "scorecard_sha256": sha(card)}
            self._commit("agent_moved", self._req(data, actor, rk, body, agent), actor,
                         evidence=("agent_lifecycle", f"agent:{agent}", data, (actor, rk)))
        return self.department_view()["agents"][agent]

    def _a_agent_moved(self, d, at):
        st = self.agent_states.setdefault(d["agent"], {"state": "active", "history": []})
        st["state"] = d["to"]
        st["history"].append({"from": d["from"], "to": d["to"], "reason": d["reason"], "by": d["actor"], "at": at,
                              "scorecard_sha256": d["scorecard_sha256"]})

    # ------------------------------------------------------------------ scorecards and queues

    def _scorecard(self, agent: str) -> dict:
        runs, outcomes, produced = 0, {}, 0
        for a in self.audits.values():
            if a["status"] != "completed":
                continue
            for e in a["report"]["agents"]:
                if e["agent"] == agent:
                    runs += 1
                    outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
                    produced += len(e["findings"])
        if agent == "selene":
            for g in self.log_ingests.values():
                if g["status"] == "finished":
                    e = g["report"]["envelope"]
                    runs += 1
                    outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
                    produced += len(e["findings"])
        confirmed = overturned = drifts = 0
        for s in self.schedules.values():
            for d in s["drifts"]:
                drifts += 1
                confirmed += d["drift"]["persisted_by_agent"].get(agent, 0)
                overturned += d["drift"]["overturned_by_agent"].get(agent, 0)
        if agent == "osei":
            runs += drifts
            outcomes["OK"] = outcomes.get("OK", 0) + drifts

        def rate(n):
            return None if runs == 0 else round(n / runs, 4)
        judged = confirmed + overturned
        return {"agent": agent, "runs": runs, "outcomes": dict(sorted(outcomes.items())),
                "findings_produced": produced, "findings_confirmed": confirmed, "findings_overturned": overturned,
                "overturn_rate": None if judged == 0 else round(overturned / judged, 4),
                "not_connected_rate": rate(outcomes.get("NOT_CONNECTED", 0)),
                "failure_rate": rate(outcomes.get("FAILED", 0)),
                "restricted_runs": outcomes.get("RESTRICTED", 0),
                "basis": "completed audit reports, finished log ingests and recorded drift reports"}

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
                                   "queue": self._queues(a), "scorecard": self._scorecard(a)} for a in AGENTS},
                    "lifecycle": {"states": list(STATES), "moves": {k: list(v) for k, v in MOVES.items()},
                                  "andre_only": "any move into or out of restricted, and into retired"}}
