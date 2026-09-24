import type { RecordedFindingsResult } from "@/types/finding";

// Fix wave 1 (Sep 24 2026): the dashboard used to GET /revenue-recovery/scan
// on every page view, which ran every agent and appended ~10 duplicate
// findings to the evidence ledger per view. It now reads the findings the
// ledger already recorded, from the orchestrator's read-only route. Running
// a scan is an explicit POST /revenue-recovery/scan, never a page view.
//
// Read at request time (not module load) so a value set when the server
// starts is always the one used.
//
// ORCHESTRATOR_SERVICE_TOKEN is deliberately NOT prefixed with NEXT_PUBLIC_ —
// it must stay server-side only. This module is only imported by the server
// component in src/app/page.tsx, so the token never reaches the browser.
export async function fetchRecordedFindings(): Promise<RecordedFindingsResult> {
  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? "http://localhost:8080";
  const token = process.env.ORCHESTRATOR_SERVICE_TOKEN;
  if (!token) {
    throw new Error(
      "ORCHESTRATOR_SERVICE_TOKEN is not set. The dashboard cannot call " +
        "orchestrator-go without it — set it to the same value orchestrator-go " +
        "was started with (its ORCHESTRATOR_SERVICE_TOKEN env var)."
    );
  }

  const res = await fetch(`${orchestratorUrl}/revenue-recovery/findings`, {
    method: "GET",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });
  if (!res.ok) {
    throw new Error(`orchestrator returned ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as RecordedFindingsResult;
}
