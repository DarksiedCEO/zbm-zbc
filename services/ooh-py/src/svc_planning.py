"""
OOH-3 (ADR 0021): planning and pricing — the client's brief, rate-card cost estimates, the plan view, the plan
approval guards, and markup evidence.

Rules enforced here, each with its own reason code and test:

* A campaign's units are planned against the client's brief: budget cap, formats, flight window, brief document.
  No unit is added before the brief (BRIEF_REQUIRED); a unit's format must be one the brief asks for
  (FORMAT_NOT_IN_BRIEF) and its flight must lie inside the brief's window (FLIGHT_OUTSIDE_BRIEF). The brief is
  written only while planning (BRIEF_LOCKED after the plan is approved; a change goes through CHANGE_REQUESTED).
* A cost estimate comes only from the face's sourced rate card (amount and period), prorated by day to the unit's
  flight, class ESTIMATED, naming the rate-card sources. A face with no sourced rate card has no estimate
  (RATE_CARD_UNKNOWN): nothing is filled in.
* Each plan line's cost basis is its current vendor quote, else its latest estimate, else UNKNOWN. The plan totals
  say how much of the total is quoted, estimated and unknown, and whether it fits the brief's budget (UNKNOWN when
  any line is unknown). A plan is approved only with no UNKNOWN line and within the budget cap.
* A deal's markup is set from recorded evidence: the commercial model's ``markup_basis_ref`` must name a markup
  evidence record of this campaign (MARKUP_BASIS_UNKNOWN). 15% stays a suggestion that is never applied.
* The plan view is for preparers and Compliance only: it carries vendor cost, so the hub never reads it.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import policy as P
from errors import Conflict, Invalid
from ledger import derived_id
from reasons import R


class PlanningMixin:
    # ================================================================================================ the brief

    def set_brief(self, actor: str, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("brief", cid, body)
            if self._idem(actor, rk, body):
                return self.campaign_view(tid, cid)
            self.check_kill(tenant=tid, capability="campaigns")
            if c["state"] not in P.BRIEF_STATES:
                raise Conflict(R("BRIEF_LOCKED"))
            self._expect(c, body)
            if body["currency"] not in P.CURRENCIES:
                raise Invalid(R("CURRENCY_UNSUPPORTED"))
            if Decimal(body["budget_cap"]) <= 0:
                raise Invalid(R("INVALID"), field="budget_cap")
            if len(set(body["formats"])) != len(body["formats"]):
                raise Invalid(R("INVALID"), field="formats")
            try:
                ws, we = date.fromisoformat(body["flight_window_start"]), date.fromisoformat(body["flight_window_end"])
            except ValueError:
                raise Invalid(R("FLIGHT_INVALID")) from None
            if ws > we or (we - ws).days + 1 > P.BRIEF_WINDOW_MAX_DAYS:
                raise Invalid(R("FLIGHT_INVALID"))
            window = [ws.isoformat(), we.isoformat()]
            # AEGIS OOH-3/4 source F-1: a new brief must still hold every live unit (else the plan would hold units
            # outside it); withdraw or move a unit first
            probe = {"formats": sorted(body["formats"]), "flight_window": window}
            for uid in c["inventory_ids"]:
                u = self.inventory[uid]
                if u["state"] not in ("WITHDRAWN", "UNAVAILABLE"):
                    self._fits(probe, u["format"], u["flight"])
            data = {"campaign_id": cid, "brief": {"budget_cap": body["budget_cap"], "currency": body["currency"],
                                                  "formats": sorted(body["formats"]), "flight_window": window,
                                                  "brief_doc_ref": body["brief_doc_ref"]}}
            self._commit("brief_set", self._req(data, actor, rk, body, cid), actor,
                         evidence=self._ev("campaign_brief_set", cid, data, actor, rk))
            return self.campaign_view(tid, cid)

    def _a_brief_set(self, d, at):
        c = self.campaigns[d["campaign_id"]]
        c["brief"] = {**d["brief"], "set_by": d["actor"], "set_at": at}
        c["version"] += 1

    def check_brief_fit(self, c: dict, fmt: str, flight: Optional[list]) -> None:
        """A unit fits the brief: the brief exists, names its format, and its window holds the flight."""
        b = c.get("brief")
        if b is None:
            raise Conflict(R("BRIEF_REQUIRED"))
        self._fits(b, fmt, flight)

    @staticmethod
    def _fits(b: dict, fmt: str, flight: Optional[list]) -> None:
        if fmt not in b["formats"]:
            raise Conflict(R("FORMAT_NOT_IN_BRIEF"))
        w = b["flight_window"]
        if flight is not None and (flight[0] < w[0] or flight[1] > w[1]):
            raise Conflict(R("FLIGHT_OUTSIDE_BRIEF"))

    # ================================================================================================ estimates

    def _current_attr(self, face: dict, name: str) -> Optional[dict]:
        e = face["attributes"].get(name)
        return next((h for h in e["history"] if h["current"]), None) if e else None

    def estimate_from_rate_card(self, actor: str, tid: str, cid: str, uid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            u = self._unit(cid, uid)
            rk = self.rk("estimate", uid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.price_view(prev[1])
            self.check_kill(tenant=tid, capability="prices")
            self._writable(c, P.BRIEF_STATES + ("PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED"))
            if u["flight"] is None:
                raise Conflict(R("FLIGHT_REQUIRED"))
            face = self.faces[u["face_id"]]
            amt, per = self._current_attr(face, "rate_card_amount"), self._current_attr(face, "rate_card_period_days")
            if amt is None or per is None:
                raise Conflict(R("RATE_CARD_UNKNOWN"))               # no sourced rate card: no estimate
            if amt["source_id"] != per["source_id"]:
                raise Conflict(R("RATE_CARD_UNKNOWN"))   # red team F6: amount and period come from ONE rate card
            days = (date.fromisoformat(u["flight"][1]) - date.fromisoformat(u["flight"][0])).days + 1
            cost = (Decimal(amt["value"]) * Decimal(days) / Decimal(per["value"])).quantize(P.CENT,
                                                                                            rounding=ROUND_HALF_UP)
            pid = self._fresh(self.prices, derived_id("prc", tid, actor, rk))
            data = {"price_id": pid, "campaign_id": cid, "tenant_id": tid, "price_type": "VENDOR_COST_ESTIMATE",
                    "amount": P.fmt(cost), "currency": "USD", "vendor": u["vendor"], "inventory_ids": [uid],
                    "quote_refs": [], "evidence_class": "ESTIMATED", "valid_until": None, "source_ref": None,
                    "vendor_text": None, "method": body["method"], "flight": list(u["flight"]), "flight_days": days,
                    "rate_card_amount": amt["value"], "rate_card_period_days": per["value"],
                    "rate_card_source_ids": sorted({amt["source_id"], per["source_id"]}),
                    "rate_card_as_of": max(amt["as_of"], per["as_of"])}
            ev = {k: data[k] for k in ("price_id", "price_type", "amount", "currency", "vendor", "inventory_ids",
                                       "evidence_class", "method", "flight", "flight_days", "rate_card_amount",
                                       "rate_card_period_days", "rate_card_source_ids")}
            self._commit("price_recorded", self._req(data, actor, rk, body, pid), actor,
                         evidence=self._ev("rate_card_estimate_recorded", cid, ev, actor, rk))
            return self.price_view(pid)

    # ================================================================================================ the plan view

    def _line_cost(self, c: dict, u: dict) -> dict:
        """The unit's cost basis: its current quote (not retired, for its flight), else its latest estimate for its
        flight, else UNKNOWN."""
        quotes, estimates = [], []
        for pid in c["price_ids"]:
            p = self.prices[pid]
            if u["unit_id"] not in p["inventory_ids"]:
                continue
            if p["price_type"] == "VENDOR_QUOTE" and not p.get("retired") \
                    and p.get("flights", {}).get(u["unit_id"]) == u["flight"] and not self._expired(p["valid_until"]):
                quotes.append(p)
            elif p["price_type"] == "VENDOR_COST_ESTIMATE" and p.get("method") in P.ESTIMATE_METHODS \
                    and p.get("flight") == u["flight"] and len(p["inventory_ids"]) == 1 \
                    and p.get("rate_card_source_ids") == self._current_rate_card(u):   # current rate-card ones only
                estimates.append(p)
        if quotes:
            q = quotes[-1]
            share = Decimal(q["amount"]) / Decimal(len(q["inventory_ids"]))     # a multi-unit quote, split evenly
            exact = len(q["inventory_ids"]) == 1
            return {"basis": "VENDOR_QUOTE", "price_id": q["price_id"], "amount": P.fmt(share),
                    "evidence_class": q["evidence_class"], "split_evenly_from_multi_unit_quote": not exact}
        if estimates:
            e = estimates[-1]
            return {"basis": "VENDOR_COST_ESTIMATE", "price_id": e["price_id"], "amount": e["amount"],
                    "evidence_class": e["evidence_class"], "split_evenly_from_multi_unit_quote": False}
        return {"basis": "UNKNOWN", "price_id": None, "amount": None, "evidence_class": "UNKNOWN",
                "split_evenly_from_multi_unit_quote": False}

    def _current_rate_card(self, u: dict) -> Optional[list]:
        """The source ids of the face's current rate card (red team F6: an estimate from a replaced card is stale)."""
        face = self.faces.get(u.get("face_id") or "")
        if face is None:
            return None
        amt, per = self._current_attr(face, "rate_card_amount"), self._current_attr(face, "rate_card_period_days")
        return sorted({amt["source_id"], per["source_id"]}) if amt and per else None

    def plan(self, c: dict) -> dict:
        lines, by_vendor, counted = [], {}, set()
        quoted = estimated = Decimal(0)
        unknown = 0
        for uid in c["inventory_ids"]:
            u = self.inventory[uid]
            if u["state"] in ("WITHDRAWN", "UNAVAILABLE"):
                continue
            cost = self._line_cost(c, u)
            lines.append({"unit_id": uid, "face_id": u["face_id"], "vendor": u["vendor"], "format": u["format"],
                          "market": u["market"], "flight": u["flight"], "state": u["state"],
                          "proof_requirement": u["proof_requirement"], "cost": cost})
            if cost["basis"] == "UNKNOWN":
                unknown += 1
                continue
            if cost["basis"] == "VENDOR_QUOTE":
                # AEGIS OOH-3/4 security F2: a quote is owed in full while any of its units is live, so it counts
                # once at its whole amount (never a share that a withdrawn sibling would make vanish)
                if cost["price_id"] in counted:
                    continue
                counted.add(cost["price_id"])
                amount = Decimal(self.prices[cost["price_id"]]["amount"])
                quoted += amount
            else:
                amount = Decimal(cost["amount"])
                estimated += amount
            by_vendor[u["vendor"]] = by_vendor.get(u["vendor"], Decimal(0)) + amount
        b = c.get("brief")
        total = quoted + estimated
        fit = None
        if b is not None:
            fit = "UNKNOWN" if unknown else ("WITHIN" if total <= Decimal(b["budget_cap"]) else "OVER")
        warnings = [{"code": "VENDOR_TOTAL_ABOVE_SINGLE_PURCHASE_LIMIT", "vendor": v, "amount": P.fmt(t),
                     "limit": self.settings.max_purchase_amount}
                    for v, t in sorted(by_vendor.items()) if t > Decimal(self.settings.max_purchase_amount)]
        return {"campaign_id": c["campaign_id"], "lines": lines, "brief": dict(b) if b else None,
                "totals": {"quoted": P.fmt(quoted), "estimated": P.fmt(estimated), "known": P.fmt(total),
                           "unknown_lines": unknown, "currency": "USD"},
                "budget_fit": fit, "warnings": warnings,
                "markup": {"suggested_pct": P.SUGGESTED_MARKUP_PCT, "suggested_applied": False,
                           "deal_pct": (c["commercial"] or {}).get("markup_pct")},
                "approvable": b is not None and unknown == 0 and fit != "OVER"}

    def plan_guard(self, c: dict) -> Optional[str]:
        """The PLAN_APPROVED guard (OOH-3): a brief, no line of unknown cost, within the budget cap."""
        if c.get("brief") is None:
            return "NO_BRIEF"
        p = self.plan(c)
        for ln in p["lines"]:                            # every line still inside the brief (defence in depth)
            try:
                self._fits(c["brief"], ln["format"], ln["flight"])
            except Conflict:
                return "PLAN_UNIT_OUTSIDE_BRIEF"
        if p["totals"]["unknown_lines"]:
            return "PLAN_COST_UNKNOWN"
        if p["budget_fit"] == "OVER":
            return "PLAN_OVER_BUDGET"
        return None                                      # == plan(c)["approvable"]

    # ================================================================================================ markup evidence

    def record_markup_evidence(self, actor: str, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("markup_evidence", cid, body)
            if self._idem(actor, rk, body):
                return self.campaign_view(tid, cid)
            self.check_kill(tenant=tid, capability="prices")
            self._writable(c, P.BRIEF_STATES + ("PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED"))
            mid = self._fresh({e["evidence_id"] for e in c["markup_evidence"]}, derived_id("mkx", cid, actor, rk))
            data = {"campaign_id": cid, "evidence_id": mid, "kind": body["kind"], "doc_ref": body["doc_ref"]}
            self._commit("markup_evidence_recorded", self._req(data, actor, rk, body, cid), actor,
                         evidence=self._ev("markup_evidence_recorded", cid, data, actor, rk))
            return self.campaign_view(tid, cid)

    def _a_markup_evidence_recorded(self, d, at):
        c = self.campaigns[d["campaign_id"]]
        c["markup_evidence"].append({"evidence_id": d["evidence_id"], "kind": d["kind"], "doc_ref": d["doc_ref"],
                                     "recorded_by": d["actor"], "recorded_at": at})
        c["version"] += 1
