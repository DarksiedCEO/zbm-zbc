"""
Answer-engine runs: budgets, record-first reservations and settlement (ADR 0017 Wave 4, W4-2 / W4-4).

A METERED engine (a real adapter that costs money; Perplexity) is asked only inside a reservation recorded FIRST:

  engine_calls_reserved   before each prompt's requests: (run, prompt) -> calls reserved and their spend at the
                          configured reserve per call. Typed evidence on the ledger, then the log line, then memory
                          (``_commit``). Refused — nothing is asked — when a kill switch covers the engine, when the
                          global or the tenant's daily call or spend budget would be passed, when ANY of the four
                          budgets is 0 (0 means OFF: fail closed), or when the reservation cannot be recorded.
  engine_run_settled      after the engine's prompts: calls actually made, spend (the provider's reported cost where
                          it gave one, the reserve per call where it did not: a request whose cost is unknown is
                          charged in full), answers, the stop reason. Bookkeeping, recorded even under the write
                          switch (like an interruption record).

Budgets are per UTC day of the service clock. A run's usage is its settlement when there is one, otherwise the sum of
its reservations: a process that dies mid-run leaves its reservations charged (fail closed). Payloads carry ids,
counts, Decimal strings and hashes: never a prompt, an answer, a key or a URL.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from errors import Unavailable
from ledger import derived_id
from perplexity import EngineError
from reasons import R

ZERO = Decimal("0")
MICRO = Decimal("0.000001")


def _d(x) -> Decimal:
    return Decimal(str(x))


class _Meter:
    """One prompt's reservation: at most ``calls`` requests and at most their reserved spend."""

    def __init__(self, calls: int, per_call: Decimal):
        self.reserved_calls, self.per_call = calls, per_call
        self.reserved_spend = per_call * calls
        self.calls, self.known, self.unknown = 0, ZERO, 0

    def spent(self) -> Decimal:
        return self.known + self.per_call * self.unknown

    def before(self) -> None:
        if self.calls >= self.reserved_calls:
            raise EngineError("ENGINE_BUDGET_EXHAUSTED", "RESERVATION_USED")
        if self.spent() + self.per_call > self.reserved_spend:
            raise EngineError("ENGINE_BUDGET_EXHAUSTED", "RESERVATION_SPENT")
        self.calls += 1

    def after(self, cost: Optional[str]) -> None:
        if cost is None:
            self.unknown += 1
        else:
            self.known += _d(cost)


class _Run:
    def __init__(self, svc, tid: str, aid: str, engine: str, model: Optional[str], ps_sha: Optional[str]):
        self.svc, self.tid, self.aid, self.engine, self.model, self.ps_sha = svc, tid, aid, engine, model, ps_sha
        self.run_id = derived_id("egr", aid, engine)
        self.meters: list[_Meter] = []

    def reserve(self, prompt_index: int, calls: int) -> _Meter:
        meter = self.svc._engine_reserve(self, prompt_index, calls)
        self.meters.append(meter)
        return meter

    def settle(self, answers: int, stop: Optional[str]) -> dict:
        calls = sum(m.calls for m in self.meters)
        unknown = sum(m.unknown for m in self.meters)
        spend = sum((m.spent() for m in self.meters), ZERO).quantize(MICRO)
        usage = {"run_id": self.run_id, "calls": calls, "calls_cost_unknown": unknown, "spend_usd": str(spend),
                 "reserved_calls": sum(m.reserved_calls for m in self.meters),
                 "reserved_usd": str(sum((m.reserved_spend for m in self.meters), ZERO).quantize(MICRO)),
                 "answers": answers, "stop_reason": stop, "settled": False}
        if not self.meters:
            return usage
        try:
            self.svc._engine_settle(self, usage)
            usage["settled"] = True
        except Unavailable:
            pass                                   # the reservations stay charged (fail closed)
        return usage


class _Budget:
    def __init__(self, svc, tid: str, aid: str, ps_sha: Optional[str]):
        self.svc, self.tid, self.aid, self.ps_sha = svc, tid, aid, ps_sha

    def open(self, engine: str, model: Optional[str]) -> _Run:
        return _Run(self.svc, self.tid, self.aid, engine, model, self.ps_sha)


class EnginesMixin:
    METERED = ("perplexity",)

    def engine_budget(self, tid: str, aid: str, ps: Optional[dict]) -> _Budget:
        return _Budget(self, tid, aid, None if ps is None else ps.get("sha256"))

    def _engine_limits(self, engine: str) -> dict:
        if engine != "perplexity":
            return {"daily_calls": 0, "daily_spend_usd": "0", "tenant_daily_calls": 0, "tenant_daily_spend_usd": "0",
                    "reserve_usd_per_call": "0"}
        return dict(self.settings.perplexity_budget)

    def engine_usage(self, engine: str, day: str, tenant: Optional[str] = None) -> tuple[int, Decimal]:
        calls, spend = 0, ZERO
        for r in self.engine_runs.values():
            if r["engine"] != engine or r["day"] != day or (tenant is not None and r["tenant_id"] != tenant):
                continue
            if r["settled"] is not None:
                calls += r["settled"]["calls"]
                spend += _d(r["settled"]["spend_usd"])
            else:
                calls += sum(x["calls"] for x in r["reservations"].values())
                spend += sum((_d(x["spend_usd"]) for x in r["reservations"].values()), ZERO)
        return calls, spend

    def _engine_reserve(self, run: _Run, prompt_index: int, calls: int) -> _Meter:
        lim = self._engine_limits(run.engine)
        per_call = _d(lim["reserve_usd_per_call"])
        with self.lock:
            code = self.kill_code(tenant=run.tid, capability="ai_probe", provider=run.engine, write=True) \
                or self.kill_code(capability="audit")
            if code is not None:
                raise EngineError("ENGINE_KILLED", code)
            if self._closed or not self.verify_integrity()["ok"]:
                raise EngineError("ENGINE_BUDGET_EXHAUSTED", "RESERVATION_NOT_RECORDED")
            if min(lim["daily_calls"], lim["tenant_daily_calls"]) <= 0 or \
                    min(_d(lim["daily_spend_usd"]), _d(lim["tenant_daily_spend_usd"])) <= 0:
                raise EngineError("ENGINE_BUDGET_EXHAUSTED", "BUDGET_OFF")
            day = self.today()
            spend = (per_call * calls).quantize(MICRO)
            g_calls, g_spend = self.engine_usage(run.engine, day)
            t_calls, t_spend = self.engine_usage(run.engine, day, run.tid)
            if g_calls + calls > lim["daily_calls"] or g_spend + spend > _d(lim["daily_spend_usd"]):
                raise EngineError("ENGINE_BUDGET_EXHAUSTED", "GLOBAL_DAILY_BUDGET")
            if t_calls + calls > lim["tenant_daily_calls"] or t_spend + spend > _d(lim["tenant_daily_spend_usd"]):
                raise EngineError("ENGINE_BUDGET_EXHAUSTED", "TENANT_DAILY_BUDGET")
            data = {"run_id": run.run_id, "audit_id": run.aid, "tenant_id": run.tid, "engine": run.engine,
                    "model": run.model, "prompt_index": prompt_index, "calls": calls, "spend_usd": str(spend),
                    "day": day, "prompt_set_sha256": run.ps_sha}
            try:
                self._commit("engine_calls_reserved", data, "seo",
                             evidence=("engine_calls_reserved", f"audit:{run.aid}", dict(data),
                                       ("reserve", run.run_id, prompt_index)))
            except Unavailable:
                raise EngineError("ENGINE_BUDGET_EXHAUSTED", "RESERVATION_NOT_RECORDED") from None
        return _Meter(calls, per_call)

    def _engine_settle(self, run: _Run, usage: dict) -> None:
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            data = {"run_id": run.run_id, "audit_id": run.aid, "tenant_id": run.tid, "engine": run.engine,
                    "model": run.model, **{k: usage[k] for k in ("calls", "calls_cost_unknown", "spend_usd",
                                                                  "reserved_calls", "reserved_usd", "answers",
                                                                  "stop_reason")}}
            self._commit("engine_run_settled", data, "seo",
                         evidence=("engine_run_settled", f"audit:{run.aid}", dict(data), ("settle", run.run_id)))

    def _a_engine_calls_reserved(self, d, at):
        r = self.engine_runs.setdefault(d["run_id"], {"run_id": d["run_id"], "audit_id": d["audit_id"],
                                                      "tenant_id": d["tenant_id"], "engine": d["engine"],
                                                      "model": d["model"], "day": d["day"], "reservations": {},
                                                      "settled": None, "reserved_at": at})
        r["reservations"][str(d["prompt_index"])] = {"calls": d["calls"], "spend_usd": d["spend_usd"]}

    def _a_engine_run_settled(self, d, at):
        r = self.engine_runs.get(d["run_id"])
        if r is None:
            return
        r["settled"] = {k: d[k] for k in ("calls", "calls_cost_unknown", "spend_usd", "reserved_calls",
                                          "reserved_usd", "answers", "stop_reason")}
        r["settled_at"] = at

    # ------------------------------------------------------------------ views

    def engine_status(self) -> dict:
        """/status: each answer engine's adapter state (never the key), and for metered engines the budgets and
        today's usage."""
        out = self.ports.engine_details()
        day = self.today()
        for e in self.METERED:
            if e in out:
                calls, spend = self.engine_usage(e, day)
                lim = self._engine_limits(e)
                off = min(lim["daily_calls"], lim["tenant_daily_calls"]) <= 0 or \
                    min(_d(lim["daily_spend_usd"]), _d(lim["tenant_daily_spend_usd"])) <= 0
                out[e] = {**out[e], "budget": {**lim, "state": "OFF" if off else "on", "day": day,
                                               "used_calls": calls, "used_spend_usd": str(spend.quantize(MICRO))},
                          "killed": self.kill_code(capability="ai_probe", provider=e) is not None}
        return out

    def engine_scorecard(self) -> dict:
        """Callum's engine runs, from the recorded reservations and settlements only."""
        out: dict = {}
        for r in self.engine_runs.values():
            e = out.setdefault(r["engine"], {"runs": 0, "settled": 0, "unsettled": 0, "calls": 0, "answers": 0,
                                             "spend_usd": "0.000000", "stops": {}})
            e["runs"] += 1
            if r["settled"] is None:
                e["unsettled"] += 1
                continue
            s = r["settled"]
            e["settled"] += 1
            e["calls"] += s["calls"]
            e["answers"] += s["answers"]
            e["spend_usd"] = str((_d(e["spend_usd"]) + _d(s["spend_usd"])).quantize(MICRO))
            if s["stop_reason"]:
                e["stops"][s["stop_reason"]] = e["stops"].get(s["stop_reason"], 0) + 1
        return out
