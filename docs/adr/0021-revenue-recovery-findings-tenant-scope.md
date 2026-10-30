# ADR 0021 — Revenue Recovery findings are read per tenant

Status: **proposed, not merged** (branch `fix/rr-findings-tenant-scope` from `919c29b`, Oct 30 2026). Amends the
`GET /revenue-recovery/findings` contract of ADR 0001 / ADR 0003 section 13. Closes AEGIS A2 finding M-1 for the
default path; the client-facing credential is an open decision (below).

## Context

AEGIS A2 M-1: `GET /revenue-recovery/findings` on the public orchestrator returned every tenant's findings, scans,
claims and excluded scans in one response, with no client filter. That was safe only because the one caller is the
dashboard, which is Andre-only (one scrypt password, one orchestrator bearer token). A client-facing view, or a second
tenant served by any caller other than the operator, would read every other tenant's findings.

Where the data lives: the findings are the `revenue_recovery` events of the evidence ledger (ledger-rust). The tenant
is recorded only on a scan's `rr_scan_started` / `rr_scan_completed` events (`subject_id`); finding events are keyed by
finding id and tied to their tenant through their scan. detection-py is not on this read path (it computes findings
per scan request, for the one `client_id` it is given, and stores none). So the lowest layer that can filter by
tenant without a ledger schema change is the orchestrator's ledger fold (`internal/orchestrator/recorded.go`).

## Decision

1. `GET /revenue-recovery/findings` takes exactly one of `?client_id=<tenant>` or `?scope=all`, each at most once, and
   no other parameter. Neither: `400` `"reason":"FINDINGS_SCOPE_REQUIRED"`. Every refusal happens before the ledger is
   read. `client_id` follows `ValidClientID` (1-64 characters, `[A-Za-z0-9][A-Za-z0-9._:-]*`), the rule scan tenants
   are recorded under.
2. `scope=all` (every tenant) is granted only to the operator credential. `findingsHandler(orch, allowAllTenants)`
   takes the grant from the route that mounts it: the `ORCHESTRATOR_SERVICE_TOKEN` route passes `true`; any future
   tenant-bound credential must pass `false` (then `scope=all` is `403` `"reason":"ALL_TENANTS_FORBIDDEN"`).
3. `Orchestrator.RecordedFindings(ctx, FindingsScope)` refuses the zero scope (`ErrInvalidScope`), so no Go caller gets
   every tenant by omission either. The fold, validation and every per-client rule (latest scan, backdating, N1) run
   over the whole ledger unchanged; the result is narrowed afterwards, so a tenant's view is exactly the operator
   view's rows for that tenant. In a tenant view: findings, overlapping claims, scans, excluded scans,
   `latest_scan_uncounted`, `finding_entries_total` and `non_live_data_source` are that tenant's; legacy findings
   (pre-Oct-6, no tenant) are omitted (`legacy_findings: []`, count 0).
4. The response says which view it is: `"scope": "client" | "all_tenants"`, `"scope_client_id"`.
5. The dashboard (operator view) requests `?scope=all`. Deploy the dashboard first: an older orchestrator ignores the
   query, so the new dashboard works against both; an old dashboard against the new orchestrator gets `400`.

## Consequences and residuals

- The ledger-wide fields (`ledger_entries_total`, `ledger_total_source`, `ledger_entries_read`, `ledger_verify`) still
  describe the whole ledger in a tenant view: counts and a verdict, no other tenant's rows. A client-facing view should
  decide whether to show them (they reveal overall ledger volume).
- Each request still reads and folds the whole ledger (AEGIS L-4); per-tenant filtering at the ledger needs a tenant on
  every event or a ledger-side index, out of scope here.

## Open decision (product, Andre)

How a client authenticates to a client-facing findings view: a per-tenant bearer issued by the orchestrator, a
dashboard session bound to one tenant that the dashboard maps to `client_id`, or something else. Until decided there is
no tenant-bound credential; the only caller is the operator dashboard.
