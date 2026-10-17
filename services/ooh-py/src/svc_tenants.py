"""
Tenants and kill switches (ADR 0019 decisions 7-8; seo-py's svc_tenants.py, trimmed).

Every campaign, inventory unit, price object, funds record and authorization names exactly one tenant; every read is
filtered by the caller's tenant scope, and a tenant-scoped caller asking for another tenant's object gets the same 404
as for an object that does not exist (no existence oracle). A reference to another tenant's object inside a body
(a quote id, an inventory id) is refused exactly like a reference to nothing.

Kill switches, each refusing with its own reason code:
  global                 every state change except the kill switches themselves (reads stay available)
  write                  the same, kept separate so Compliance (38) can freeze writes without "global"
  tenant:<id>            everything for that tenant
  capability:<name>      campaigns, inventory, prices, funds, purchase (authorization), submission, registry (OOH-2)
Engaging is easy, releasing is not: the dashboard, the agent runtime or Compliance (38) may ENGAGE any switch; only
Andre releases one. An engage the ledger cannot record still takes effect at once, in memory (``volatile``: fail
closed), and is reported as unrecorded. Switches set by the environment (OOH_KILL_GLOBAL, OOH_KILLED_CAPABILITIES)
cannot be released at runtime at all.
"""

from __future__ import annotations

import json

from typing import Optional

from config import CAPABILITY_SWITCHES, TENANT_ID
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from reasons import R
from svc_campaigns import _spelled_out

# Corporate-form words that never make a vendor a different vendor (AEGIS R2-M1).
CORPORATE_FORMS = ("the", "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co", "corp", "corporation",
                   "company", "plc")
# Digits written for letters, and letters that read alike, folded to one skeleton (war room R2: "l4mar", "lamar-1nc").
_SKELETON = str.maketrans({"4": "a", "3": "e", "0": "o", "5": "s", "7": "t", "1": "i", "l": "i", "8": "b", "9": "g"})
_FORMS = {f.translate(_SKELETON) for f in CORPORATE_FORMS}


def vendor_identity_key(value: str) -> str:
    """One vendor's identity: NFKC, case-folded, spelled-out runs joined, look-alike letters and leetspeak digits
    folded to one skeleton, separators and corporate-form words removed. ``lamar``, ``la-mar``, ``l4mar``,
    ``lamar-1nc`` and "The Lamar Company, Inc." are one vendor. A collision only ever refuses a registration."""
    return "".join(t for t in (x.translate(_SKELETON) for x in _spelled_out(value)) if t not in _FORMS)


def parse_switch(name: str) -> tuple[str, Optional[str]]:
    if name in ("global", "write"):
        return name, None
    kind, _, arg = name.partition(":")
    if kind == "capability" and arg in CAPABILITY_SWITCHES:
        return kind, arg
    if kind == "tenant" and TENANT_ID.fullmatch(arg or ""):
        return kind, arg
    raise Invalid(R("SWITCH_UNKNOWN"))


class TenantsMixin:
    # ------------------------------------------------------------------ kill switches: checks

    def kill_code(self, tenant: Optional[str] = None, capability: Optional[str] = None) -> Optional[str]:
        """The reason code of the first engaged switch that covers a state change, or None."""
        s, env = self.switches, self.settings
        vol = {(v["kind"], v["arg"]) for v in self.volatile_kills}
        if env.kill_global or s["global"] or ("global", None) in vol:
            return "KILLED_GLOBAL"
        if s["write"] or ("write", None) in vol:
            return "KILLED_WRITE"
        if tenant is not None:
            t = self.tenants.get(tenant)
            if (t is not None and t["killed"]) or ("tenant", tenant) in vol:
                return "KILLED_TENANT"
        if capability is not None and (capability in env.killed_capabilities or s["capability"].get(capability)
                                       or ("capability", capability) in vol):
            return "KILLED_CAPABILITY"
        return None

    def check_kill(self, **kw) -> None:
        code = self.kill_code(**kw)
        if code is not None:
            raise Forbidden(R(code))

    def switch_view(self) -> dict:
        s, env = self.switches, self.settings
        return {"global": bool(s["global"] or env.kill_global), "global_by_env": env.kill_global,
                "write": bool(s["write"]),
                "capabilities": {c: bool(s["capability"].get(c) or c in env.killed_capabilities)
                                 for c in CAPABILITY_SWITCHES},
                "capabilities_by_env": list(env.killed_capabilities),
                "tenants_killed": sorted(t for t, v in self.tenants.items() if v["killed"]),
                "volatile_unrecorded": [dict(v) for v in self.volatile_kills]}

    # ------------------------------------------------------------------ kill switches: set

    def set_switch(self, actor: str, body: dict, andre: bool) -> dict:
        kind, arg = parse_switch(body["switch"])
        engaged = body["engaged"]
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            if not engaged and not andre:
                raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
            if kind == "tenant" and arg not in self.tenants:
                raise NotFound(R("TENANT_NOT_FOUND"))
            actor = "andre" if andre else actor
            rk = self.rk("switch", body["switch"], body)
            prev = self._idem(actor, rk, body)
            if prev:
                return {**self.switch_view(), "recorded": True}
            data = {"kind": kind, "arg": arg, "engaged": engaged}
            try:
                if not self.integrity["ok"]:
                    raise Unavailable(R("INTEGRITY_UNVERIFIED"))
                self._commit("switch_set", self._req(data, actor, rk, body, None), actor,
                             evidence=("kill_switch_set", f"switch:{kind}:{arg or '-'}", data, (actor, rk)))
            except Unavailable:
                if not engaged:
                    raise                                 # a release must be recorded to take effect
                if not any(v["kind"] == kind and v["arg"] == arg for v in self.volatile_kills):
                    self.volatile_kills.append({"kind": kind, "arg": arg})
                return {**self.switch_view(), "recorded": False}
            return {**self.switch_view(), "recorded": True}

    def _a_switch_set(self, d, at):
        kind, arg, on = d["kind"], d["arg"], d["engaged"]
        if kind in ("global", "write"):
            self.switches[kind] = on
        elif kind == "capability":
            self.switches[kind][arg] = on
        else:
            self.tenants[arg]["killed"] = on
        if on:
            self.volatile_kills = [v for v in self.volatile_kills if not (v["kind"] == kind and v["arg"] == arg)]

    # ------------------------------------------------------------------ vendors (AEGIS R2-M1)

    def register_vendor(self, body: dict) -> dict:
        """Andre registers a vendor (an operator ZBM buys from). Global, not per tenant: ZBM is the only buyer. A
        vendor id or legal name whose identity key matches any registered vendor's id or name is refused, so free
        text can never give one vendor a second identity."""
        vid = body["vendor_id"]
        with self.lock:
            self._gate()
            self.check_kill()
            rk = self.rk("vendor", vid, body)
            if self._idem("andre", rk, body):
                return dict(self.vendors[vid])
            keys = {vendor_identity_key(vid), vendor_identity_key(body["legal_name"])}
            if "" in keys:
                raise Invalid(R("INVALID"), field="vendor")
            if vid in self.vendors:
                raise Conflict(R("VENDOR_EXISTS"))                    # an id is never shared, acknowledged or not
            colliding = sorted(v["vendor_id"] for v in self.vendors.values() if {v["id_key"], v["name_key"]} & keys)
            ack = body.get("distinct_from_ack")
            if colliding and (ack is None or sorted(set(ack["vendor_ids"])) != colliding):
                raise Conflict(R("VENDOR_EXISTS"))
            if ack is not None and not colliding:
                raise Invalid(R("INVALID"), field="distinct_from_ack")   # nothing to acknowledge
            data = {"vendor_id": vid, "legal_name": body["legal_name"], "id_key": vendor_identity_key(vid),
                    "name_key": vendor_identity_key(body["legal_name"]), "distinct_from": colliding,
                    "distinct_reason": ack["reason"] if ack else None}
            # AEGIS R3-L: Andre may register a genuinely distinct vendor whose name normalises onto another's
            # ("Model Media" / "Mode1 Media"), naming every vendor it collides with and a reason from a closed set
            event = "vendor_registered_distinct_by_andre" if ack else "vendor_registered"
            self._commit("vendor_registered", self._req(data, "andre", rk, body, vid), "andre",
                         evidence=(event, f"vendor:{vid}", {k: data[k] for k in ("vendor_id", "id_key", "name_key",
                                                                                 "distinct_from", "distinct_reason")},
                                   ("andre", rk)))
            return dict(self.vendors[vid])

    def _a_vendor_registered(self, d, at):
        self.vendors[d["vendor_id"]] = {k: d.get(k) for k in ("vendor_id", "legal_name", "id_key", "name_key",
                                                             "distinct_from", "distinct_reason")} | {
            "registered_by": d["actor"], "registered_at": at}

    def vendors_view(self) -> list:
        with self.lock:
            return json.loads(json.dumps([self.vendors[v] for v in sorted(self.vendors)]))

    def require_vendor(self, vid: Optional[str]) -> None:
        if vid is not None and vid not in self.vendors:
            raise Conflict(R("VENDOR_UNKNOWN"))

    # ------------------------------------------------------------------ tenants

    def create_tenant(self, body: dict) -> dict:
        tid = body["tenant_id"]
        with self.lock:
            self._gate()
            self.check_kill()
            rk = self.rk("tenant", tid, body)
            if self._idem("andre", rk, body):
                return self.tenant_view(tid)
            if tid in self.tenants:
                raise Conflict(R("TENANT_EXISTS"))
            data = {"tenant_id": tid}
            self._commit("tenant_created", self._req(data, "andre", rk, body, tid), "andre",
                         evidence=("tenant_created", f"tenant:{tid}", data, ("andre", rk)))
            return self.tenant_view(tid)

    def _a_tenant_created(self, d, at):
        self.tenants[d["tenant_id"]] = {"tenant_id": d["tenant_id"], "killed": False, "created_at": at}

    def tenant_view(self, tid: str) -> dict:
        with self.lock:
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            return {**t, "campaigns": sum(1 for c in self.campaigns.values() if c["tenant_id"] == tid)}

    def tenants_view(self) -> list:
        with self.lock:
            return [self.tenant_view(t) for t in sorted(self.tenants)]
