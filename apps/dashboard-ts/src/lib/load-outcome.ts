import { ledgerState } from "./ledger-status.ts";
import type { RecordedFindingsResult } from "../types/finding.ts";

// LOW-A (fix wave 1, Sep 24 2026): the result of reading the recorded
// findings, and the HTTP status it maps to. The page used to render an
// orchestrator failure with HTTP 200, so monitoring saw a healthy dashboard.
//
//   503  the dashboard cannot get an answer: orchestrator unreachable, timed
//        out, or ORCHESTRATOR_SERVICE_TOKEN not set (nothing to ask with)
//   502  the orchestrator answered, but not with usable findings: it
//        rejected the token (401/403), returned any other non-2xx, sent a
//        body that is not the findings contract, or reported that the
//        evidence ledger does not verify (integrity failure)
//   200  findings read and the ledger verified (including verified-empty)
export type FailureKind =
  | "not_configured"
  | "unreachable"
  | "timeout"
  | "upstream_auth"
  | "upstream_error"
  | "bad_response";

export type LoadOutcome =
  | { ok: true; result: RecordedFindingsResult }
  | { ok: false; kind: FailureKind; message: string; correlationId: string };

const UNAVAILABLE: ReadonlySet<FailureKind> = new Set(["not_configured", "unreachable", "timeout"]);

export function httpStatusFor(outcome: LoadOutcome): 200 | 502 | 503 {
  if (outcome.ok) return ledgerState(outcome.result) === "invalid" ? 502 : 200;
  return UNAVAILABLE.has(outcome.kind) ? 503 : 502;
}

// /healthz body status word.
export function healthWord(outcome: LoadOutcome): "ok" | "unavailable" | "upstream_error" | "ledger_invalid" {
  if (outcome.ok) return ledgerState(outcome.result) === "invalid" ? "ledger_invalid" : "ok";
  return UNAVAILABLE.has(outcome.kind) ? "unavailable" : "upstream_error";
}

// A correlation id for failures the orchestrator did not give one for
// (unreachable, timeout, not configured): logged with the raw detail
// server-side and shown on the page, so the two can be matched.
export function newCorrelationId(): string {
  return globalThis.crypto.randomUUID().replace(/-/g, "").slice(0, 16);
}
