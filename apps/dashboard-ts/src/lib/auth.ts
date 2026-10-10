import { createHmac, randomBytes, scrypt as scryptCb, timingSafeEqual, type ScryptOptions } from "node:crypto";

// Bug sweep E, F-5 (Oct 9 2026): the dashboard had no authentication. On a
// public host (Vercel) every Revenue Recovery finding would have been readable
// by anyone with the URL. Server-side only (src/proxy.ts and the /api/login
// route import this; nothing here is NEXT_PUBLIC_, so no secret reaches a
// client bundle). Fails closed: a missing or malformed setting makes every
// route answer 503 (src/proxy.ts), never an open dashboard.
//
//   DASHBOARD_PASSWORD_HASH   scrypt$<N>$<r>$<p>$<salt b64url>$<key b64url>
//                             (make one with `npm run hash-password`, which
//                             reads the password from stdin)
//   DASHBOARD_SESSION_SECRET  >= 32 bytes; signs the session cookie
//   DASHBOARD_SESSION_TTL_SECONDS  optional, 300..604800, default 43200 (12 h)
//
// The session cookie is `__Host-zbm_dashboard_session`: HttpOnly, Secure,
// SameSite=Strict, Path=/, with Max-Age = the TTL, and the expiry is ALSO
// inside the signed value (a replayed old cookie is refused server-side). The
// signing key is derived from the secret AND the password hash, so changing
// the password (or the secret) signs every existing session out.

export const SESSION_COOKIE = "__Host-zbm_dashboard_session";
export const DEFAULT_TTL_S = 43_200;
export const MIN_TTL_S = 300;
export const MAX_TTL_S = 604_800;
export const MIN_SECRET_BYTES = 32;
export const MAX_PASSWORD_BYTES = 1024;
const MAX_COOKIE_CHARS = 512;
const SKEW_S = 60;

export type PasswordHash = { N: number; r: number; p: number; salt: Buffer; key: Buffer; raw: string };
export type AuthConfig =
  | { ok: true; hash: PasswordHash; signingKey: Buffer; ttlS: number }
  | { ok: false; reason: string };

export function parsePasswordHash(raw: string | undefined): PasswordHash | string {
  if (!raw) return "DASHBOARD_PASSWORD_HASH is not set";
  const parts = raw.trim().split("$");
  if (parts.length !== 6 || parts[0] !== "scrypt") return "DASHBOARD_PASSWORD_HASH is not scrypt$N$r$p$salt$key";
  const [N, r, p] = parts.slice(1, 4).map((x) => (/^[0-9]{1,8}$/.test(x) ? Number(x) : NaN));
  if (!(N >= 16_384 && N <= 1_048_576 && (N & (N - 1)) === 0)) return "DASHBOARD_PASSWORD_HASH: N must be a power of two, 2^14..2^20";
  if (!(r >= 8 && r <= 32)) return "DASHBOARD_PASSWORD_HASH: r must be 8..32";
  if (!(p >= 1 && p <= 16)) return "DASHBOARD_PASSWORD_HASH: p must be 1..16";
  const b64url = /^[A-Za-z0-9_-]+$/;
  if (!b64url.test(parts[4]) || !b64url.test(parts[5])) return "DASHBOARD_PASSWORD_HASH: salt and key must be base64url";
  const salt = Buffer.from(parts[4], "base64url");
  const key = Buffer.from(parts[5], "base64url");
  if (salt.length < 16) return "DASHBOARD_PASSWORD_HASH: salt must be at least 16 bytes";
  if (key.length < 32 || key.length > 64) return "DASHBOARD_PASSWORD_HASH: key must be 32..64 bytes";
  return { N, r, p, salt, key, raw: raw.trim() };
}

export function authConfig(env: Record<string, string | undefined>): AuthConfig {
  const hash = parsePasswordHash(env.DASHBOARD_PASSWORD_HASH);
  if (typeof hash === "string") return { ok: false, reason: hash };
  const secret = env.DASHBOARD_SESSION_SECRET ?? "";
  if (Buffer.byteLength(secret, "utf8") < MIN_SECRET_BYTES) {
    return { ok: false, reason: `DASHBOARD_SESSION_SECRET must be at least ${MIN_SECRET_BYTES} bytes` };
  }
  if (env.ORCHESTRATOR_SERVICE_TOKEN && secret === env.ORCHESTRATOR_SERVICE_TOKEN) {
    return { ok: false, reason: "DASHBOARD_SESSION_SECRET must not equal ORCHESTRATOR_SERVICE_TOKEN" };
  }
  let ttlS = DEFAULT_TTL_S;
  const rawTtl = env.DASHBOARD_SESSION_TTL_SECONDS;
  if (rawTtl !== undefined && rawTtl !== "") {
    const n = /^[0-9]{1,7}$/.test(rawTtl) ? Number(rawTtl) : NaN;
    if (!(n >= MIN_TTL_S && n <= MAX_TTL_S)) {
      return { ok: false, reason: `DASHBOARD_SESSION_TTL_SECONDS must be an integer ${MIN_TTL_S}..${MAX_TTL_S}` };
    }
    ttlS = n;
  }
  const signingKey = createHmac("sha256", secret).update(`zbm-dashboard-session-key/v1\n${hash.raw}`).digest();
  return { ok: true, hash, signingKey, ttlS };
}

function scrypt(password: Buffer, salt: Buffer, keylen: number, opts: ScryptOptions): Promise<Buffer> {
  return new Promise((resolve, reject) =>
    scryptCb(password, salt, keylen, opts, (err, key) => (err ? reject(err) : resolve(key)))
  );
}

function scryptOpts(N: number, r: number, p: number): ScryptOptions {
  return { N, r, p, maxmem: 256 * N * r + 1024 * 1024 };
}

/** Constant-time check of a password against the configured scrypt hash. */
export async function verifyPassword(password: string, hash: PasswordHash): Promise<boolean> {
  const pw = Buffer.from(password, "utf8");
  if (pw.length === 0 || pw.length > MAX_PASSWORD_BYTES) return false;
  const got = await scrypt(pw, hash.salt, hash.key.length, scryptOpts(hash.N, hash.r, hash.p));
  return got.length === hash.key.length && timingSafeEqual(got, hash.key);
}

/** The DASHBOARD_PASSWORD_HASH value for a password (scripts/hash-password.mjs). */
export async function hashPassword(password: string, N = 32_768, r = 8, p = 1): Promise<string> {
  const pw = Buffer.from(password, "utf8");
  if (pw.length === 0 || pw.length > MAX_PASSWORD_BYTES) throw new Error(`password must be 1..${MAX_PASSWORD_BYTES} bytes`);
  const salt = randomBytes(16);
  const key = await scrypt(pw, salt, 32, scryptOpts(N, r, p));
  return `scrypt$${N}$${r}$${p}$${salt.toString("base64url")}$${key.toString("base64url")}`;
}

function sign(cfg: Extract<AuthConfig, { ok: true }>, body: string): Buffer {
  return createHmac("sha256", cfg.signingKey).update(`zbm-dashboard-session/v1\n${body}`).digest();
}

/** A new signed session value; ``nowS`` is Unix seconds. */
export function issueSession(cfg: Extract<AuthConfig, { ok: true }>, nowS: number): string {
  const body = Buffer.from(
    JSON.stringify({ v: 1, iat: nowS, exp: nowS + cfg.ttlS, n: randomBytes(16).toString("base64url") }),
    "utf8"
  ).toString("base64url");
  return `${body}.${sign(cfg, body).toString("base64url")}`;
}

/** True only for an untampered, unexpired session signed with the current key. */
export function verifySession(cfg: Extract<AuthConfig, { ok: true }>, value: string | undefined, nowS: number): boolean {
  if (!value || value.length > MAX_COOKIE_CHARS) return false;
  const dot = value.indexOf(".");
  if (dot <= 0 || dot !== value.lastIndexOf(".")) return false;
  const body = value.slice(0, dot);
  const sigText = value.slice(dot + 1);
  if (!/^[A-Za-z0-9_-]+$/.test(body) || !/^[A-Za-z0-9_-]+$/.test(sigText)) return false;
  const want = sign(cfg, body);
  const got = Buffer.from(sigText, "base64url");
  if (got.length !== want.length || !timingSafeEqual(got, want)) return false;
  let claims: unknown;
  try {
    claims = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
  } catch {
    return false;
  }
  if (!claims || typeof claims !== "object") return false;
  const { v, iat, exp } = claims as Record<string, unknown>;
  if (v !== 1 || !Number.isInteger(iat) || !Number.isInteger(exp)) return false;
  const i = iat as number;
  const e = exp as number;
  return e > nowS && i <= nowS + SKEW_S && e - i <= cfg.ttlS && e - i > 0;
}

export function sessionCookie(value: string, ttlS: number): string {
  return `${SESSION_COOKIE}=${value}; Path=/; Max-Age=${ttlS}; HttpOnly; Secure; SameSite=Strict`;
}

export function clearedSessionCookie(): string {
  return `${SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict`;
}

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

// Wave F (AEGIS A-4): a device that has signed in before carries a long-lived device cookie, so the login limiter
// can keep it a slot of its own whatever addresses a flood claims (src/lib/rate-limit.ts). Signed with a key derived
// from the session signing key (so rotating the secret or the password retires every device cookie), HttpOnly,
// Secure, SameSite=Strict, and re-issued with a NEW device id at every successful sign-in (rotation). It grants no
// access: only a place in the login queue.
export const DEVICE_COOKIE = "__Host-zbm_dashboard_device";
export const DEVICE_TTL_S = 90 * 86_400;

function deviceSign(cfg: Extract<AuthConfig, { ok: true }>, body: string): Buffer {
  const key = createHmac("sha256", cfg.signingKey).update("zbm-dashboard-device-key/v1").digest();
  return createHmac("sha256", key).update(`zbm-dashboard-device/v1\n${body}`).digest();
}

/** A new signed device value (a fresh random device id). */
export function issueDevice(cfg: Extract<AuthConfig, { ok: true }>, nowS: number): string {
  const body = Buffer.from(
    JSON.stringify({ v: 1, d: randomBytes(16).toString("base64url"), iat: nowS, exp: nowS + DEVICE_TTL_S }),
    "utf8"
  ).toString("base64url");
  return `${body}.${deviceSign(cfg, body).toString("base64url")}`;
}

/** The device id of an untampered, unexpired device cookie signed with the current key; null otherwise. */
export function verifyDevice(cfg: Extract<AuthConfig, { ok: true }>, value: string | undefined, nowS: number): string | null {
  if (!value || value.length > MAX_COOKIE_CHARS) return null;
  const dot = value.indexOf(".");
  if (dot <= 0 || dot !== value.lastIndexOf(".")) return null;
  const body = value.slice(0, dot);
  const sigText = value.slice(dot + 1);
  if (!/^[A-Za-z0-9_-]+$/.test(body) || !/^[A-Za-z0-9_-]+$/.test(sigText)) return null;
  const want = deviceSign(cfg, body);
  const got = Buffer.from(sigText, "base64url");
  if (got.length !== want.length || !timingSafeEqual(got, want)) return null;
  let claims: unknown;
  try {
    claims = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
  } catch {
    return null;
  }
  if (!claims || typeof claims !== "object") return null;
  const { v, d, iat, exp } = claims as Record<string, unknown>;
  if (v !== 1 || typeof d !== "string" || !/^[A-Za-z0-9_-]{16,64}$/.test(d)) return null;
  if (!Number.isInteger(iat) || !Number.isInteger(exp)) return null;
  const i = iat as number;
  const e = exp as number;
  return e > nowS && i <= nowS + SKEW_S && e - i <= DEVICE_TTL_S && e - i > 0 ? d : null;
}

export function deviceCookie(value: string): string {
  return `${DEVICE_COOKIE}=${value}; Path=/; Max-Age=${DEVICE_TTL_S}; HttpOnly; Secure; SameSite=Strict`;
}

/** One cookie's value from a Cookie header (route handlers read the raw header). */
export function cookieValue(header: string | null, name: string): string | undefined {
  for (const part of (header ?? "").split(";")) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq).trim() === name) return part.slice(eq + 1).trim();
  }
  return undefined;
}
