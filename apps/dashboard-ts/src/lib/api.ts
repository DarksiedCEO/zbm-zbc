import type { ScanResult } from "@/types/finding";

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? "http://localhost:8080";

// Deliberately NOT prefixed with NEXT_PUBLIC_ — this token must stay
// server-side only. fetchScanResult runs in a server component (this file
// is never imported by client components), so process.env here reads the
// real server-side value and the token never reaches the browser bundle.
// orchestrator-go fails closed without ORCHESTRATOR_SERVICE_TOKEN set, so
// a missing value here isn't a silent-empty-header problem in practice —
// the request will just 401 — but failing fast with a clear message is
// still better than an opaque 401 the first time someone loads the page.
const ORCHESTRATOR_SERVICE_TOKEN = process.env.ORCHESTRATOR_SERVICE_TOKEN;

export async function fetchScanResult(): Promise<ScanResult> {
  if (!ORCHESTRATOR_SERVICE_TOKEN) {
    throw new Error(
      "ORCHESTRATOR_SERVICE_TOKEN is not set. The dashboard cannot call " +
        "orchestrator-go without it — set it to the same value orchestrator-go " +
        "was started with (its ORCHESTRATOR_SERVICE_TOKEN env var)."
    );
  }

  const res = await fetch(`${ORCHESTRATOR_URL}/revenue-recovery/scan`, {
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${ORCHESTRATOR_SERVICE_TOKEN}`,
    },
  });
  if (!res.ok) {
    throw new Error(`orchestrator returned ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as ScanResult;
}
