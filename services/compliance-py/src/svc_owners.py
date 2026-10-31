"""
Client ownership records and owner screening in the activation gate (ADR 0027 decision 4). See ``owners`` for the
rules; this is the service side: the Andre-only record, the per-owner OFAC searches, what the gate and the activation
status check, and the hold an owner hit opens.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import owners as OWN
from clock import iso, parse_iso
from errors import Invalid, NotFound
from intelligences import i08_sanctions
from ledger import derived_id

OWNER_HOLD_KIND = "sanctions_owner_match"
_KEYS = ("client_id", "lane", "client_kind", "owners", "no_owner_25", "client_attestation")


def _params(row: Optional[dict]) -> tuple[int, int]:
    p = (row or {}).get("parameters") or {}
    d, a = p.get("owner_disclosure_bp", 2500), p.get("aggregate_block_bp", 5000)
    ok = all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 10000 for x in (d, a))
    if not ok:
        raise ValueError("HR-14 owner parameters are invalid")
    return d, a


class OwnersMixin:

    @property
    def ownerships(self) -> dict:
        """(lane, client_id) -> the current ownership record (event-sourced; kind ``ownership``)."""
        d = self.__dict__.get("_ownerships")
        if d is None:
            d = self.__dict__["_ownerships"] = {}
        return d

    def _apply_ownership(self, r: dict) -> None:
        self.ownerships[(r["lane"], r["client_id"])] = r

    # ------------------------------------------------------------------ the record (Andre)

    def put_ownership(self, request_id: str, client_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, f"ownership/{client_id}", body)
            if cached:
                return cached
            try:
                OWN.validate(body)
            except OWN.OwnershipInvalid as exc:
                raise Invalid(str(exc)) from None
            now = self._now()
            att = body.get("client_attestation")
            if att is not None:
                try:
                    at = parse_iso(att["attested_at"])
                except (KeyError, TypeError, ValueError):
                    raise Invalid("client_attestation.attested_at must be an RFC 3339 timestamp with offset") from None
                if at > now + timedelta(minutes=5):
                    raise Invalid("client_attestation.attested_at is in the future")
                att = {**att, "attested_at": iso(at)}
            rec = {"client_id": client_id, "lane": body["lane"], "client_kind": body["client_kind"],
                   "owners": sorted((dict(o) for o in body.get("owners") or []), key=lambda o: o["owner_id"]),
                   "no_owner_25": bool(body.get("no_owner_25")), "client_attestation": att}
            rec["ownership_sha256"] = self._ownership_sha(rec)
            rec.update(recorded_at=iso(now), recorded_by="andre")
            eid = self._record(derived_id("own", client_id, body["lane"], rec["ownership_sha256"]),
                               "client_ownership_recorded", "andre", client_id,
                               {"client_id": client_id, "lane": body["lane"], "client_kind": rec["client_kind"],
                                "owners": len(rec["owners"]), "ownership_sha256": rec["ownership_sha256"]},
                               "Andre recorded who owns a client")
            self._commit("ownership", rec, [eid])
            out = {**self._ownership_view(client_id, body["lane"]), "ledger_event_ids": [eid]}
            return self._idem_store(key, h, out)

    @staticmethod
    def _ownership_sha(rec: dict) -> str:
        from service import _sha
        return _sha({k: rec.get(k) for k in _KEYS})

    def ownership_view(self, client_id: str, lane: str) -> dict:
        with self.lock:
            if (lane, client_id) not in self.ownerships:
                raise NotFound("no ownership record for this client in this lane")
            return self._ownership_view(client_id, lane)

    def _ownership_view(self, client_id: str, lane: str) -> dict:
        rec = self.ownerships[(lane, client_id)]
        eff = OWN.effective(rec["owners"])
        problems = self.owner_screening_problems(lane, client_id, self._now(), age_key="rescreen_after_days")
        try:
            _d, agg = _params((self.rows_by_id() or {}).get("HR-14"))
        except ValueError:
            agg = 5000
        share, blocked = OWN.blocked(rec["owners"], lambda oid: self._owner_hit(lane, client_id, oid), agg)
        return {"client_id": client_id, "lane": lane, "client_kind": rec["client_kind"],
                "ownership_sha256": rec["ownership_sha256"], "recorded_at": rec["recorded_at"],
                "client_attested": bool(rec.get("client_attestation")), "no_owner_25": rec["no_owner_25"],
                "owners": [{"owner_id": o["owner_id"], "kind": o["kind"], "parent_owner_id": o.get("parent_owner_id"),
                            "ownership_bp": o["ownership_bp"], "effective_bp": int(eff[o["owner_id"]] * 10000),
                            "country": o["country"],
                            "latest_search": self._owner_search_view(lane, client_id, o["owner_id"])}
                           for o in rec["owners"]],
                "blocked_bp": share, "blocked": share >= agg, "blocked_owner_ids": sorted(blocked),
                "problems": problems}

    def _owner_search_view(self, lane: str, client_id: str, owner_id: str) -> Optional[dict]:
        att = self._owner_attestation(lane, client_id, owner_id)
        if att is None:
            return None
        return {"attestation_id": att["attestation_id"], "searched_at": att["searched_at"], "result": att["result"]}

    # ------------------------------------------------------------------ searches

    def _owner_attestations(self, lane: str, client_id: str, owner_id: str) -> list[dict]:
        return [a for a in self.attestations.values() if a.get("owner_id") == owner_id
                and a.get("subject_id") == client_id and a.get("lane") == lane]

    def _owner_attestation(self, lane: str, client_id: str, owner_id: str) -> Optional[dict]:
        c = self._owner_attestations(lane, client_id, owner_id)
        return max(c, key=lambda a: (a["searched_at"], a["recorded_at"], a["attestation_id"])) if c else None

    def _owner_hit(self, lane: str, client_id: str, owner_id: str) -> bool:
        """Any search of this owner that was not a clean no-match, ever (a hit is never investigated around)."""
        return any(a.get("result") != "no_match" for a in self._owner_attestations(lane, client_id, owner_id))

    def owner_hold(self, client_id: str, lane: str, owner_id: str, aid: str, now: datetime) -> dict:
        """The hold an owner's hit opens on the CLIENT: permanent. ``sanctions_match`` (sanctions-grounded) when the
        blocked owners now hold 50% or more in aggregate, else ``sanctions_owner_match``."""
        rec = self.ownerships.get((lane, client_id))
        kind = OWNER_HOLD_KIND
        if rec is not None:
            try:
                _d, agg = _params((self.rows_by_id() or {}).get("HR-14"))
            except ValueError:
                agg = 5000
            share, _ = OWN.blocked(rec["owners"], lambda oid: oid == owner_id or self._owner_hit(lane, client_id, oid),
                                   agg)
            if share >= agg:
                kind = "sanctions_match"
        from service import _b32
        return {"hold_id": "hold-" + _b32(f"sat|{aid}", 20), "subject_id": client_id, "kind": kind,
                "obligation_id": "HR-14", "releasable": False, "status": "open", "opened_at": iso(now),
                "released_at": None, "detail": f"OFAC search {aid} of owner {owner_id}"}

    def ownership_blocked(self, lane: str, client_id: str) -> bool:
        """The 50 Percent Rule on the current record: blocked owners hold the aggregate threshold or more."""
        rec = self.ownerships.get((lane, client_id))
        if rec is None:
            return False
        try:
            _d, agg = _params((self.rows_by_id() or {}).get("HR-14"))
        except ValueError:
            agg = 5000
        share, _ = OWN.blocked(rec["owners"], lambda oid: self._owner_hit(lane, client_id, oid), agg)
        return share >= agg

    # ------------------------------------------------------------------ what the gate and the status check

    def owner_screening_problems(self, lane: str, client_id: str, now: datetime, age_key: str = "max_age_days",
                                 pinned: Optional[dict] = None) -> list[str]:
        """Every reason the client's ownership does not pass now ([] = it passes). ``pinned`` (the activation
        status): the owner searches the ruling captured, re-checked for their age."""
        rows = self.rows_by_id() or {}
        row = rows.get("HR-14")
        params = (row or {}).get("parameters") or {}
        try:
            disclosure, agg = _params(row)
        except ValueError:
            return ["HR-14 owner parameters are invalid: blocked"]
        rec = self.ownerships.get((lane, client_id))
        out = OWN.disclosure_problems(rec, disclosure)
        if rec is None:
            return out
        for o in rec["owners"]:
            oid = o["owner_id"]
            att = self.attestations.get((pinned or {}).get(oid) or "") if pinned is not None else \
                self._owner_attestation(lane, client_id, oid)
            if att is None:
                out.append(f"owner {oid}: no OFAC search is recorded for this owner" if pinned is None else
                           f"owner {oid}: the ruling names no search of this owner: re-activate")
                continue
            if att.get("searched_name_sha256") != o["name_sha256"]:
                out.append(f"owner {oid}: the OFAC search on record was for another name")
                continue
            p = i08_sanctions.attestation_problem(att, client_id, now, params, lane=lane, age_key=age_key)
            if p:
                out.append(f"owner {oid}: {p}")
        share, _ = OWN.blocked(rec["owners"], lambda oid: self._owner_hit(lane, client_id, oid), agg)
        if share >= agg:
            out.append(f"owners on a sanctions list hold {share / 100:.2f}% of the client in aggregate (OFAC 50 "
                       "Percent Rule): the client is blocked")
        return out

    def ownership_snapshot(self, lane: str, client_id: str) -> Optional[dict]:
        """What an ALLOWED activation ruling records: the ownership record's hash and each owner's search."""
        rec = self.ownerships.get((lane, client_id))
        if rec is None:
            return None
        return {"ownership_sha256": rec["ownership_sha256"],
                "owner_attestation_ids": {o["owner_id"]: (self._owner_attestation(lane, client_id, o["owner_id"])
                                                          or {}).get("attestation_id") for o in rec["owners"]}}

    def ownership_status(self, lane: str, client_id: str, prof: dict, now: datetime,
                         rescreen_days: int) -> tuple[list[str], Optional[datetime], bool]:
        """(reasons, the earliest owner search expiry, sanctions-grounded) for the CURRENT activation status."""
        rows = self.rows_by_id() or {}
        try:
            _d, agg = _params(rows.get("HR-14"))
        except ValueError:
            return ["HR-14 owner parameters are invalid: blocked"], None, False
        rec = self.ownerships.get((lane, client_id))
        snap = prof.get("ownership")
        reasons: list[str] = []
        if rec is None:
            return ["no ownership record: re-activate after Andre records it"], None, False
        share, _ = OWN.blocked(rec["owners"], lambda oid: self._owner_hit(lane, client_id, oid), agg)
        grounded = share >= agg
        if not isinstance(snap, dict):
            return ["the ruling predates owner screening: re-activate"], None, grounded
        if snap.get("ownership_sha256") != rec["ownership_sha256"]:
            reasons.append("the client's ownership record changed since the ruling: re-activate")
            return reasons, None, grounded
        pinned = snap.get("owner_attestation_ids") or {}
        reasons += self.owner_screening_problems(lane, client_id, now, age_key="rescreen_after_days", pinned=pinned)
        reasons = [r if "older than" not in r else r.replace("re-screen required", "re-screen the owner and "
                                                                                   "re-activate") for r in reasons]
        until = None
        for oid, aid in pinned.items():
            a = self.attestations.get(aid or "")
            if a is not None:
                t = parse_iso(a["searched_at"]) + timedelta(days=rescreen_days)
                until = t if until is None or t < until else until
        return reasons, until, grounded
