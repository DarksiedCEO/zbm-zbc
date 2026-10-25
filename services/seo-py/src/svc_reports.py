"""
The client report of a completed audit (ADR 0017, section "Client report"): rendered by ``client_report``
(deterministic, no model call) and recorded before it is handed out.

Rules:
  - Andre's (the dashboard caller plus his approval token), for one tenant's completed audit; another tenant's audit
    answers the same 404 as a missing one;
  - record-first: ``client_report_rendered`` (audit id, tenant, format, renderer version, the audit report's SHA-256,
    the document's SHA-256 and size, the branding's SHA-256) is on the ledger and the log before the document leaves
    the service; a render that cannot be recorded is not returned;
  - idempotent by request id: the same request answers the same document; when the retained view changed since (the
    Search Console breakdowns expired) the replay is refused ``REPORT_CHANGED`` instead of handing out a document that
    differs from the recorded hash;
  - kill switches: global, write, the tenant's and ``capability:reports`` (and ``capability:audit``, the product the
    report belongs to).
"""

from __future__ import annotations

import client_report
from errors import Conflict
from ledger import derived_id
from reasons import R


class ReportsMixin:
    def render_client_report(self, tid: str, aid: str, body: dict) -> dict:
        fmt = body["format"]
        with self.lock:
            self._gate()
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
            view = self.audit_view(tid, aid)                     # 404 AUDIT_NOT_FOUND for another tenant's audit
            self.check_kill(tenant=tid, capability="reports", write=True)
            self.check_kill(capability="audit")
            if view["status"] != "completed":
                raise Conflict(R("AUDIT_NOT_COMPLETED"))
            rk = self.rk("client_report", f"{tid}/{aid}", body)
            prev = self._idem("andre", rk, body)
            try:
                data, _model = client_report.render(view, tid, fmt, self.settings.report_branding)
            except client_report.ReportInputError:
                raise Conflict(R("REPORT_NOT_RENDERABLE")) from None
            dsha = client_report.document_sha256(data)
            if prev:
                rec = self.client_reports[prev[1]]
                if rec["document_sha256"] != dsha:
                    raise Conflict(R("REPORT_CHANGED"))
                return self._report_answer(rec, data)
            rid = derived_id("rpt", tid, aid, rk)
            rec = {"report_id": rid, "tenant_id": tid, "audit_id": aid, "format": fmt,
                   "renderer_version": client_report.RENDERER_VERSION,
                   "audit_report_sha256": view["report_sha256"], "document_sha256": dsha, "bytes": len(data),
                   "branding_sha256": self.settings.report_branding.digest()}
            self._commit("client_report_rendered", self._req(rec, "andre", rk, body, rid), "andre",
                         evidence=("client_report_rendered", f"audit:{aid}", dict(rec), ("andre", rk)))
            return self._report_answer(self.client_reports[rid], data)

    def _a_client_report_rendered(self, d, at):
        self.client_reports[d["report_id"]] = {k: d[k] for k in (
            "report_id", "tenant_id", "audit_id", "format", "renderer_version", "audit_report_sha256",
            "document_sha256", "bytes", "branding_sha256")} | {"rendered_at": at}

    @staticmethod
    def _report_answer(rec: dict, data: bytes) -> dict:
        return {**rec, "media_type": client_report.FORMATS[rec["format"]], "content": data.decode("utf-8")}

    def client_reports_view(self, tid: str, aid: str) -> list:
        """The recorded renders of one audit (ids and hashes only; never the document)."""
        with self.lock:
            self.audit_view(tid, aid, full=False)
            return [dict(r) for r in sorted(self.client_reports.values(), key=lambda r: (r["rendered_at"],
                                                                                           r["report_id"]))
                    if r["tenant_id"] == tid and r["audit_id"] == aid][:1000]
