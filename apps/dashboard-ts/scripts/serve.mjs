// Starts `next start` / `next dev` bound to loopback by default (fix wave 3,
// AEGIS D2). Next.js binds 0.0.0.0 — every network interface — unless given
// -H; before bug sweep E this dashboard had no auth of its own, and it is still not reachable
// from the network by default (build contract section 0: 127.0.0.1 by
// default, overridable by an env var, never 0.0.0.0 by default). Bug sweep E
// (F-5) added authentication (src/proxy.ts); the loopback default stays.
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
import http from "node:http";
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

// Wave F (AEGIS A-4): Next runs in THIS process, so every request it serves passes through http.Server's "request"
// event here first. The socket address is written into x-zbm-peer-addr (over anything the client sent), and the
// env flag tells the login limiter (src/lib/rate-limit.ts) that the header is the server's own.
export const PEER_HEADER = "x-zbm-peer-addr";
export function installPeerHeader() {
  process.env.ZBM_DASHBOARD_PEER_HEADER = "1";
  const emit = http.Server.prototype.emit;
  http.Server.prototype.emit = function (event, req, ...rest) {
    if (event === "request" && req && req.headers) req.headers[PEER_HEADER] = req.socket?.remoteAddress ?? "";
    return emit.call(this, event, req, ...rest);
  };
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
      `WARNING: dashboard binding ${host} (DASHBOARD_BIND_ADDR) — reachable from the network; every route ` +
        "needs a session (DASHBOARD_PASSWORD_HASH / DASHBOARD_SESSION_SECRET), but the session cookie is Secure: " +
        "serve it only behind HTTPS."
    );
  }
  installPeerHeader();
  const require = createRequire(import.meta.url);
  const nextBin = require.resolve("next/dist/bin/next");
  process.argv = [process.execPath, nextBin, ...args];
  require(nextBin);
}
