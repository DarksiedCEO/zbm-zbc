import { authConfig, issueSession, MAX_PASSWORD_BYTES, nowSeconds, sessionCookie, verifyPassword } from "@/lib/auth";
import { clientKey, FailureLimiter, sameOrigin } from "@/lib/rate-limit";

// Bug sweep E, F-5: POST /api/login. Public (src/proxy.ts), and refused with
// 503 there when authentication is not configured. Accepts a form post (the
// /login page: 303 to "/" with the session cookie, or 303 back to
// /login?error=1) or JSON {"password": "..."} (200 {"ok": true} + cookie, or
// 401). Rate limited (src/lib/rate-limit.ts, per process): the attempt is
// reserved per client BEFORE any await (AEGIS M-1) — 429 with Retry-After once
// a client has 5 failures or attempts in flight; once the global budget is
// spent, checks are serialised, never refused (AEGIS M-2: the owner still gets
// in). The Origin header is required and must match scheme and host (L-2).
export const dynamic = "force-dynamic";

const limiter = new FailureLimiter();
const MAX_BODY_BYTES = 4096;
const NO_STORE = { "cache-control": "no-store" };

async function readPassword(req: Request, isJson: boolean): Promise<string | null> {
  let text: string;
  try {
    text = await req.text();
  } catch {
    return null;
  }
  if (Buffer.byteLength(text, "utf8") > MAX_BODY_BYTES) return null;
  if (isJson) {
    try {
      const v = JSON.parse(text) as unknown;
      const pw = v && typeof v === "object" ? (v as Record<string, unknown>).password : undefined;
      return typeof pw === "string" ? pw : null;
    } catch {
      return null;
    }
  }
  return new URLSearchParams(text).get("password");
}

export async function POST(req: Request) {
  const cfg = authConfig(process.env);
  if (!cfg.ok) {
    return Response.json({ status: "unavailable", error: "dashboard authentication is not configured" }, { status: 503, headers: NO_STORE });
  }
  const isJson = (req.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json");
  if (!sameOrigin(req)) {
    return Response.json({ error: "login needs a same-origin Origin header" }, { status: 403, headers: NO_STORE });
  }
  const key = clientKey(req.headers);
  const r = limiter.reserve(key); // synchronous: parallel requests cannot all pass (M-1)
  if ("retryAfterS" in r) {
    return Response.json(
      { error: "too many failed attempts; try again later" },
      { status: 429, headers: { ...NO_STORE, "retry-after": String(r.retryAfterS) } }
    );
  }
  let ok = false;
  try {
    const password = await readPassword(req, isJson);
    ok =
      password !== null &&
      Buffer.byteLength(password, "utf8") <= MAX_PASSWORD_BYTES &&
      (await limiter.throttled(() => verifyPassword(password, cfg.hash)));
  } finally {
    if (ok) limiter.succeed(r);
    else limiter.fail();
  }
  if (!ok) {
    return isJson
      ? Response.json({ error: "invalid credentials" }, { status: 401, headers: NO_STORE })
      : new Response(null, { status: 303, headers: { ...NO_STORE, location: "/login?error=1" } });
  }
  const cookie = sessionCookie(issueSession(cfg, nowSeconds()), cfg.ttlS);
  return isJson
    ? Response.json({ ok: true }, { status: 200, headers: { ...NO_STORE, "set-cookie": cookie } })
    : new Response(null, { status: 303, headers: { ...NO_STORE, location: "/", "set-cookie": cookie } });
}
