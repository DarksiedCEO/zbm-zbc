import { NextResponse, type NextRequest } from "next/server";

import { loadRecordedFindingsCached } from "@/lib/api";
import { authConfig, nowSeconds, SESSION_COOKIE, verifySession } from "@/lib/auth";
import { encodeHandoff, HANDOFF_HEADER } from "@/lib/handoff";
import { httpStatusFor } from "@/lib/load-outcome";

// The Next 16 proxy (formerly "middleware"): runs before EVERY request — every
// page, route handler and asset — on the Node.js runtime (the Next 16 default).
//
// Bug sweep E, F-5 (Oct 9 2026): authentication, enforced here and failing
// closed (src/lib/auth.ts):
//
//   0. authentication not configured (DASHBOARD_PASSWORD_HASH /
//      DASHBOARD_SESSION_SECRET missing or malformed) -> 503 on EVERY route,
//      the login page included: the dashboard is never served open;
//   1. public without a session: GET/HEAD /login, POST /api/login,
//      POST /api/logout, and Next's build assets under /_next/static/ (the
//      client bundles, which hold no secret and no data);
//   2. everything else needs a valid session cookie: a page GET/HEAD without
//      one is redirected (303) to /login; an API route, /healthz, or any other
//      method answers 401 JSON.
//
// LOW-A (fix wave 1, Sep 24 2026): "/" used to render an orchestrator failure
// with HTTP 200. The status is decided here, before the page renders (see
// src/lib/handoff.ts for why the page itself cannot set it):
//
//   a. every request: drop any client-supplied handoff header;
//   b. GET/HEAD "/": read the recorded findings ONCE (cached briefly per
//      process: src/lib/api.ts loadRecordedFindingsCached), hand the outcome to
//      the page, and answer with its status — 200, 502 or 503
//      (src/lib/load-outcome.ts); tests/status.live.test.mjs checks it;
//   c. any other method on "/": 405.

const NO_STORE = { "cache-control": "no-store" };
let loggedReason: string | null = null;

function isPublic(path: string, method: string): boolean {
  if (path.startsWith("/_next/static/")) return true;
  if (path === "/login") return method === "GET" || method === "HEAD";
  if (path === "/api/login" || path === "/api/logout") return method === "POST";
  return false;
}

function wantsJson(path: string): boolean {
  return path.startsWith("/api/") || path === "/healthz";
}

export async function proxy(req: NextRequest) {
  const headers = new Headers(req.headers);
  headers.delete(HANDOFF_HEADER);
  const path = req.nextUrl.pathname;
  const method = req.method;

  const cfg = authConfig(process.env);
  if (!cfg.ok) {
    if (loggedReason !== cfg.reason) {
      loggedReason = cfg.reason;
      console.error(`dashboard: authentication is not configured (${cfg.reason}); every route answers 503`);
    }
    const msg = "dashboard authentication is not configured (DASHBOARD_PASSWORD_HASH, DASHBOARD_SESSION_SECRET)";
    return wantsJson(path)
      ? NextResponse.json({ status: "unavailable", error: msg }, { status: 503, headers: NO_STORE })
      : new NextResponse(`503 Service Unavailable: ${msg}\n`, {
          status: 503,
          headers: { ...NO_STORE, "content-type": "text/plain; charset=utf-8" },
        });
  }

  if (isPublic(path, method)) {
    return NextResponse.next({ request: { headers } });
  }

  if (!verifySession(cfg, req.cookies.get(SESSION_COOKIE)?.value, nowSeconds())) {
    if ((method === "GET" || method === "HEAD") && !wantsJson(path)) {
      const to = req.nextUrl.clone();
      to.pathname = "/login";
      to.search = "";
      return NextResponse.redirect(to, { status: 303, headers: NO_STORE });
    }
    return NextResponse.json({ error: "authentication required" }, { status: 401, headers: NO_STORE });
  }

  if (path !== "/") {
    return NextResponse.next({ request: { headers } });
  }
  if (method !== "GET" && method !== "HEAD") {
    return new NextResponse(null, { status: 405, headers: { Allow: "GET, HEAD" } });
  }

  const outcome = await loadRecordedFindingsCached();
  headers.set(HANDOFF_HEADER, encodeHandoff(outcome));
  return NextResponse.next({ status: httpStatusFor(outcome), request: { headers } });
}

// No matcher: the proxy must see every request so authentication is never
// skipped and the handoff header can never reach the page unscrubbed.
