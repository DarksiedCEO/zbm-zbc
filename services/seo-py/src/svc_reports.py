"""
The client report of a completed audit (ADR 0017, section "Client report"): rendered by ``client_report``
(deterministic, no model call) and recorded before it is handed out.

Rules:
  - Andre's (the dashboard caller plus his approval token), for one tenant's completed audit; another tenant's audit
    answers the same 404 as a missing one;
  - record-first: ``client_report_rendered`` (audit id, tenant, format, renderer version, the audit report's keyed
    HMAC (sink job B; its plain SHA-256 only for an audit recorded before), the document's SHA-256 and size, the
    branding's SHA-256) is on the ledger and the log before the document leaves
    the service; a render that cannot be recorded is not returned;
  - idempotent by request id: the same request answers the same document; when the retained view changed since (the
    Search Console breakdowns expired) the replay is refused ``REPORT_CHANGED`` instead of handing out a document that
    differs from the recorded hash;
  - kill switches: global, write, the tenant's and ``capability:reports`` (and ``capability:audit``, the product the
    report belongs to).
"""

from __future__ import annotations

import urllib.parse

import client_report
from errors import Conflict
from ledger import derived_id
from primitives import codes as codes_mod
from reasons import R


class ReportsMixin:
    def render_client_report(self, tid: str, aid: str, body: dict) -> dict:
        fmt = body["format"]
        with self.lock:
            self._gate()
            t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            self.audit_view(tid, aid, full=False)         # 404 AUDIT_NOT_FOUND first: nothing else is revealed
            client = t.get("finance_client_id") if t["kind"] == "client" else None
            if t["kind"] == "client" and not client:
                raise Conflict(R("TENANT_FINANCE_CLIENT_UNBOUND"))
        # ADR 0024: a client's report is delivered only while Compliance's activation is a current ALLOW (asked
        # outside the lock; no override)
        if client is not None:
            self._verify_compliance(client)
        rk = self.rk("client_report", f"{tid}/{aid}", body)
        with self.lock:
            self._report_checks(tid, aid, client)
        # sink job B: the audit report is read from the tenant's vault and the document rendered OUTSIDE the lock; a
        # report that cannot be read refuses VAULT_UNAVAILABLE (nothing is rendered from anything else)
        view = self.client_report_view(tid, aid)
        try:
            data, model = client_report.render(view, tid, fmt, self.settings.report_branding)
        except client_report.ReportInputError:
            raise Conflict(R("REPORT_NOT_RENDERABLE")) from None
        finally:
            view = {k: v for k, v in view.items() if k != "report"}
        dsha = client_report.document_sha256(data)
        with self.lock:
            self._report_checks(tid, aid, client)         # re-checked: the lock was released for the vault read
            prev = self._idem("andre", rk, body)
            if prev:
                rec = self.client_reports[prev[1]]
                if rec["document_sha256"] != dsha:
                    raise Conflict(R("REPORT_CHANGED"))
                return self._report_answer(rec, data)
            rid = derived_id("rpt", tid, aid, rk)
            # sink job B: the audit report's hash is the keyed HMAC recorded with it (``audit_report_hmac``); an audit
            # recorded before sink job B has its plain SHA-256 (``audit_report_sha256``) and no HMAC
            rec = {"report_id": rid, "tenant_id": tid, "audit_id": aid, "format": fmt,
                   "renderer_version": model["renderer_version"],
                   "audit_report_hmac": view.get("report_hmac"),
                   "audit_report_sha256": view.get("report_sha256"), "document_sha256": dsha, "bytes": len(data),
                   "branding_sha256": self.settings.report_branding.digest()}
            self._commit("client_report_rendered", self._req(rec, "andre", rk, body, rid), "andre",
                         evidence=("client_report_rendered", f"audit:{aid}", dict(rec), ("andre", rk)))
            return self._report_answer(self.client_reports[rid], data)

    def _report_checks(self, tid: str, aid: str, client) -> None:
        """Under the lock: the tenant's binding is the one Compliance was asked about, the audit is this tenant's and
        completed, and no kill switch refuses the render."""
        self._gate()
        t = self._get(self.tenants, tid, "TENANT_NOT_FOUND")
        if t["kind"] == "client" and t.get("finance_client_id") != client:
            raise Conflict(R("TENANT_FINANCE_CLIENT_CHANGED"))
        view = self.audit_view(tid, aid, full=False)      # 404 AUDIT_NOT_FOUND for another tenant's audit
        self.check_kill(tenant=tid, capability="reports", write=True)
        self.check_kill(capability="audit")
        if view["status"] != "completed":
            raise Conflict(R("AUDIT_NOT_COMPLETED"))

    def _a_client_report_rendered(self, d, at):
        self.client_reports[d["report_id"]] = {k: d[k] for k in (
            "report_id", "tenant_id", "audit_id", "format", "renderer_version", "audit_report_sha256",
            "document_sha256", "bytes", "branding_sha256")} | {"rendered_at": at,
                                                               "audit_report_hmac": d.get("audit_report_hmac")}

    @staticmethod
    def _report_answer(rec: dict, data: bytes) -> dict:
        return {**rec, "media_type": client_report.FORMATS[rec["format"]], "content": data.decode("utf-8")}

    def client_report_view(self, tid: str, aid: str) -> dict:
        """OUTSIDE the lock: the audit view a client report is rendered from (spec D-02g): the recorded view with
        every code resolved through the run's evidence map, the request paths in their URL form."""
        view = self._audit_recorded(tid, aid)
        # spec D-02g: the owner's client report shows the codes RESOLVED through the run's evidence map (this
        # tenant's only), cleaned as every crawled value is (client_report CR-5); a code without a value is shown
        # "[not available]"
        resolved = codes_mod.resolve(view["report"], self._evidence_values(tid, aid))
        with self.lock:
            rec = self.audits.get(aid) or {}
            raw_paths = [p for p in rec.get("paths") or [] if isinstance(p, str)]
        # the operator's own request paths come back resolved: a flagged one is shown in the strict lane's kept form,
        # exactly as before codes (review c0c09bd1, ``_report_paths_kept``)
        resolved = self._report_paths_kept(resolved, {"scheme": resolved.get("scheme"),
                                                      "domain": resolved.get("domain"), "paths": raw_paths})
        # a request path is a URL path: a resolved one is shown percent-encoded (a space as %20), and one that does
        # not resolve to a URL path keeps its stored (coded) form
        stored = view["report"].get("paths") or []
        shown = []
        for i, p in enumerate(resolved.get("paths") or []):
            q = urllib.parse.quote(p, safe="".join(chr(c) for c in range(0x21, 0x7f) if chr(c) != "%")) \
                if isinstance(p, str) else p
            ok = isinstance(q, str) and q.startswith("/") and len(q) <= 501 and codes_mod.NOT_AVAILABLE not in p
            shown.append(q if ok else (stored[i] if i < len(stored) else q))
        if "paths" in resolved:
            resolved["paths"] = shown
        view["report"] = resolved
        return view

    def client_reports_view(self, tid: str, aid: str) -> list:
        """The recorded renders of one audit (ids and hashes only; never the document)."""
        with self.lock:
            self.audit_view(tid, aid, full=False)
            return [dict(r) for r in sorted(self.client_reports.values(), key=lambda r: (r["rendered_at"],
                                                                                           r["report_id"]))
                    if r["tenant_id"] == tid and r["audit_id"] == aid][:1000]
