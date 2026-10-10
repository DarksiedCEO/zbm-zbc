import { authConfig, issueSession, MAX_PASSWORD_BYTES, nowSeconds, sessionCookie, verifyPassword } from "@/lib/auth";
import { clientKey, FailureLimiter } from "@/lib/rate-limit";

// Bug sweep E, F-5: POST /api/login. Public (src/proxy.ts), and refused with
// 503 there when authentication is not configured. Accepts a form post (the
// /login page: 303 to "/" with the session cookie, or 303 back to
// /login?error=1) or JSON {"password": "..."} (200 {"ok": true} + cookie, or
// 401). Failed attempts are rate limited (src/lib/rate-limit.ts, per process):
// 429 with Retry-After, the right password included, before any scrypt work.
export const dynamic = "force-dynamic";

const limiter = new FailureLimiter();
const MAX_BODY_BYTES = 4096;
const NO_STORE = { "cache-control": "no-store" };

function sameOrigin(req: Request): boolean {
  const origin = req.headers.get("origin");
  if (!origin) return true; // non-browser clients; the cookie is SameSite=Strict regardless
  try {
    return new URL(origin).host === req.headers.get("host");
  } catch {
    return false;
  }
}

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
    return Response.json({ error: "cross-origin login refused" }, { status: 403, headers: NO_STORE });
  }
  const key = clientKey(req.headers);
  const wait = limiter.retryAfterS(key);
  if (wait !== null) {
    return Response.json(
      { error: "too many failed attempts; try again later" },
      { status: 429, headers: { ...NO_STORE, "retry-after": String(wait) } }
    );
  }
  const password = await readPassword(req, isJson);
  const ok =
    password !== null &&
    Buffer.byteLength(password, "utf8") <= MAX_PASSWORD_BYTES &&
    (await verifyPassword(password, cfg.hash));
  if (!ok) {
    limiter.fail(key);
    return isJson
      ? Response.json({ error: "invalid credentials" }, { status: 401, headers: NO_STORE })
      : new Response(null, { status: 303, headers: { ...NO_STORE, location: "/login?error=1" } });
  }
  const cookie = sessionCookie(issueSession(cfg, nowSeconds()), cfg.ttlS);
  return isJson
    ? Response.json({ ok: true }, { status: 200, headers: { ...NO_STORE, "set-cookie": cookie } })
    : new Response(null, { status: 303, headers: { ...NO_STORE, location: "/", "set-cookie": cookie } });
}
