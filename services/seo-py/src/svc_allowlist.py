"""The client-view catalog allowlist wired into the service (core-spec/2 AL-1, AL-2, TI-1, V-3/V-4; card C2-15b).

  service API (no HTTP route yet: the route surface is the lead's call)  ``cv_allowlist_sync(tid, body, actor)``
        body {source: "shopify", integration_tenant: <the integration's tenant>, tokens: [catalog tokens]}

Live wiring defaults OFF (``Settings.cv_allowlist``, env ``SEO_CV_ALLOWLIST``): OFF, the sync is refused and no view
is ever scoped (every view is exactly today's). Each attempt is ONE audited commit: the typed audit row (actor, time,
count, source, outcome; ledger event ``cv_allowlist_changed``) is recorded on the ledger FIRST (``_commit`` evidence),
then the log line ``cv_allowlist_set`` (tenant, outcome, count, the vault blob ref of the codes), then memory. A
refused attempt (foreign integration, not a catalog source, over the cap) is audited the same way and changes nothing.
The set is kept ONLY as B-CD-01 ``text`` codes, in a blob of the tenant's own vault; no raw token is written anywhere.
After a restart the log replays the ref and the codes are read back from the tenant's vault on the first owner view.
Fail closed: no vault key, a blob that cannot be read, or a code that is not ``{c:text:<16hex>}`` -> no allowlist for
that tenant (spans stay masked). ``_cv_scope(tid)`` wraps ONLY the owning tenant's client view (svc_citations)."""
from __future__ import annotations

import contextlib
import re

import vault as vault_mod
from agents import cv_allowlist
from errors import Invalid, Unavailable
from reasons import R

LOG_KIND = "cv_allowlist_set"
_CODE = re.compile(r"\{c:text:[0-9a-f]{16}\}\Z")


class CvAllowlistMixin:
    """State: ``self._cv_allow`` (CatalogAllowlist, memory) and ``self._cv_allow_refs`` {tid: blob ref | None}."""

    def _cv_allow_init(self) -> None:
        self._cv_allow = cv_allowlist.CatalogAllowlist(self.vault.coder, self._cv_allow_no_direct)
        self._cv_allow_refs: dict = {}

    @staticmethod
    def _cv_allow_no_direct(kind, row):
        raise Unavailable(R("LEDGER_UNAVAILABLE"))       # the service never audits outside ``_commit``

    def cv_allowlist_sync(self, tid: str, body: dict, actor: str) -> dict:
        if not getattr(self.settings, "cv_allowlist", False):
            raise Unavailable(R("INVALID"))              # live wiring OFF (default): nothing is audited or changed
        self._gate()
        with self.lock:
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
        b = body if isinstance(body, dict) else {}
        tokens, source, itid = b.get("tokens"), b.get("source"), b.get("integration_tenant")
        if not isinstance(tokens, list) or not isinstance(source, str) or not isinstance(itid, str):
            raise Invalid(R("INVALID"))
        try:
            row, new = self._cv_allow.prepare(tid, tokens, actor=actor, source=source, integration_tenant=itid,
                                              at=_iso(self.now()))
        except cv_allowlist.AllowlistRefused:
            raise Invalid(R("SINK_REFUSED")) from None   # the audit row would not be typed: nothing written
        except vault_mod.VaultUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        ref = None
        if new is not None:
            try:
                ref = self.vault.put_blob(tid, sorted(new))      # codes only (V-3 / V-4), the tenant's own vault
            except vault_mod.VaultUnavailable:
                raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        data = {"tenant_id": tid, "outcome": row["outcome"], "count": row["count"], "ref": ref}
        self._commit(LOG_KIND, data, actor, evidence=(cv_allowlist.AUDIT_EVENT, tid, row, (tid, row["at"])))
        if new is None:
            raise Invalid(R("INVALID"))                  # refused (outcome in the audit row); the set is unchanged
        self._cv_allow.install(tid, new)
        return {"tenant_id": tid, "outcome": row["outcome"], "count": row["count"]}

    def _a_cv_allowlist_set(self, d, at):
        if d.get("outcome") != "applied":
            return
        self._cv_allow_refs[d["tenant_id"]] = d.get("ref")
        self._cv_allow.forget(d["tenant_id"])            # (re)loaded from the vault on the next owner view

    def _cv_allow_load(self, tid: str) -> bool:
        """True when the tenant's set is in memory (read back from its vault blob if needed); fail closed."""
        if self._cv_allow.count(tid) or tid not in self._cv_allow_refs:
            return self._cv_allow.count(tid) > 0
        try:
            got = self.vault.get_blob(tid, self._cv_allow_refs[tid])
        except Exception:  # noqa: BLE001 - any read failure: no allowlist (masked), never a crash of the view
            return False
        if not isinstance(got, list) or not all(isinstance(c, str) and _CODE.match(c) for c in got):
            return False
        self._cv_allow.install(tid, got)
        return bool(got)

    def _cv_scope(self, tid: str):
        """The owner's client view of ``tid`` (only): allowlist scope when wiring is ON and the set loads."""
        if not getattr(self.settings, "cv_allowlist", False) or not self.vault.key_configured:
            return contextlib.nullcontext()
        try:
            if not self._cv_allow_load(tid):
                return contextlib.nullcontext()
            return cv_allowlist.scope(self._cv_allow, tid)
        except Exception:  # noqa: BLE001 - fail closed: masked as without the allowlist
            return contextlib.nullcontext()


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
