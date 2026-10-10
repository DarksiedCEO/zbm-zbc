// Bug sweep E, F-5 (Oct 9 2026): the dashboard's authentication building
// blocks and the brief ledger cache, unit level, with injected clocks (no
// wall-clock assertions). The on-the-wire behaviour (every route, redirects,
// 401/503, cookies, rate limit) is tests/status.live.test.mjs.
//
// Run: npm test

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  authConfig,
  hashPassword,
  issueSession,
  parsePasswordHash,
  sessionCookie,
  verifyPassword,
  verifySession,
  DEFAULT_TTL_S,
} from "../src/lib/auth.ts";
import { clientKey, FailureLimiter, sameOrigin } from "../src/lib/rate-limit.ts";
import { ledgerCacheMs, loadRecordedFindingsCached, resetLedgerCacheForTests, DEFAULT_LEDGER_CACHE_MS } from "../src/lib/api.ts";

const SECRET = "unit-test-session-secret-0123456789abcdef";
const NOW = 1_800_000_000;

async function env(extra: Record<string, string> = {}) {
  return { DASHBOARD_PASSWORD_HASH: await hashPassword("correct horse battery", 16384), DASHBOARD_SESSION_SECRET: SECRET, ...extra };
}

function ok(cfg: ReturnType<typeof authConfig>) {
  if (!cfg.ok) throw new Error(`config refused: ${cfg.reason}`);
  return cfg;
}

test("missing or malformed settings are refused (the proxy then answers 503 everywhere)", async () => {
  const good = await env();
  assert.equal(authConfig({}).ok, false);
  assert.equal(authConfig({ ...good, DASHBOARD_PASSWORD_HASH: undefined }).ok, false);
  assert.equal(authConfig({ ...good, DASHBOARD_SESSION_SECRET: undefined }).ok, false);
  assert.equal(authConfig({ ...good, DASHBOARD_SESSION_SECRET: "x".repeat(31) }).ok, false);
  assert.equal(authConfig({ ...good, DASHBOARD_PASSWORD_HASH: "hunter2" }).ok, false);
  assert.equal(authConfig({ ...good, DASHBOARD_SESSION_TTL_SECONDS: "10" }).ok, false);
  assert.equal(authConfig({ ...good, DASHBOARD_SESSION_TTL_SECONDS: "1e5" }).ok, false);
  assert.equal(authConfig({ ...good, ORCHESTRATOR_SERVICE_TOKEN: SECRET }).ok, false);
  assert.equal(ok(authConfig(good)).ttlS, DEFAULT_TTL_S);
  assert.equal(ok(authConfig({ ...good, DASHBOARD_SESSION_TTL_SECONDS: "3600" })).ttlS, 3600);
  for (const bad of ["scrypt$1000$8$1$c2FsdHNhbHRzYWx0c2FsdA$" + "a".repeat(43), "scrypt$16384$8$1$short$" + "a".repeat(43), "bcrypt$x"]) {
    assert.equal(typeof parsePasswordHash(bad), "string", bad);
  }
});

test("password check: the right one passes, anything else fails", async () => {
  const cfg = ok(authConfig(await env()));
  assert.equal(await verifyPassword("correct horse battery", cfg.hash), true);
  for (const wrong of ["", "correct horse batter", "correct horse battery ", "CORRECT HORSE BATTERY", "x".repeat(2000)]) {
    assert.equal(await verifyPassword(wrong, cfg.hash), false, JSON.stringify(wrong.slice(0, 30)));
  }
});

test("session: valid until its signed expiry; tampered, expired, re-keyed or garbage values are refused", async () => {
  const e = await env();
  const cfg = ok(authConfig(e));
  const v = issueSession(cfg, NOW);
  assert.equal(verifySession(cfg, v, NOW), true);
  assert.equal(verifySession(cfg, v, NOW + cfg.ttlS - 1), true);
  assert.equal(verifySession(cfg, v, NOW + cfg.ttlS), false, "expired");
  assert.equal(verifySession(cfg, v, NOW - 3600), false, "issued in the future");
  const [body, sig] = v.split(".");
  const flip = (s: string, i: number) => s.slice(0, i) + (s[i] === "A" ? "B" : "A") + s.slice(i + 1);
  for (const forged of [`${flip(body, 2)}.${sig}`, `${body}.${flip(sig, 2)}`, `${body}.${sig}x`, body, `${body}.`, "", "a.b.c"]) {
    assert.equal(verifySession(cfg, forged, NOW), false, forged.slice(0, 24));
  }
  const otherSecret = ok(authConfig({ ...e, DASHBOARD_SESSION_SECRET: "another-secret-another-secret-0123456789" }));
  assert.equal(verifySession(otherSecret, v, NOW), false, "another secret");
  const newPassword = ok(authConfig({ ...e, DASHBOARD_PASSWORD_HASH: await hashPassword("a new password", 16384) }));
  assert.equal(verifySession(newPassword, v, NOW), false, "a password change signs sessions out");
  const shorter = ok(authConfig({ ...e, DASHBOARD_SESSION_TTL_SECONDS: "600" }));
  assert.equal(verifySession(shorter, v, NOW), false, "a session longer than the configured TTL");
  const cookie = sessionCookie(v, cfg.ttlS);
  for (const attr of ["HttpOnly", "Secure", "SameSite=Strict", "Path=/", `Max-Age=${cfg.ttlS}`]) assert.ok(cookie.includes(`; ${attr}`), attr);
  assert.ok(cookie.startsWith("__Host-"));
});

test("login limiter: per-client reservations are atomic (M-1); the global budget only serialises (M-2)", async () => {
  let t = 0;
  const lim = new FailureLimiter({ windowMs: 1000, perKey: 3, global: 5, maxKeys: 100 }, () => t);
  // M-1: reservations are taken synchronously, so in-flight attempts count before any failure is recorded
  const held = [lim.reserve("a"), lim.reserve("a"), lim.reserve("a")];
  assert.ok(held.every((r) => !("retryAfterS" in r)));
  const fourth = lim.reserve("a");
  assert.ok("retryAfterS" in fourth && fourth.retryAfterS >= 1, "a 4th parallel attempt passed");
  // a success gives its reservation back; a failure keeps it
  lim.succeed(held[0] as Exclude<(typeof held)[0], { retryAfterS: number }>);
  lim.fail();
  lim.fail();
  assert.ok(!("retryAfterS" in lim.reserve("a")), "a released reservation still counted");
  assert.ok("retryAfterS" in lim.reserve("a"));
  t += 1000;
  assert.ok(!("retryAfterS" in lim.reserve("a")), "the window slid past");
  // M-2: past the global budget nobody is refused; checks run one at a time
  for (let i = 0; i < 5; i++) lim.fail();
  assert.equal(lim.globalSpent(), true);
  assert.ok(!("retryAfterS" in lim.reserve("owner")), "the global budget blocked a fresh client");
  let running = 0;
  let peak = 0;
  const check = async () => {
    running++;
    peak = Math.max(peak, running);
    await new Promise((r) => setImmediate(r));
    running--;
    return true;
  };
  const results = await Promise.all([1, 2, 3, 4].map(() => lim.throttled(check)));
  assert.deepEqual(results, [true, true, true, true]);
  assert.equal(peak, 1, "checks were not serialised past the global budget");
  t += 1000;
  assert.equal(lim.globalSpent(), false);
  const h = new Headers({ "x-forwarded-for": " 203.0.113.9 , 10.0.0.1" });
  assert.equal(clientKey(h), "203.0.113.9");
  assert.equal(clientKey(new Headers()), "unknown");
});

test("same-origin check: Origin required, scheme and host must both match (L-1/L-2)", () => {
  const req = (origin?: string) =>
    new Request("http://127.0.0.1:3000/api/login", { method: "POST", headers: { host: "127.0.0.1:3000", ...(origin === undefined ? {} : { origin }) } });
  assert.equal(sameOrigin(req("http://127.0.0.1:3000")), true);
  assert.equal(sameOrigin(req()), false, "missing Origin");
  assert.equal(sameOrigin(req("null")), false);
  assert.equal(sameOrigin(req("https://127.0.0.1:3000")), false, "scheme differs");
  assert.equal(sameOrigin(req("http://evil.example:3000")), false);
  assert.equal(sameOrigin(req("http://127.0.0.1:3001")), false);
  const behindTls = new Request("http://10.0.0.5:3000/api/login", {
    method: "POST",
    headers: { host: "dash.example.com", "x-forwarded-proto": "https", origin: "https://dash.example.com" },
  });
  assert.equal(sameOrigin(behindTls), true, "behind a TLS proxy");
  const downgraded = new Request("http://10.0.0.5:3000/api/login", {
    method: "POST",
    headers: { host: "dash.example.com", "x-forwarded-proto": "https", origin: "http://dash.example.com" },
  });
  assert.equal(sameOrigin(downgraded), false, "http Origin on an https site");
});

const OK_BODY = {
  findings: [],
  overlapping_claims: {},
  scans: [],
  excluded_scans: [],
  ledger_entries_total: 0,
  ledger_total_source: "head",
  ledger_entries_read: 0,
  finding_entries_total: 0,
  legacy_finding_entries_ignored: 0,
  legacy_findings: [],
  ledger_verify: { valid: true, entries: 0, error: "" },
  non_live_data_source: true,
  latest_scan_uncounted: {},
};

test("ledger cache: one read per TTL per process, single flight, failures never cached", async () => {
  resetLedgerCacheForTests();
  const E = { ORCHESTRATOR_URL: "http://127.0.0.1:1", ORCHESTRATOR_SERVICE_TOKEN: "t", DASHBOARD_LEDGER_CACHE_MS: "5000" };
  let calls = 0;
  let fail = false;
  const fetchImpl = (async () => {
    calls++;
    return fail ? new Response("{}", { status: 500 }) : new Response(JSON.stringify(OK_BODY), { status: 200 });
  }) as unknown as typeof fetch;
  let t = 0;
  const now = () => t;
  assert.equal((await loadRecordedFindingsCached(E, fetchImpl, now)).ok, true);
  t = 4999;
  assert.equal((await loadRecordedFindingsCached(E, fetchImpl, now)).ok, true);
  assert.equal(calls, 1, "a page view inside the TTL re-read the ledger");
  t = 5000;
  await loadRecordedFindingsCached(E, fetchImpl, now);
  assert.equal(calls, 2, "the TTL is bounded");
  // single flight: concurrent views share one read
  t = 20_000;
  await Promise.all([1, 2, 3].map(() => loadRecordedFindingsCached(E, fetchImpl, now)));
  assert.equal(calls, 3);
  // a failure is not cached
  t = 40_000;
  fail = true;
  assert.equal((await loadRecordedFindingsCached(E, fetchImpl, now)).ok, false);
  assert.equal((await loadRecordedFindingsCached(E, fetchImpl, now)).ok, false);
  assert.equal(calls, 5);
  // another token is another key
  fail = false;
  await loadRecordedFindingsCached({ ...E, ORCHESTRATOR_SERVICE_TOKEN: "u" }, fetchImpl, now);
  assert.equal(calls, 6);
  // 0 turns it off
  await loadRecordedFindingsCached({ ...E, DASHBOARD_LEDGER_CACHE_MS: "0" }, fetchImpl, now);
  await loadRecordedFindingsCached({ ...E, DASHBOARD_LEDGER_CACHE_MS: "0" }, fetchImpl, now);
  assert.equal(calls, 8);
  assert.equal(ledgerCacheMs({}), DEFAULT_LEDGER_CACHE_MS);
  assert.equal(ledgerCacheMs({ DASHBOARD_LEDGER_CACHE_MS: "600000" }), DEFAULT_LEDGER_CACHE_MS, "above the bound -> default");
  resetLedgerCacheForTests();
});
