// LOW-A (fix wave 1, Sep 24 2026): every way reading the recorded findings
// can go, and the HTTP status each maps to (503 cannot get an answer, 502
// upstream answered badly, 200 verified). The on-the-wire check is
// tests/status.live.test.mjs; this pins the mapping and the handoff.
//
// Run: npm test

import { test } from "node:test";
import assert from "node:assert/strict";

import { loadRecordedFindings, timeoutMs, DEFAULT_TIMEOUT_MS } from "../src/lib/api.ts";
import { httpStatusFor, healthWord } from "../src/lib/load-outcome.ts";
import { decodeHandoff, encodeHandoff } from "../src/lib/handoff.ts";

const ENV = { ORCHESTRATOR_URL: "http://127.0.0.1:1", ORCHESTRATOR_SERVICE_TOKEN: "t" };
const OK_BODY = {
  findings: [],
  overlapping_claims: {},
  scans: [],
  excluded_scans: [],
  ledger_entries_total: 0,
  ledger_total_source: "head" as const,
  ledger_entries_read: 0,
  finding_entries_total: 0,
  legacy_finding_entries_ignored: 0,
  legacy_findings: [],
  ledger_verify: { valid: true, entries: 0, error: "" },
  non_live_data_source: true,
};

function respond(status: number, body: unknown): typeof fetch {
  return (async () => new Response(typeof body === "string" ? body : JSON.stringify(body), { status })) as typeof fetch;
}
function throwing(err: Error): typeof fetch {
  return (async () => {
    throw err;
  }) as typeof fetch;
}

// Server-log noise from the failure paths is expected; keep test output readable.
console.error = () => {};

test("healthy and verified -> 200", async () => {
  const o = await loadRecordedFindings(ENV, respond(200, OK_BODY));
  assert.equal(o.ok, true);
  assert.equal(httpStatusFor(o), 200);
  assert.equal(healthWord(o), "ok");
});

test("ledger does not verify (or no verdict) -> 502", async () => {
  for (const ledger_verify of [{ valid: false, entries: 2, error: "ChainBroken" }, null]) {
    const o = await loadRecordedFindings(ENV, respond(200, { ...OK_BODY, ledger_entries_total: 2, ledger_verify }));
    assert.equal(httpStatusFor(o), 502);
    assert.equal(healthWord(o), "ledger_invalid");
  }
});

test("orchestrator unreachable -> 503 with a generated correlation id, no address", async () => {
  const err = new TypeError("fetch failed", { cause: new Error("connect ECONNREFUSED 127.0.0.1:20173") });
  const o = await loadRecordedFindings(ENV, throwing(err));
  assert.equal(o.ok, false);
  if (o.ok) return;
  assert.equal(o.kind, "unreachable");
  assert.equal(httpStatusFor(o), 503);
  assert.match(o.correlationId, /^[0-9a-f]{16}$/);
  assert.ok(!o.message.includes("127.0.0.1") && !o.message.includes("20173"), o.message);
});

test("timeout -> 503; the request carries an abort signal", async () => {
  let sawSignal = false;
  const hang = (async (_u: unknown, init?: RequestInit) => {
    sawSignal = init?.signal instanceof AbortSignal;
    return new Promise<Response>((_r, reject) => {
      init?.signal?.addEventListener("abort", () => reject(init.signal!.reason));
    });
  }) as typeof fetch;
  // AbortSignal.timeout's timer is unref'd; hold the event loop open for it.
  const keepAlive = setTimeout(() => {}, 5000);
  const o = await loadRecordedFindings({ ...ENV, ORCHESTRATOR_TIMEOUT_MS: "50" }, hang);
  clearTimeout(keepAlive);
  assert.ok(sawSignal);
  assert.equal(o.ok, false);
  if (o.ok) return;
  assert.equal(o.kind, "timeout");
  assert.equal(httpStatusFor(o), 503);
  assert.match(o.message, /within 50 ms/);
});

test("token not set -> 503", async () => {
  const o = await loadRecordedFindings({ ORCHESTRATOR_URL: "http://x" }, respond(200, OK_BODY));
  assert.equal(o.ok, false);
  if (o.ok) return;
  assert.equal(o.kind, "not_configured");
  assert.equal(httpStatusFor(o), 503);
});

test("token rejected -> 502 (upstream_auth)", async () => {
  for (const s of [401, 403]) {
    const o = await loadRecordedFindings(ENV, respond(s, { error: "invalid token" }));
    assert.equal(o.ok, false);
    if (o.ok) return;
    assert.equal(o.kind, "upstream_auth");
    assert.equal(httpStatusFor(o), 502);
    assert.equal(o.message, `orchestrator returned ${s}: invalid token`);
  }
});

test("orchestrator error keeps ITS correlation id; body is scrubbed", async () => {
  const o = await loadRecordedFindings(
    ENV,
    respond(502, { error: "ledger at http://10.0.0.5:8090 is down", correlation_id: "abc123DEF_-" })
  );
  assert.equal(o.ok, false);
  if (o.ok) return;
  assert.equal(o.kind, "upstream_error");
  assert.equal(httpStatusFor(o), 502);
  assert.equal(o.correlationId, "abc123DEF_-");
  assert.ok(!o.message.includes("10.0.0.5") && !o.message.includes("8090"), o.message);
});

test("2xx with a body that is not the contract -> 502", async () => {
  for (const body of ["not json", { hello: 1 }, [], "null"]) {
    const o = await loadRecordedFindings(ENV, respond(200, body));
    assert.equal(o.ok, false, JSON.stringify(body));
    assert.equal(httpStatusFor(o), 502);
  }
});

test("ORCHESTRATOR_TIMEOUT_MS parsing", () => {
  assert.equal(timeoutMs({}), DEFAULT_TIMEOUT_MS);
  assert.equal(timeoutMs({ ORCHESTRATOR_TIMEOUT_MS: "2500" }), 2500);
  for (const bad of ["0", "-1", "abc", "1.5", "99999999"]) assert.equal(timeoutMs({ ORCHESTRATOR_TIMEOUT_MS: bad }), DEFAULT_TIMEOUT_MS);
});

test("handoff round-trips and rejects anything malformed", async () => {
  const ok = await loadRecordedFindings(ENV, respond(200, OK_BODY));
  assert.deepEqual(decodeHandoff(encodeHandoff(ok)), ok);
  const bad = await loadRecordedFindings(ENV, respond(401, { error: "invalid token" }));
  assert.deepEqual(decodeHandoff(encodeHandoff(bad)), bad);
  const enc = (v: unknown) => Buffer.from(JSON.stringify(v)).toString("base64url");
  for (const v of [null, "", "%%%", enc(1), enc({ ok: false, kind: "made_up", message: "m", correlationId: "c" }), enc({ ok: true })]) {
    assert.equal(decodeHandoff(v), null, String(v));
  }
});
