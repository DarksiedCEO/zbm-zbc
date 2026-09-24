// D3 (fix wave 3): the dashboard used to display `orchestrator returned
// ${status}: ${raw body}` — and the orchestrator's body carried internal
// URLs / host:port (e.g. `Get "http://127.0.0.1:8090/ledger/verify": dial
// tcp 127.0.0.1:8090: connection refused`). The page now shows only the
// orchestrator's public message and correlation id; raw detail goes to the
// dashboard server log.
//
// Run: npm test

import { test } from "node:test";
import assert from "node:assert/strict";

import { describeOrchestratorFailure, describeFetchFailure } from "../src/lib/orchestrator-error.ts";

test("shows the orchestrator's public error and correlation id", () => {
  const msg = describeOrchestratorFailure(
    502,
    JSON.stringify({ error: "ledger check before scan failed: ledger-rust is unreachable (GET /ledger/verify)", correlation_id: "a1b2c3d4e5f60718" })
  );
  assert.equal(
    msg,
    "orchestrator returned 502: ledger check before scan failed: ledger-rust is unreachable (GET /ledger/verify) (correlation id a1b2c3d4e5f60718)"
  );
});

test("never displays an internal URL, host:port or a raw body", () => {
  const leaky = [
    `{"error":"ledger verify: Get \\"http://127.0.0.1:19641/ledger/verify\\": dial tcp 127.0.0.1:19641: connect: connection refused"}`,
    `{"error":"upstream at https://ledger.internal:8090/x failed","correlation_id":"x1"}`,
    `{"error":"dial tcp [::1]:8090: refused"}`,
    `<html>Bad Gateway from 10.0.0.5:8080</html>`,
    ``,
  ];
  for (const body of leaky) {
    const msg = describeOrchestratorFailure(502, body);
    for (const bad of ["127.0.0.1", "19641", "http://", "https://", "ledger.internal", "10.0.0.5", "[::1]", "8090", "8080"]) {
      assert.ok(!msg.includes(bad), `leaked ${bad}: ${msg}`);
    }
    assert.ok(msg.startsWith("orchestrator returned 502"), msg);
  }
});

test("a fetch failure (orchestrator unreachable) shows no address", () => {
  const err = new TypeError("fetch failed", { cause: new Error("connect ECONNREFUSED 127.0.0.1:19642") });
  const msg = describeFetchFailure(err);
  assert.ok(!msg.includes("127.0.0.1") && !msg.includes("19642"), msg);
  assert.match(msg, /could not reach the orchestrator/);
});
