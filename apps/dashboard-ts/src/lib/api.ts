import { newCorrelationId, type FailureKind, type LoadOutcome } from "./load-outcome.ts";
import { describeFetchFailure, describeTimeout, parseOrchestratorFailure } from "./orchestrator-error.ts";
import type { RecordedFindingsResult } from "../types/finding.ts";

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
// it must stay server-side only. This module is only imported server-side
// (src/proxy.ts, src/app/healthz/route.ts), so the token never reaches the
// browser.
//
// LOW-A (fix wave 1): this never throws. Every failure becomes a
// LoadOutcome with a kind (-> HTTP 502/503, see load-outcome.ts), a
// page-safe message and a correlation id. The request is bounded by
// ORCHESTRATOR_TIMEOUT_MS (default 10000) — it used to have no timeout.
export const DEFAULT_TIMEOUT_MS = 10_000;

export function timeoutMs(env: Record<string, string | undefined>): number {
  const raw = env.ORCHESTRATOR_TIMEOUT_MS;
  if (raw === undefined || raw === "") return DEFAULT_TIMEOUT_MS;
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 && n <= 600_000 ? n : DEFAULT_TIMEOUT_MS;
}

function isTimeout(e: unknown): boolean {
  return e instanceof Error && (e.name === "TimeoutError" || e.name === "AbortError");
}

function fail(kind: FailureKind, message: string, correlationId: string | null, detail: unknown): LoadOutcome {
  const id = correlationId ?? newCorrelationId();
  // Fix wave 3 (AEGIS D3): full detail (it can hold internal URLs,
  // host:port, raw bodies) goes to the server log only, under the same id.
  console.error(`dashboard: load failed [${kind}] correlation_id=${id}:`, detail);
  return { ok: false, kind, message, correlationId: id };
}

const EVIDENCE_CLASSES: ReadonlySet<unknown> = new Set(["OBSERVED", "ESTIMATED", "MODELED", "UNKNOWN"]);

// AEGIS M1 (Oct 7 2026): every finding must say whether the latest scan still
// found it and how its figure was obtained, and name its tenant and scan. A
// body without them (an orchestrator from before the fix wave) is refused as
// not the contract — never rendered with every finding looking current.
function looksLikeRecordedFinding(v: unknown): boolean {
  if (!v || typeof v !== "object") return false;
  const f = v as Record<string, unknown>;
  return (
    typeof f.finding_id === "string" &&
    typeof f.client_id === "string" &&
    typeof f.scan_id === "string" &&
    (f.period_label === null || typeof f.period_label === "string") &&
    typeof f.present_in_latest_scan === "boolean" &&
    EVIDENCE_CLASSES.has(f.evidence_class) &&
    (f.amount_usd === null) === (f.evidence_class === "UNKNOWN")
  );
}

function looksLikeFindings(v: unknown): v is RecordedFindingsResult {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return (
    Array.isArray(o.findings) &&
    o.findings.every(looksLikeRecordedFinding) &&
    !!o.overlapping_claims &&
    typeof o.overlapping_claims === "object" &&
    Array.isArray(o.excluded_scans) &&
    Array.isArray(o.legacy_findings) &&
    typeof o.legacy_finding_entries_ignored === "number" &&
    typeof o.ledger_entries_total === "number" &&
    typeof o.finding_entries_total === "number" &&
    "ledger_verify" in o
  );
}

export async function loadRecordedFindings(
  env: Record<string, string | undefined> = process.env,
  fetchImpl: typeof fetch = fetch
): Promise<LoadOutcome> {
  const orchestratorUrl = env.ORCHESTRATOR_URL ?? "http://localhost:8080";
  const token = env.ORCHESTRATOR_SERVICE_TOKEN;
  if (!token) {
    return fail(
      "not_configured",
      "ORCHESTRATOR_SERVICE_TOKEN is not set. The dashboard cannot call " +
        "orchestrator-go without it — set it to the same value orchestrator-go " +
        "was started with (its ORCHESTRATOR_SERVICE_TOKEN env var).",
      null,
      "ORCHESTRATOR_SERVICE_TOKEN is not set"
    );
  }

  const ms = timeoutMs(env);
  const signal = AbortSignal.timeout(ms);
  let res: Response;
  try {
    res = await fetchImpl(`${orchestratorUrl}/revenue-recovery/findings`, {
      method: "GET",
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });
  } catch (e) {
    if (isTimeout(e)) return fail("timeout", describeTimeout(ms), null, e);
    return fail("unreachable", describeFetchFailure(e), null, e);
  }

  let body: string;
  try {
    body = await res.text();
  } catch (e) {
    if (isTimeout(e)) return fail("timeout", describeTimeout(ms), null, e);
    return fail("bad_response", `orchestrator returned ${res.status} with an unreadable body`, null, e);
  }

  if (!res.ok) {
    const { message, correlationId } = parseOrchestratorFailure(res.status, body);
    const kind: FailureKind = res.status === 401 || res.status === 403 ? "upstream_auth" : "upstream_error";
    return fail(kind, message, correlationId, `orchestrator returned ${res.status}: ${body}`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch (e) {
    return fail("bad_response", "orchestrator returned a body that is not JSON", null, e);
  }
  if (!looksLikeFindings(parsed)) {
    return fail("bad_response", "orchestrator returned JSON that is not the recorded-findings contract", null, body.slice(0, 2000));
  }
  return { ok: true, result: parsed };
}
