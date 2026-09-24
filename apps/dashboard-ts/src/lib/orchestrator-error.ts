// What the page may show when the orchestrator call fails (fix wave 3,
// AEGIS D3). orchestrator-go now returns {"error": <public message>,
// "correlation_id": <id>} with no internal addresses; the page shows only
// those two fields. Anything else — an unexpected body, a transport error —
// is summarized without its text, and the raw detail is logged server-side
// (see src/lib/api.ts). As defense in depth, anything that looks like a URL
// or host:port is scrubbed even from the public message.

const URL_LIKE = /\b[a-z][a-z0-9+.-]*:\/\/\S+/gi;
const HOST_PORT = /(\[[0-9a-f:.]+\]|\b[a-z0-9][a-z0-9.-]*|\b\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}\b/gi;
const IPV4 = /\b\d{1,3}(?:\.\d{1,3}){3}\b/g;
const CORRELATION_ID = /^[A-Za-z0-9_-]{1,64}$/;

function scrub(text: string): string {
  return text.replace(URL_LIKE, "[address hidden]").replace(HOST_PORT, "[address hidden]").replace(IPV4, "[address hidden]");
}

// LOW-A (fix wave 1): the message and the orchestrator's correlation id,
// separately, so the page, the server log and /healthz carry the same id.
export function parseOrchestratorFailure(status: number, bodyText: string): { message: string; correlationId: string | null } {
  let error: unknown;
  let correlationId: unknown;
  try {
    const parsed: unknown = JSON.parse(bodyText);
    if (parsed && typeof parsed === "object") {
      error = (parsed as Record<string, unknown>).error;
      correlationId = (parsed as Record<string, unknown>).correlation_id;
    }
  } catch {
    // not JSON — fall through to the generic message
  }
  const head = `orchestrator returned ${status}`;
  const id = typeof correlationId === "string" && CORRELATION_ID.test(correlationId) ? correlationId : null;
  if (typeof error !== "string" || error === "") {
    return { message: `${head} (details in the dashboard server log)`, correlationId: id };
  }
  return { message: `${head}: ${scrub(error).slice(0, 500)}`, correlationId: id };
}

export function describeOrchestratorFailure(status: number, bodyText: string): string {
  const { message, correlationId } = parseOrchestratorFailure(status, bodyText);
  return correlationId ? `${message} (correlation id ${correlationId})` : message;
}

export function describeFetchFailure(_err: unknown): string {
  return "could not reach the orchestrator (details in the dashboard server log)";
}

export function describeTimeout(ms: number): string {
  return `the orchestrator did not answer within ${ms} ms (request timed out)`;
}
