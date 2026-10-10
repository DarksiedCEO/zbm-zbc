"""
Tenants and kill switches (ADR 0017 decisions 7-9).

Tenant isolation is the record-first log + ledger pattern (flag 4 default: no Postgres): every audit, entity record
and report names exactly one tenant, every read is filtered by the caller's tenant scope, and a tenant-scoped caller
asking for another tenant's object gets the same 404 as for an object that does not exist (no existence oracle).

Kill switches, each refusing with its own reason code:
  global                 everything that observes or writes (reads of stored state stay available)
  write                  every state change except the kill switches themselves
  tenant:<id>            everything for that tenant
  capability:<name>      fetch, render, ai_probe, audit, entity_write, prompt_sets
  provider:<name>        web (all outbound crawling), openai, anthropic, google, perplexity
Engaging is easy, releasing is not: the dashboard or Compliance (38) may ENGAGE any switch; only Andre releases one.
An engage the ledger cannot record still takes effect at once, in memory (``volatile``: fail closed), and is
reported as unrecorded. Switches set by the environment (SEO_KILL_GLOBAL, SEO_KILLED_CAPABILITIES,
SEO_KILLED_PROVIDERS) cannot be released at runtime at all.
"""

from __future__ import annotations

import re
from typing import Optional

from config import CAPABILITY_SWITCHES, PROVIDER_SWITCHES, TENANT_ID
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from primitives import Killed
from reasons import R

TENANT_KINDS = ("own", "client")
DOMAIN = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
DOMAINS_MAX = 20
OWN_TENANT = "zbm"


def valid_domain(d) -> bool:
    return isinstance(d, str) and bool(DOMAIN.fullmatch(d)) and not d.endswith((".local", ".internal", ".localhost",
                                                                                ".arpa", ".invalid"))


def _site(d: str) -> str:
    return d[4:] if d.startswith("www.") else d


def _overlaps(a: str, b: str) -> bool:
    """Same site, or one is a subdomain of the other."""
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def parse_switch(name: str) -> tuple[str, Optional[str]]:
    if name in ("global", "write"):
        return name, None
    kind, _, arg = name.partition(":")
    if kind == "capability" and arg in CAPABILITY_SWITCHES:
        return kind, arg
    if kind == "provider" and arg in PROVIDER_SWITCHES:
        return kind, arg
    if kind == "tenant" and TENANT_ID.fullmatch(arg or ""):
        return kind, arg
    raise Invalid(R("SWITCH_UNKNOWN"))


class TenantsMixin:
    # ------------------------------------------------------------------ kill switches: checks

    def kill_code(self, tenant: Optional[str] = None, capability: Optional[str] = None,
                  provider: Optional[str] = None, write: bool = False) -> Optional[str]:
        """The reason code of the first engaged switch that covers this action, or None."""
        s, env = self.switches, self.settings
        vol = {(v["kind"], v["arg"]) for v in self.volatile_kills}
        if env.kill_global or s["global"] or ("global", None) in vol:
            return "KILLED_GLOBAL"
        if write and (s["write"] or ("write", None) in vol):
            return "KILLED_WRITE"
        if tenant is not None:
            t = self.tenants.get(tenant)
            if (t is not None and t["killed"]) or ("tenant", tenant) in vol:
                return "KILLED_TENANT"
        if capability is not None and (capability in env.killed_capabilities or s["capability"].get(capability)
                                       or ("capability", capability) in vol):
            return "KILLED_CAPABILITY"
        if provider is not None and (provider in env.killed_providers or s["provider"].get(provider)
                                     or ("provider", provider) in vol):
            return "KILLED_PROVIDER"
        return None

    def check_kill(self, **kw) -> None:
        code = self.kill_code(**kw)
        if code is not None:
            raise Forbidden(R(code))

    def guard(self, tenant: str, run: str = "audit"):
        """A callable a run checks before every outbound action (raises ``Killed``). It reads live state, so a switch
        engaged mid-run stops the run at its next step."""
        def check(capability: Optional[str] = None, provider: Optional[str] = None) -> None:
            with self.lock:
                # AEGIS M3: a run also stops for the audit capability and the write switch (its result could not be
                # recorded), checked between steps and before every socket operation
                code = self.kill_code(tenant=tenant, capability=capability, provider=provider, write=True) \
                    or self.kill_code(capability=run)
                if code is None and self._closed:
                    code = "SERVICE_CLOSED"
            if code is not None:
                raise Killed(code)
        return check

    def switch_view(self) -> dict:
        s, env = self.switches, self.settings
        return {"global": bool(s["global"] or env.kill_global), "global_by_env": env.kill_global,
                "write": bool(s["write"]),
                "capabilities": {c: bool(s["capability"].get(c) or c in env.killed_capabilities)
                                 for c in CAPABILITY_SWITCHES},
                "capabilities_by_env": list(env.killed_capabilities),
                "providers": {p: bool(s["provider"].get(p) or p in env.killed_providers) for p in PROVIDER_SWITCHES},
                "providers_by_env": list(env.killed_providers),
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
        elif kind in ("capability", "provider"):
            self.switches[kind][arg] = on
        else:
            self.tenants[arg]["killed"] = on
        if on:
            self.volatile_kills = [v for v in self.volatile_kills if not (v["kind"] == kind and v["arg"] == arg)]

    # ------------------------------------------------------------------ tenants

    def _seed(self) -> None:
        if OWN_TENANT not in self.tenants:
            data = {"tenant_id": OWN_TENANT, "kind": "own"}
            self._commit("tenant_created", data, "seo",
                         evidence=("tenant_created", f"tenant:{OWN_TENANT}", data, ("seed", OWN_TENANT)))
        self._seed_entity()

    def create_tenant(self, body: dict) -> dict:
        tid, kind = body["tenant_id"], body["kind"]
        if kind not in TENANT_KINDS:
            raise Invalid(R("TENANT_KIND_INVALID"))
        with self.lock:
            self._gate()
            self.check_kill(write=True)
            rk = self.rk("tenant", tid, body)
            if self._idem("andre", rk, body):
                return self.tenant_view(tid)
            if tid in self.tenants:
                raise Conflict(R("TENANT_EXISTS"))
            data = {"tenant_id": tid, "kind": kind}
            self._commit("tenant_created", self._req(data, "andre", rk, body, tid), "andre",
                         evidence=("tenant_created", f"tenant:{tid}", data, ("andre", rk)))
            return self.tenant_view(tid)

    def _a_tenant_created(self, d, at):
        self.tenants[d["tenant_id"]] = {"tenant_id": d["tenant_id"], "kind": d["kind"], "domains": [],
                                        "killed": False, "created_at": at}

    def set_domains(self, tid: str, body: dict) -> dict:
        domains = body["domains"]
        if len(domains) > DOMAINS_MAX:
            raise Invalid(R("DOMAIN_LIMIT"))
        if not all(valid_domain(d) for d in domains) or len(set(domains)) != len(domains):
            raise Invalid(R("DOMAIN_INVALID"))
        with self.lock:
            self._gate()
            self.check_kill(tenant=tid, write=True)
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            rk = self.rk("domains", tid, body)
            if self._idem("andre", rk, body):
                return self.tenant_view(tid)
            # AEGIS M2: a domain (or its www. twin) belongs to ONE tenant, so a client's site can never be audited
            # free through the own-properties tenant (or another client's paid audit)
            # AEGIS N1: also a subdomain or a parent of another tenant's domain (shop.site.test vs site.test)
            taken = {_site(d) for other, t in self.tenants.items() if other != tid for d in t["domains"]}
            if any(_overlaps(_site(d), x) for d in domains for x in taken):
                raise Conflict(R("DOMAIN_TAKEN"))
            data = {"tenant_id": tid, "domains": sorted(domains)}
            self._commit("tenant_domains_set", self._req(data, "andre", rk, body, tid), "andre",
                         evidence=("tenant_domains_set", f"tenant:{tid}", data, ("andre", rk)))
            return self.tenant_view(tid)

    def _a_tenant_domains_set(self, d, at):
        self.tenants[d["tenant_id"]]["domains"] = list(d["domains"])

    def tenant_view(self, tid: str) -> dict:
        with self.lock:
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            return {**t, "domains": list(t["domains"]),
                    "audits": sum(1 for a in self.audits.values() if a["tenant_id"] == tid)}

    def tenants_view(self) -> list:
        with self.lock:
            return [self.tenant_view(t) for t in sorted(self.tenants)]
