// LOW-A (fix wave 1, Sep 24 2026): when the orchestrator was unreachable or
// rejected the token, "/" rendered the error with HTTP 200, so any monitor
// saw a healthy dashboard. This test starts the BUILT dashboard (`next
// start`, via scripts/serve.mjs) against a stub orchestrator and checks the
// status code on the wire — not a unit-level mapping — for every case:
//
//   orchestrator unreachable            -> 503 (+ sanitized message, correlation id)
//   orchestrator times out              -> 503
//   orchestrator rejects the token      -> 502 (+ orchestrator's message)
//   orchestrator 502 with correlation id -> 502 (+ that correlation id)
//   ledger does not verify              -> 502 (integrity failure shown)
//   healthy                             -> 200 (findings shown, incl. a 5,000-finding ledger)
//   client-forged handoff header        -> ignored
//   HEAD /                              -> same status as GET; POST / -> 405
//
// and the same for GET /healthz (JSON). Needs `npm run build` first; it is
// reported as SKIPPED (never silently passed) when there is no build, and CI
// fails the dashboard job on any skip (devtools/hygiene_check.py, fix wave 25).
//
// Ports (fix wave 25, scout C2-2): none is fixed. The stub orchestrator and the
// "closed" port are bound by this process on port 0; the dashboard is started
// with `-p 0` and the test uses the port ITS OWN child printed after binding
// ("- Local: http://127.0.0.1:<port>"), so a server left on some port by another
// run or worktree can never be the one under test. The child must still be
// alive when the port is read, and is killed with its whole process group.
//
// Run: npm run build && npm test

import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { createServer } from "node:http";
import { once } from "node:events";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const built = existsSync(join(root, ".next", "BUILD_ID"));
let DASH_PORT = 0; // set from the dashboard child's own announcement
let STUB_PORT = 0; // set when the stub is listening
let CLOSED_PORT = 0; // a port this process bound and closed again: nothing listens there
const TOKEN = "live-test-token";

const VERIFIED = {
  findings: [
    {
      seq: 1, finding_id: "f-1", agent_id: "discount_misuse", entity_id: "ORD-LIVE-1",
      leak_category: "discount_misuse", amount_usd: "12.30", value_classification: "observed",
      decision_confidence: "high", recorded_at: "2026-09-24T00:00:00Z", prev_hash: "0", hash: "h1",
      amount_out_of_contract: false, first_seq: 1, times_recorded: 1, amounts_differ_across_records: false,
    },
  ],
  overlapping_claims: {},
  ledger_entries_total: 1,
  finding_entries_total: 1,
  ledger_verify: { valid: true, entries: 1, error: "" },
  non_live_data_source: true,
};

// What the stub orchestrator answers next; switched per test.
let mode = "ok";
const stub = createServer((req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (req.headers.authorization !== `Bearer ${TOKEN}`) return send(401, { error: "invalid token" });
  switch (mode) {
    case "ok":
      return send(200, VERIFIED);
    case "invalid-ledger":
      return send(200, { ...VERIFIED, ledger_verify: { valid: false, entries: 1, error: "ChainBroken { at_seq: 1 }" } });
    case "upstream-502":
      return send(502, {
        error: "GET /revenue-recovery/findings failed: ledger-rust is unreachable (GET /ledger/verify) at http://127.0.0.1:8090",
        correlation_id: "c0ffee0123456789",
      });
    case "big": {
      // ~5,000 findings (~2.6 MB of JSON): the proxy -> page handoff is an
      // in-process request header, and must carry a large ledger intact.
      const findings = Array.from({ length: 5000 }, (_, i) => ({ ...VERIFIED.findings[0], seq: i + 1, finding_id: `f-${i + 1}`, entity_id: `ORD-BIG-${i + 1}` }));
      return send(200, { ...VERIFIED, findings, ledger_entries_total: 5000, finding_entries_total: 5000, ledger_verify: { valid: true, entries: 5000, error: "" } });
    }
    case "hang":
      return; // never answers: the dashboard's timeout must fire
    default:
      return send(500, {});
  }
});

let dash;
let dashLog = "";

// The port the dashboard child announced, or an error if the child exited (or
// said nothing) first. Only this child can have printed it, after it bound.
function announcedPort(child) {
  return new Promise((resolve, reject) => {
    let seen = "";
    const onData = (d) => {
      seen += d;
      const m = /- Local:\s+http:\/\/127\.0\.0\.1:(\d+)/.exec(seen);
      if (m) done(null, Number(m[1]));
    };
    const onExit = (code, signal) => done(new Error(`dashboard exited (code ${code}, signal ${signal}) before announcing its port`));
    const timer = setTimeout(() => done(new Error("dashboard announced no port within 60 s")), 60_000);
    function done(err, port) {
      clearTimeout(timer);
      child.stdout.off("data", onData);
      child.off("exit", onExit);
      if (err) reject(err);
      else resolve(port);
    }
    child.stdout.on("data", onData);
    child.once("exit", onExit);
  });
}

function killGroup(child, signal) {
  try {
    process.kill(-child.pid, signal); // the launcher leads its own process group (detached)
  } catch {
    // already gone
  }
}

async function startDashboard(env) {
  dashLog = "";
  const child = spawn(process.execPath, ["scripts/serve.mjs", "start", "-p", "0"], {
    cwd: root,
    env: { ...process.env, ...env, PORT: "0" },
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
  });
  child.stdout.on("data", (d) => (dashLog += d));
  child.stderr.on("data", (d) => (dashLog += d));
  try {
    DASH_PORT = await announcedPort(child);
    for (let i = 0; i < 100; i++) {
      if (child.exitCode !== null || child.signalCode !== null) throw new Error("dashboard exited after announcing its port");
      try {
        await fetch(`http://127.0.0.1:${DASH_PORT}/healthz`, { signal: AbortSignal.timeout(5000) });
        return child;
      } catch {
        await new Promise((r) => setTimeout(r, 100));
      }
    }
    throw new Error("dashboard announced a port but never answered on it");
  } catch (e) {
    const exited = child.exitCode !== null || child.signalCode !== null ? Promise.resolve() : once(child, "exit");
    killGroup(child, "SIGKILL");
    await exited;
    throw new Error(`dashboard did not start: ${e.message}\n${dashLog}`);
  }
}

async function stopDashboard() {
  if (!dash) return;
  const child = dash;
  dash = undefined;
  const exited = child.exitCode !== null || child.signalCode !== null ? Promise.resolve() : once(child, "exit");
  killGroup(child, "SIGTERM");
  await exited;
  killGroup(child, "SIGKILL"); // anything left in the group
}

// Listen on port 0 and resolve with the port; a listen error rejects (it used
// to leave the before() hook pending until the 120 s test timeout).
function listenOnAnyPort(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
}

async function get(path, headers = {}) {
  const res = await fetch(`http://127.0.0.1:${DASH_PORT}${path}`, { headers, signal: AbortSignal.timeout(20000) });
  return { status: res.status, body: await res.text() };
}

before(async () => {
  if (!built) return;
  STUB_PORT = await listenOnAnyPort(stub);
  const probe = createServer();
  CLOSED_PORT = await listenOnAnyPort(probe);
  await new Promise((r) => probe.close(r));
});

after(async () => {
  await stopDashboard();
  stub.closeAllConnections();
  if (stub.listening) await new Promise((r) => stub.close(r));
});

const opts = built ? { timeout: 120_000 } : { skip: "no .next build — run `npm run build` first" };

test("orchestrator unreachable -> 503 on the wire, message and correlation id still rendered", opts, async () => {
  dash = await startDashboard({ ORCHESTRATOR_URL: `http://127.0.0.1:${CLOSED_PORT}`, ORCHESTRATOR_SERVICE_TOKEN: TOKEN });
  const page = await get("/");
  assert.equal(page.status, 503, "GET / status");
  assert.match(page.body, /could not reach the orchestrator/);
  assert.match(page.body, /correlation id [0-9a-f]{16}/);
  assert.ok(!page.body.includes(String(CLOSED_PORT)), "internal port leaked into the page");

  const hz = await get("/healthz");
  assert.equal(hz.status, 503, "GET /healthz status");
  const j = JSON.parse(hz.body);
  assert.equal(j.status, "unavailable");
  assert.match(j.correlation_id, /^[0-9a-f]{16}$/);

  // HEAD carries the same status (monitors often use it); other methods
  // are refused outright rather than rendering without an outcome.
  const head = await fetch(`http://127.0.0.1:${DASH_PORT}/`, { method: "HEAD", signal: AbortSignal.timeout(20000) });
  assert.equal(head.status, 503, "HEAD / status");
  const post = await fetch(`http://127.0.0.1:${DASH_PORT}/`, { method: "POST", signal: AbortSignal.timeout(20000) });
  assert.equal(post.status, 405, "POST / status");
  assert.equal(post.headers.get("allow"), "GET, HEAD");
  await stopDashboard();
});

test("token not set -> 503 (the dashboard cannot serve findings)", opts, async () => {
  dash = await startDashboard({ ORCHESTRATOR_URL: `http://127.0.0.1:${STUB_PORT}`, ORCHESTRATOR_SERVICE_TOKEN: "" });
  const page = await get("/");
  assert.equal(page.status, 503);
  assert.match(page.body, /ORCHESTRATOR_SERVICE_TOKEN is not set/);
  assert.equal((await get("/healthz")).status, 503);
  await stopDashboard();
});

test("upstream rejects the token / errors / ledger invalid / times out -> 502/502/502/503; healthy -> 200", opts, async () => {
  dash = await startDashboard({
    ORCHESTRATOR_URL: `http://127.0.0.1:${STUB_PORT}`,
    ORCHESTRATOR_SERVICE_TOKEN: TOKEN,
    ORCHESTRATOR_TIMEOUT_MS: "1500",
  });

  mode = "ok";
  let page = await get("/");
  assert.equal(page.status, 200, "healthy GET /");
  assert.match(page.body, /ORD-LIVE-1/);
  assert.match(page.body, /hash chain verified/);
  let hz = await get("/healthz");
  assert.equal(hz.status, 200);
  assert.equal(JSON.parse(hz.body).status, "ok");

  mode = "big";
  page = await get("/");
  assert.equal(page.status, 200, "large ledger GET /");
  // (assert.ok, not assert.match: a failure must not print a 10 MB page)
  assert.ok(page.body.includes(">ORD-BIG-1<"), "first finding missing");
  assert.ok(page.body.includes(">ORD-BIG-5000<"), "last finding missing");
  assert.ok(/5000(<!-- -->)? distinct findings/.test(page.body), "count missing");
  mode = "ok";

  // A client cannot inject its own "outcome" through the internal handoff header.
  const forged = Buffer.from(JSON.stringify({ ok: false, kind: "upstream_error", message: "FORGED", correlationId: "0123456789abcdef" })).toString("base64url");
  page = await get("/", { "x-zbm-dashboard-load": forged });
  assert.equal(page.status, 200, "forged handoff header changed the status");
  assert.ok(!page.body.includes("FORGED"), "forged handoff header was rendered");

  mode = "upstream-502";
  page = await get("/");
  assert.equal(page.status, 502, "orchestrator 502 GET /");
  assert.match(page.body, /ledger-rust is unreachable/);
  assert.match(page.body, /correlation id c0ffee0123456789/);
  assert.ok(!page.body.includes("127.0.0.1:8090"), "internal address leaked");
  hz = await get("/healthz");
  assert.equal(hz.status, 502);
  assert.equal(JSON.parse(hz.body).correlation_id, "c0ffee0123456789");

  mode = "invalid-ledger";
  page = await get("/");
  assert.equal(page.status, 502, "ledger integrity failure GET /");
  assert.match(page.body, /LEDGER INTEGRITY FAILURE/);
  hz = await get("/healthz");
  assert.equal(hz.status, 502);
  assert.equal(JSON.parse(hz.body).status, "ledger_invalid");

  mode = "hang";
  page = await get("/");
  assert.equal(page.status, 503, "orchestrator timeout GET /");
  assert.match(page.body, /timed out/);
  await stopDashboard();

  // Wrong token: the orchestrator answers 401.
  mode = "ok";
  dash = await startDashboard({ ORCHESTRATOR_URL: `http://127.0.0.1:${STUB_PORT}`, ORCHESTRATOR_SERVICE_TOKEN: "wrong" });
  page = await get("/");
  assert.equal(page.status, 502, "token rejected GET /");
  assert.match(page.body, /orchestrator returned 401: invalid token/);
  assert.equal((await get("/healthz")).status, 502);
  await stopDashboard();
});
