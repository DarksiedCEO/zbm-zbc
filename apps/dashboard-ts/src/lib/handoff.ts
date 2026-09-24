import type { FailureKind, LoadOutcome } from "./load-outcome.ts";

// LOW-A (fix wave 1): how src/proxy.ts hands its single orchestrator read to
// the page. Why this exists: Next.js App Router gives a page component no way
// to set its response status other than notFound()/forbidden()/
// unauthorized() (404/403/401), redirect() (3xx), or throwing (500 with the
// message replaced by a digest). A 502/503 that still renders the sanitized
// message therefore has to be decided BEFORE the page renders — in the
// proxy, via NextResponse.next({ status }) — and the page must render the
// very outcome that decided the status (one upstream read; the status and
// the page can never disagree).
//
// Channel: the documented proxy -> page mechanism, an overridden request
// header (NextResponse.next({ request: { headers } }) + headers() in the
// page). The value is base64url(JSON). The proxy runs on every request and
// always deletes any client-supplied value first (see src/proxy.ts), so a
// client cannot inject an outcome.
export const HANDOFF_HEADER = "x-zbm-dashboard-load";

const KINDS: ReadonlySet<FailureKind> = new Set([
  "not_configured",
  "unreachable",
  "timeout",
  "upstream_auth",
  "upstream_error",
  "bad_response",
]);

export function encodeHandoff(outcome: LoadOutcome): string {
  return Buffer.from(JSON.stringify(outcome), "utf8").toString("base64url");
}

// null when absent or malformed — the page then fails closed (see page.tsx).
export function decodeHandoff(value: string | null): LoadOutcome | null {
  if (!value) return null;
  let v: unknown;
  try {
    v = JSON.parse(Buffer.from(value, "base64url").toString("utf8"));
  } catch {
    return null;
  }
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  if (o.ok === true && o.result && typeof o.result === "object") return o as LoadOutcome;
  if (
    o.ok === false &&
    typeof o.kind === "string" &&
    KINDS.has(o.kind as FailureKind) &&
    typeof o.message === "string" &&
    typeof o.correlationId === "string"
  ) {
    return o as LoadOutcome;
  }
  return null;
}
