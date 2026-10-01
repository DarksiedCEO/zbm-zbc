// Starts `next start` / `next dev` bound to loopback by default (fix wave 3,
// AEGIS D2). Next.js binds 0.0.0.0 — every network interface — unless given
// -H, and this dashboard has no auth of its own, so it must not be reachable
// from the network by default (build contract section 0: 127.0.0.1 by
// default, overridable by an env var, never 0.0.0.0 by default).
//
//   npm start                      -> next start -H 127.0.0.1   (port: PORT or 3000)
//   DASHBOARD_BIND_ADDR=0.0.0.0 npm start   -> explicit override, with a warning
//   npm start -- -p 19650          -> extra args are passed through to next
//
// -H/--hostname in the extra args is refused: the bind address has exactly
// one knob, DASHBOARD_BIND_ADDR. tests/bind.test.mjs pins all of this.
//
// Fix wave 25 (scout C2-13): Next's CLI runs IN THIS PROCESS (it reads
// process.argv), not as a child. Before, the launcher spawned `next` and
// forwarded only SIGINT/SIGTERM; a SIGKILL (or SIGHUP) of the launcher — a
// killed test runner, a closed terminal — left `next-server` running on its
// port, and the next run talked to that stale server. With one process there
// is nothing to orphan for `next start`. (`next dev` still forks its own
// worker; that is Next's design and dev is not used by the tests or in CI.)
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

export const DEFAULT_BIND_ADDR = "127.0.0.1";

export function isLoopback(host) {
  return host === "localhost" || host === "::1" || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
}

export function nextArgs(mode, env, extra) {
  if (mode !== "start" && mode !== "dev") {
    throw new Error(`serve.mjs: mode must be "start" or "dev", got ${JSON.stringify(mode)}`);
  }
  for (const a of extra) {
    if (a === "-H" || a.startsWith("-H") || a === "--hostname" || a.startsWith("--hostname=")) {
      throw new Error("serve.mjs: set the bind address with DASHBOARD_BIND_ADDR, not -H/--hostname");
    }
  }
  const host = env.DASHBOARD_BIND_ADDR || DEFAULT_BIND_ADDR;
  return [mode, "-H", host, ...extra];
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [mode, ...extra] = process.argv.slice(2);
  let args;
  try {
    args = nextArgs(mode, process.env, extra);
  } catch (e) {
    console.error(e.message);
    process.exit(2);
  }
  const host = args[2];
  if (!isLoopback(host)) {
    console.error(
      `WARNING: dashboard binding ${host} (DASHBOARD_BIND_ADDR) — it has NO authentication; ` +
        "anyone who can reach this address can read every recorded finding."
    );
  }
  const require = createRequire(import.meta.url);
  const nextBin = require.resolve("next/dist/bin/next");
  process.argv = [process.execPath, nextBin, ...args];
  require(nextBin);
}
