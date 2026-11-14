"""
Farrah's monthly revenue attribution runs (farrah-spec/1; CLARIFICATIONS v1.1 GF-1..GF-5) and the tenant fields they
read (the plan and the revenue basis, GF-1; the lead-source channel map, GF-3).

  PUT  /seo/v1/tenants/{tid}/plan                       {plan, revenue_basis}          dashboard + Andre
  PUT  /seo/v1/tenants/{tid}/channel-map                {organic_search, ai_referral, other_known}  dashboard + Andre
  POST /seo/v1/tenants/{tid}/attribution-runs           {month: YYYY-MM}               dashboard, seo_agent, scheduler
  GET  /seo/v1/tenants/{tid}/attribution-runs[/{fid}]   dashboard, seo_agent, hub (own tenant): figure, band, label,
                                                        assumptions; compliance_38: machine facts, NO amounts;
                                                        finance_31: nothing

Rules:
  - the plan gate (B-FA-02): the tenant's plan (config tier ids ``managed_1/2/3`` = Entry / Growth / Full, GF-1)
    must be Growth or Full; Entry or no plan -> 403 PLAN_EXCLUDES_ATTRIBUTION, no run. ZBM's own tenant runs
    regardless of plan (Q-FA-4). The plan is re-read under the lock just before the result is committed: a plan
    downgraded mid-run refuses the run at commit (A-FA-03);
  - kill switches (CB-6, B-FA-05): global, write, the tenant, ``capability:audit`` and ``capability:attribution``,
    checked at the request, between steps and before the commit; engaged mid-run -> ``attribution_run_interrupted``
    with the switch's code, the result discarded. A restricted Farrah answers 403 AGENT_RESTRICTED (CB-2);
  - Farrah reads ONLY Osei's datasets, through ``self.osei_source`` (Osei's store). Osei full is not on the base, so
    in production the store is absent and every run is NOT_CONNECTED (GF-2); the conformance suite installs the
    fixture store. It is not a port: Farrah calls no port and spends nothing;
  - record-first (CB-7): ``attribution_run_requested`` (tenant, month, basis, input dataset ids) on the ledger and the
    log before anything is computed; ``attribution_run_recorded`` with ids, labels, row counts and the result's keyed
    HMAC; NO amount ever reaches the log or the ledger (Q-FA-5, W-FA-2): the result is sealed whole in the tenant's
    vault (``_vaulted_seal(tid, "revenue_attribution", doc)``) and read back only through ``svc_vaulted.owner_view``;
  - one running run per tenant (409 RUN_IN_PROGRESS); a replay of a request key answers the first run (CB-8); a month
    whose inputs are unchanged answers its current run; a month whose basis dataset (or another input) changed gets a
    new run that ``supersedes`` the old one (the old run stays, marked superseded, B-FA-29);
  - only a CLOSED month (its last day + 3 days of data lag before now, on the injected clock) -> else 422
    MONTH_NOT_CLOSED.
The amounts of a result are numbers this service computed (exact Decimal strings): ``secret_guard.guard_record``
sweeps every other field of the document, and ``owner_view`` masks every other field; the amounts are kept as the
numbers they are (guard_record keeps numbers; a 9+ digit amount string would otherwise read as a token, an open
question for the core, QB-FA-9).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import config
import vault as vault_mod
from agents import farrah, secret_guard
from clock import iso
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ledger import derived_id
from primitives import Killed
from reasons import R
from svc_vaulted import canonical, normalised, owner_view

log = logging.getLogger("seo.attribution")
RESULT_FIELD = "revenue_attribution"           # W-FA-2: the vault field of the result
MAP_FIELD = "channel_map"                      # GF-3: the tenant's declared lead_source mapping (operator text vault)
INPUTS_FIELD = "attribution_inputs"            # the keyed HMAC of what a run was computed from (supersede rule)
DATASET_FIELD = "dataset"                      # Osei's W-OS-2 seal field: the keyed HMAC its records carry
CAPABILITY = "attribution"
ATT = "att"


def _amount_paths(doc: dict) -> list:
    """The fields of a result that are this service's own machine values: the amounts (exact Decimal results), the
    run id (``ledger.derived_id``) and the input dataset ids (Osei's keyed ids, validated ``ds-<16 hex>``)."""
    out = [("totals", k) for k in sorted(farrah.TOTALS_KEYS)]
    out += [("channels", c, k) for c in farrah.ATTRIBUTED for k in ("figure", "lower", "upper")]
    out += [("run_id",), ("evidence_ref", "run_id"), ("inputs",)]
    return out


def split_amounts(doc: dict) -> tuple[dict, dict]:
    """(the document with its machine values taken out, those values by path). The amounts are this service's own
    exact Decimal results and the ids its own or Osei's validated ids; everything else is what the sweep and the
    masker judge. The machine values are checked here instead: a value of another shape refuses the document."""
    import copy
    skel = copy.deepcopy(doc)
    amounts = {}
    for p in _amount_paths(doc):
        node = skel
        for k in p[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict) and p[-1] in node:
            amounts[p] = node[p[-1]]
            node[p[-1]] = None
    for p, v in amounts.items():
        if not _machine(p, v):
            raise GuardWithheld("machine")
    return skel, amounts


_RUN_ID = re.compile(r"seo-att-[0-9a-f]{40}")
_DS_ID = re.compile(r"ds-[0-9a-f]{16}")
_AMOUNT_TEXT = re.compile(r"-?[0-9]{1,200}(\.[0-9]{1,200})?")


def _machine(path: tuple, v) -> bool:
    if path[-1] == "run_id":
        return isinstance(v, str) and bool(_RUN_ID.fullmatch(v))
    if path == ("inputs",):
        return isinstance(v, list) and len(v) <= 2 and all(isinstance(x, str) and _DS_ID.fullmatch(x) for x in v)
    return v is None or (isinstance(v, str) and bool(_AMOUNT_TEXT.fullmatch(v)))


def join_amounts(skel: dict, amounts: dict) -> dict:
    for p, v in amounts.items():
        node = skel
        for k in p[:-1]:
            node = node[k]
        node[p[-1]] = v
    return skel


def _hex(dataset_id: str) -> str:
    """A dataset id ``ds-<16 hex>`` as the log writes it: its keyed-HMAC hex part (sink class ``hash``)."""
    return dataset_id[3:]


class AttributionMixin:
    osei_source = None                         # Osei's dataset store (GF-2): absent on the base -> NOT_CONNECTED

    # ------------------------------------------------------------------ tenant plan and revenue basis (GF-1)

    def set_plan(self, tid: str, body: dict) -> dict:
        plan, basis = body["plan"], body["revenue_basis"]
        if plan not in config.PLAN_IDS or basis not in farrah.BASES:
            raise Invalid(R("INVALID"), field="plan")
        with self.lock:
            self._gate()
            self.check_kill(tenant=tid, write=True)
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            rk = self.rk("plan", tid, body)
            if self._idem("andre", rk, body):
                return self.tenant_view(tid)
            data = {"tenant_id": tid, "plan": plan, "revenue_basis": basis}
            self._commit("tenant_plan_set", self._req(data, "andre", rk, body, tid), "andre",
                         evidence=("tenant_plan_set", f"tenant:{tid}", data, ("andre", rk)))
            return self.tenant_view(tid)

    def _a_tenant_plan_set(self, d, at):
        t = self.tenants[d["tenant_id"]]
        t["plan"], t["revenue_basis"] = d["plan"], d["revenue_basis"]

    def plan_name(self, tid: str) -> Optional[str]:
        return config.PLAN_IDS.get((self.tenants.get(tid) or {}).get("plan"))

    # ------------------------------------------------------------------ the lead_source channel map (GF-3)

    def set_channel_map(self, tid: str, body: dict) -> dict:
        doc = normalised({"v": 1, **{c: list(body[c]) for c in ("organic_search", "ai_referral", "other_known")}})
        with self.lock:
            self._gate()
            self.check_kill(tenant=tid, write=True)
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self._optext_key_check()
        sealed = self._doc_seal(tid, MAP_FIELD, doc, self.optext_vault)
        try:
            with self.lock:
                self._gate()
                self.check_kill(tenant=tid, write=True)
                self._get(self.tenants, tid, "TENANT_NOT_FOUND")
                rk = self.rk("channel_map", tid, body)
                if self._idem("andre", rk, body):
                    return self.tenant_view(tid)
                counts = {c: len(doc[c]) for c in ("organic_search", "ai_referral", "other_known")}
                data = {"tenant_id": tid, "map_ref": sealed.ref, "map_hmac": sealed.hmac, "counts": counts}
                self._doc_commit(sealed, doc, lambda: self._commit(
                    "tenant_channel_map_set", self._req(data, "andre", rk, body, tid), "andre",
                    evidence=("tenant_channel_map_set", f"tenant:{tid}",
                              {"tenant_id": tid, "map_hmac": sealed.hmac, **counts}, ("andre", rk))))
                return self.tenant_view(tid)
        finally:
            sealed.discard()

    def _a_tenant_channel_map_set(self, d, at):
        t = self.tenants[d["tenant_id"]]
        doc = self._doc_applied(d["tenant_id"], MAP_FIELD, d["map_ref"], d["map_hmac"], self.optext_vault)
        t["channel_map_hmac"] = d["map_hmac"]
        t["channel_map_counts"] = dict(d["counts"])
        if doc is None:
            t["channel_map"], t["channel_map_unavailable"] = None, True
        else:
            t["channel_map"] = {c: list(doc[c]) for c in ("organic_search", "ai_referral", "other_known")}
            t["channel_map_unavailable"] = False

    # ------------------------------------------------------------------ runs

    def _attribution_rules(self, t: dict, tid: str) -> str:
        """Under the lock: kill switches, the run guard, the plan gate, the declared basis. Returns the basis."""
        self.check_kill(tenant=tid, capability=CAPABILITY, write=True)
        self.check_kill(capability="audit")
        if self.agent_blocked(farrah.AGENT):
            raise Forbidden(R("AGENT_RESTRICTED"))
        if not farrah.plan_allows(tid, self.plan_name(tid), self._own_tenant()):
            raise Forbidden(R("PLAN_EXCLUDES_ATTRIBUTION"))
        basis = t.get("revenue_basis")
        if basis not in farrah.BASES:
            raise Conflict(R("REVENUE_BASIS_UNSET"))
        if basis == "crm" and t.get("channel_map_unavailable"):
            raise Unavailable(R("VAULT_UNAVAILABLE"))
        return basis

    @staticmethod
    def _own_tenant() -> str:
        from svc_tenants import OWN_TENANT
        return OWN_TENANT

    def _osei_view(self, tid: str):
        """Osei's datasets, records, reconciliation and freshness for the tenant (outside the lock), or None when the
        store is absent (NOT_CONNECTED) or cannot answer."""
        src = self.osei_source
        if src is None:
            return None
        try:
            return src.view(tid)
        except Exception as exc:  # noqa: BLE001 - a store that cannot answer is not connected
            log.error("osei store unavailable: %s", type(exc).__name__)
            return None

    def _verify_dataset(self, tid: str, doc: dict, mac: str) -> bool:
        try:
            return self.vault.field_hash_matches(tid, DATASET_FIELD, canonical(doc), mac)
        except (TypeError, ValueError):
            return False

    def request_attribution_run(self, actor: str, tid: str, body: dict) -> dict:
        month = body["month"]
        farrah.month_bounds(month)
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            rk = self.rk("attribution_run", tid, body)
            prev = self._idem(actor, rk, body)
            if not prev:
                basis = self._attribution_rules(t, tid)
                if not self.vault.key_configured:
                    raise Unavailable(R("VAULT_UNAVAILABLE"))       # AC-10: refused before any input is read
        if prev:
            return self.attribution_run_view(tid, prev[1])
        view = self._osei_view(tid)                                  # outside the lock: Osei's store, never a port
        empty = {"basis": basis, "dataset": None, "covering": 0, "valid": 0, "refused": {}, "state": None,
                 "reconcile_difference": None, "other_dataset_id": None}
        sel = farrah.select(tid, month, basis, view, self._verify_dataset) if view is not None else empty
        tz = sel["dataset"]["window"]["timezone"] if sel["dataset"] is not None else "UTC"
        if not farrah.month_closed(month, self.now(), tz):
            raise Invalid(R("MONTH_NOT_CLOSED"), field="month")
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            prev = self._idem(actor, rk, body)
            if not prev:
                if self._attribution_rules(t, tid) != basis:
                    raise Conflict(R("REVENUE_BASIS_CHANGED"))
                if any(r["tenant_id"] == tid and r["run_id"] in self.running_attribution
                       for r in self.attribution_runs.values()):
                    raise Conflict(R("RUN_IN_PROGRESS"))
                sig = {**farrah.signature(sel), "connected": view is not None, "rules": farrah.RULES_VERSION,
                       "map": t.get("channel_map_hmac") if basis == "crm" else None}
                try:
                    sig_hmac = self.vault.field_hash(tid, INPUTS_FIELD, canonical(sig))[1]
                except vault_mod.VaultError:
                    raise Unavailable(R("VAULT_UNAVAILABLE")) from None
                cur = self._current_run(tid, month)
                if cur is not None and cur.get("inputs_hmac") == sig_hmac:
                    return self.attribution_run_view(tid, cur["run_id"])   # unchanged inputs: the current run
                fid = derived_id(ATT, tid, actor, rk)
                ids = [_hex(sel["dataset"]["dataset_id"])] if sel["dataset"] is not None else []
                first, _last = farrah.month_bounds(month)
                data = {"run_id": fid, "tenant_id": tid, "month_start": first.isoformat(), "basis": basis,
                        "input_dataset_hex": ids, "inputs_hmac": sig_hmac,
                        "supersedes": cur["run_id"] if cur is not None else None}
                ev = ("attribution_run_requested", f"attribution_run:{fid}",
                      {"run_id": fid, "tenant_id": tid, "month_start": data["month_start"], "basis": basis,
                       "input_dataset_hex": ids, "inputs_hmac": sig_hmac}, (actor, rk))
                self._commit("attribution_run_requested", self._req(data, actor, rk, body, fid), actor, evidence=ev)
                self.running_attribution.add(fid)
                channel_map = t.get("channel_map")
        if prev:
            return self.attribution_run_view(tid, prev[1])
        return self._execute_attribution(fid, tid, month, sel, channel_map, view is not None)

    def _current_run(self, tid: str, month: str) -> Optional[dict]:
        first, _ = farrah.month_bounds(month)
        runs = [r for r in self.attribution_runs.values() if r["tenant_id"] == tid and r["status"] == "completed"
                and r["month_start"] == first.isoformat() and r.get("superseded_by") is None]
        return max(runs, key=lambda r: (r["completed_at"], r["run_id"])) if runs else None

    def _execute_attribution(self, fid: str, tid: str, month: str, sel: dict, channel_map, connected: bool) -> dict:
        from svc_audits import _error_type, where_code
        guard = self.guard(tid)
        sealed = None
        try:
            try:
                guard(capability=CAPABILITY, agent=farrah.AGENT)
                produced_at = iso(self.now())
                doc, facts = farrah.compute(tid, fid, month, sel, channel_map, produced_at, connected)
                guard(capability=CAPABILITY, agent=farrah.AGENT)
                doc = self._attribution_prepare(doc)
                sealed = self._vaulted_seal(tid, RESULT_FIELD, doc)
                doc = None
                with self.lock:
                    self._gate()
                    stop = self.kill_code(tenant=tid, capability=CAPABILITY, write=True) or \
                        self.kill_code(capability="audit") or \
                        ("AGENT_RESTRICTED" if self.agent_blocked(farrah.AGENT) else None)
                    if stop is None and not farrah.plan_allows(tid, self.plan_name(tid), self._own_tenant()):
                        stop = "PLAN_EXCLUDES_ATTRIBUTION"       # A-FA-03: downgraded mid-run: refused at commit
                    if stop is None:
                        self._record_attribution(fid, tid, sealed, facts)
            except Killed as k:
                return self._interrupt_attribution(tid, fid, k.code)
            except Exception as exc:  # noqa: BLE001 - a run that crashes is closed now, never left running
                where = where_code(exc)
                log.error("attribution run failed: %s at %s", type(exc).__name__, where)
                failure = {"agent": farrah.AGENT, "where": where, "error_type": _error_type(exc)}
                if isinstance(exc, vault_mod.VaultUnavailable):
                    failure["code"] = R("VAULT_UNAVAILABLE")
                return self._interrupt_attribution(tid, fid, R("RUN_FAILED"), failure)
            if stop is not None:
                return self._interrupt_attribution(tid, fid, stop)
            return self.attribution_run_view(tid, fid)
        finally:
            if sealed is not None:
                sealed.discard()
            with self.lock:
                self.running_attribution.discard(fid)

    def _attribution_prepare(self, doc: dict) -> dict:
        """W-FA-2: the fail-closed sweep over every field of the document but its amounts (this service's own exact
        numbers). A field the sweep would withhold fails the run: a figure is never stored mangled or half."""
        if not farrah.validate_result(doc):
            raise ValueError("result shape")
        skel, amounts = split_amounts(doc)
        swept = secret_guard.guard_record(skel)
        if swept != skel:
            raise GuardWithheld("guard")
        return normalised(join_amounts(swept, amounts))

    def _record_attribution(self, fid: str, tid: str, sealed, facts: dict) -> None:
        """Under the lock: one vault append, then the commit. The log and the ledger take ids, labels, row counts,
        codes and the result's keyed HMAC: NO amount (W-FA-2, Q-FA-5)."""
        machine = {"outcome": facts["outcome"], "labels": facts["labels"], "rows": facts["rows"],
                   "unknown_rows": facts["unknown_rows"], "refund_rows": facts["refund_rows"],
                   "assumptions": facts["assumptions"], "input_dataset_hex": [_hex(x) for x in facts["inputs"]],
                   "limitations": facts["limitations"], "inputs_refused": facts["inputs_refused"]}
        data = {"run_id": fid, "result_ref": sealed.ref, "result_hmac": sealed.hmac, "facts": machine}
        ev = ("attribution_run_recorded", f"attribution_run:{fid}",
              {"run_id": fid, "tenant_id": tid, "result_hmac": sealed.hmac, "outcome": machine["outcome"],
               "labels": machine["labels"], "rows": machine["rows"]}, ("result", fid))
        self._vaulted_commit(sealed, lambda: self._commit("attribution_run_recorded", data, "seo", evidence=ev))

    def _interrupt_attribution(self, tid: str, fid: str, reason: str, failure: Optional[dict] = None) -> dict:
        with self.lock:
            self._gate()
            data = {"run_id": fid, "reason": reason}
            if failure:
                data["failure"] = failure
            self._commit("attribution_run_interrupted", data, "seo",
                         evidence=("attribution_run_interrupted", f"attribution_run:{fid}",
                                   {"run_id": fid, "reason": reason, **(failure or {})}, ("interrupted", fid)))
        return self.attribution_run_view(tid, fid)

    def mark_interrupted_attribution_runs(self) -> int:
        """CB-7: a run left ``running`` by a process that is gone (the ``interrupted-audits`` job)."""
        n = 0
        with self.lock:
            stale = [r["run_id"] for r in self.attribution_runs.values()
                     if r["status"] == "running" and r["run_id"] not in self.running_attribution]
            for fid in stale:
                self._gate()
                self._commit("attribution_run_interrupted", {"run_id": fid, "reason": "PROCESS_LOST"}, "scheduler",
                             evidence=("attribution_run_interrupted", f"attribution_run:{fid}",
                                       {"run_id": fid, "reason": "PROCESS_LOST"}, ("interrupted", fid)))
                n += 1
        return n

    # ------------------------------------------------------------------ replay

    def _a_attribution_run_requested(self, d, at):
        self.attribution_runs[d["run_id"]] = {
            "run_id": d["run_id"], "tenant_id": d["tenant_id"], "month_start": d["month_start"], "basis": d["basis"],
            "input_dataset_hex": list(d["input_dataset_hex"]), "inputs_hmac": d["inputs_hmac"],
            "supersedes": d.get("supersedes"), "superseded_by": None, "status": "running", "requested_at": at,
            "requested_by": d["actor"], "completed_at": None, "result_ref": None, "result_hmac": None, "facts": None}

    def _a_attribution_run_recorded(self, d, at):
        r = self.attribution_runs[d["run_id"]]
        r.update(status="completed", completed_at=at, result_ref=d["result_ref"], result_hmac=d["result_hmac"],
                 facts=d["facts"])
        old = self.attribution_runs.get(r.get("supersedes") or "")
        if old is not None:
            old["superseded_by"] = r["run_id"]

    def _a_attribution_run_interrupted(self, d, at):
        r = self.attribution_runs[d["run_id"]]
        r.update(status="interrupted", completed_at=at, interrupted_reason=d.get("reason", "PROCESS_LOST"))
        if d.get("failure"):
            r["failure"] = dict(d["failure"])

    # ------------------------------------------------------------------ views

    def _attribution_run(self, tid: str, fid: str) -> dict:
        r = self.attribution_runs.get(fid)
        if r is None or r["tenant_id"] != tid:                  # CB-10: another tenant's run = a missing one
            raise NotFound(R("ATTRIBUTION_RUN_NOT_FOUND"))
        return r

    def attribution_run_view(self, tid: str, fid: str, who: str = "dashboard") -> dict:
        """The owner's view (dashboard, seo_agent, hub own tenant: W-FA-3) with the result read from the vault
        outside the lock and passed through ``owner_view``; compliance_38 gets the machine facts only (labels,
        counts, codes, ids: no amount, D-FA-01)."""
        with self.lock:
            r = dict(self._attribution_run(tid, fid))
            if r["status"] == "running" and fid not in self.running_attribution:
                r["status"] = "running_elsewhere_or_interrupted"
        facts = r.get("facts") or {}
        out = {"run_id": fid, "tenant_id": tid, "month": r["month_start"][:7], "basis": r["basis"],
               "status": r["status"], "requested_at": r["requested_at"], "completed_at": r["completed_at"],
               "supersedes": r.get("supersedes"), "superseded_by": r.get("superseded_by"),
               "outcome": facts.get("outcome") if r["status"] == "completed" else
               ("KILLED" if str(r.get("interrupted_reason", "")).startswith("KILLED") else
                "BLOCKED" if r.get("interrupted_reason") == "PLAN_EXCLUDES_ATTRIBUTION" else
                "FAILED" if r["status"] == "interrupted" else None),
               "labels": facts.get("labels"), "rows": facts.get("rows"), "assumptions": facts.get("assumptions"),
               "inputs": ["ds-" + x for x in facts.get("input_dataset_hex", r["input_dataset_hex"])],
               "findings": [], "methodology": [farrah.RULES_VERSION, farrah.AI_REFERRERS_VERSION,
                                               "thresholds:heuristic:uncalibrated"],
               "limitations": list(facts.get("limitations") or []), "not_connected":
               farrah.NOT_CONNECTED in (facts.get("limitations") or []),
               "refresh_tier": farrah.REFRESH_TIER, "produced_at": r["completed_at"],
               "evidence_ref": {"run_id": fid, "contract": farrah.CONTRACT, "item_key": "channels"}}
        if r["status"] == "interrupted":
            out["interrupted_reason"] = r.get("interrupted_reason")
        if who == "compliance_38" or r["status"] != "completed":
            return out
        doc = self._vaulted_open(tid, RESULT_FIELD, r["result_ref"], r["result_hmac"])
        if doc is None or not farrah.validate_result(doc):
            out["result"], out["vault_unavailable"] = None, True
            return out
        skel, amounts = split_amounts(doc)
        out["result"] = join_amounts(owner_view(skel), amounts)
        out["display"] = {c: {k: farrah.display(doc["channels"][c][k]) for k in ("figure", "lower", "upper")}
                          for c in farrah.ATTRIBUTED}
        return out

    def attribution_runs_view(self, tid: str, who: str = "dashboard") -> list:
        with self.lock:
            fids = [r["run_id"] for r in sorted(self.attribution_runs.values(),
                                                 key=lambda r: (r["requested_at"], r["run_id"]))
                    if r["tenant_id"] == tid][:1000]
        return [self.attribution_run_view(tid, f, who="compliance_38" if who == "compliance_38" else "list")
                for f in fids]

    def attribution_scorecard(self) -> dict:
        """Farrah's runs from recorded runs only (CB-1): a rate over 0 runs is null."""
        out = {"runs": 0, "completed": 0, "interrupted": 0, "outcomes": {}}
        for r in self.attribution_runs.values():
            out["runs"] += 1
            if r["status"] == "completed":
                out["completed"] += 1
                o = (r.get("facts") or {}).get("outcome")
                out["outcomes"][o] = out["outcomes"].get(o, 0) + 1
            elif r["status"] == "interrupted":
                out["interrupted"] += 1
        out["outcomes"] = dict(sorted(out["outcomes"].items()))
        return out


class GuardWithheld(Exception):
    """The sweep would have withheld a field of a result: the run fails closed (RUN_FAILED)."""


def plan_view(t: dict) -> dict:
    """The plan fields of a tenant view (machine values only; the channel map's values never leave the vault)."""
    return {"plan": t.get("plan"), "plan_name": config.PLAN_IDS.get(t.get("plan")),
            "revenue_basis": t.get("revenue_basis"), "channel_map_counts": t.get("channel_map_counts")}

