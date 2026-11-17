"""Per-tenant catalog allowlist for the client view (core-spec/2 AL-1, AL-2; owner default Q2-7; TI-1).

Per tenant the store keeps ONLY B-CD-01 codes of kind ``text`` (``codes.code("text", token)`` under the tenant's own
``vault.Coder``) of the exact tokens of the tenant's own catalog (Shopify product / variant SKUs, barcodes, model
numbers from the tenant's integration). No raw token is kept: the codes are computed inside a throw-away
``codes.coding`` block whose evidence map is dropped. Inside ``scope(store, tenant_id)`` a CV-3 / CV-4 span whose code
(of its exact input text) is in THAT tenant's set is shown (``cv_shape`` asks ``shown``); never a span that holds a
K-1 strong key or a K-5 shape (card, SSN, phone, userinfo, e-mail), never another tenant's set (each tenant's codes
are keyed by its own coder and looked up under the scoped tenant only).

Cap [OWNER DEFAULT Q2-7]: ``CAP`` distinct tokens a tenant; a larger set is refused and the set stays as it was.
Sources: the tenant's own catalog integrations only (``integration_tenant`` must equal the tenant). Every change
attempt writes ONE audit row (actor, time, count, source, outcome; typed T-1 fields, SINK_FIELDS ledger kind
``AUDIT_KIND``) BEFORE the set changes; if the row is not typed or the audit write fails, the set is unchanged."""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from datetime import datetime, timezone

import sink_guard
from primitives import codes

CAP = 50_000                                             # Q2-7 [OWNER DEFAULT]: catalog tokens a tenant
SOURCES = ("shopify",)                                   # the tenant's own catalog integrations (closed enum)
OUTCOMES = ("applied", "refused_cap", "refused_source")  # audit outcome enum (closed)
AUDIT_EVENT = "cv_allowlist_changed"
AUDIT_KIND = sink_guard.LEDGER + AUDIT_EVENT             # the ledger sink kind of the audit row (T-3)


class AllowlistRefused(Exception):
    """A change was refused (the reason is an OUTCOMES word; no value is carried)."""

    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


_ACTIVE: ContextVar = ContextVar("cv_allowlist", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _code(coder, raw: str) -> str:
    with codes.coding(coder) as ev:                      # throw-away block: its evidence map is cleared, never kept
        out = codes.code("text", raw)
        ev.values.clear()
    return out


class CatalogAllowlist:
    """{tenant_id: frozenset of ``{c:text:<16hex>}``}; ``coder_for(tenant_id)`` returns the tenant's ``vault.Coder``;
    ``audit(kind, row)`` writes one audit row to its sink (the service: the ledger) and raises on failure."""

    __slots__ = ("_coder_for", "_audit", "_sets")

    def __init__(self, coder_for, audit):
        self._coder_for = coder_for
        self._audit = audit
        self._sets: dict = {}

    def count(self, tenant_id: str) -> int:
        return len(self._sets.get(tenant_id, ()))

    def codes_of(self, tenant_id: str) -> frozenset:
        return self._sets.get(tenant_id, frozenset())

    def replace(self, tenant_id: str, tokens, *, actor: str, source: str, integration_tenant: str,
                at: str | None = None) -> int:
        """Set the tenant's allowlist to exactly ``tokens`` (a catalog sync). Audit row first; returns the count."""
        row, new = self.prepare(tenant_id, tokens, actor=actor, source=source, integration_tenant=integration_tenant,
                                at=at)
        self._audit(AUDIT_KIND, row)                     # audit BEFORE the change; a failure leaves the set unchanged
        if new is None:
            raise AllowlistRefused(row["outcome"])
        return self.install(tenant_id, new)

    def install(self, tenant_id: str, new) -> int:
        """Install an already-audited set of codes (C2-15b: the service, after its audited commit / on replay)."""
        self._sets[tenant_id] = frozenset(new)
        return len(self._sets[tenant_id])

    def forget(self, tenant_id: str) -> None:
        self._sets.pop(tenant_id, None)

    def prepare(self, tenant_id: str, tokens, *, actor: str, source: str, integration_tenant: str,
                at: str | None = None) -> tuple:
        """(typed audit row, frozenset of codes | None when refused) for one change attempt; nothing is changed."""
        toks = {t for t in tokens if isinstance(t, str) and t}
        outcome = "applied"
        if source not in SOURCES or integration_tenant != tenant_id:
            outcome = "refused_source"
        elif len(toks) > CAP:
            outcome = "refused_cap"
        row = {"tenant_id": tenant_id, "actor": actor, "at": at or _now(), "source": source if source in SOURCES
               else "unknown", "count": len(toks) if outcome == "applied" else 0,
               "previous_count": self.count(tenant_id), "requested_count": len(toks), "outcome": outcome}
        if sink_guard.violations(AUDIT_KIND, row):       # T-5: an untyped / mistyped row is never written
            raise AllowlistRefused("audit_untyped")
        new = frozenset(_code(self._coder_for(tenant_id), t) for t in toks) if outcome == "applied" else None
        return row, new


@contextlib.contextmanager
def scope(store: CatalogAllowlist, tenant_id: str):
    """The client view inside this block shows the spans listed for ``tenant_id`` (and only that tenant)."""
    entry = (store._coder_for(tenant_id), store.codes_of(tenant_id)) if store.count(tenant_id) else None
    tok = _ACTIVE.set(entry)
    try:
        yield
    finally:
        _ACTIVE.reset(tok)


def active() -> bool:
    return _ACTIVE.get() is not None


def _never(view_text: str) -> bool:
    """AL-1: a span that holds a K-1 strong key or a K-5 shape is never exempt (whole span and each token core)."""
    from agents import cv_shape
    from agents import secret_guard as sg                # lazy: secret_guard imports cv_shape
    for s in [view_text] + [cv_shape.core(t) for t in view_text.split(" ")]:
        if s and (sg._cv_prefix_found(s) or sg._cv_numbers(s) or sg._cv_email(s) or sg._cv_userinfo(s)):
            return True
    return False


def shown(input_text: str, view_text: str) -> bool:
    """True when the span's exact input text is in the scoped tenant's set and it is not K-1 / K-5."""
    entry = _ACTIVE.get()
    if entry is None or not input_text:
        return False
    coder, listed = entry
    if _code(coder, input_text) not in listed:
        return False
    return not _never(view_text)
