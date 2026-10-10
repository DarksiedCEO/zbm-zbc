import { loadRecordedFindingsCached } from "@/lib/api";
import { healthWord, httpStatusFor } from "@/lib/load-outcome";

// LOW-A (fix wave 1, Sep 24 2026): a monitoring endpoint whose status is the
// dashboard's real ability to show findings — the same read "/" does, the
// same 200/502/503 mapping (src/lib/load-outcome.ts), as JSON:
//
//   200 {"status":"ok","ledger_entries":N}
//   503 {"status":"unavailable","error":...,"correlation_id":...}
//   502 {"status":"upstream_error","error":...,"correlation_id":...}
//   502 {"status":"ledger_invalid","error":...}
//
// Read-only like "/": the orchestrator route it calls never writes. Bug sweep
// E: it needs a session like every route (401 without one; src/proxy.ts), and
// it shares the brief per-process ledger cache with "/" (src/lib/api.ts).
export const dynamic = "force-dynamic";

export async function GET() {
  const outcome = await loadRecordedFindingsCached();
  const status = httpStatusFor(outcome);
  const body: Record<string, unknown> = { status: healthWord(outcome) };
  if (outcome.ok) {
    body.ledger_entries = outcome.result.ledger_entries_total;
    if (status !== 200) body.error = `evidence ledger did not verify: ${outcome.result.ledger_verify?.error ?? "no verdict"}`;
  } else {
    body.error = outcome.message;
    body.correlation_id = outcome.correlationId;
  }
  return Response.json(body, { status, headers: { "cache-control": "no-store" } });
}
