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

import { test, before, after, afterEach } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { createServer } from "node:http";
import { once } from "node:events";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { authConfig, hashPassword, issueSession, nowSeconds, SESSION_COOKIE } from "../src/lib/auth.ts";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const built = existsSync(join(root, ".next", "BUILD_ID"));
let DASH_PORT = 0; // set from the dashboard child's own announcement
let STUB_PORT = 0; // set when the stub is listening
let CLOSED_PORT = 0; // a port this process bound and closed again: nothing listens there
const TOKEN = "live-test-token";
// Bug sweep E, F-5: every route needs a session now. These tests run the
// dashboard with authentication configured and send a valid session cookie;
// tests/auth.live.test.mjs covers the authentication itself. The ledger read
// cache is off here (DASHBOARD_LEDGER_CACHE_MS=0): these tests switch the stub's
// answer between requests.
let AUTH = {};
let COOKIE = "";

// A finding exactly as orchestrator-go's GET /revenue-recovery/findings
// serves one (internal/orchestrator/recorded.go RecordedFinding).
const ROW = {
  seq: 1, finding_id: "rrf1-" + "a".repeat(40), client_id: "fixture-pool", agent_id: "discount-misuse-v1",
  leak_category: "discount_misuse", entity_type: "order", entity_id: "ORD-LIVE-1", period_label: null,
  amount_usd: "12.30", value_classification: "observed", decision_confidence: "high", evidence_class: "OBSERVED",
  methodology_id: "disc_excess_best_code", scan_id: "1".repeat(32), payload_sha256: "0".repeat(64),
  recorded_at: "2026-09-24T00:00:00Z", prev_hash: "0", hash: "h1", amount_out_of_contract: false, first_seq: 1,
  times_recorded: 1, amounts_differ_across_records: false, present_in_latest_scan: true, value_basis: null,
  labels_exceed_evidence: null, quotable: true,
};

const VERIFIED = {
  findings: [ROW],
  overlapping_claims: {},
  scans: [],
  excluded_scans: [],
  ledger_entries_total: 1,
  ledger_total_source: "head",
  ledger_entries_read: 1,
  finding_entries_total: 1,
  legacy_finding_entries_ignored: 0,
  legacy_findings: [],
  ledger_verify: { valid: true, entries: 1, error: "" },
  non_live_data_source: true,
  latest_scan_uncounted: {},
};

// AEGIS M1/M4/L3 (Oct 7 2026): a stale ESTIMATED finding with its commission
// basis, an over-claiming pre-invariant record, an abandoned and an aborted
// scan, and a legacy entry — every one must be visible and labelled.
const MIXED = {
  ...VERIFIED,
  findings: [
    { ...ROW, seq: 9, finding_id: "rrf1-" + "b".repeat(40), entity_id: "ORD-CURRENT" },
    {
      ...ROW, seq: 3, finding_id: "rrf1-" + "c".repeat(40), entity_id: "ORD-STALE", agent_id: "affiliate-coupon-extension-v1",
      leak_category: "affiliate_coupon_extension", amount_usd: "12.00", value_classification: "attributed",
      decision_confidence: "medium", evidence_class: "ESTIMATED", present_in_latest_scan: false,
      value_basis: { base_usd: "120.00", rate_percent: "10" }, quotable: false,
    },
    {
      ...ROW, seq: 10, finding_id: "rrf1-" + "d".repeat(40), entity_type: "subscription", entity_id: "SUB-OVER",
      period_label: "2026-05-15", amount_usd: "39.00", evidence_class: "ESTIMATED",
      labels_exceed_evidence: "classification observed needs OBSERVED evidence, the figure is ESTIMATED", quotable: false,
    },
    { ...ROW, seq: 11, finding_id: "rrf1-" + "e".repeat(40), entity_id: "ORD-NOFIG", amount_usd: null,
      value_classification: null, decision_confidence: null, evidence_class: "UNKNOWN", quotable: false },
  ],
  scans: [
    { scan_id: "4".repeat(32), client_id: "fixture-pool", data_source: "fixtures", fixture: true, tenant_defaulted: true,
      as_of: "2026-10-06T00:00:00Z", findings: 2, started_seq: 1, completed_seq: 4, backdated: false },
    { scan_id: "5".repeat(32), client_id: "fixture-pool", data_source: "fixtures", fixture: true, tenant_defaulted: true,
      as_of: "2026-09-01T00:00:00Z", findings: 2, started_seq: 5, completed_seq: 8, backdated: true },
  ],
  excluded_scans: [
    { scan_id: "2".repeat(32), client_id: "fixture-pool", finding_events: 4, reason: "not completed (failed, or still running)", status: "abandoned", started_at: "2026-10-06T00:00:00Z" },
    { scan_id: "3".repeat(32), client_id: "fixture-pool", finding_events: 1, reason: "aborted before completion", status: "aborted", started_at: "2026-10-06T01:00:00Z" },
    { scan_id: "6".repeat(32), client_id: "fixture-pool", finding_events: 1, reason: "unreadable scan_completed record: rrc3: completion record format is newer", status: "unsupported_format", started_at: "2026-10-07T01:00:00Z" },
  ],
  latest_scan_uncounted: { "fixture-pool": "6".repeat(32) },
  legacy_finding_entries_ignored: 1,
  legacy_findings: [
    { seq: 0, finding_id: "disc-ord_1007", agent_id: "discount-misuse-v1", entity_id: "ORD-LEGACY", leak_category: "discount_misuse",
      amount_usd: "54.38", amount_out_of_contract: false, value_classification: "observed", decision_confidence: "very_high",
      recorded_at: "2026-09-24T10:00:00Z", hash: "h0" },
  ],
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
    case "mixed":
      return send(200, MIXED);
    case "pre-fix-wave":
      // An orchestrator from before the fix wave: findings without
      // present_in_latest_scan / evidence_class. Must not render as current.
      return send(200, { ...VERIFIED, findings: [{ ...ROW, present_in_latest_scan: undefined, evidence_class: undefined }] });
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
    env: { ...process.env, ...AUTH, ...env, PORT: "0" },
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
  const res = await fetch(`http://127.0.0.1:${DASH_PORT}${path}`, { headers: { cookie: COOKIE, ...headers }, signal: AbortSignal.timeout(20000) });
  return { status: res.status, body: await res.text() };
}

before(async () => {
  if (!built) return;
  AUTH = {
    DASHBOARD_PASSWORD_HASH: await hashPassword("live-test-password-1", 16384),
    DASHBOARD_SESSION_SECRET: "live-test-session-secret-0123456789abcdef",
    DASHBOARD_LEDGER_CACHE_MS: "0",
  };
  COOKIE = `${SESSION_COOKIE}=${issueSession(authConfig(AUTH), nowSeconds())}`;
  STUB_PORT = await listenOnAnyPort(stub);
  const probe = createServer();
  CLOSED_PORT = await listenOnAnyPort(probe);
  await new Promise((r) => probe.close(r));
});

// A failed assertion must never leave a dashboard running (it would hold the
// test file open until its timeout): every test ends with the child stopped.
afterEach(async () => {
  await stopDashboard();
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
  const head = await fetch(`http://127.0.0.1:${DASH_PORT}/`, { method: "HEAD", headers: { cookie: COOKIE }, signal: AbortSignal.timeout(20000) });
  assert.equal(head.status, 503, "HEAD / status");
  const post = await fetch(`http://127.0.0.1:${DASH_PORT}/`, { method: "POST", headers: { cookie: COOKIE }, signal: AbortSignal.timeout(20000) });
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

  // AEGIS M1/M4/L3 on the wire: stale vs current, ESTIMATED vs OBSERVED,
  // excluded scans and legacy entries are all visible and labelled.
  mode = "mixed";
  page = await get("/");
  assert.equal(page.status, 200, "mixed GET /");
  const rowOf = (entity) => {
    const i = page.body.indexOf(`>${entity}<`);
    assert.ok(i > 0, `${entity} missing`);
    const start = page.body.lastIndexOf("<tr", i);
    return page.body.slice(start, page.body.indexOf("</tr>", i));
  };
  const stale = rowOf("ORD-STALE");
  assert.match(stale, /data-stale="true"/);
  assert.match(stale, /STALE — not in latest scan/);
  assert.match(stale, /data-evidence="ESTIMATED"/);
  assert.match(stale, />ESTIMATED</);
  assert.match(stale, /est\. (<!-- -->)?\$12\.00/);
  assert.match(stale, /10(<!-- -->)?% of (<!-- -->)?\$120\.00/);
  const current = rowOf("ORD-CURRENT");
  assert.match(current, /data-stale="false"/);
  assert.ok(!current.includes("STALE"), "a current finding is labelled stale");
  assert.match(current, />OBSERVED</);
  assert.ok(!current.includes("est."), "an OBSERVED figure is marked estimated");
  assert.ok(page.body.indexOf(">ORD-CURRENT<") < page.body.indexOf(">ORD-STALE<"), "stale finding listed among current ones");
  assert.match(rowOf("SUB-OVER"), /LABELS EXCEED EVIDENCE/);
  assert.match(rowOf("ORD-NOFIG"), /NO FIGURE/);
  assert.match(page.body, /1(<!-- -->)? stale/);
  assert.match(page.body, /Excluded scans \((<!-- -->)?3(<!-- -->)?\)/);
  assert.match(page.body, /ABANDONED — never finished/);
  assert.match(page.body, /ABORTED — the scan failed/);
  assert.match(page.body, /Legacy ledger findings \((<!-- -->)?1(<!-- -->)?\) — LEGACY, not counted/);
  assert.match(rowOf("ORD-LEGACY"), /data-legacy="true"/);
  // N4: the served quotable flag is shown.
  assert.match(current, /data-quote="quotable"/);
  assert.match(stale, /data-quote="not-quotable"/);
  // N3: as_of per scan, the backdated one flagged.
  assert.match(page.body, /2026-10-06T00:00:00Z/);
  assert.match(page.body, /data-backdated="true"/);
  assert.match(page.body, /2026-09-01T00:00:00Z — BACKDATED/);
  // N1: a newer-format latest scan is refused loudly.
  assert.match(page.body, /UNSUPPORTED FORMAT — written by a newer orchestrator-go/);
  assert.match(page.body, /The latest scan could not be counted/);

  // A pre-fix-wave body is not the contract: refused (502), never shown as current.
  mode = "pre-fix-wave";
  page = await get("/");
  assert.equal(page.status, 502, "pre-fix-wave body GET /");
  assert.match(page.body, /not the recorded-findings contract/);

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

// ---------------------------------------------------------------------------
// Bug sweep E, F-5 (Oct 9 2026): authentication on the wire (src/proxy.ts,
// src/lib/auth.ts, src/app/api/login/route.ts). Fail closed: no session ->
// redirect (pages) or 401 (API routes, /healthz, other methods) on every
// route; bad password, tampered or expired cookie refused; login rate
// limited; authentication not configured -> 503 on every route.

const ROUTES_PAGES = ["/", "/login-not-a-page", "/anything/at/all"];
const ROUTES_API = ["/healthz", "/api/whatever", "/api/login"];

async function raw(path, init = {}) {
  const res = await fetch(`http://127.0.0.1:${DASH_PORT}${path}`, { redirect: "manual", signal: AbortSignal.timeout(20000), ...init });
  return { status: res.status, headers: res.headers, body: await res.text() };
}

function loginForm(password, xff) {
  return raw("/api/login", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", "x-forwarded-for": xff },
    body: new URLSearchParams({ password }).toString(),
  });
}

test("authentication not configured -> 503 on every route, the login page included (never open)", opts, async () => {
  for (const missing of [
    { DASHBOARD_PASSWORD_HASH: "" },
    { DASHBOARD_SESSION_SECRET: "" },
    { DASHBOARD_SESSION_SECRET: "too-short" },
    { DASHBOARD_PASSWORD_HASH: "plain-text-password" },
  ]) {
    dash = await startDashboard({ ORCHESTRATOR_URL: `http://127.0.0.1:${STUB_PORT}`, ORCHESTRATOR_SERVICE_TOKEN: TOKEN, ...missing });
    for (const path of [...ROUTES_PAGES, ...ROUTES_API, "/login"]) {
      const r = await raw(path, { headers: { cookie: COOKIE } });
      assert.equal(r.status, 503, `${JSON.stringify(missing)} GET ${path}`);
      assert.ok(!r.body.includes("ORD-LIVE-1"), "a finding was served without authentication configured");
    }
    assert.equal((await loginForm("live-test-password-1", "10.0.0.9")).status, 503);
    await stopDashboard();
  }
});

test("no session -> 303 to /login for pages, 401 for API routes, /healthz and other methods", opts, async () => {
  mode = "ok";
  dash = await startDashboard({ ORCHESTRATOR_URL: `http://127.0.0.1:${STUB_PORT}`, ORCHESTRATOR_SERVICE_TOKEN: TOKEN });
  for (const path of ROUTES_PAGES) {
    for (const method of ["GET", "HEAD"]) {
      const r = await raw(path, { method });
      assert.equal(r.status, 303, `${method} ${path}`);
      assert.equal(new URL(r.headers.get("location"), `http://127.0.0.1:${DASH_PORT}`).pathname, "/login");
      assert.ok(!r.body.includes("ORD-LIVE-1"));
    }
  }
  for (const path of ["/healthz", "/api/whatever"]) {
    const r = await raw(path);
    assert.equal(r.status, 401, `GET ${path}`);
    assert.equal(JSON.parse(r.body).error, "authentication required");
  }
  assert.equal((await raw("/", { method: "POST" })).status, 401, "POST / without a session");
  assert.equal((await raw("/api/login")).status, 401, "GET /api/login is not public");
  const login = await raw("/login");
  assert.equal(login.status, 200);
  assert.match(login.body, /action="\/api\/login"/);
  assert.ok(!login.body.includes("live-test-session-secret"), "a secret reached the page");
  await stopDashboard();
});

test("bad password, tampered and expired cookies are refused; a good login works; logout clears", opts, async () => {
  mode = "ok";
  dash = await startDashboard({ ORCHESTRATOR_URL: `http://127.0.0.1:${STUB_PORT}`, ORCHESTRATOR_SERVICE_TOKEN: TOKEN });
  const bad = await loginForm("not-the-password", "10.0.0.1");
  assert.equal(bad.status, 303);
  assert.equal(bad.headers.get("location"), "/login?error=1");
  assert.equal(bad.headers.get("set-cookie"), null, "a failed login set a cookie");
  const badJson = await raw("/api/login", {
    method: "POST",
    headers: { "content-type": "application/json", "x-forwarded-for": "10.0.0.1" },
    body: JSON.stringify({ password: "not-the-password" }),
  });
  assert.equal(badJson.status, 401);
  assert.equal(badJson.headers.get("set-cookie"), null);

  const good = await loginForm("live-test-password-1", "10.0.0.2");
  assert.equal(good.status, 303);
  assert.equal(good.headers.get("location"), "/");
  const setCookie = good.headers.get("set-cookie");
  assert.match(setCookie, new RegExp(`^${SESSION_COOKIE}=[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+;`));
  for (const attr of [/; HttpOnly/, /; Secure/, /; SameSite=Strict/, /; Path=\//, /; Max-Age=43200/]) assert.match(setCookie, attr);
  const fresh = setCookie.split(";")[0];
  const page = await raw("/", { headers: { cookie: fresh } });
  assert.equal(page.status, 200);
  assert.match(page.body, /ORD-LIVE-1/);
  assert.equal((await raw("/healthz", { headers: { cookie: fresh } })).status, 200);

  // Tampered: one character of the payload, then of the signature.
  const value = fresh.slice(SESSION_COOKIE.length + 1);
  const [body, sig] = value.split(".");
  const flip = (s, i) => s.slice(0, i) + (s[i] === "A" ? "B" : "A") + s.slice(i + 1);
  for (const forged of [`${flip(body, 3)}.${sig}`, `${body}.${flip(sig, 5)}`, `${body}.`, body, "x.y"]) {
    const r = await raw("/", { headers: { cookie: `${SESSION_COOKIE}=${forged}` } });
    assert.equal(r.status, 303, `tampered cookie ${forged.slice(0, 20)} accepted`);
    assert.equal((await raw("/healthz", { headers: { cookie: `${SESSION_COOKIE}=${forged}` } })).status, 401);
  }
  // Expired: signed with the right key, but its signed expiry has passed.
  const cfg = authConfig(AUTH);
  const expired = `${SESSION_COOKIE}=${issueSession(cfg, nowSeconds() - cfg.ttlS - 3600)}`;
  assert.equal((await raw("/", { headers: { cookie: expired } })).status, 303, "expired cookie accepted");
  // Signed with another secret: refused.
  const other = authConfig({ ...AUTH, DASHBOARD_SESSION_SECRET: "another-secret-another-secret-0123456789" });
  assert.equal((await raw("/", { headers: { cookie: `${SESSION_COOKIE}=${issueSession(other, nowSeconds())}` } })).status, 303);

  const out = await raw("/api/logout", { method: "POST", headers: { cookie: fresh } });
  assert.equal(out.status, 303);
  assert.match(out.headers.get("set-cookie"), /Max-Age=0/);
  await stopDashboard();
});

test("login is rate limited: after 5 failures from one client even the right password gets 429", opts, async () => {
  dash = await startDashboard({ ORCHESTRATOR_URL: `http://127.0.0.1:${STUB_PORT}`, ORCHESTRATOR_SERVICE_TOKEN: TOKEN });
  for (let i = 0; i < 5; i++) assert.equal((await loginForm(`wrong-${i}`, "10.0.0.7")).status, 303);
  const blocked = await loginForm("live-test-password-1", "10.0.0.7");
  assert.equal(blocked.status, 429);
  assert.ok(Number(blocked.headers.get("retry-after")) >= 1);
  assert.equal(blocked.headers.get("set-cookie"), null);
  assert.equal((await loginForm("live-test-password-1", "10.0.0.8")).status, 303, "another client is not blocked");
  await stopDashboard();
});
