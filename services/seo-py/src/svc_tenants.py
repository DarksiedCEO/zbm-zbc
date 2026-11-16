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
  provider:<name>        web (all outbound crawling), openai, anthropic, google, perplexity, gsc (Search Console)
Engaging is easy, releasing is not: the dashboard or Compliance (38) may ENGAGE any switch; only Andre releases one.
An engage the ledger cannot record still takes effect at once, in memory (``volatile``: fail closed), and is
reported as unrecorded. Switches set by the environment (SEO_KILL_GLOBAL, SEO_KILLED_CAPABILITIES,
SEO_KILLED_PROVIDERS) cannot be released at runtime at all.
"""

from __future__ import annotations

import re
from typing import Optional

from agents import domain_match, secret_guard
from config import CAPABILITY_SWITCHES, PROVIDER_SWITCHES, TENANT_ID, TENANT_KINDS   # C2-6a2: kinds moved
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from primitives import Killed
from reasons import R
import sink_guard
from finance_client import CLIENT_ID as FINANCE_CLIENT_REF   # C2-6g4: the declared grammar
from svc_optext import url_view

# review 2b26d980 SHOULD 1: a punycode TLD (``xn--p1ai``) is a TLD too (an IDN domain is registered as A-labels)
DOMAIN = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,58}[a-z0-9])")
DOMAINS_MAX = 20
OWN_TENANT = "zbm"


def valid_domain(d) -> bool:
    """A registrable host name; never a public suffix (``github.io``, ``co.uk``: AEGIS L3, agents/domain_match.py),
    whose every customer subdomain would otherwise count as the tenant's site."""
    return isinstance(d, str) and bool(DOMAIN.fullmatch(d)) and not d.endswith((".local", ".internal", ".localhost",
                                                                                ".arpa", ".invalid")) \
        and not domain_match.is_public_suffix(d)


def domain_invalid(d) -> bool:
    """C2-2g (lead ruling F2f-3, core-spec/2 H rules, T-3): a domain entering from the API (a registered domain, a
    competitor domain, the domain of an audit / schedule / citation run / log ingest) must be a host name in the
    grammar the sink guard's host rule uses (``sink_guard.host_shaped``: dotted labels, no leading / trailing
    hyphen, a letter or ``xn--`` IDNA TLD; and no ``syntax`` / ``tld`` / ``idna`` refusal of ``host_refusal``).
    Anything else (``-0231``, ``0.154``, ``000778``) is refused 422 DOMAIN_INVALID before any write; a host name the
    host rule refuses for its content (key- or number-shaped labels) stays IDENTIFIER_WITHHELD (domain_refused)."""
    return not sink_guard.host_shaped(d) or sink_guard.host_refusal(d) in ("syntax", "tld", "idna")


# C2-6i (lead ruling 07:40 PT): the predicate lives in sink_guard (cycle-free: it needs only secret_guard and the
# sink's classify) so the sink's own-id check calls the SAME function; this module-level name is that function
identifier_refused = sink_guard.identifier_refused


def finance_ref_refused(x) -> bool:
    """C2-6g4 (lead ruling 2026-11-15 03:20 PT, F6g3-1; OWNER DECISION 02:00 PT E-COMP; spec O-4 I, T-2): since E-COMP
    the raw Finance client reference never reaches a sink (every sink holds its T-2 code), so the route does NOT judge
    it with ``sink_guard.classify`` (a sink rule). It is validated by its own exact declared grammar
    (``finance_client.CLIENT_ID``, ``[A-Za-z0-9._-]{1,100}``) after the unchanged identifier checks
    (``secret_guard.identifier_refusal``, ``secret_guard.folded_prose``). The sink still refuses a raw reference in
    its T-2 fields. Other identifier kinds keep ``identifier_refused``."""
    if not isinstance(x, str) or secret_guard.identifier_refusal(x) is not None or secret_guard.folded_prose(x):
        return True
    return FINANCE_CLIENT_REF.fullmatch(x) is None

def domain_refused(d) -> bool:
    """Review 2b26d980 SHOULD 1: a DOMAIN field (a tenant's domain, a competitor domain) is judged as a host name by
    the sink guard's host rule (``sink_guard.host_refusal``: a credential word in a label is a name,
    ``1password.com``; a key-shaped or number-shaped host is refused).
    Review 98b76cde: the registration lane keeps exactly what release 2b26d980 did with an e-mail form in a domain
    (``first-last-at-domain.tld`` is refused; a mailbox provider's sub-domain and an ``at`` LABEL are not e-mail
    forms), and a domain that release registered is never refused now (P1): one the rule does not take is judged
    by that release's identifier rules (``secret_guard.identifier_refusal`` without the "@"-less forms, which are
    for a tenant id and a Finance client id, then the sink guard)."""
    if not sink_guard.host_shaped(d):
        return identifier_refused(d)
    if sink_guard.host_refusal(d) is None and not secret_guard.email_form(d):
        return False
    return secret_guard.identifier_refusal(d, atless=False) is not None or sink_guard.classify(d) is None


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
        def check(capability: Optional[str] = None, provider: Optional[str] = None,
                  agent: Optional[str] = None) -> None:
            with self.lock:
                # AEGIS M3: a run also stops for the audit capability and the write switch (its result could not be
                # recorded), checked between steps and before every socket operation
                code = self.kill_code(tenant=tenant, capability=capability, provider=provider, write=True) \
                    or self.kill_code(capability=run)
                if code is None and self._closed:
                    code = "SERVICE_CLOSED"
                if code is None and agent is not None and self.agent_blocked(agent):
                    code = "AGENT_RESTRICTED"
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
            sink_guard.register_tenant(OWN_TENANT)        # F6f3-1 (ruling 02:15 PT): registered before any sink write
            self._commit("tenant_created", data, "seo",
                         evidence=("tenant_created", f"tenant:{OWN_TENANT}", data, ("seed", OWN_TENANT)))
        self._seed_entity()

    def create_tenant(self, body: dict) -> dict:
        tid, kind = body["tenant_id"], body["kind"]
        if kind not in TENANT_KINDS:
            raise Invalid(R("TENANT_KIND_INVALID"))
        if identifier_refused(tid, prose=True):           # review c0c09bd1: an identifier, not prose
            raise Invalid(R("IDENTIFIER_WITHHELD"))
        with self.lock:
            self._gate()
            self.check_kill(write=True)
            rk = self.rk("tenant", tid, body)
            if self._idem("andre", rk, body):
                return self.tenant_view(tid)
            if tid in self.tenants:
                raise Conflict(R("TENANT_EXISTS"))
            data = {"tenant_id": tid, "kind": kind}
            sink_guard.register_tenant(tid)               # F6f3-1 (ruling 02:15 PT): registered before any sink write
            self._commit("tenant_created", self._req(data, "andre", rk, body, tid), "andre",
                         evidence=("tenant_created", f"tenant:{tid}", data, ("andre", rk)))
            return self.tenant_view(tid)

    def _a_tenant_created(self, d, at):
        sink_guard.register_tenant(d["tenant_id"])        # F6f3-1: replay / restart re-registers every tenant
        self.tenants[d["tenant_id"]] = {"tenant_id": d["tenant_id"], "kind": d["kind"], "domains": [],
                                        "killed": False, "created_at": at, "finance_client_id": None,
                                        "gsc_property": None}

    def set_domains(self, tid: str, body: dict) -> dict:
        domains = body["domains"]
        if len(domains) > DOMAINS_MAX:
            raise Invalid(R("DOMAIN_LIMIT"))
        if not all(valid_domain(d) and not domain_invalid(d) for d in domains) or len(set(domains)) != len(domains):
            raise Invalid(R("DOMAIN_INVALID"))
        if any(domain_refused(d) for d in domains):       # review c0c09bd1: a registrable host name, not prose
            raise Invalid(R("IDENTIFIER_WITHHELD"))
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

    def set_finance_client(self, tid: str, body: dict) -> dict:
        """Andre binds a client tenant to its Finance (31) client id (Legal's party reference at Finance), so a paid
        run can check that the invoice is this client's (ADR 0017 W3-2). Re-binding is allowed and recorded; the
        id is never read from a page, an invoice or a caller other than Andre."""
        if finance_ref_refused(body["finance_client_id"]):   # C2-6g4 ruling 03:20 PT: its own grammar (O-4 I)
            raise Invalid(R("IDENTIFIER_WITHHELD"))
        with self.lock:
            self._gate()
            self.check_kill(tenant=tid, write=True)
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            rk = self.rk("finance_client", tid, body)
            # C2-6g2 E-COMP (owner decision 2026-11-15 02:00 PT): every sink (ledger, seo_log, idempotency hash) and
            # the runtime binding hold ONLY the T-2 reference c:text:<16hex>; the raw reference is in the tenant vault
            code = self._fc_code(tid, body["finance_client_id"])
            hbody = {**body, "finance_client_id": code}
            if self._idem("andre", rk, hbody):
                return self.tenant_view(tid)
            if t["kind"] != "client":
                raise Invalid(R("TENANT_KIND_INVALID"))
            vid = self._fc_seal(tid, code, body["finance_client_id"])
            data = {"tenant_id": tid, "finance_client_id": code}
            try:
                self._commit("tenant_finance_client_set", self._req(data, "andre", rk, hbody, tid), "andre",
                             evidence=("tenant_finance_client_set", f"tenant:{tid}", data, ("andre", rk)))
            except BaseException:
                self.optext_vault.note_orphans(1)
                raise
            self._fc_vids()[(tid, code)] = vid
            return self.tenant_view(tid)

    def _a_tenant_finance_client_set(self, d, at):
        tid, ref = d["tenant_id"], d["finance_client_id"]
        if not (isinstance(ref, str) and self._FC_T2.match(ref)):
            ref = self._fc_legacy(tid, ref)
        self.tenants[tid]["finance_client_id"] = ref   # the T-2 code (E-COMP)

    # C2-10c3 (OWNER DECISION 2026-11-15 ~15:40 PT "count and contain", V-6 exception): a line written by ffd28ca8 /
    # cbdf5e78 / 3120f0b0 holds the RAW party id. The line stays untouched (append-only; counted by
    # legacy_prose_lines, withheld by sink_guard.withhold in audit_export: C2-10c4); the replay binds the T-2 code
    # and keeps the raw value only in the tenant's vault (sealed once, the first start that finds it unsealed;
    # never in a new log line or ledger event)
    _FC_T2 = re.compile(r"c:text:[0-9a-f]{16}\Z")

    def _fc_legacy(self, tid, raw):
        if not (isinstance(tid, str) and isinstance(raw, str)):
            return None
        try:
            code = self._fc_code(tid, raw)
        except Exception:  # noqa: BLE001 - no key / store error: fail closed (no binding; paid runs refuse)
            return None
        if self._fc_open(tid, code) is None and not getattr(self, "_observe_only", False):
            try:
                self._fc_vids()[(tid, code)] = self._fc_seal(tid, code, raw)
            except Exception:  # noqa: BLE001 - not sealed: the binding stays, E-COMP opens nothing (fail closed)
                pass
        return code

    # C2-6g2 E-COMP: the finance client reference in the tenant's operator text vault, {code -> raw}
    FC_KIND = "finance_client/v1"

    def _fc_vids(self) -> dict:
        m = getattr(self, "_fc_vid_map", None)
        if m is None:
            m = self._fc_vid_map = {}
        return m

    def _fc_code(self, tid: str, raw: str) -> str:
        """The T-2 reference of ``raw`` for ``tid`` (B-CD-01 kind ``text``). No key: VAULT_UNAVAILABLE."""
        import vault as vault_mod
        from errors import Unavailable
        try:
            return "c:text:" + self.optext_vault.coder(tid).code("text", raw)
        except vault_mod.VaultUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        except vault_mod.VaultError:
            raise Unavailable(R("STORE_UNAVAILABLE")) from None

    def _fc_code_or_none(self, tid, raw):
        """``_fc_code`` for a comparison (Finance's answer): None when it cannot be coded (never equal)."""
        try:
            return self._fc_code(tid, raw) if isinstance(raw, str) and isinstance(tid, str) else None
        except Exception:  # noqa: BLE001 - fail closed: no code, no match
            return None

    def _fc_seal(self, tid: str, code: str, raw: str) -> str:
        import vault as vault_mod
        from errors import Unavailable
        try:
            return self.optext_vault.put(tid, {"kind": self.FC_KIND, "code": code, "ref": raw})
        except vault_mod.VaultUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        except vault_mod.VaultError:
            raise Unavailable(R("STORE_UNAVAILABLE")) from None

    def _fc_open(self, tid, code):
        """E-COMP: the raw reference behind ``code`` from ``tid``'s OWN vault, or None (no key, lost, another
        tenant's record: AES-GCM is bound to the tenant). Used ONLY to build the Compliance activation request."""
        import vault as vault_mod
        if not isinstance(tid, str) or not isinstance(code, str):
            return None
        vids = self._fc_vids()
        vid = vids.get((tid, code))
        cands = [vid] if vid is not None else list(reversed(list(getattr(self.optext_vault, "_index", {}))))
        for v in cands:
            try:
                rec = self.optext_vault.get(tid, v)
            except vault_mod.VaultError:
                continue
            if isinstance(rec, dict) and rec.get("kind") == self.FC_KIND and rec.get("code") == code \
                    and isinstance(rec.get("ref"), str):
                vids[(tid, code)] = v
                return rec["ref"]
        return None

    def tenant_view(self, tid: str) -> dict:
        with self.lock:
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            out = {**{k: v for k, v in t.items() if k not in ("gsc_property_host", "gsc_vault_unavailable")},
                   "domains": list(t["domains"]),
                   "audits": sum(1 for a in self.audits.values() if a["tenant_id"] == tid)}
            if t.get("gsc_vault_unavailable"):            # sink job D: bound, the property text cannot be read
                out["gsc_vault_unavailable"] = True
            prop = t.get("gsc_property")
            if isinstance(prop, str) and prop.startswith("http"):
                shown, alert = url_view(prop)             # review c0c09bd1: the owner's view, client-view masked
                out["gsc_property"] = shown
                if alert:
                    out["gsc_property_masked"] = True
            return out

    def tenants_view(self) -> list:
        with self.lock:
            return [self.tenant_view(t) for t in sorted(self.tenants)]
