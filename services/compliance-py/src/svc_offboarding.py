"""
Client offboarding, Compliance's side (ADR 0027 decision 3; MSA 15.5).

``POST /compliance/v1/clients/{client_id}/offboarding`` (Andre only). A dry run lists what Compliance holds for the
client; the real run (naming the dry run's ``plan_sha256``) closes the client's activation (every department then
reads REFUSE, until a NEW activation after this run) and records a hash-only deletion certificate on the ledger.

Compliance deletes nothing: everything it holds about a client is a record the law requires or the evidence of a
decision, and none of it is personal information by design (names are stored only as SHA-256 hashes, places as codes):

- OFAC screening records (the client's and its owners' recorded searches, the ownership record): kept 10 years
  (31 CFR 501.601, as amended effective March 21, 2025); the MSA's 7-year floor is exceeded by law (MSA 15.5(d));
- activation rulings (the decisions money and work relied on): kept 7 years with the financial records they gate.

An open hold on the client blocks the run (a sanctions hold's records are never touched while it is open).
"""

from __future__ import annotations

import hashlib
import json

from clock import iso
from errors import Conflict
from ledger import derived_id

KEEP_OFAC = "10 years after the search (31 CFR 501.601)"
KEEP_RULINGS = "7 years (MSA 15.5(a), with the financial records they gate)"


class OffboardingMixin:

    @property
    def offboarded(self) -> dict:
        """(lane, client_id) -> when the client was offboarded (event-sourced; kind ``client_offboarded``)."""
        d = self.__dict__.get("_offboarded")
        if d is None:
            d = self.__dict__["_offboarded"] = {}
        return d

    def _apply_offboarded(self, r: dict) -> None:
        self.offboarded[(r["lane"], r["client_id"])] = {"at": r["executed_at"], "ruling_id": r.get("ruling_id")}

    def offboarded_reason(self, lane: str, client_id: str, ruling_id: str) -> list[str]:
        """The latest activation ruling is the one the offboarding closed (no new activation since)."""
        rec = self.offboarded.get((lane, client_id))
        if rec is not None and rec["ruling_id"] == ruling_id:
            return [f"the client was offboarded on {rec['at'][:10]}: a new activation is needed to serve it again"]
        return []

    def _offboarding_plan(self, lane: str, client_id: str) -> dict:
        atts = [a for a in self.attestations.values() if a.get("subject_id") == client_id and a.get("lane") == lane]
        rulings = [r for r in self.rulings.values() if r.get("gate") == "activation" and r.get("subject_id") == client_id
                   and r.get("lane") == lane]
        own = 1 if (lane, client_id) in self.ownerships else 0
        holds = sorted(h["hold_id"] for h in self.holds.values() if h["status"] == "open" and h["subject_id"] == client_id)
        held = [
            {"kind": "ofac_searches", "count": len(atts), "action": "keep_10_years", "retention": KEEP_OFAC,
             "note": "client and owner searches; names stored as hashes only"},
            {"kind": "ownership_record", "count": own, "action": "keep_10_years", "retention": KEEP_OFAC,
             "note": "owner names stored as hashes only"},
            {"kind": "activation_rulings", "count": len(rulings), "action": "keep_7_years", "retention": KEEP_RULINGS},
            {"kind": "activation", "count": 1 if self.latest_activation.get((lane, client_id)) else 0,
             "action": "close", "note": "every department reads REFUSE until a new activation"},
        ]
        blockers = [f"{len(holds)} open hold(s) on the client: resolve them first (sanctions records are never touched "
                    "while a hold is open)"] if holds else []
        plan = {"client_id_sha256": hashlib.sha256(client_id.encode()).hexdigest(), "lane": lane, "held": held,
                "blockers": blockers, "warnings": [], "log_length": len(self.log)}
        from service import _sha
        plan["plan_sha256"] = _sha(plan)
        return plan

    def offboard_client(self, request_id: str, client_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, f"clients/{client_id}/offboarding", body)
            if cached:
                return cached
            lane = body.get("lane") or "client"
            plan = self._offboarding_plan(lane, client_id)
            public = {k: v for k, v in plan.items() if k != "log_length"}
            if body["dry_run"]:
                return {**public, "dry_run": True, "request_id": request_id}
            if body.get("plan_sha256") != plan["plan_sha256"]:
                raise Conflict("the plan changed since the dry run (or this is not its plan_sha256): run the dry run "
                               "again and name its plan_sha256")
            if plan["blockers"]:
                raise Conflict("offboarding blocked: " + "; ".join(plan["blockers"]))
            now = self._now()
            cert = {"certificate_version": 1, "service": "compliance", "client_id_sha256": plan["client_id_sha256"],
                    "lane": lane, "executed_at": iso(now), "executed_by": "andre", "plan_sha256": plan["plan_sha256"],
                    "deleted": {}, "closed": {"activation": plan["held"][3]["count"]},
                    "kept": {x["kind"]: {"count": x["count"], "retention": x["retention"]} for x in plan["held"]
                             if x["action"].startswith("keep")}}
            cert_sha = hashlib.sha256(json.dumps(cert, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            eid = self._record(derived_id("offb", client_id, lane, cert_sha), "client_offboarded", "andre", client_id,
                               {"certificate_sha256": cert_sha, "plan_sha256": plan["plan_sha256"], "lane": lane},
                               "Client offboarded: activation closed, deletion certificate recorded")
            self._commit("client_offboarded", {"client_id": client_id, "lane": lane, "executed_at": iso(now),
                                               "ruling_id": self.latest_activation.get((lane, client_id)),
                                               "certificate": cert, "certificate_sha256": cert_sha}, [eid])
            return self._idem_store(key, h, {"certificate": cert, "certificate_sha256": cert_sha,
                                             "ledger_event_ids": [eid], "request_id": request_id})
