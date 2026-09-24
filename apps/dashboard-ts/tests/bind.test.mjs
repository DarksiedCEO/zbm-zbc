// D2 (AEGIS round 2, CONFIRMED): `next start` binds 0.0.0.0 (every network
// interface) unless told otherwise, and this dashboard has no auth. The
// package scripts now go through scripts/serve.mjs, which binds 127.0.0.1
// unless DASHBOARD_BIND_ADDR says otherwise. This test fails if the start
// (or dev) command would bind a non-loopback address by default.
//
// Run: npm test

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8"));

test("start and dev go through the loopback launcher, never bare next start/dev", () => {
  assert.equal(pkg.scripts.start, "node scripts/serve.mjs start");
  assert.equal(pkg.scripts.dev, "node scripts/serve.mjs dev");
  for (const [name, cmd] of Object.entries(pkg.scripts)) {
    assert.ok(!/\bnext\s+(start|dev)\b/.test(cmd), `script "${name}" runs next start/dev directly: ${cmd}`);
  }
});

test("default bind (no DASHBOARD_BIND_ADDR) is loopback for start and dev", async () => {
  const { nextArgs, isLoopback } = await import("../scripts/serve.mjs");
  for (const mode of ["start", "dev"]) {
    // Unrelated env that must not change the bind (HOSTNAME is set in many containers).
    for (const env of [{}, { HOSTNAME: "0.0.0.0" }, { DASHBOARD_BIND_ADDR: "" }]) {
      const args = nextArgs(mode, env, []);
      const h = args.indexOf("-H");
      assert.ok(h >= 0, `${mode}: no -H in ${args.join(" ")}`);
      assert.equal(args[h + 1], "127.0.0.1", `${mode} ${JSON.stringify(env)}`);
      assert.ok(isLoopback(args[h + 1]));
      assert.equal(args.filter((a) => a === "-H" || a === "--hostname").length, 1);
    }
  }
});

test("DASHBOARD_BIND_ADDR is an explicit override; -H in passthrough args is refused", async () => {
  const { nextArgs, isLoopback } = await import("../scripts/serve.mjs");
  const args = nextArgs("start", { DASHBOARD_BIND_ADDR: "0.0.0.0" }, ["-p", "19650"]);
  assert.deepEqual(args.slice(-2), ["-p", "19650"]);
  assert.equal(args[args.indexOf("-H") + 1], "0.0.0.0");
  assert.ok(!isLoopback("0.0.0.0") && !isLoopback("::") && !isLoopback("10.0.0.5"));
  assert.ok(isLoopback("127.0.0.1") && isLoopback("::1") && isLoopback("localhost") && isLoopback("127.1.2.3"));
  for (const extra of [["-H", "0.0.0.0"], ["--hostname", "0.0.0.0"], ["--hostname=0.0.0.0"], ["-H0.0.0.0"]]) {
    assert.throws(() => nextArgs("start", {}, extra), /DASHBOARD_BIND_ADDR/, extra.join(" "));
  }
  assert.throws(() => nextArgs("build", {}, []), /mode/);
});
