import type { ScanResult } from "@/types/finding";

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? "http://localhost:8080";

export async function fetchScanResult(): Promise<ScanResult> {
  const res = await fetch(`${ORCHESTRATOR_URL}/revenue-recovery/scan`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`orchestrator returned ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as ScanResult;
}
