// Bug sweep E, F-5: login rate limiting, per process, sliding window, with an
// injected monotonic clock (tests never depend on wall-clock time).
//
// Per client (the first X-Forwarded-For / X-Real-IP address, which the host
// sets on Vercel): a HARD limit. AEGIS M-1 (Oct 9 2026): an attempt is
// RESERVED synchronously, before any await (body read, scrypt), so parallel
// requests from one client cannot all pass the check before the first failure
// is recorded; a reservation counts as a failure until the password is
// verified, and only a success gives it back.
//
// Across all clients: a BACKOFF, never a block. AEGIS M-2: a hard global limit
// let 50 bad attempts from anywhere lock the owner out indefinitely. Once the
// global failure budget is spent, password checks are serialised (one scrypt
// at a time, per process), which bounds the guess rate from rotating or
// spoofed client keys while the right password still gets through.

export type LimiterOptions = { windowMs: number; perKey: number; global: number; maxKeys: number };
export const LOGIN_LIMITS: LimiterOptions = { windowMs: 15 * 60_000, perKey: 5, global: 50, maxKeys: 10_000 };

export type Reservation = { key: string; at: number; released: boolean };

export class FailureLimiter {
  private readonly byKey = new Map<string, Reservation[]>();
  private all: number[] = [];
  private gate: Promise<void> = Promise.resolve();
  private readonly opts: LimiterOptions;
  private readonly now: () => number;

  // (no TypeScript parameter properties: `node --test` runs this file with type stripping only)
  constructor(opts: LimiterOptions = LOGIN_LIMITS, now: () => number = () => performance.now()) {
    this.opts = opts;
    this.now = now;
  }

  private live(key: string, t: number): Reservation[] {
    const cutoff = t - this.opts.windowMs;
    const mine = (this.byKey.get(key) ?? []).filter((r) => !r.released && r.at > cutoff);
    if (mine.length) this.byKey.set(key, mine);
    else this.byKey.delete(key);
    return mine;
  }

  /** Synchronous: a reservation, or the seconds until this client may try again. */
  reserve(key: string): Reservation | { retryAfterS: number } {
    const t = this.now();
    const mine = this.live(key, t);
    if (mine.length >= this.opts.perKey) {
      const until = mine[mine.length - this.opts.perKey].at + this.opts.windowMs;
      return { retryAfterS: Math.max(1, Math.ceil((until - t) / 1000)) };
    }
    const r: Reservation = { key, at: t, released: false };
    mine.push(r);
    this.byKey.delete(key); // re-insert: Map order = least recently active first
    this.byKey.set(key, mine);
    while (this.byKey.size > this.opts.maxKeys) {
      const oldest = this.byKey.keys().next().value as string;
      this.byKey.delete(oldest);
    }
    return r;
  }

  /** The password was right: the reservation is given back. */
  succeed(r: Reservation): void {
    r.released = true;
    this.live(r.key, this.now());
  }

  /** The password was wrong: the reservation stays (it is the failure) and the global count grows. */
  fail(): void {
    this.all.push(this.now());
  }

  globalSpent(): boolean {
    const cutoff = this.now() - this.opts.windowMs;
    let i = 0;
    while (i < this.all.length && this.all[i] <= cutoff) i++;
    if (i) this.all = this.all.slice(i);
    return this.all.length >= this.opts.global;
  }

  /** Runs ``check`` directly, or one at a time once the global budget is spent (backoff, never a block). */
  async throttled<T>(check: () => Promise<T>): Promise<T> {
    if (!this.globalSpent()) return check();
    const prev = this.gate;
    let done: () => void = () => {};
    this.gate = new Promise<void>((resolve) => {
      done = resolve;
    });
    await prev;
    try {
      return await check();
    } finally {
      done();
    }
  }
}

export function clientKey(headers: Headers): string {
  const xff = headers.get("x-forwarded-for");
  const first = xff ? xff.split(",")[0].trim() : "";
  const ip = first || (headers.get("x-real-ip") ?? "").trim();
  return ip ? ip.slice(0, 64) : "unknown";
}

/** AEGIS L-1/L-2: a login or logout POST must carry an Origin whose scheme AND host are this site's: the
 * Host header, with the scheme the host's proxy reports (X-Forwarded-Proto on Vercel), else the request's own
 * (Next's ``req.url`` names the server's bind host, not the one the browser used). */
export function sameOrigin(req: Request): boolean {
  const origin = req.headers.get("origin");
  const host = req.headers.get("host");
  if (!origin || origin === "null" || !host) return false;
  try {
    const fwd = (req.headers.get("x-forwarded-proto") ?? "").split(",")[0].trim().toLowerCase();
    const scheme = fwd === "https" || fwd === "http" ? fwd : new URL(req.url).protocol.replace(":", "");
    return new URL(origin).origin === new URL(`${scheme}://${host}`).origin;
  } catch {
    return false;
  }
}
